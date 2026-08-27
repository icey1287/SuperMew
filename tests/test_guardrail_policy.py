from __future__ import annotations

import json
import re
from dataclasses import replace

import pytest

from backend.guardrails import (
    DEFAULT_GUARDRAIL_POLICY,
    DestinationCapability,
    GuardrailDecision,
    GuardrailDirective,
    GuardrailPolicy,
    GuardrailReasonCode,
    SkillToolScope,
    ToolArgsSummary,
    ToolGuardrail,
    ToolGuardrailRequest,
    ToolGuardrailResult,
    destination_context_binding,
)


def _request(**overrides: object) -> ToolGuardrailRequest:
    values: dict[str, object] = {
        "user_id": "user-1",
        "roles": frozenset({"member"}),
        "tenant_id": "tenant-1",
        "thread_id": "thread-1",
        "run_id": "run-1",
        "tool_name": "search_knowledge_base",
        "tool_group": "knowledge",
        "tool_args_summary": ToolArgsSummary.from_mapping(
            {"query": "private question text"}
        ),
        "active_skill": "knowledge-base",
        "active_skill_registered": True,
        "active_skill_scope_allows": True,
        "channel": "run",
        "network_policy": "restricted",
        "destination_capability": None,
        "resource_scope": "knowledge-read",
        "descriptor_requires_approval": False,
        "approval_granted": False,
    }
    values.update(overrides)
    return ToolGuardrailRequest(**values)  # type: ignore[arg-type]


def _web_request(**overrides: object) -> ToolGuardrailRequest:
    values: dict[str, object] = {
        "tool_name": "web_search",
        "tool_group": "web-research",
        "active_skill": "web-research",
        "network_policy": "restricted",
        "resource_scope": "public-web",
    }
    values.update(overrides)
    return _request(**values)


def _web_fetch_request(**overrides: object) -> ToolGuardrailRequest:
    values: dict[str, object] = {"tool_name": "web_fetch"}
    values.update(overrides)
    return _web_request(**values)


def _capability(
    request: ToolGuardrailRequest,
    **overrides: object,
) -> DestinationCapability:
    assert request.user_id is not None
    assert request.tenant_id is not None
    assert request.thread_id is not None
    assert request.run_id is not None
    assert request.tool_name is not None
    assert request.network_policy is not None
    assert request.resource_scope is not None
    values: dict[str, object] = {
        "capability_id": f"destcap_{'1' * 64}",
        "issuer": "web-url-policy",
        "policy_hash": "2" * 64,
        "context_binding": destination_context_binding(
            user_id=request.user_id,
            tenant_id=request.tenant_id,
            thread_id=request.thread_id,
            run_id=request.run_id,
        ),
        "destination_hash": "3" * 64,
        "tool_name": request.tool_name,
        "network_policy": request.network_policy,
        "resource_scope": request.resource_scope,
        "signature": "opaque-url-policy-signature",
    }
    values.update(overrides)
    return DestinationCapability(**values)  # type: ignore[arg-type]


class _Verifier:
    def __init__(self, result: bool | Exception) -> None:
        self.result = result
        self.calls = 0

    def verify(
        self,
        capability: DestinationCapability,
        *,
        request: ToolGuardrailRequest,
    ) -> bool:
        del capability, request
        self.calls += 1
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def test_low_risk_skill_scoped_tool_is_allowed_with_stable_policy_identity() -> None:
    first = ToolGuardrail().evaluate(_request())
    second = ToolGuardrail().evaluate(_request())

    assert first.decision is GuardrailDecision.ALLOW
    assert first.reason_code is GuardrailReasonCode.ALLOWED
    assert first.policy_version == "1.1.0"
    assert re.fullmatch(r"[0-9a-f]{64}", first.policy_hash)
    assert first == second
    assert first.safe_metadata["context_complete"] is True


