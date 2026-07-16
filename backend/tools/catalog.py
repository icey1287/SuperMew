from __future__ import annotations

import os
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from backend.tools.contracts import TOOL_RESULT_V1_SCHEMA
from backend.tools.knowledge import make_search_knowledge_base
from backend.tools.registry import (
    ToolDescriptor,
    ToolExposure,
    ToolRegistry,
)
from backend.tools.weather import make_weather_tool


class _StrictInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class KnowledgeQuery(_StrictInput):
    query: str = Field(min_length=1, max_length=16_000)


class WeatherQuery(_StrictInput):
    location: str = Field(min_length=1, max_length=120)
    extensions: Literal["base", "all"] = "base"


class DescribeSkillInput(_StrictInput):
    name: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$", max_length=64)


class ToolSearchInput(_StrictInput):
    query: str = Field(min_length=1, max_length=240)
    limit: int = Field(default=5, ge=1, le=8)


_STRING_OUTPUT_SCHEMA = {"type": "string"}


def _control_placeholder(name: str):
    def build(_request_context):
        raise RuntimeError(f"control tool {name} requires a request-owned override")

    return build


def build_default_tool_registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(
        ToolDescriptor(
            name="search_knowledge_base",
            description="Search uploaded and organizational knowledge with cited evidence.",
            group="knowledge",
            version="1.0.0",
            input_schema=KnowledgeQuery.model_json_schema(),
            output_schema=_STRING_OUTPUT_SCHEMA,
            timeout=90.0,
            max_concurrency=4,
            idempotent=True,
            required_roles=frozenset(),
            required_secrets=frozenset(),
            requires_approval=False,
            network_policy="restricted",
            result_size_limit=524_288,
        ),
        make_search_knowledge_base,
        exposure=ToolExposure.RESIDENT,
    )
    registry.register(
        ToolDescriptor(
            name="get_current_weather",
            description="Get current weather or forecasts for a city in China.",
            group="weather",
            version="1.0.0",
            input_schema=WeatherQuery.model_json_schema(),
            output_schema=_STRING_OUTPUT_SCHEMA,
            timeout=10.0,
            max_concurrency=4,
            idempotent=True,
            required_roles=frozenset(),
            required_secrets=frozenset({"AMAP_WEATHER_API", "AMAP_API_KEY"}),
            requires_approval=False,
            network_policy="restricted",
            result_size_limit=65_536,
        ),
        make_weather_tool,
        exposure=ToolExposure.RESIDENT,
    )
    registry.register(
        ToolDescriptor(
            name="describe_skill",
            description="Load the full instructions for one authorized Skill.",
            group="registry-control",
            version="1.0.0",
            input_schema=DescribeSkillInput.model_json_schema(),
            output_schema=TOOL_RESULT_V1_SCHEMA,
            timeout=2.0,
            max_concurrency=16,
            idempotent=True,
            required_roles=frozenset(),
            required_secrets=frozenset(),
            requires_approval=False,
            network_policy="none",
            result_size_limit=524_288,
        ),
        _control_placeholder("describe_skill"),
        exposure=ToolExposure.CONTROL,
    )
    registry.register(
        ToolDescriptor(
            name="tool_search",
            description="Reveal full schemas for authorized deferred tools matching a query.",
            group="registry-control",
            version="1.0.0",
            input_schema=ToolSearchInput.model_json_schema(),
            output_schema=TOOL_RESULT_V1_SCHEMA,
            timeout=2.0,
            max_concurrency=16,
            idempotent=True,
            required_roles=frozenset(),
            required_secrets=frozenset(),
            requires_approval=False,
            network_policy="none",
            result_size_limit=262_144,
        ),
        _control_placeholder("tool_search"),
        exposure=ToolExposure.CONTROL,
    )
    registry.freeze()
    return registry


def configured_secret_names(registry: ToolRegistry) -> frozenset[str]:
    required: set[str] = set()
    for name in registry.names:
        descriptor = registry.descriptor(name)
        if descriptor is not None:
            required.update(descriptor.required_secrets)
    return frozenset(name for name in required if os.getenv(name, "").strip())


tool_registry = build_default_tool_registry()


__all__ = [
    "build_default_tool_registry",
    "configured_secret_names",
    "tool_registry",
]
