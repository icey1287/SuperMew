"""Authenticated, secret-free projection of the Skill and Tool registries."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from backend.skills import SkillAccess, SkillRegistry
from backend.skills.registry import SkillControlPlaneSummary
from backend.tools.registry import ToolDescriptor, ToolRegistry


AvailabilityReason = Literal["permission_required", "not_configured"]
SecretNamesProvider = Callable[[ToolRegistry], frozenset[str]]


@dataclass(frozen=True, slots=True)
class CapabilitySkill:
    name: str
    version: str
    description: str
    activation: str
    available: bool
    availability_reason: AvailabilityReason | None
    required_roles: tuple[str, ...]
    tool_names: tuple[str, ...]
    approval_tools: tuple[str, ...]
    network_policies: tuple[str, ...]
    resource_scopes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CapabilityTool:
    name: str
    description: str
    group: str
    version: str
    exposure: str
    available: bool
    availability_reason: AvailabilityReason | None
    required_roles: tuple[str, ...]
    requires_approval: bool
    network_policy: str
    resource_scope: str
    idempotent: bool


@dataclass(frozen=True, slots=True)
class CapabilitySnapshot:
    schema_version: Literal[1]
    catalog_hash: str
    skills: tuple[CapabilitySkill, ...]
    tools: tuple[CapabilityTool, ...]


def _availability(
    descriptor: ToolDescriptor,
    *,
    roles: frozenset[str],
    configured_secret_names: frozenset[str],
) -> tuple[bool, AvailabilityReason | None]:
    if not descriptor.required_roles.issubset(roles):
        return False, "permission_required"
    if not descriptor.required_secrets.issubset(configured_secret_names):
        return False, "not_configured"
    return True, None


class CapabilityCatalog:
    """One control-plane Interface over the authoritative Registry snapshots."""

    def __init__(
        self,
        *,
        skills: SkillRegistry,
        tools: ToolRegistry,
        secret_names_provider: SecretNamesProvider,
    ) -> None:
        self._skills = skills
        self._tools = tools
        self._secret_names_provider = secret_names_provider

    def snapshot(self, *, role: str) -> CapabilitySnapshot:
        roles = frozenset({role})
        configured_secret_names = frozenset(self._secret_names_provider(self._tools))
        skill_access = SkillAccess(
            roles=roles,
            available_secrets=configured_secret_names,
        )
        tool_descriptors = {
            name: descriptor
            for name in self._tools.names
            if (descriptor := self._tools.descriptor(name)) is not None
        }
        tools = tuple(
            self._tool_capability(
                descriptor,
                roles=roles,
                configured_secret_names=configured_secret_names,
            )
            for descriptor in tool_descriptors.values()
        )
        skills = tuple(
            self._skill_capability(
                summary,
                tool_descriptors=tool_descriptors,
                roles=roles,
                configured_secret_names=configured_secret_names,
            )
            for summary in self._skills.control_plane_catalog(skill_access)
        )
        return CapabilitySnapshot(
            schema_version=1,
            catalog_hash=self._tools.catalog_hash,
            skills=skills,
            tools=tools,
        )

    def _tool_capability(
        self,
        descriptor: ToolDescriptor,
        *,
        roles: frozenset[str],
        configured_secret_names: frozenset[str],
    ) -> CapabilityTool:
        available, reason = _availability(
            descriptor,
            roles=roles,
            configured_secret_names=configured_secret_names,
        )
        exposure = self._tools.exposure(descriptor.name)
        if exposure is None:
            raise RuntimeError("registered Tool is missing its exposure")
        return CapabilityTool(
            name=descriptor.name,
            description=descriptor.description,
            group=descriptor.group,
            version=descriptor.version,
            exposure=exposure.value,
            available=available,
            availability_reason=reason,
            required_roles=tuple(sorted(descriptor.required_roles)),
            requires_approval=descriptor.requires_approval,
            network_policy=descriptor.network_policy,
            resource_scope=descriptor.resource_scope,
            idempotent=descriptor.idempotent,
        )

    @staticmethod
    def _skill_capability(
        summary: SkillControlPlaneSummary,
        *,
        tool_descriptors: dict[str, ToolDescriptor],
        roles: frozenset[str],
        configured_secret_names: frozenset[str],
    ) -> CapabilitySkill:
        descriptors = tuple(tool_descriptors[name] for name in summary.tool_names)
        required_roles = frozenset(summary.required_roles).union(
            *(descriptor.required_roles for descriptor in descriptors)
        )
        permission_required = not required_roles.issubset(roles)
        not_configured = summary.availability_reason == "not_configured" or any(
            not descriptor.required_secrets.issubset(configured_secret_names)
            for descriptor in descriptors
        )
        reason: AvailabilityReason | None = None
        if permission_required:
            reason = "permission_required"
        elif not_configured:
            reason = "not_configured"
        return CapabilitySkill(
            name=summary.name,
            version=summary.version,
            description=summary.description,
            activation=summary.activation,
            available=reason is None,
            availability_reason=reason,
            required_roles=tuple(sorted(required_roles)),
            tool_names=summary.tool_names,
            approval_tools=tuple(
                sorted(
                    descriptor.name
                    for descriptor in descriptors
                    if descriptor.requires_approval
                )
            ),
            network_policies=tuple(
                sorted({descriptor.network_policy for descriptor in descriptors})
            ),
            resource_scopes=tuple(
                sorted({descriptor.resource_scope for descriptor in descriptors})
            ),
        )


__all__ = [
    "AvailabilityReason",
    "CapabilityCatalog",
    "CapabilitySkill",
    "CapabilitySnapshot",
    "CapabilityTool",
    "SecretNamesProvider",
]