def test_policy_hash_is_canonical_across_set_and_scope_order() -> None:
    shuffled = replace(
        DEFAULT_GUARDRAIL_POLICY,
        known_channels=frozenset(
            reversed(tuple(DEFAULT_GUARDRAIL_POLICY.known_channels))
        ),
        skill_scopes=tuple(reversed(DEFAULT_GUARDRAIL_POLICY.skill_scopes)),
        resident_tools=frozenset(
            reversed(tuple(DEFAULT_GUARDRAIL_POLICY.resident_tools))
        ),
    )
    changed = replace(DEFAULT_GUARDRAIL_POLICY, version="1.0.1")

    assert shuffled.policy_hash == DEFAULT_GUARDRAIL_POLICY.policy_hash
    assert changed.policy_hash != DEFAULT_GUARDRAIL_POLICY.policy_hash


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("user_id", None),
        ("roles", None),
        ("tenant_id", None),
        ("thread_id", None),
        ("run_id", None),
        ("tool_name", None),
        ("tool_group", None),
        ("tool_args_summary", None),
        ("active_skill", ""),
        ("active_skill_registered", None),
        ("active_skill_scope_allows", None),
        ("channel", None),
        ("network_policy", None),
        ("resource_scope", None),
        ("descriptor_requires_approval", None),
        ("approval_granted", None),
    ],
)
def test_incomplete_context_is_denied_before_provider_execution(
    field_name: str,
    value: object,
) -> None:
    result = ToolGuardrail().evaluate(_request(**{field_name: value}))

    assert result.decision is GuardrailDecision.DENY
    assert result.reason_code is GuardrailReasonCode.CONTEXT_INCOMPLETE


def test_unavailable_argument_summary_is_incomplete_and_denied() -> None:
    cyclic: dict[str, object] = {}
    cyclic["payload"] = cyclic
    summary = ToolArgsSummary.from_mapping(cyclic)

    assert summary.complete is False
    result = ToolGuardrail().evaluate(_request(tool_args_summary=summary))
    assert result.reason_code is GuardrailReasonCode.CONTEXT_INCOMPLETE


@pytest.mark.parametrize(
    ("overrides", "reason_code"),
    [
        ({"tool_name": "UPPERCASE"}, GuardrailReasonCode.CONTEXT_UNKNOWN),
        ({"channel": "mobile"}, GuardrailReasonCode.CHANNEL_UNKNOWN),
        (
            {"network_policy": "unrestricted"},
            GuardrailReasonCode.NETWORK_POLICY_UNKNOWN,
        ),
        (
            {"resource_scope": "host-root"},
            GuardrailReasonCode.RESOURCE_SCOPE_UNKNOWN,
        ),
        ({"tool_group": "email"}, GuardrailReasonCode.TOOL_GROUP_UNKNOWN),
    ],
)
def test_unknown_or_out_of_scope_context_is_denied(
    overrides: dict[str, object],
    reason_code: GuardrailReasonCode,
) -> None:
    result = ToolGuardrail().evaluate(_request(**overrides))

    assert result.decision is GuardrailDecision.DENY
    assert result.reason_code is reason_code


def test_session_authorized_web_search_does_not_require_an_active_skill() -> None:
    result = ToolGuardrail().evaluate(
        _web_request(
            active_skill=None,
            active_skill_registered=False,
            active_skill_scope_allows=False,
            destination_capability=None,
        )
    )

    assert result.decision is GuardrailDecision.ALLOW
    assert result.reason_code is GuardrailReasonCode.ALLOWED


def test_inactive_skill_allows_an_authorized_resident_tool() -> None:
    result = ToolGuardrail().evaluate(_request(active_skill=None))

    assert result.decision is GuardrailDecision.ALLOW
    assert result.reason_code is GuardrailReasonCode.ALLOWED


def test_registry_control_tool_can_activate_a_skill_without_an_active_skill() -> None:
    result = ToolGuardrail().evaluate(
        _request(
            active_skill=None,
            tool_name="tool_search",
            tool_group="registry-control",
            network_policy="none",
            resource_scope="none",
        )
    )

    assert result.decision is GuardrailDecision.ALLOW
    assert result.reason_code is GuardrailReasonCode.ALLOWED


def test_descriptor_approval_is_requested_without_minting_a_token() -> None:
    result = ToolGuardrail().evaluate(_request(descriptor_requires_approval=True))

    assert result.decision is GuardrailDecision.REQUIRE_APPROVAL
    assert result.reason_code is GuardrailReasonCode.DESCRIPTOR_APPROVAL_REQUIRED
    assert not hasattr(result, "approval_token")
    assert "approval_token" not in result.safe_metadata


def test_run_bound_approval_satisfies_descriptor_gate() -> None:
    result = ToolGuardrail().evaluate(
        _request(descriptor_requires_approval=True, approval_granted=True)
    )

    assert result.decision is GuardrailDecision.ALLOW
    assert result.reason_code is GuardrailReasonCode.ALLOWED


