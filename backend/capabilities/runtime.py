"""Runtime Adapter binding the Capability catalog to production registries."""

from backend.agent.factory import runtime_factory
from backend.capabilities.catalog import CapabilityCatalog
from backend.tools.catalog import configured_secret_names, tool_registry


runtime_capability_catalog = CapabilityCatalog(
    skills=runtime_factory.skills,
    tools=tool_registry,
    secret_names_provider=configured_secret_names,
)


__all__ = ["runtime_capability_catalog"]
