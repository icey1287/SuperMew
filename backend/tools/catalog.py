from __future__ import annotations

import os
from collections.abc import Iterable
from functools import partial
from typing import Annotated, Literal

from langchain_core.tools import tool
from pydantic import BaseModel, ConfigDict, Field

from backend.core.settings import (
    SandboxSettings,
    SqlAssistantSettings,
    WebResearchSettings,
)
from backend.tools.sandbox import SANDBOX_METADATA_KEYS
from backend.tools.contracts import TOOL_RESULT_V1_SCHEMA
from backend.tools.registry import (
    ToolDescriptor,
    ToolExposure,
    ToolRegistry,
)
from backend.tools.sql import (
    SQL_QUERY_METADATA_KEYS,
    SQL_SCHEMA_METADATA_KEYS,
    SqlAssistantRuntime,
    make_sql_query,
    make_sql_schema,
)
from backend.tools.web import (
    WEB_RESEARCH_METADATA_KEYS,
    WebResearchRuntime,
    make_web_fetch,
    make_web_search,
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


QualifiedTableName = Annotated[
    str,
    Field(
        min_length=3,
        max_length=127,
        pattern=r"^[A-Za-z_][A-Za-z0-9_$]{0,62}\."
        r"[A-Za-z_][A-Za-z0-9_$]{0,62}$",
    ),
]


class SqlSchemaInput(_StrictInput):
    tables: tuple[QualifiedTableName, ...] = Field(default=(), max_length=64)


class SqlQueryInput(_StrictInput):
    sql: str = Field(min_length=1, max_length=100_000)


BareDomain = Annotated[
    str,
    Field(
        min_length=3,
        max_length=253,
        pattern=(
            r"^(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+"
            r"[A-Za-z]{2,63}$"
        ),
    ),
]


class WebSearchInput(_StrictInput):
    query: str = Field(min_length=1, max_length=16_384)
    max_results: int = Field(default=5, ge=1, le=50)
    allowed_domains: tuple[BareDomain, ...] = Field(default=(), max_length=8)


class WebFetchInput(_StrictInput):
    evidence_id: str = Field(pattern=r"^web_ev_[0-9a-f]{64}$")


class SandboxExecuteInput(_StrictInput):
    language: Literal["python", "sh"]
    source: str = Field(min_length=1, max_length=4_194_304)


_STRING_OUTPUT_SCHEMA = {"type": "string"}


def _control_placeholder(name: str):
    def build(_request_context):
        raise RuntimeError(f"control tool {name} requires a request-owned override")

    return build


def _knowledge_placeholder(_request_context):
    @tool("search_knowledge_base")
    def search_knowledge_base(query: str) -> str:
        """Search the current Document Version Catalog."""

        del query
        raise RuntimeError(
            "search_knowledge_base requires the Run-owned checkpoint Adapter"
        )

    return search_knowledge_base


def build_default_tool_registry(
    *,
    sql_assistant_settings: SqlAssistantSettings | None = None,
    web_research_settings: WebResearchSettings | None = None,
    sandbox_settings: SandboxSettings | None = None,
    sql_runtime: SqlAssistantRuntime | None = None,
    web_runtime: WebResearchRuntime | None = None,
    freeze: bool = True,
) -> ToolRegistry:
    sql_settings = sql_assistant_settings or SqlAssistantSettings()
    web_settings = web_research_settings or WebResearchSettings()
    sandbox_config = sandbox_settings or SandboxSettings()
    web_search_schema = WebSearchInput.model_json_schema()
    web_search_schema["properties"]["query"]["maxLength"] = web_settings.max_query_bytes
    web_search_schema["properties"]["max_results"].update(
        default=web_settings.default_search_results,
        maximum=web_settings.max_search_results,
    )
    sandbox_schema = SandboxExecuteInput.model_json_schema()
    sandbox_schema["properties"]["source"]["maxLength"] = (
        sandbox_config.max_source_bytes
    )
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
            resource_scope="knowledge-read",
        ),
        _knowledge_placeholder,
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
            resource_scope="public-web",
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
            resource_scope="none",
            observability_metadata_keys=frozenset(
                {"skill_name", "skill_version", "activation_source"}
            ),
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
            resource_scope="none",
            observability_metadata_keys=frozenset({"revealed_count"}),
        ),
        _control_placeholder("tool_search"),
        exposure=ToolExposure.CONTROL,
    )
    registry.register(
        ToolDescriptor(
            name="sql_schema",
            description=(
                "Describe only the authorized PostgreSQL tables and columns for "
                "read-only analysis."
            ),
            group="sql",
            version="1.0.0",
            input_schema=SqlSchemaInput.model_json_schema(),
            output_schema=TOOL_RESULT_V1_SCHEMA,
            timeout=(
                sql_settings.connect_timeout_seconds
                + (sql_settings.pool_timeout_seconds * 2)
                + (sql_settings.schema_timeout_seconds * 2)
                + 1.0
            ),
            max_concurrency=sql_settings.pool_max_size,
            idempotent=True,
            required_roles=frozenset({"admin"}),
            required_secrets=frozenset({"SQL_ASSISTANT_DSN"}),
            requires_approval=False,
            network_policy="private-data",
            result_size_limit=sql_settings.max_result_bytes,
            resource_scope="private-data-read",
            observability_metadata_keys=SQL_SCHEMA_METADATA_KEYS,
        ),
        partial(make_sql_schema, runtime=sql_runtime),
        exposure=ToolExposure.DEFERRED,
    )
    registry.register(
        ToolDescriptor(
            name="sql_query",
            description=(
                "Execute one policy-checked, bounded, read-only PostgreSQL query "
                "against authorized business data."
            ),
            group="sql",
            version="1.0.0",
            input_schema=SqlQueryInput.model_json_schema(),
            output_schema=TOOL_RESULT_V1_SCHEMA,
            timeout=(
                sql_settings.connect_timeout_seconds
                + (sql_settings.pool_timeout_seconds * 3)
                + (sql_settings.schema_timeout_seconds * 2)
                + sql_settings.statement_timeout_seconds
                + 1.0
            ),
            max_concurrency=sql_settings.pool_max_size,
            idempotent=True,
            required_roles=frozenset({"admin"}),
            required_secrets=frozenset({"SQL_ASSISTANT_DSN"}),
            requires_approval=False,
            network_policy="private-data",
            result_size_limit=sql_settings.max_result_bytes,
            resource_scope="private-data-read",
            observability_metadata_keys=SQL_QUERY_METADATA_KEYS,
        ),
        partial(make_sql_query, runtime=sql_runtime),
        exposure=ToolExposure.DEFERRED,
    )
    registry.register(
        ToolDescriptor(
            name="web_search",
            description=(
                "Search the public web for bounded evidence with stable citation "
                "identities and optional official-domain filtering."
            ),
            group="web-research",
            version="1.1.0",
            input_schema=web_search_schema,
            output_schema=TOOL_RESULT_V1_SCHEMA,
            timeout=web_settings.request_timeout_seconds + 1.0,
            max_concurrency=web_settings.max_concurrency,
            idempotent=True,
            required_roles=frozenset(),
            required_secrets=frozenset({"WEB_RESEARCH_RUNTIME"}),
            requires_approval=False,
            network_policy="restricted",
            result_size_limit=web_settings.max_total_evidence_bytes + 65_536,
            resource_scope="public-web",
            observability_metadata_keys=WEB_RESEARCH_METADATA_KEYS,
        ),
        partial(
            make_web_search,
            runtime=web_runtime,
            default_results=web_settings.default_search_results,
            max_total_evidence_bytes=web_settings.max_total_evidence_bytes,
        ),
        exposure=ToolExposure.DEFERRED,
    )
    registry.register(
        ToolDescriptor(
            name="web_fetch",
            description=(
                "Fetch and extract one public page previously authorized by "
                "web_search in this Run."
            ),
            group="web-research",
            version="1.1.0",
            input_schema=WebFetchInput.model_json_schema(),
            output_schema=TOOL_RESULT_V1_SCHEMA,
            timeout=web_settings.request_timeout_seconds + 1.0,
            max_concurrency=web_settings.max_concurrency,
            idempotent=True,
            required_roles=frozenset(),
            required_secrets=frozenset({"WEB_RESEARCH_RUNTIME"}),
            requires_approval=False,
            network_policy="restricted",
            result_size_limit=web_settings.max_total_evidence_bytes + 65_536,
            resource_scope="public-web",
            observability_metadata_keys=WEB_RESEARCH_METADATA_KEYS,
        ),
        partial(
            make_web_fetch,
            runtime=web_runtime,
            max_total_evidence_bytes=web_settings.max_total_evidence_bytes,
        ),
        exposure=ToolExposure.DEFERRED,
    )
    registry.register(
        ToolDescriptor(
            name="sandbox_execute",
            description=(
                "Execute bounded Python or shell source in a Run-owned, "
                "network-disabled isolated Sandbox."
            ),
            group="sandbox-execution",
            version="1.0.0",
            input_schema=sandbox_schema,
            output_schema=TOOL_RESULT_V1_SCHEMA,
            timeout=(
                sandbox_config.timeout_seconds
                + sandbox_config.cleanup_timeout_seconds
                + 1.0
            ),
            max_concurrency=sandbox_config.max_concurrency,
            idempotent=False,
            required_roles=frozenset({"admin"}),
            required_secrets=frozenset({"SANDBOX_RUNTIME"}),
            requires_approval=True,
            network_policy="none",
            result_size_limit=sandbox_config.max_output_bytes + 65_536,
            resource_scope="code-execution",
            observability_metadata_keys=SANDBOX_METADATA_KEYS,
        ),
        _control_placeholder("sandbox_execute"),
        exposure=ToolExposure.DEFERRED,
    )
    if freeze:
        registry.freeze()
    return registry