@pytest.mark.parametrize(
    "tool_group",
    ["shell", "code", "process", "network-private", "high-risk"],
)
def test_high_risk_groups_are_denied_by_default(tool_group: str) -> None:
    result = ToolGuardrail().evaluate(
        _request(
            active_skill="sandbox",
            tool_name="sandbox_operation",
            tool_group=tool_group,
            network_policy="none",
            resource_scope="code-execution",
        )
    )

    assert result.decision is GuardrailDecision.DENY
    assert result.reason_code is GuardrailReasonCode.HIGH_RISK_TOOL_DENIED


def test_file_write_requires_approval_by_default() -> None:
    result = ToolGuardrail().evaluate(
        _request(
            active_skill="file-manager",
            tool_name="write_thread_file",
            tool_group="file-write",
            network_policy="none",
            resource_scope="thread-write",
        )
    )

    assert result.decision is GuardrailDecision.REQUIRE_APPROVAL
    assert result.reason_code is GuardrailReasonCode.HIGH_RISK_TOOL_APPROVAL_REQUIRED


def test_descriptor_approval_cannot_widen_a_hard_denied_tool_group() -> None:
    result = ToolGuardrail().evaluate(
        _request(
            active_skill="sandbox",
            tool_name="sandbox_shell",
            tool_group="shell",
            network_policy="none",
            resource_scope="process",
            descriptor_requires_approval=True,
        )
    )

    assert result.decision is GuardrailDecision.DENY
    assert result.reason_code is GuardrailReasonCode.HIGH_RISK_TOOL_DENIED


def test_sandbox_execution_requires_a_run_bound_approval() -> None:
    request = _request(
        active_skill="sandbox",
        tool_name="sandbox_execute",
        tool_group="sandbox-execution",
        network_policy="none",
        resource_scope="code-execution",
        descriptor_requires_approval=True,
    )

    required = ToolGuardrail().evaluate(request)
    allowed = ToolGuardrail().evaluate(replace(request, approval_granted=True))

    assert required.decision is GuardrailDecision.REQUIRE_APPROVAL
    assert required.reason_code is GuardrailReasonCode.DESCRIPTOR_APPROVAL_REQUIRED
    assert allowed.decision is GuardrailDecision.ALLOW
    assert allowed.reason_code is GuardrailReasonCode.ALLOWED


def test_sql_private_data_requires_exact_skill_admin_tool_and_scope() -> None:
    allowed = _request(
        active_skill="sql-assistant",
        roles=frozenset({"admin"}),
        tool_name="sql_query",
        tool_group="sql",
        network_policy="private-data",
        resource_scope="private-data-read",
    )

    assert ToolGuardrail().evaluate(allowed).decision is GuardrailDecision.ALLOW

    matrix = (
        (
            {"roles": frozenset({"member"})},
            GuardrailReasonCode.SQL_ADMIN_REQUIRED,
        ),
        (
            {"network_policy": "restricted"},
            GuardrailReasonCode.SQL_PRIVATE_NETWORK_REQUIRED,
        ),
        (
            {"resource_scope": "thread-read"},
            GuardrailReasonCode.SQL_RESOURCE_SCOPE_DENIED,
        ),
        (
            {"tool_group": "knowledge"},
            GuardrailReasonCode.SQL_CONTEXT_REQUIRED,
        ),
    )
    for overrides, reason_code in matrix:
        result = ToolGuardrail().evaluate(replace(allowed, **overrides))
        assert result.decision is GuardrailDecision.DENY
        assert result.reason_code is reason_code


def test_sql_group_cannot_expand_the_read_only_descriptor_allowlist() -> None:
    scopes = tuple(
        SkillToolScope(
            "sql-assistant",
            allowed_tools=scope.allowed_tools,
            allowed_groups=frozenset({"sql"}),
        )
        if scope.skill_name == "sql-assistant"
        else scope
        for scope in DEFAULT_GUARDRAIL_POLICY.skill_scopes
    )
    policy = replace(DEFAULT_GUARDRAIL_POLICY, skill_scopes=scopes)
    request = _request(
        active_skill="sql-assistant",
        roles=frozenset({"admin"}),
        tool_name="sql_write",
        tool_group="sql",
        network_policy="private-data",
        resource_scope="private-data-read",
    )

    result = ToolGuardrail(policy).evaluate(request)

    assert result.decision is GuardrailDecision.DENY
    assert result.reason_code is GuardrailReasonCode.SQL_READ_ONLY_TOOL_REQUIRED


