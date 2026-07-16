"""Deterministic, fail-closed Tool Guardrail policy Implementation.

One small ``ToolGuardrail.evaluate`` Interface hides context validation, Skill
scope attenuation, high-risk defaults, SQL and network rules, capability
verification, provider failures, policy identity, and audit-safe metadata. This
Depth gives middleware and future Sandbox Adapters the same policy Leverage and
keeps security decisions local to one Module.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from backend.guardrails.contracts import (
    DestinationCapability,
    GuardrailDecision,
    GuardrailDirective,
    GuardrailReasonCode,
    SafeMetadataValue,
    ToolGuardrailRequest,
    ToolGuardrailResult,
    destination_context_binding,
)


_STABLE_ID_RE = re.compile(r"[a-z][a-z0-9_.:-]{0,127}")
_POLICY_VERSION_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.+-]{0,63}")


def _stable_identifier(value: str, *, field_name: str) -> str:
    if not isinstance(value, str) or _STABLE_ID_RE.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a stable identifier")
    return value


def _stable_set(values: frozenset[str], *, field_name: str) -> frozenset[str]:
    try:
        normalized = frozenset(values)
    except TypeError as exc:
        raise TypeError(f"{field_name} must be an iterable of strings") from exc
    for value in normalized:
        _stable_identifier(value, field_name=field_name)
    return normalized


def _canonical_fingerprint(payload: object) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class SkillToolScope:
    """Tools and risk groups admitted by one active Skill."""

    skill_name: str
    allowed_tools: frozenset[str] = field(default_factory=frozenset)
    allowed_groups: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        _stable_identifier(self.skill_name, field_name="skill_name")
        object.__setattr__(
            self,
            "allowed_tools",
            _stable_set(self.allowed_tools, field_name="allowed_tools"),
        )
        object.__setattr__(
            self,
            "allowed_groups",
            _stable_set(self.allowed_groups, field_name="allowed_groups"),
        )
        if not self.allowed_tools and not self.allowed_groups:
            raise ValueError("a Skill scope must allow at least one tool or group")

    def allows(self, *, tool_name: str, tool_group: str) -> bool:
        return tool_name in self.allowed_tools or tool_group in self.allowed_groups

    def canonical_record(self) -> dict[str, object]:
        return {
            "skill_name": self.skill_name,
            "allowed_tools": sorted(self.allowed_tools),
            "allowed_groups": sorted(self.allowed_groups),
        }


_DEFAULT_HIGH_RISK_DENY_GROUPS = frozenset(
    {"shell", "code", "process", "network-private", "high-risk"}
)
_DEFAULT_APPROVAL_GROUPS = frozenset({"file-write", "sandbox-execution"})


@dataclass(frozen=True, slots=True)
class GuardrailPolicy:
    """Immutable deterministic policy snapshot with canonical identity."""

    version: str = "1.1.0"
    known_channels: frozenset[str] = frozenset({"api", "chat", "web", "worker"})
    known_network_policies: frozenset[str] = frozenset(
        {"none", "restricted", "private-data"}
    )
    known_resource_scopes: frozenset[str] = frozenset(
        {
            "none",
            "knowledge-read",
            "public-web",
            "private-data-read",
            "thread-read",
            "thread-write",
            "process",
            "code-execution",
        }
    )
    known_tool_groups: frozenset[str] = frozenset(
        {
            "knowledge",
            "weather",
            "registry-control",
            "sql",
            "web-research",
            "shell",
            "code",
            "file-write",
            "sandbox-execution",
            "process",
            "network-private",
            "high-risk",
        }
    )
    skill_scopes: tuple[SkillToolScope, ...] = (
        SkillToolScope(
            "knowledge-base",
            allowed_tools=frozenset({"search_knowledge_base"}),
        ),
        SkillToolScope(
            "sql-assistant",
            allowed_tools=frozenset({"sql_query", "sql_schema"}),
        ),
        SkillToolScope(
            "web-research",
            allowed_tools=frozenset({"web_fetch", "web_search"}),
        ),
        SkillToolScope(
            "sandbox",
            allowed_tools=frozenset({"sandbox_execute"}),
        ),
        SkillToolScope(
            "file-manager",
            allowed_groups=frozenset({"file-write"}),
        ),
    )
    resident_tools: frozenset[str] = frozenset(
        {"get_current_weather", "search_knowledge_base"}
    )
    control_tools: frozenset[str] = frozenset({"describe_skill", "tool_search"})
    control_groups: frozenset[str] = frozenset({"registry-control"})
    hard_deny_groups: frozenset[str] = _DEFAULT_HIGH_RISK_DENY_GROUPS
    approval_groups: frozenset[str] = _DEFAULT_APPROVAL_GROUPS
    restricted_web_tools: frozenset[str] = frozenset({"web_fetch", "web_search"})
    destination_capability_tools: frozenset[str] = frozenset({"web_fetch"})
    restricted_network_policy: str = "restricted"
    private_network_policy: str = "private-data"
    public_web_scope: str = "public-web"
    web_group: str = "web-research"
    web_skill: str = "web-research"
    sql_group: str = "sql"
    sql_skill: str = "sql-assistant"
    sql_admin_role: str = "admin"
    sql_read_scope: str = "private-data-read"
    sql_read_only_tools: frozenset[str] = frozenset({"sql_query", "sql_schema"})
    destination_capability_issuer: str = "web-url-policy"

    def __post_init__(self) -> None:
        if (
            not isinstance(self.version, str)
            or _POLICY_VERSION_RE.fullmatch(self.version) is None
        ):
            raise ValueError("version must be a stable policy version")
        set_fields = (
            "known_channels",
            "known_network_policies",
            "known_resource_scopes",
            "known_tool_groups",
            "resident_tools",
            "control_tools",
            "control_groups",
            "hard_deny_groups",
            "approval_groups",
            "restricted_web_tools",
            "destination_capability_tools",
            "sql_read_only_tools",
        )
        for field_name in set_fields:
            object.__setattr__(
                self,
                field_name,
                _stable_set(getattr(self, field_name), field_name=field_name),
            )
        identifier_fields = (
            "restricted_network_policy",
            "private_network_policy",
            "public_web_scope",
            "web_group",
            "web_skill",
            "sql_group",
            "sql_skill",
            "sql_admin_role",
            "sql_read_scope",
            "destination_capability_issuer",
        )
        for field_name in identifier_fields:
            _stable_identifier(getattr(self, field_name), field_name=field_name)

        scopes = tuple(sorted(self.skill_scopes, key=lambda item: item.skill_name))
        if any(not isinstance(scope, SkillToolScope) for scope in scopes):
            raise TypeError("skill_scopes must contain SkillToolScope values")
        scope_names = [scope.skill_name for scope in scopes]
        if len(scope_names) != len(set(scope_names)):
            raise ValueError("skill_scopes must use unique Skill names")
        object.__setattr__(self, "skill_scopes", scopes)

        if self.hard_deny_groups.intersection(self.approval_groups):
            raise ValueError("hard_deny_groups and approval_groups cannot overlap")
        group_subsets = (
            self.control_groups,
            self.hard_deny_groups,
            self.approval_groups,
            frozenset({self.web_group}),
            frozenset({self.sql_group}),
        )
        if any(not subset.issubset(self.known_tool_groups) for subset in group_subsets):
            raise ValueError("configured policy groups must be known")
        if self.restricted_network_policy not in self.known_network_policies:
            raise ValueError("restricted_network_policy must be known")
        if self.private_network_policy not in self.known_network_policies:
            raise ValueError("private_network_policy must be known")
        if self.public_web_scope not in self.known_resource_scopes:
            raise ValueError("public_web_scope must be known")
        if self.sql_read_scope not in self.known_resource_scopes:
            raise ValueError("sql_read_scope must be known")
        if not self.destination_capability_tools.issubset(self.restricted_web_tools):
            raise ValueError(
                "destination_capability_tools must be restricted web tools"
            )
        web_scope = self.scope_for(self.web_skill)
        if web_scope is None or not self.restricted_web_tools.issubset(
            web_scope.allowed_tools
        ):
            raise ValueError(
                "restricted_web_tools must be inside the Web Research Skill scope"
            )
        sql_scope = self.scope_for(self.sql_skill)
        if sql_scope is None or not self.sql_read_only_tools.issubset(
            sql_scope.allowed_tools
        ):
            raise ValueError("sql_read_only_tools must be inside the SQL Skill scope")
        for scope in self.skill_scopes:
            if not scope.allowed_groups.issubset(self.known_tool_groups):
                raise ValueError("Skill allowed_groups must be known")

    def scope_for(self, skill_name: str) -> SkillToolScope | None:
        return next(
            (scope for scope in self.skill_scopes if scope.skill_name == skill_name),
            None,
        )

    @property
    def policy_hash(self) -> str:
        return _canonical_fingerprint(
            {
                "schema_version": 1,
                "version": self.version,
                "known_channels": sorted(self.known_channels),
                "known_network_policies": sorted(self.known_network_policies),
                "known_resource_scopes": sorted(self.known_resource_scopes),
                "known_tool_groups": sorted(self.known_tool_groups),
                "skill_scopes": [
                    scope.canonical_record() for scope in self.skill_scopes
                ],
                "resident_tools": sorted(self.resident_tools),
                "control_tools": sorted(self.control_tools),
                "control_groups": sorted(self.control_groups),
                "hard_deny_groups": sorted(self.hard_deny_groups),
                "approval_groups": sorted(self.approval_groups),
                "restricted_web_tools": sorted(self.restricted_web_tools),
                "destination_capability_tools": sorted(
                    self.destination_capability_tools
                ),
                "restricted_network_policy": self.restricted_network_policy,
                "private_network_policy": self.private_network_policy,
                "public_web_scope": self.public_web_scope,
                "web_group": self.web_group,
                "web_skill": self.web_skill,
                "sql_group": self.sql_group,
                "sql_skill": self.sql_skill,
                "sql_admin_role": self.sql_admin_role,
                "sql_read_scope": self.sql_read_scope,
                "sql_read_only_tools": sorted(self.sql_read_only_tools),
                "destination_capability_issuer": (self.destination_capability_issuer),
            }
        )


DEFAULT_GUARDRAIL_POLICY = GuardrailPolicy()


@runtime_checkable
class DestinationCapabilityVerifier(Protocol):
    """Adapter Seam for verifying URL-policy capability signatures."""

    def verify(
        self,
        capability: DestinationCapability,
        *,
        request: ToolGuardrailRequest,
    ) -> bool: ...


@runtime_checkable
class ToolGuardrailProvider(Protocol):
    """Optional policy Adapter that may only attenuate a base ALLOW."""

    def decide(self, request: ToolGuardrailRequest) -> GuardrailDirective: ...


class DeterministicToolGuardrailProvider:
    """Default policy Adapter implementing the accepted decision matrix."""

    def __init__(
        self,
        policy: GuardrailPolicy = DEFAULT_GUARDRAIL_POLICY,
        *,
        destination_verifier: DestinationCapabilityVerifier | None = None,
    ) -> None:
        if not isinstance(policy, GuardrailPolicy):
            raise TypeError("policy must be a GuardrailPolicy")
        if destination_verifier is not None and not isinstance(
            destination_verifier,
            DestinationCapabilityVerifier,
        ):
            raise TypeError(
                "destination_verifier must satisfy DestinationCapabilityVerifier"
            )
        self.policy = policy
        self.destination_verifier = destination_verifier

    @staticmethod
    def _directive(
        decision: GuardrailDecision,
        reason_code: GuardrailReasonCode,
    ) -> GuardrailDirective:
        return GuardrailDirective(decision=decision, reason_code=reason_code)

    def decide(self, request: ToolGuardrailRequest) -> GuardrailDirective:
        if request.missing_context_fields:
            return self._directive(
                GuardrailDecision.DENY,
                GuardrailReasonCode.CONTEXT_INCOMPLETE,
            )
        if request.has_unknown_context_values:
            return self._directive(
                GuardrailDecision.DENY,
                GuardrailReasonCode.CONTEXT_UNKNOWN,
            )

        assert request.tool_name is not None
        assert request.tool_group is not None
        assert request.channel is not None
        assert request.network_policy is not None
        assert request.resource_scope is not None
        assert request.roles is not None
        assert request.descriptor_requires_approval is not None
        assert request.approval_granted is not None

        if request.channel not in self.policy.known_channels:
            return self._directive(
                GuardrailDecision.DENY,
                GuardrailReasonCode.CHANNEL_UNKNOWN,
            )
        if request.network_policy not in self.policy.known_network_policies:
            return self._directive(
                GuardrailDecision.DENY,
                GuardrailReasonCode.NETWORK_POLICY_UNKNOWN,
            )
        if request.resource_scope not in self.policy.known_resource_scopes:
            return self._directive(
                GuardrailDecision.DENY,
                GuardrailReasonCode.RESOURCE_SCOPE_UNKNOWN,
            )
        if request.tool_group not in self.policy.known_tool_groups:
            return self._directive(
                GuardrailDecision.DENY,
                GuardrailReasonCode.TOOL_GROUP_UNKNOWN,
            )
        if request.tool_group in self.policy.hard_deny_groups:
            return self._directive(
                GuardrailDecision.DENY,
                GuardrailReasonCode.HIGH_RISK_TOOL_DENIED,
            )

        scoped = self._check_skill_scope(request)
        if scoped is not None:
            return scoped

        sql_rule = self._check_sql(request)
        if sql_rule is not None:
            return sql_rule

        destination_rule = self._check_destination(request)
        if destination_rule is not None:
            return destination_rule

        if request.descriptor_requires_approval and not request.approval_granted:
            return self._directive(
                GuardrailDecision.REQUIRE_APPROVAL,
                GuardrailReasonCode.DESCRIPTOR_APPROVAL_REQUIRED,
            )
        if request.tool_group in self.policy.approval_groups:
            if request.approval_granted:
                return self._directive(
                    GuardrailDecision.ALLOW,
                    GuardrailReasonCode.ALLOWED,
                )
            return self._directive(
                GuardrailDecision.REQUIRE_APPROVAL,
                GuardrailReasonCode.HIGH_RISK_TOOL_APPROVAL_REQUIRED,
            )
        return self._directive(
            GuardrailDecision.ALLOW,
            GuardrailReasonCode.ALLOWED,
        )

    def _check_skill_scope(
        self,
        request: ToolGuardrailRequest,
    ) -> GuardrailDirective | None:
        assert request.tool_name is not None
        assert request.tool_group is not None
        is_control = (
            request.tool_name in self.policy.control_tools
            and request.tool_group in self.policy.control_groups
        )
        if request.active_skill is None:
            if is_control or request.tool_name in self.policy.resident_tools:
                return None
            return self._directive(
                GuardrailDecision.DENY,
                GuardrailReasonCode.ACTIVE_SKILL_REQUIRED,
            )

        if request.active_skill_registered is not True:
            return self._directive(
                GuardrailDecision.DENY,
                GuardrailReasonCode.ACTIVE_SKILL_UNKNOWN,
            )
        if is_control:
            return None
        if request.active_skill_scope_allows is not True:
            return self._directive(
                GuardrailDecision.DENY,
                GuardrailReasonCode.TOOL_OUTSIDE_ACTIVE_SKILL,
            )
        scope = self.policy.scope_for(request.active_skill)
        if scope is None or scope.allows(
            tool_name=request.tool_name,
            tool_group=request.tool_group,
        ):
            return None
        return self._directive(
            GuardrailDecision.DENY,
            GuardrailReasonCode.TOOL_OUTSIDE_ACTIVE_SKILL,
        )

    def _check_sql(
        self,
        request: ToolGuardrailRequest,
    ) -> GuardrailDirective | None:
        assert request.tool_name is not None
        assert request.tool_group is not None
        assert request.network_policy is not None
        assert request.resource_scope is not None
        assert request.roles is not None

        is_sql_group = request.tool_group == self.policy.sql_group
        is_sql_tool = request.tool_name in self.policy.sql_read_only_tools
        if not is_sql_group and not is_sql_tool:
            if request.network_policy == self.policy.private_network_policy:
                return self._directive(
                    GuardrailDecision.DENY,
                    GuardrailReasonCode.PRIVATE_NETWORK_DENIED,
                )
            return None
        if not is_sql_group or request.active_skill != self.policy.sql_skill:
            return self._directive(
                GuardrailDecision.DENY,
                GuardrailReasonCode.SQL_CONTEXT_REQUIRED,
            )
        if request.network_policy != self.policy.private_network_policy:
            return self._directive(
                GuardrailDecision.DENY,
                GuardrailReasonCode.SQL_PRIVATE_NETWORK_REQUIRED,
            )
        if self.policy.sql_admin_role not in request.roles:
            return self._directive(
                GuardrailDecision.DENY,
                GuardrailReasonCode.SQL_ADMIN_REQUIRED,
            )
        if not is_sql_tool:
            return self._directive(
                GuardrailDecision.DENY,
                GuardrailReasonCode.SQL_READ_ONLY_TOOL_REQUIRED,
            )
        if request.resource_scope != self.policy.sql_read_scope:
            return self._directive(
                GuardrailDecision.DENY,
                GuardrailReasonCode.SQL_RESOURCE_SCOPE_DENIED,
            )
        return None

    def _check_destination(
        self,
        request: ToolGuardrailRequest,
    ) -> GuardrailDirective | None:
        assert request.tool_name is not None
        assert request.tool_group is not None
        assert request.network_policy is not None
        assert request.resource_scope is not None
        assert request.user_id is not None
        assert request.tenant_id is not None
        assert request.thread_id is not None
        assert request.run_id is not None

        is_web_group = request.tool_group == self.policy.web_group
        is_web_tool = request.tool_name in self.policy.restricted_web_tools
        if not is_web_group and not is_web_tool:
            return None
        if (
            not is_web_group
            or not is_web_tool
            or request.active_skill != self.policy.web_skill
        ):
            return self._directive(
                GuardrailDecision.DENY,
                GuardrailReasonCode.WEB_CONTEXT_REQUIRED,
            )
        if (
            request.network_policy != self.policy.restricted_network_policy
            or request.resource_scope != self.policy.public_web_scope
        ):
            return self._directive(
                GuardrailDecision.DENY,
                GuardrailReasonCode.DESTINATION_CAPABILITY_INVALID,
            )
        if request.tool_name not in self.policy.destination_capability_tools:
            return None
        capability = request.destination_capability
        if capability is None:
            return self._directive(
                GuardrailDecision.DENY,
                GuardrailReasonCode.DESTINATION_CAPABILITY_REQUIRED,
            )
        expected_binding = destination_context_binding(
            user_id=request.user_id,
            tenant_id=request.tenant_id,
            thread_id=request.thread_id,
            run_id=request.run_id,
        )
        if not (
            capability.issuer == self.policy.destination_capability_issuer
            and capability.tool_name == request.tool_name
            and capability.network_policy == request.network_policy
            and capability.resource_scope == request.resource_scope
            and hmac.compare_digest(capability.context_binding, expected_binding)
        ):
            return self._directive(
                GuardrailDecision.DENY,
                GuardrailReasonCode.DESTINATION_CAPABILITY_INVALID,
            )
        if self.destination_verifier is None:
            return self._directive(
                GuardrailDecision.DENY,
                GuardrailReasonCode.DESTINATION_CAPABILITY_UNVERIFIED,
            )
        try:
            verified = self.destination_verifier.verify(
                capability,
                request=request,
            )
        except Exception:
            return self._directive(
                GuardrailDecision.DENY,
                GuardrailReasonCode.DESTINATION_CAPABILITY_PROVIDER_FAILED,
            )
        if verified is not True:
            return self._directive(
                GuardrailDecision.DENY,
                GuardrailReasonCode.DESTINATION_CAPABILITY_INVALID,
            )
        return None


def _safe_identifier(value: object) -> str:
    if isinstance(value, str) and _STABLE_ID_RE.fullmatch(value) is not None:
        return value
    return "unknown"


def _safe_metadata(request: object) -> dict[str, SafeMetadataValue]:
    if not isinstance(request, ToolGuardrailRequest):
        return {
            "active_skill": "unknown",
            "active_skill_registered": None,
            "active_skill_scope_allows": None,
            "approval_granted": None,
            "channel": "unknown",
            "context_complete": False,
            "descriptor_requires_approval": None,
            "destination_capability_present": False,
            "network_policy": "unknown",
            "resource_scope": "unknown",
            "role_count": 0,
            "tool_group": "unknown",
            "tool_name": "unknown",
        }
    return {
        "active_skill": (
            "inactive"
            if request.active_skill is None
            else _safe_identifier(request.active_skill)
        ),
        "active_skill_registered": request.active_skill_registered,
        "active_skill_scope_allows": request.active_skill_scope_allows,
        "approval_granted": request.approval_granted,
        "channel": _safe_identifier(request.channel),
        "context_complete": request.context_complete,
        "descriptor_requires_approval": request.descriptor_requires_approval,
        "destination_capability_present": (request.destination_capability is not None),
        "network_policy": _safe_identifier(request.network_policy),
        "resource_scope": _safe_identifier(request.resource_scope),
        "role_count": min(len(request.roles or ()), 1_024),
        "tool_group": _safe_identifier(request.tool_group),
        "tool_name": _safe_identifier(request.tool_name),
    }


class ToolGuardrail:
    """Fail-closed execution Interface over one policy provider Adapter.

    The result intentionally has no approval token. ``REQUIRE_APPROVAL`` only
    transfers control to a separate durable approval Module; this core neither
    mints nor trusts an approval credential.
    """

    def __init__(
        self,
        policy: GuardrailPolicy = DEFAULT_GUARDRAIL_POLICY,
        *,
        provider: ToolGuardrailProvider | None = None,
        destination_verifier: DestinationCapabilityVerifier | None = None,
    ) -> None:
        if not isinstance(policy, GuardrailPolicy):
            raise TypeError("policy must be a GuardrailPolicy")
        if provider is not None and not isinstance(provider, ToolGuardrailProvider):
            raise TypeError("provider must satisfy ToolGuardrailProvider")
        self.policy = policy
        self.base_provider = DeterministicToolGuardrailProvider(
            policy,
            destination_verifier=destination_verifier,
        )
        self.provider = provider

    def evaluate(self, request: ToolGuardrailRequest | object) -> ToolGuardrailResult:
        if not isinstance(request, ToolGuardrailRequest):
            directive = GuardrailDirective(
                decision=GuardrailDecision.DENY,
                reason_code=GuardrailReasonCode.CONTEXT_INCOMPLETE,
            )
        else:
            try:
                base_directive = self.base_provider.decide(request)
                if not isinstance(base_directive, GuardrailDirective):
                    raise TypeError("base provider returned an invalid directive")
                if (
                    base_directive.decision is not GuardrailDecision.DENY
                    and self.provider is not None
                ):
                    provider_directive = self.provider.decide(request)
                    if not isinstance(provider_directive, GuardrailDirective):
                        raise TypeError("provider returned an invalid directive")
                    if (
                        base_directive.decision is GuardrailDecision.ALLOW
                        or provider_directive.decision is GuardrailDecision.DENY
                    ):
                        directive = provider_directive
                    else:
                        directive = base_directive
                else:
                    directive = base_directive
            except Exception:
                directive = GuardrailDirective(
                    decision=GuardrailDecision.DENY,
                    reason_code=GuardrailReasonCode.POLICY_PROVIDER_FAILED,
                )
        return ToolGuardrailResult(
            decision=directive.decision,
            reason_code=directive.reason_code,
            policy_version=self.policy.version,
            policy_hash=self.policy.policy_hash,
            safe_metadata=_safe_metadata(request),
        )


__all__ = [
    "DEFAULT_GUARDRAIL_POLICY",
    "DestinationCapabilityVerifier",
    "DeterministicToolGuardrailProvider",
    "GuardrailPolicy",
    "SkillToolScope",
    "ToolGuardrail",
    "ToolGuardrailProvider",
]
