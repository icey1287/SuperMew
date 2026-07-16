"""Pure contracts for the Tool Guardrail Module.

The Interface deliberately accepts only structural argument summaries and an
opaque destination capability. Raw tool arguments, request bodies, secrets,
network destinations, and approval tokens never cross this Seam.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Final


_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_CAPABILITY_ID_RE = re.compile(r"destcap_[0-9a-f]{64}")
_STABLE_ID_RE = re.compile(r"[a-z][a-z0-9_.:-]{0,127}")
_POLICY_VERSION_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.+-]{0,63}")
_SECRET_KEY_PARTS: Final = frozenset(
    {
        "api",
        "apikey",
        "authorization",
        "cookie",
        "credential",
        "key",
        "password",
        "secret",
        "session",
        "token",
    }
)
_BODY_KEY_PARTS: Final = frozenset(
    {
        "body",
        "content",
        "data",
        "document",
        "payload",
        "query",
        "sql",
        "text",
    }
)
_KEY_PART_RE = re.compile(r"[A-Za-z0-9]+")
_MAX_ARGUMENT_NODES: Final = 4_096
_MAX_ARGUMENT_DEPTH: Final = 16
_SAFE_METADATA_KEYS: Final = frozenset(
    {
        "active_skill",
        "active_skill_registered",
        "active_skill_scope_allows",
        "approval_granted",
        "channel",
        "context_complete",
        "descriptor_requires_approval",
        "destination_capability_present",
        "network_policy",
        "resource_scope",
        "role_count",
        "tool_group",
        "tool_name",
    }
)
_SAFE_METADATA_IDENTIFIER_KEYS: Final = frozenset(
    {
        "active_skill",
        "channel",
        "network_policy",
        "resource_scope",
        "tool_group",
        "tool_name",
    }
)
_SAFE_METADATA_BOOLEAN_KEYS: Final = frozenset(
    {
        "active_skill_registered",
        "active_skill_scope_allows",
        "approval_granted",
        "context_complete",
        "destination_capability_present",
    }
)


SafeMetadataValue = str | int | bool | None


def _canonical_fingerprint(payload: object) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def _non_negative_int(value: int, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{field_name} must be an integer")
    if value < 0:
        raise ValueError(f"{field_name} must not be negative")
    return value


def _stable_identifier(value: str, *, field_name: str) -> str:
    if not isinstance(value, str) or _STABLE_ID_RE.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a stable identifier")
    return value


def _sha256(value: str, *, field_name: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a lowercase SHA-256 digest")
    return value


def _context_text_is_complete(value: object) -> bool:
    if not isinstance(value, str) or not value or value != value.strip():
        return False
    if len(value.encode("utf-8", errors="replace")) > 512:
        return False
    return not any(
        ord(character) < 0x20 or ord(character) == 0x7F for character in value
    )


def _stable_context_identifier(value: object) -> bool:
    return isinstance(value, str) and _STABLE_ID_RE.fullmatch(value) is not None


def destination_context_binding(
    *,
    user_id: str,
    tenant_id: str,
    thread_id: str,
    run_id: str,
) -> str:
    """Return the opaque binding a URL policy signs into a capability."""

    fields = {
        "user_id": user_id,
        "tenant_id": tenant_id,
        "thread_id": thread_id,
        "run_id": run_id,
    }
    if any(not _context_text_is_complete(value) for value in fields.values()):
        raise ValueError("destination context fields must be complete")
    return _canonical_fingerprint({"schema_version": 1, **fields})


class GuardrailDecision(StrEnum):
    ALLOW = "ALLOW"
    DENY = "DENY"
    REQUIRE_APPROVAL = "REQUIRE_APPROVAL"


class GuardrailReasonCode(StrEnum):
    ALLOWED = "ALLOWED"
    CONTEXT_INCOMPLETE = "CONTEXT_INCOMPLETE"
    CONTEXT_UNKNOWN = "CONTEXT_UNKNOWN"
    CHANNEL_UNKNOWN = "CHANNEL_UNKNOWN"
    NETWORK_POLICY_UNKNOWN = "NETWORK_POLICY_UNKNOWN"
    RESOURCE_SCOPE_UNKNOWN = "RESOURCE_SCOPE_UNKNOWN"
    TOOL_GROUP_UNKNOWN = "TOOL_GROUP_UNKNOWN"
    REGISTRY_POLICY_DENIED = "REGISTRY_POLICY_DENIED"
    ACTIVE_SKILL_REQUIRED = "ACTIVE_SKILL_REQUIRED"
    ACTIVE_SKILL_UNKNOWN = "ACTIVE_SKILL_UNKNOWN"
    TOOL_OUTSIDE_ACTIVE_SKILL = "TOOL_OUTSIDE_ACTIVE_SKILL"
    DESCRIPTOR_APPROVAL_REQUIRED = "DESCRIPTOR_APPROVAL_REQUIRED"
    HIGH_RISK_TOOL_DENIED = "HIGH_RISK_TOOL_DENIED"
    HIGH_RISK_TOOL_APPROVAL_REQUIRED = "HIGH_RISK_TOOL_APPROVAL_REQUIRED"
    PRIVATE_NETWORK_DENIED = "PRIVATE_NETWORK_DENIED"
    SQL_CONTEXT_REQUIRED = "SQL_CONTEXT_REQUIRED"
    SQL_PRIVATE_NETWORK_REQUIRED = "SQL_PRIVATE_NETWORK_REQUIRED"
    SQL_ADMIN_REQUIRED = "SQL_ADMIN_REQUIRED"
    SQL_READ_ONLY_TOOL_REQUIRED = "SQL_READ_ONLY_TOOL_REQUIRED"
    SQL_RESOURCE_SCOPE_DENIED = "SQL_RESOURCE_SCOPE_DENIED"
    WEB_CONTEXT_REQUIRED = "WEB_CONTEXT_REQUIRED"
    DESTINATION_CAPABILITY_REQUIRED = "DESTINATION_CAPABILITY_REQUIRED"
    DESTINATION_CAPABILITY_INVALID = "DESTINATION_CAPABILITY_INVALID"
    DESTINATION_CAPABILITY_UNVERIFIED = "DESTINATION_CAPABILITY_UNVERIFIED"
    DESTINATION_CAPABILITY_PROVIDER_FAILED = "DESTINATION_CAPABILITY_PROVIDER_FAILED"
    POLICY_PROVIDER_DENIED = "POLICY_PROVIDER_DENIED"
    POLICY_PROVIDER_APPROVAL_REQUIRED = "POLICY_PROVIDER_APPROVAL_REQUIRED"
    POLICY_PROVIDER_FAILED = "POLICY_PROVIDER_FAILED"


@dataclass(frozen=True, slots=True)
class ToolArgsSummary:
    """Bounded structural summary that never retains argument keys or values."""

    complete: bool
    argument_count: int
    scalar_count: int
    collection_count: int
    secret_field_count: int
    body_field_count: int
    unknown_value_count: int
    max_depth: int
    shape_hash: str = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.complete, bool):
            raise TypeError("complete must be a bool")
        for field_name in (
            "argument_count",
            "scalar_count",
            "collection_count",
            "secret_field_count",
            "body_field_count",
            "unknown_value_count",
            "max_depth",
        ):
            _non_negative_int(getattr(self, field_name), field_name=field_name)
        _sha256(self.shape_hash, field_name="shape_hash")

    @classmethod
    def unavailable(cls) -> ToolArgsSummary:
        return cls(
            complete=False,
            argument_count=0,
            scalar_count=0,
            collection_count=0,
            secret_field_count=0,
            body_field_count=0,
            unknown_value_count=1,
            max_depth=0,
            shape_hash=_canonical_fingerprint(
                {"schema_version": 1, "state": "unavailable"}
            ),
        )

    @classmethod
    def from_mapping(cls, arguments: Mapping[str, Any]) -> ToolArgsSummary:
        """Summarize JSON-like arguments without retaining raw keys or values.

        Unsupported values, cycles, excessive depth, and oversized structures
        produce an incomplete summary. The Guardrail then denies the call.
        """

        if not isinstance(arguments, Mapping):
            return cls.unavailable()

        counters = {
            "scalar_count": 0,
            "collection_count": 0,
            "secret_field_count": 0,
            "body_field_count": 0,
            "unknown_value_count": 0,
            "max_depth": 0,
            "node_count": 0,
        }
        complete = True
        active_objects: set[int] = set()

        def classify_key(key: object) -> str:
            nonlocal complete
            if not isinstance(key, str):
                complete = False
                counters["unknown_value_count"] += 1
                return "invalid-key"
            parts = {part.casefold() for part in _KEY_PART_RE.findall(key)}
            compact = "".join(parts)
            if parts.intersection(_SECRET_KEY_PARTS) or any(
                marker in compact
                for marker in ("apikey", "accesstoken", "refreshtoken")
            ):
                counters["secret_field_count"] += 1
                return "secret-field"
            if parts.intersection(_BODY_KEY_PARTS):
                counters["body_field_count"] += 1
                return "body-field"
            return "ordinary-field"

        def visit(value: object, depth: int) -> object:
            nonlocal complete
            counters["node_count"] += 1
            counters["max_depth"] = max(counters["max_depth"], depth)
            if (
                counters["node_count"] > _MAX_ARGUMENT_NODES
                or depth > _MAX_ARGUMENT_DEPTH
            ):
                complete = False
                counters["unknown_value_count"] += 1
                return "limit-exceeded"
            if value is None:
                counters["scalar_count"] += 1
                return "null"
            if isinstance(value, bool):
                counters["scalar_count"] += 1
                return "boolean"
            if isinstance(value, (int, float)):
                counters["scalar_count"] += 1
                if isinstance(value, float) and not math.isfinite(value):
                    complete = False
                    counters["unknown_value_count"] += 1
                    return "invalid-number"
                return "number"
            if isinstance(value, str):
                counters["scalar_count"] += 1
                return "string"
            if isinstance(value, (bytes, bytearray, memoryview)):
                counters["scalar_count"] += 1
                return "binary"
            if isinstance(value, Mapping):
                identity = id(value)
                if identity in active_objects:
                    complete = False
                    counters["unknown_value_count"] += 1
                    return "cycle"
                counters["collection_count"] += 1
                active_objects.add(identity)
                try:
                    entries = [
                        (classify_key(key), visit(item, depth + 1))
                        for key, item in value.items()
                    ]
                finally:
                    active_objects.discard(identity)
                return ["object", sorted(entries, key=lambda item: repr(item))]
            if isinstance(value, (list, tuple)):
                identity = id(value)
                if identity in active_objects:
                    complete = False
                    counters["unknown_value_count"] += 1
                    return "cycle"
                counters["collection_count"] += 1
                active_objects.add(identity)
                try:
                    items = [visit(item, depth + 1) for item in value]
                finally:
                    active_objects.discard(identity)
                return ["array", items]
            complete = False
            counters["unknown_value_count"] += 1
            return "unknown"

        try:
            shape = visit(arguments, 0)
            argument_count = len(arguments)
        except Exception:
            return cls.unavailable()

        return cls(
            complete=complete,
            argument_count=argument_count,
            scalar_count=counters["scalar_count"],
            collection_count=counters["collection_count"],
            secret_field_count=counters["secret_field_count"],
            body_field_count=counters["body_field_count"],
            unknown_value_count=counters["unknown_value_count"],
            max_depth=counters["max_depth"],
            shape_hash=_canonical_fingerprint({"schema_version": 1, "shape": shape}),
        )


@dataclass(frozen=True, slots=True, repr=False)
class DestinationCapability:
    """Signed, destination-redacted claims issued by the URL policy Adapter."""

    capability_id: str
    issuer: str
    policy_hash: str = field(repr=False)
    context_binding: str = field(repr=False)
    destination_hash: str = field(repr=False)
    tool_name: str
    network_policy: str
    resource_scope: str
    signature: str = field(repr=False)

    def __post_init__(self) -> None:
        if (
            not isinstance(self.capability_id, str)
            or _CAPABILITY_ID_RE.fullmatch(self.capability_id) is None
        ):
            raise ValueError("capability_id must be a destination capability ID")
        _stable_identifier(self.issuer, field_name="issuer")
        _sha256(self.policy_hash, field_name="policy_hash")
        _sha256(self.context_binding, field_name="context_binding")
        _sha256(self.destination_hash, field_name="destination_hash")
        _stable_identifier(self.tool_name, field_name="tool_name")
        _stable_identifier(self.network_policy, field_name="network_policy")
        _stable_identifier(self.resource_scope, field_name="resource_scope")
        if (
            not isinstance(self.signature, str)
            or not self.signature
            or len(self.signature) > 8_192
            or not self.signature.isascii()
            or any(
                ord(character) < 0x21 or ord(character) > 0x7E
                for character in self.signature
            )
        ):
            raise ValueError("signature must be a bounded opaque token")

    def __repr__(self) -> str:
        return (
            "DestinationCapability("
            f"issuer={self.issuer!r}, "
            f"tool_name={self.tool_name!r}, "
            f"network_policy={self.network_policy!r}, "
            f"resource_scope={self.resource_scope!r}, signed=True)"
        )


@dataclass(frozen=True, slots=True, repr=False)
class ToolGuardrailRequest:
    """Complete Run-local context required before a tool side effect."""

    user_id: str | None
    roles: frozenset[str] | None
    tenant_id: str | None
    thread_id: str | None
    run_id: str | None
    tool_name: str | None
    tool_group: str | None
    tool_args_summary: ToolArgsSummary | None = field(repr=False)
    active_skill: str | None
    active_skill_registered: bool | None
    active_skill_scope_allows: bool | None
    channel: str | None
    network_policy: str | None
    destination_capability: DestinationCapability | None = field(
        default=None,
        repr=False,
    )
    resource_scope: str | None = None
    descriptor_requires_approval: bool | None = None
    approval_granted: bool | None = None

    def __post_init__(self) -> None:
        if self.roles is not None:
            try:
                normalized_roles = frozenset(self.roles)
            except TypeError as exc:
                raise TypeError("roles must be an iterable of strings or None") from exc
            if any(not isinstance(role, str) for role in normalized_roles):
                raise TypeError("roles must contain strings")
            object.__setattr__(self, "roles", normalized_roles)
        if self.tool_args_summary is not None and not isinstance(
            self.tool_args_summary,
            ToolArgsSummary,
        ):
            raise TypeError("tool_args_summary must be ToolArgsSummary or None")
        for field_name in (
            "active_skill_registered",
            "active_skill_scope_allows",
        ):
            value = getattr(self, field_name)
            if value is not None and not isinstance(value, bool):
                raise TypeError(f"{field_name} must be a bool or None")
        if self.destination_capability is not None and not isinstance(
            self.destination_capability,
            DestinationCapability,
        ):
            raise TypeError(
                "destination_capability must be DestinationCapability or None"
            )
        if self.descriptor_requires_approval is not None and not isinstance(
            self.descriptor_requires_approval,
            bool,
        ):
            raise TypeError("descriptor_requires_approval must be a bool or None")
        if self.approval_granted is not None and not isinstance(
            self.approval_granted,
            bool,
        ):
            raise TypeError("approval_granted must be a bool or None")

    @property
    def missing_context_fields(self) -> tuple[str, ...]:
        missing: list[str] = []
        for field_name in (
            "user_id",
            "tenant_id",
            "thread_id",
            "run_id",
            "tool_name",
            "tool_group",
            "channel",
            "network_policy",
            "resource_scope",
        ):
            if not _context_text_is_complete(getattr(self, field_name)):
                missing.append(field_name)
        if self.roles is None:
            missing.append("roles")
        if self.tool_args_summary is None or not self.tool_args_summary.complete:
            missing.append("tool_args_summary")
        if self.descriptor_requires_approval is None:
            missing.append("descriptor_requires_approval")
        if self.approval_granted is None:
            missing.append("approval_granted")
        if self.active_skill_registered is None:
            missing.append("active_skill_registered")
        if self.active_skill_scope_allows is None:
            missing.append("active_skill_scope_allows")
        if self.active_skill is not None and not _context_text_is_complete(
            self.active_skill
        ):
            missing.append("active_skill")
        return tuple(missing)

    @property
    def has_unknown_context_values(self) -> bool:
        stable_fields = (
            self.tool_name,
            self.tool_group,
            self.channel,
            self.network_policy,
            self.resource_scope,
        )
        if any(not _stable_context_identifier(value) for value in stable_fields):
            return True
        if self.active_skill is not None and not _stable_context_identifier(
            self.active_skill
        ):
            return True
        return self.roles is not None and (
            len(self.roles) > 1_024
            or any(not _stable_context_identifier(role) for role in self.roles)
        )

    @property
    def context_complete(self) -> bool:
        return not self.missing_context_fields and not self.has_unknown_context_values

    def __repr__(self) -> str:
        return (
            "ToolGuardrailRequest("
            f"context_complete={self.context_complete!r}, "
            f"role_count={len(self.roles or ())!r}, "
            f"has_active_skill={self.active_skill is not None!r}, "
            "has_destination_capability="
            f"{self.destination_capability is not None!r})"
        )


@dataclass(frozen=True, slots=True)
class GuardrailDirective:
    """Provider output before the Module adds policy identity and metadata."""

    decision: GuardrailDecision
    reason_code: GuardrailReasonCode

    def __post_init__(self) -> None:
        decision = GuardrailDecision(self.decision)
        reason_code = GuardrailReasonCode(self.reason_code)
        if decision is GuardrailDecision.ALLOW and reason_code is not (
            GuardrailReasonCode.ALLOWED
        ):
            raise ValueError("ALLOW directives must use ALLOWED")
        approval_reasons = {
            GuardrailReasonCode.DESCRIPTOR_APPROVAL_REQUIRED,
            GuardrailReasonCode.HIGH_RISK_TOOL_APPROVAL_REQUIRED,
            GuardrailReasonCode.POLICY_PROVIDER_APPROVAL_REQUIRED,
        }
        if (
            decision is GuardrailDecision.REQUIRE_APPROVAL
            and reason_code not in approval_reasons
        ):
            raise ValueError("REQUIRE_APPROVAL uses an approval reason code")
        if decision is GuardrailDecision.DENY and (
            reason_code is GuardrailReasonCode.ALLOWED
            or reason_code in approval_reasons
        ):
            raise ValueError("DENY directives must use a denial reason code")
        object.__setattr__(self, "decision", decision)
        object.__setattr__(self, "reason_code", reason_code)


@dataclass(frozen=True, slots=True)
class ToolGuardrailResult:
    """Stable, audit-safe result returned at the execution Seam."""

    decision: GuardrailDecision
    reason_code: GuardrailReasonCode
    policy_version: str
    policy_hash: str
    safe_metadata: Mapping[str, SafeMetadataValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        decision = GuardrailDecision(self.decision)
        reason_code = GuardrailReasonCode(self.reason_code)
        GuardrailDirective(decision=decision, reason_code=reason_code)
        if (
            not isinstance(self.policy_version, str)
            or _POLICY_VERSION_RE.fullmatch(self.policy_version) is None
        ):
            raise ValueError("policy_version must be a stable version")
        _sha256(self.policy_hash, field_name="policy_hash")
        metadata = dict(self.safe_metadata)
        unknown_keys = set(metadata).difference(_SAFE_METADATA_KEYS)
        if unknown_keys:
            raise ValueError("safe_metadata contains an unknown key")
        for key, value in metadata.items():
            if not isinstance(key, str) or not isinstance(
                value,
                (str, int, bool, type(None)),
            ):
                raise TypeError("safe_metadata contains an unsafe value")
            if key in _SAFE_METADATA_IDENTIFIER_KEYS and (
                not isinstance(value, str) or _STABLE_ID_RE.fullmatch(value) is None
            ):
                raise ValueError("safe_metadata contains an unsafe identifier")
            if (
                key in _SAFE_METADATA_BOOLEAN_KEYS
                and value is not None
                and not isinstance(value, bool)
            ):
                raise TypeError("safe_metadata contains an unsafe boolean")
            if (
                key == "descriptor_requires_approval"
                and value is not None
                and not isinstance(
                    value,
                    bool,
                )
            ):
                raise TypeError("safe_metadata contains an unsafe approval flag")
            if key == "role_count" and (
                isinstance(value, bool)
                or not isinstance(value, int)
                or not 0 <= value <= 1_024
            ):
                raise ValueError("safe_metadata contains an unsafe role count")
        object.__setattr__(self, "decision", decision)
        object.__setattr__(self, "reason_code", reason_code)
        object.__setattr__(self, "safe_metadata", MappingProxyType(metadata))


__all__ = [
    "DestinationCapability",
    "GuardrailDecision",
    "GuardrailDirective",
    "GuardrailReasonCode",
    "SafeMetadataValue",
    "ToolArgsSummary",
    "ToolGuardrailRequest",
    "ToolGuardrailResult",
    "destination_context_binding",
]