def test_non_sql_private_network_access_is_denied() -> None:
    result = ToolGuardrail().evaluate(_request(network_policy="private-data"))

    assert result.decision is GuardrailDecision.DENY
    assert result.reason_code is GuardrailReasonCode.PRIVATE_NETWORK_DENIED


def test_restricted_web_requires_a_verified_url_policy_capability() -> None:
    request = _web_fetch_request()
    missing = ToolGuardrail().evaluate(request)
    capability = _capability(request)
    unverified = ToolGuardrail().evaluate(
        replace(request, destination_capability=capability)
    )
    denied_verifier = _Verifier(False)
    invalid = ToolGuardrail(destination_verifier=denied_verifier).evaluate(
        replace(request, destination_capability=capability)
    )
    allowed_verifier = _Verifier(True)
    allowed = ToolGuardrail(destination_verifier=allowed_verifier).evaluate(
        replace(request, destination_capability=capability)
    )

    assert missing.reason_code is GuardrailReasonCode.DESTINATION_CAPABILITY_REQUIRED
    assert (
        unverified.reason_code is GuardrailReasonCode.DESTINATION_CAPABILITY_UNVERIFIED
    )
    assert invalid.reason_code is GuardrailReasonCode.DESTINATION_CAPABILITY_INVALID
    assert allowed.decision is GuardrailDecision.ALLOW
    assert denied_verifier.calls == 1
    assert allowed_verifier.calls == 1


def test_web_search_uses_fixed_provider_origin_without_a_run_destination_capability() -> (
    None
):
    verifier = _Verifier(RuntimeError("web_search must not verify a capability"))
    result = ToolGuardrail(destination_verifier=verifier).evaluate(_web_request())

    assert result.decision is GuardrailDecision.ALLOW
    assert result.reason_code is GuardrailReasonCode.ALLOWED
    assert verifier.calls == 0


def test_destination_capability_is_bound_before_signature_provider_runs() -> None:
    request = _web_fetch_request()
    wrong_binding = destination_context_binding(
        user_id="user-1",
        tenant_id="tenant-1",
        thread_id="thread-1",
        run_id="different-run",
    )
    verifier = _Verifier(True)
    result = ToolGuardrail(destination_verifier=verifier).evaluate(
        replace(
            request,
            destination_capability=_capability(
                request,
                context_binding=wrong_binding,
            ),
        )
    )

    assert result.decision is GuardrailDecision.DENY
    assert result.reason_code is GuardrailReasonCode.DESTINATION_CAPABILITY_INVALID
    assert verifier.calls == 0


def test_destination_capability_provider_exception_fails_closed_and_redacted() -> None:
    request = _web_fetch_request()
    verifier = _Verifier(RuntimeError("private verifier failure"))
    result = ToolGuardrail(destination_verifier=verifier).evaluate(
        replace(request, destination_capability=_capability(request))
    )

    assert result.decision is GuardrailDecision.DENY
    assert (
        result.reason_code is GuardrailReasonCode.DESTINATION_CAPABILITY_PROVIDER_FAILED
    )
    assert "private verifier failure" not in repr(result)


def test_policy_provider_exception_fails_closed_without_exception_details() -> None:
    class BrokenProvider:
        def decide(self, request: ToolGuardrailRequest):
            del request
            raise RuntimeError("secret provider diagnostic")

    result = ToolGuardrail(provider=BrokenProvider()).evaluate(_request())

    assert result.decision is GuardrailDecision.DENY
    assert result.reason_code is GuardrailReasonCode.POLICY_PROVIDER_FAILED
    assert "secret provider diagnostic" not in repr(result)
    assert "secret provider diagnostic" not in json.dumps(
        dict(result.safe_metadata),
        ensure_ascii=False,
    )


def test_policy_provider_cannot_widen_a_deterministic_denial() -> None:
    class PermissiveProvider:
        def __init__(self) -> None:
            self.calls = 0

        def decide(self, request: ToolGuardrailRequest) -> GuardrailDirective:
            del request
            self.calls += 1
            return GuardrailDirective(
                GuardrailDecision.ALLOW,
                GuardrailReasonCode.ALLOWED,
            )

    provider = PermissiveProvider()
    result = ToolGuardrail(provider=provider).evaluate(
        _request(
            active_skill="sandbox",
            tool_name="sandbox_shell",
            tool_group="shell",
            network_policy="none",
            resource_scope="process",
        )
    )

    assert result.decision is GuardrailDecision.DENY
    assert result.reason_code is GuardrailReasonCode.HIGH_RISK_TOOL_DENIED
    assert provider.calls == 0


