"""Current Capability catalog and Tool registry used by HTTP and Runs."""

from __future__ import annotations

from backend.agent.factory import runtime_factory
from backend.capabilities.catalog import CapabilityCatalog
from backend.tools.catalog import configured_secret_names, tool_registry
from backend.tools.registry import ToolRegistry


class RuntimeCapabilityCatalog:
    def __init__(self) -> None:
        self._catalog = CapabilityCatalog(
            skills=runtime_factory.skills,
            tools=tool_registry,
            secret_names_provider=configured_secret_names,
        )
        self._tools = tool_registry

    def install(
        self,
        *,
        catalog: CapabilityCatalog,
        tools: ToolRegistry,
    ) -> None:
        if not isinstance(catalog, CapabilityCatalog):
            raise TypeError("catalog must be a CapabilityCatalog")
        if not isinstance(tools, ToolRegistry):
            raise TypeError("tools must be a ToolRegistry")
        self._catalog = catalog
        self._tools = tools

    def snapshot(self, *, role: str):
        return self._catalog.snapshot(role=role)

    @property
    def tools(self) -> ToolRegistry:
        return self._tools


runtime_capability_catalog = RuntimeCapabilityCatalog()


def install_runtime_capabilities(
    *,
    catalog: CapabilityCatalog,
    tools: ToolRegistry,
) -> None:
    runtime_capability_catalog.install(
        catalog=catalog,
        tools=tools,
    )


def active_tool_registry() -> ToolRegistry:
    return runtime_capability_catalog.tools


__all__ = [
    "RuntimeCapabilityCatalog",
    "active_tool_registry",
    "install_runtime_capabilities",
    "runtime_capability_catalog",
]