def configured_secret_names(
    registry: ToolRegistry,
    *,
    sql_assistant_settings: SqlAssistantSettings | None = None,
    web_research_settings: WebResearchSettings | None = None,
    sandbox_settings: SandboxSettings | None = None,
    additional_secret_names: Iterable[str] = (),
) -> frozenset[str]:
    required: set[str] = set()
    for name in registry.names:
        descriptor = registry.descriptor(name)
        if descriptor is not None:
            required.update(descriptor.required_secrets)
    required.update(
        str(name).strip() for name in additional_secret_names if str(name).strip()
    )
    configured: set[str] = set()
    for name in required:
        if name == "SQL_ASSISTANT_DSN":
            sql_settings = sql_assistant_settings or SqlAssistantSettings()
            if sql_settings.enabled and sql_settings.dsn.get_secret_value().strip():
                configured.add(name)
            continue
        if name == "WEB_RESEARCH_RUNTIME":
            web_settings = web_research_settings or WebResearchSettings()
            if web_settings.enabled:
                configured.add(name)
            continue
        if name == "SANDBOX_RUNTIME":
            sandbox_config = sandbox_settings or SandboxSettings()
            if sandbox_config.enabled and sandbox_config.docker_image:
                configured.add(name)
            continue
        if os.getenv(name, "").strip():
            configured.add(name)
    return frozenset(configured)


tool_registry = build_default_tool_registry()


__all__ = [
    "build_default_tool_registry",
    "configured_secret_names",
    "tool_registry",
]