def test_policy_provider_cannot_widen_a_required_approval() -> None:
    class PermissiveProvider:
        def decide(self, request: ToolGuardrailRequest) -> GuardrailDirective:
            del request
            return GuardrailDirective(
                GuardrailDecision.ALLOW,
                GuardrailReasonCode.ALLOWED,
            )

    result = ToolGuardrail(provider=PermissiveProvider()).evaluate(
        _request(descriptor_requires_approval=True)
    )

    assert result.decision is GuardrailDecision.REQUIRE_APPROVAL
    assert result.reason_code is GuardrailReasonCode.DESCRIPTOR_APPROVAL_REQUIRED


def test_policy_provider_can_attenuate_a_base_allow() -> None:
    class DenyingProvider:
        def decide(self, request: ToolGuardrailRequest) -> GuardrailDirective:
            del request
            return GuardrailDirective(
                GuardrailDecision.DENY,
                GuardrailReasonCode.POLICY_PROVIDER_DENIED,
            )

    result = ToolGuardrail(provider=DenyingProvider()).evaluate(_request())

    assert result.decision is GuardrailDecision.DENY
    assert result.reason_code is GuardrailReasonCode.POLICY_PROVIDER_DENIED


def test_tool_argument_secrets_and_bodies_never_enter_repr_or_metadata() -> None:
    secret = "Bearer super-secret-token-value"
    body = "customer private document body"
    summary = ToolArgsSummary.from_mapping(
        {
            "authorization": secret,
            "body": {"content": body},
            "limit": 5,
        }
    )
    request = _request(tool_args_summary=summary)
    result = ToolGuardrail().evaluate(request)
    rendered = "\n".join(
        (
            repr(summary),
            repr(request),
            repr(result),
            json.dumps(dict(result.safe_metadata), ensure_ascii=False),
        )
    )

    assert summary.complete is True
    assert summary.secret_field_count >= 1
    assert summary.body_field_count >= 1
    assert secret not in rendered
    assert body not in rendered
    assert "authorization" not in rendered
    assert "tool_args" not in result.safe_metadata
    assert "shape_hash" not in result.safe_metadata


def test_guardrail_result_rejects_metadata_that_only_looks_audit_safe() -> None:
    with pytest.raises(ValueError, match="unsafe identifier"):
        ToolGuardrailResult(
            decision=GuardrailDecision.ALLOW,
            reason_code=GuardrailReasonCode.ALLOWED,
            policy_version="1.0.0",
            policy_hash="a" * 64,
            safe_metadata={"tool_name": "Bearer private-token"},
        )

    with pytest.raises(ValueError, match="role count"):
        ToolGuardrailResult(
            decision=GuardrailDecision.ALLOW,
            reason_code=GuardrailReasonCode.ALLOWED,
            policy_version="1.0.0",
            policy_hash="a" * 64,
            safe_metadata={"role_count": 10_000},
        )


def test_destination_signature_and_destination_are_not_rendered() -> None:
    request = _web_fetch_request()
    capability = _capability(
        request,
        signature="sensitive-capability-signature",
        destination_hash="4" * 64,
    )

    assert "sensitive-capability-signature" not in repr(capability)
    assert "4" * 64 not in repr(capability)
    assert capability.capability_id not in repr(capability)


def test_invalid_provider_result_is_denied() -> None:
    class InvalidProvider:
        def decide(self, request: ToolGuardrailRequest):
            del request
            return GuardrailDecision.ALLOW

    result = ToolGuardrail(provider=InvalidProvider()).evaluate(_request())

    assert result.decision is GuardrailDecision.DENY
    assert result.reason_code is GuardrailReasonCode.POLICY_PROVIDER_FAILED


def test_policy_rejects_overlapping_high_risk_decision_sets() -> None:
    with pytest.raises(ValueError, match="cannot overlap"):
        GuardrailPolicy(
            hard_deny_groups=frozenset({"shell"}),
            approval_groups=frozenset({"shell"}),
        )
