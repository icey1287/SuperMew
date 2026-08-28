from __future__ import annotations

from collections.abc import Mapping

from langchain_core.tools import tool

from backend.skills import SkillActivationSession, SkillRegistryError
from backend.tools.contracts import ToolResultV1, new_tool_failure, new_tool_success
from backend.tools.registry import ToolSession


def make_control_tool_overrides(holder: Mapping[str, object]) -> dict[str, object]:
    """Build request-owned control Adapters over Run-local registry sessions."""

    def _sessions() -> tuple[SkillActivationSession, ToolSession]:
        skill_session = holder.get("skill_session")
        tool_session = holder.get("tool_session")
        if not isinstance(skill_session, SkillActivationSession) or not isinstance(
            tool_session, ToolSession
        ):
            raise RuntimeError("registry control tools are not bound to a Run")
        return skill_session, tool_session

    @tool("describe_skill")
    def describe_skill(name: str) -> ToolResultV1:
        """Activate one available Skill for subsequent model calls."""

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
                    "content_hash": activated.pin.content_hash,
                    "activated": True,
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
        """Reveal authorized deferred tools for subsequent model calls."""

        _, tool_session = _sessions()
        safe_limit = max(1, min(int(limit), 8))
        descriptors = tool_session.search(query, limit=safe_limit)
        result = new_tool_success(
            data={
                "revealed_tools": [item.name for item in descriptors],
                "count": len(descriptors),
                "schemas_available": True,
            },
            observability_metadata={"revealed_count": len(descriptors)},
        )
        return result

    return {
        "describe_skill": describe_skill,
        "tool_search": tool_search,
    }


__all__ = ["make_control_tool_overrides"]
