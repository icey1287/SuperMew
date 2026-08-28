from dataclasses import replace
from datetime import datetime, timezone

import pytest

from backend.runs.request_context import RunRequestContext
from backend.guardrails import (
    DestinationCapabilityBinding,
    GuardrailDecision,
    RunDestinationCapabilityAuthority,
    ToolArgsSummary,
    ToolGuardrail,
    ToolGuardrailRequest,
)
from backend.web_research.contracts import WebEvidence, WebResearchResult


def _request(**overrides: object) -> ToolGuardrailRequest:
    values: dict[str, object] = {
        "user_id": "user-1",
        "roles": frozenset({"user"}),
        "tenant_id": "tenant-1",
        "thread_id": "thread-1",
        "run_id": "run-1",
        "tool_name": "web_fetch",
        "tool_group": "web-research",
        "tool_args_summary": ToolArgsSummary.from_mapping(
            {"evidence_id": f"web_ev_{'1' * 64}"}
        ),
        "active_skill": "web-research",
        "active_skill_registered": True,
        "active_skill_scope_allows": True,
        "channel": "run",
        "network_policy": "restricted",
        "destination_capability": None,
        "resource_scope": "public-web",
        "descriptor_requires_approval": False,
        "approval_granted": False,
    }
    values.update(overrides)
    return ToolGuardrailRequest(**values)  # type: ignore[arg-type]


def _authority(*, run_id: str = "run-1") -> RunDestinationCapabilityAuthority:
    return RunDestinationCapabilityAuthority(
        DestinationCapabilityBinding(
            user_id="user-1",
            tenant_id="tenant-1",
            thread_id="thread-1",
            run_id=run_id,
        )
    )


def test_request_owned_authority_mints_a_guardrail_verifiable_capability() -> None:
    authority = _authority()
    capability = authority.issue("https://example.invalid/public-evidence")
    request = _request(destination_capability=capability)

    result = ToolGuardrail(destination_verifier=authority).evaluate(request)

    assert result.decision is GuardrailDecision.ALLOW
    rendered = repr(authority) + repr(capability) + repr(result)
    assert "example.invalid" not in rendered
    assert capability.signature not in rendered
    assert capability.destination_hash not in rendered


def test_capability_cannot_cross_authority_or_run_identity() -> None:
    authority = _authority()
    other = _authority(run_id="run-2")
    capability = authority.issue("https://example.invalid/evidence")
    request = _request(destination_capability=capability)

    assert other.verify(capability, request=request) is False
    assert (
        authority.verify(
            capability,
            request=replace(request, run_id="run-2"),
        )
        is False
    )
    assert (
        authority.verify(
            replace(capability, capability_id=f"destcap_{'f' * 64}"),
            request=request,
        )
        is False
    )


def test_closed_authority_forgets_all_issued_capabilities() -> None:
    authority = _authority()
    capability = authority.issue("https://example.invalid/evidence")
    request = _request(destination_capability=capability)

    authority.close()

    assert authority.verify(capability, request=request) is False
    assert "state='closed'" in repr(authority)


def test_destination_binding_repr_never_exposes_run_identity() -> None:
    binding = DestinationCapabilityBinding(
        user_id="private-user",
        tenant_id="private-tenant",
        thread_id="private-thread",
        run_id="private-run",
    )

    rendered = repr(binding)
    assert rendered == "DestinationCapabilityBinding(bound=True)"
    assert "private" not in rendered


def test_chat_context_keeps_capability_internal_to_public_web_fetch_args() -> None:
    evidence = WebEvidence.create(
        canonical_url="https://www.example.edu/frozen-result",
        title="Result",
        snippet="Public evidence",
        content="Public evidence body",
        retrieved_at=datetime(2026, 7, 16, tzinfo=timezone.utc),
    )
    evidence_id = evidence.evidence_id
    context = RunRequestContext.for_sync(
        user_id="user-1",
        thread_id="thread-1",
    )
    context.configure_guardrail_context(tenant_id="tenant-1", run_id="run-1")
    context.record_web_search_result(WebResearchResult.create([evidence]))

    capability = context.destination_capability_for_tool(
        "web_fetch",
        {"evidence_id": evidence_id},
    )

    assert capability is not None
    assert (
        context.destination_capability_for_tool(
            "web_fetch",
            {"evidence_id": f"web_ev_{'b' * 64}"},
        )
        is None
    )
    assert (
        context.destination_capability_for_tool(
            "web_fetch",
            {"evidence_id": evidence_id, "destination_capability": "forged"},
        )
        is capability
    )
    context.close()
    assert (
        context.destination_capability_for_tool(
            "web_fetch",
            {"evidence_id": evidence_id},
        )
        is None
    )


def test_chat_context_rejects_security_context_rebinding() -> None:
    context = RunRequestContext.for_sync(user_id="user-1", thread_id="thread-1")
    context.configure_guardrail_context(tenant_id="tenant-1", run_id="run-1")

    try:
        context.configure_guardrail_context(tenant_id="tenant-1", run_id="run-2")
    except ValueError as exc:
        assert "cannot be rebound" in str(exc)
    else:
        raise AssertionError("guardrail context rebinding must fail")


def test_search_capability_commit_is_atomic_when_issuance_fails(monkeypatch) -> None:
    first = WebEvidence.create(
        canonical_url="https://www.example.edu/one",
        title="One",
        snippet="One",
        content="One",
        retrieved_at=datetime(2026, 7, 16, tzinfo=timezone.utc),
    )
    second = WebEvidence.create(
        canonical_url="https://www.example.edu/two",
        title="Two",
        snippet="Two",
        content="Two",
        retrieved_at=datetime(2026, 7, 16, tzinfo=timezone.utc),
    )
    context = RunRequestContext.for_sync(user_id="user-1", thread_id="thread-1")
    context.configure_guardrail_context(tenant_id="tenant-1", run_id="run-1")
    original = RunDestinationCapabilityAuthority.issue
    calls = 0

    def flaky_issue(self, destination: str, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("issuer unavailable")
        return original(self, destination, **kwargs)

    monkeypatch.setattr(RunDestinationCapabilityAuthority, "issue", flaky_issue)

    with pytest.raises(RuntimeError, match="issuer unavailable"):
        context.record_web_search_result(WebResearchResult.create([first, second]))

    assert context.web_evidence_count() == 0
    for evidence in (first, second):
        assert context.resolve_web_evidence(evidence.evidence_id) is None
        assert (
            context.destination_capability_for_tool(
                "web_fetch",
                {"evidence_id": evidence.evidence_id},
            )
            is None
        )
