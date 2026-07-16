from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from langchain_core.tools import tool

from backend.skills import SkillRegistryError
from backend.tools.contracts import ToolResultV1, new_tool_failure, new_tool_success
from backend.tools.registry import ToolDescriptor, ToolSession


def _descriptor_payload(descriptor: ToolDescriptor) -> dict[str, Any]:
    return {
        "name": descriptor.name,
        "description": descriptor.description,
        "group": descriptor.group,
        "version": descriptor.version,
        "input_schema": dict(descriptor.input_schema),
        "output_schema": dict(descriptor.output_schema),
        "timeout": descriptor.timeout,
        "max_concurrency": descriptor.max_concurrency,
        "idempotent": descriptor.idempotent,
        "requires_approval": descriptor.requires_approval,
        "network_policy": descriptor.network_policy,
        "result_size_limit": descriptor.result_size_limit,
    }


def make_control_tool_overrides(holder: Mapping[str, object]) -> dict[str, object]:
    """Build request-owned control Adapters over Run-local registry sessions."""

    def _sessions():
        skill_session = holder.get("skill_session")
        tool_session = holder.get("tool_session")
        if skill_session is None or not isinstance(tool_session, ToolSession):
            raise RuntimeError("registry control tools are not bound to a Run")
        return skill_session, tool_session

    @tool("describe_skill")
    def describe_skill(name: str) -> ToolResultV1:
        """Activate one available Skill and load its full instructions."""

        try:
            skill_session, tool_session = _sessions()
            activated = skill_session.describe(name)
            allowed = sorted(
                activated.allowed_tools.intersection(tool_session.authorized_names)
            )
            result = new_tool_success(
                data={
                    "name": activated.name,
                    "version": activated.version,
                    "description": activated.description,
                    "content_hash": activated.pin.content_hash,
                    "instructions": activated.instructions,
                    "allowed_tools": allowed,
                },
                observability_metadata={
                    "skill_name": activated.name,
                    "skill_version": activated.version,
                    "activation_source": activated.source,
                },
            )
        except SkillRegistryError:
            result = new_tool_failure(
                error_code="SKILL_NOT_AVAILABLE",
                retryable=False,
                data={"message": "该 Skill 不存在、不可用或本 Run 已激活其他 Skill。"},
            )
        return result

    @tool("tool_search")
    def tool_search(query: str, limit: int = 5) -> ToolResultV1:
        """Search authorized deferred tools and reveal matching schemas."""

        _, tool_session = _sessions()
        safe_limit = max(1, min(int(limit), 8))
        descriptors = tool_session.search(query, limit=safe_limit)
        result = new_tool_success(
            data={
                "tools": [_descriptor_payload(item) for item in descriptors],
                "count": len(descriptors),
            },
            observability_metadata={"revealed_count": len(descriptors)},
        )
        return result

    return {
        "describe_skill": describe_skill,
        "tool_search": tool_search,
    }


__all__ = ["make_control_tool_overrides"]
