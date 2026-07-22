from __future__ import annotations

import os
import re
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any

from pydantic import SecretStr

from backend.agent.factory import AgentRuntimeFactory, runtime_factory
from backend.capabilities.catalog import CapabilityCatalog
from backend.capabilities.control_contracts import (
    ManagedHttpToolRecord,
    ManagedSkillRecord,
    SqlAssistantConfigRecord,
)
from backend.capabilities.control_repository import CapabilityControlRepository
from backend.core.errors import AppError, ErrorCode
from backend.core.settings import AppSettings, SqlAssistantSettings, get_settings
from backend.skills import SkillDefinition, SkillManifest, SkillRegistry
from backend.sql_assistant.runtime import (
    SqlAssistantRuntime,
    clear_sql_assistant_runtime,
    install_sql_assistant_runtime,
)
from backend.tools.catalog import build_default_tool_registry, configured_secret_names
from backend.tools.custom_http import (
    CustomHttpToolRuntime,
    register_custom_http_tools,
    validate_custom_http_endpoint,
)
from backend.tools.registry import ToolRegistry
from backend.web_research.runtime import (
    WebResearchRuntime,
    build_web_research_runtime,
    clear_web_research_runtime,
    install_web_research_runtime,
)


_SEMVER = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:[-+].*)?$")


@dataclass(slots=True)
class CapabilityRuntime:
    settings: AppSettings
    tools: ToolRegistry
    skills: SkillRegistry
    factory: AgentRuntimeFactory
    catalog: CapabilityCatalog
    custom_http_runtime: CustomHttpToolRuntime
    sql_runtime: SqlAssistantRuntime | None
    web_runtime: WebResearchRuntime | None
    _started: bool = False
    _closed: bool = False

    def start(self) -> None:
        if self._started:
            return
        if self._closed:
            raise RuntimeError("capability runtime is closed")
        sql_started = False
        web_started = False
        try:
            if self.sql_runtime is not None:
                self.sql_runtime.start()
                sql_started = True
                install_sql_assistant_runtime(self.sql_runtime)
            if self.web_runtime is not None:
                self.web_runtime.start()
                web_started = True
                install_web_research_runtime(self.web_runtime)
            self._started = True
        except BaseException:
            if web_started and self.web_runtime is not None:
                clear_web_research_runtime(self.web_runtime)
                self.web_runtime.close()
            if sql_started and self.sql_runtime is not None:
                clear_sql_assistant_runtime(self.sql_runtime)
                self.sql_runtime.close()
            self.custom_http_runtime.close()
            self._closed = True
            raise

    def close(self) -> None:
        if self._closed:
            return
        if self.web_runtime is not None:
            clear_web_research_runtime(self.web_runtime)
            self.web_runtime.close()
        if self.sql_runtime is not None:
            clear_sql_assistant_runtime(self.sql_runtime)
            self.sql_runtime.close()
        self.custom_http_runtime.close()
        self._started = False
        self._closed = True


class CapabilityControlService:
    """Persist admin configuration and replace the current process runtime."""

    def __init__(
        self,
        repository: CapabilityControlRepository | None = None,
        *,
        settings: AppSettings | None = None,
    ) -> None:
        self.repository = repository or CapabilityControlRepository()
        self.settings = settings or get_settings()
        self._active_settings: AppSettings | None = None
        self._active_runtime: CapabilityRuntime | None = None

    @property
    def active_settings(self) -> AppSettings | None:
        return self._active_settings

    def ensure_defaults(self) -> None:
        self.repository.ensure_defaults(
            default_skills=runtime_factory.skills.definitions(),
            sql_settings=self.settings.sql_assistant,
            web_research_enabled=self.settings.web_research.enabled,
        )

    def apply_runtime(
        self,
        runtime: CapabilityRuntime | None = None,
        *,
        executor: Any | None = None,
    ) -> CapabilityRuntime:
        from backend.capabilities.runtime import install_runtime_capabilities

        if executor is None:
            from backend.runs.agent_executor import run_agent_executor

            executor = run_agent_executor

        current = runtime or self.build_runtime()
        current.start()
        previous = self._active_runtime
        executor.runtime_builder = current.factory
        install_runtime_capabilities(
            catalog=current.catalog,
            tools=current.tools,
        )
        self._active_runtime = current
        self._active_settings = current.settings
        if previous is not None and previous is not current:
            _close_quietly(previous)
        return current

    def close_runtime(self) -> None:
        runtime = self._active_runtime
        self._active_runtime = None
        self._active_settings = None
        if runtime is not None:
            runtime.close()

    def control_plane(self) -> dict[str, Any]:
        sql = self._sql_with_availability(self.repository.sql_config())
        custom_tools = self.repository.list_http_tools()
        builtin_tools = tuple(
            descriptor
            for name in runtime_factory.tools.names
            if (descriptor := runtime_factory.tools.descriptor(name)) is not None
        )
        return {
            "schema_version": 1,
            "web_research": {
                "enabled": self.repository.web_research_enabled(),
                "provider": "tavily-keyless",
                "api_key_required": False,
            },
            "sql_assistant": sql,
            "skills": self.repository.list_skills(),
            "custom_tools": custom_tools,
            "builtin_tools": [
                {
                    "name": item.name,
                    "description": item.description,
                    "group": item.group,
                    "version": item.version,
                    "required_roles": sorted(item.required_roles),
                    "requires_approval": item.requires_approval,
                    "network_policy": item.network_policy,
                    "resource_scope": item.resource_scope,
                }
                for item in builtin_tools
            ],
        }

    def create_skill(
        self,
        *,
        username: str,
        name: str,
        description: str,
        instructions: str,
        allowed_tools: tuple[str, ...],
        required_roles: tuple[str, ...] = (),
        required_secrets: tuple[str, ...] = (),
        enabled: bool = True,
    ) -> ManagedSkillRecord:
        record = self._skill_record(
            name=name,
            version="1.0.0",
            description=description,
            instructions=instructions,
            allowed_tools=allowed_tools,
            required_roles=required_roles,
            required_secrets=required_secrets,
            enabled=enabled,
            source="custom",
            created_at=_placeholder_time(),
            updated_at=_placeholder_time(),
        )
        self._validate_skill_tools(record)
        return self.repository.create_skill(record=record, username=username)

    def update_skill(
        self,
        *,
        username: str,
        name: str,
        description: str,
        instructions: str,
        allowed_tools: tuple[str, ...],
        required_roles: tuple[str, ...] = (),
        required_secrets: tuple[str, ...] = (),
        enabled: bool = True,
    ) -> ManagedSkillRecord:
        current = self._skill(name)
        record = self._skill_record(
            name=name,
            version=_next_patch(current.version),
            description=description,
            instructions=instructions,
            allowed_tools=allowed_tools,
            required_roles=required_roles,
            required_secrets=required_secrets,
            enabled=enabled,
            source=current.source,
            created_at=current.created_at,
            updated_at=current.updated_at,
        )
        self._validate_skill_tools(record)
        return self.repository.update_skill(
            name=name,
            version=record.version,
            description=record.description,
            instructions=record.instructions,
            allowed_tools=record.allowed_tools,
            required_roles=record.required_roles,
            required_secrets=record.required_secrets,
            enabled=record.enabled,
            username=username,
        )

    def delete_skill(self, *, username: str, name: str) -> None:
        self.repository.delete_skill(name=name, username=username)

    def create_http_tool(
        self,
        *,
        username: str,
        **payload: Any,
    ) -> ManagedHttpToolRecord:
        name = str(payload.get("name") or "")
        if name in runtime_factory.tools.names:
            raise AppError(
                ErrorCode.CONFLICT,
                "自定义 Tool 名称不能覆盖内建 Tool",
                status_code=409,
                category="capability",
                stage="catalog",
            )
        record = self._http_record(
            payload,
            version="1.0.0",
        )
        return self.repository.create_http_tool(record=record, username=username)

    def update_http_tool(
        self,
        *,
        username: str,
        name: str,
        **payload: Any,
    ) -> ManagedHttpToolRecord:
        current = self._http_tool(name)
        record = self._http_record(
            {**payload, "name": name},
            version=_next_patch(current.version),
            created_at=current.created_at,
        )
        if current.enabled and not record.enabled:
            references = self._skills_referencing(name, only_enabled=True)
            if references:
                raise AppError(
                    ErrorCode.CONFLICT,
                    "仍有已启用 Skill 引用该 Tool，不能停用",
                    status_code=409,
                    category="capability",
                    stage="catalog",
                    safe_details={"skills": references},
                )
        return self.repository.update_http_tool(record=record, username=username)

    def delete_http_tool(self, *, username: str, name: str) -> None:
        references = self._skills_referencing(name, only_enabled=False)
        if references:
            raise AppError(
                ErrorCode.CONFLICT,
                "仍有 Skill 引用该 Tool，不能删除",
                status_code=409,
                category="capability",
                stage="catalog",
                safe_details={"skills": references},
            )
        self.repository.delete_http_tool(name=name, username=username)

    def update_sql_assistant(
        self,
        *,
        username: str,
        **payload: Any,
    ) -> SqlAssistantConfigRecord:
        current = self.repository.sql_config()
        record = SqlAssistantConfigRecord(
            **payload,
            dsn_configured=self._secret_value(str(payload["dsn_secret_name"])) != "",
            updated_at=current.updated_at,
        )
        settings = self._sql_settings(record)
        if record.enabled:
            self._validate_sql_settings(settings)
        record = record.model_copy(
            update={
                "expected_role": settings.expected_role,
                "allowed_schemas": settings.allowed_schemas,
                "allowed_tables": settings.allowed_tables,
                "sensitive_columns": settings.sensitive_columns,
            }
        )
        saved = self.repository.update_sql_config(record=record, username=username)
        return self._sql_with_availability(saved)

    def update_web_research(self, *, username: str, enabled: bool) -> None:
        self.repository.update_web_research(
            enabled=enabled,
            username=username,
        )

    def build_runtime(self) -> CapabilityRuntime:
        sql_record = self.repository.sql_config()
        sql_settings = self._sql_settings(sql_record)
        web_settings = self.settings.web_research.model_copy(
            update={"enabled": self.repository.web_research_enabled()}
        )
        runtime_settings = self.settings.model_copy(
            update={
                "sql_assistant": sql_settings,
                "web_research": web_settings,
            }
        )
        runtime_settings.validate_startup()

        custom_http_runtime: CustomHttpToolRuntime | None = None
        sql_runtime: SqlAssistantRuntime | None = None
        web_runtime: WebResearchRuntime | None = None
        try:
            sql_runtime = (
                SqlAssistantRuntime(settings=sql_settings)
                if sql_settings.enabled
                else None
            )
            web_runtime = (
                build_web_research_runtime(web_settings)
                if web_settings.enabled
                else None
            )
            custom_http_runtime = CustomHttpToolRuntime(
                dns_timeout_seconds=web_settings.dns_timeout_seconds,
                dns_max_concurrency=web_settings.dns_max_concurrency,
                max_dns_addresses=web_settings.max_dns_addresses,
            )
            registry = build_default_tool_registry(
                sql_assistant_settings=sql_settings,
                web_research_settings=web_settings,
                sandbox_settings=runtime_settings.sandbox,
                freeze=False,
            )
            custom_tools = self.repository.list_http_tools()
            register_custom_http_tools(registry, custom_tools, custom_http_runtime)
            registry.freeze()
            skills = self._build_skill_registry(registry)
            skill_secret_names = frozenset(
                secret
                for record in self.repository.list_skills()
                if record.enabled
                for secret in record.required_secrets
            )
            secret_provider = partial(
                configured_secret_names,
                sql_assistant_settings=sql_settings,
                web_research_settings=web_settings,
                sandbox_settings=runtime_settings.sandbox,
                additional_secret_names=skill_secret_names,
            )
            factory = AgentRuntimeFactory(
                settings=runtime_settings,
                models=runtime_factory.models,
                agent_builder=runtime_factory.agent_builder,
                tools=registry,
                skills=skills,
                secret_names_provider=secret_provider,
            )
            catalog = CapabilityCatalog(
                skills=skills,
                tools=registry,
                secret_names_provider=secret_provider,
            )
            if custom_http_runtime is None:
                raise RuntimeError("custom HTTP runtime was not constructed")
            return CapabilityRuntime(
                settings=runtime_settings,
                tools=registry,
                skills=skills,
                factory=factory,
                catalog=catalog,
                custom_http_runtime=custom_http_runtime,
                sql_runtime=sql_runtime,
                web_runtime=web_runtime,
            )
        except BaseException:
            _close_quietly(web_runtime)
            _close_quietly(sql_runtime)
            _close_quietly(custom_http_runtime)
            raise

    def _build_skill_registry(self, tools: ToolRegistry) -> SkillRegistry:
        definitions: list[SkillDefinition] = []
        for record in self.repository.list_skills():
            if not record.enabled:
                continue
            manifest = SkillManifest(
                schema_version=1,
                name=record.name,
                version=record.version,
                description=record.description,
                allowed_tools=record.allowed_tools,
                required_roles=record.required_roles,
                required_secrets=record.required_secrets,
                entrypoint="SKILL.md",
            )
            definitions.append(
                SkillDefinition(manifest=manifest, instructions=record.instructions)
            )
        return SkillRegistry.from_definitions(
            definitions,
            tools.names,
            root=Path("<capability-control-plane>"),
            max_content_bytes=self.settings.skills.max_content_bytes,
        )

    def _validate_skill_tools(self, record: ManagedSkillRecord) -> None:
        builtin = set(runtime_factory.tools.names)
        custom = {item.name: item for item in self.repository.list_http_tools()}
        unknown = set(record.allowed_tools).difference(builtin, custom)
        if unknown:
            raise AppError(
                ErrorCode.INVALID_REQUEST,
                "Skill 引用了不存在的 Tool",
                status_code=400,
                category="capability",
                stage="validation",
                safe_details={"tools": sorted(unknown)},
            )
        disabled = sorted(
            name
            for name in record.allowed_tools
            if name in custom and not custom[name].enabled
        )
        if record.enabled and disabled:
            raise AppError(
                ErrorCode.CONFLICT,
                "已启用 Skill 不能引用停用的自定义 Tool",
                status_code=409,
                category="capability",
                stage="validation",
                safe_details={"tools": disabled},
            )

    def _http_record(
        self,
        payload: dict[str, Any],
        *,
        version: str,
        created_at=None,
    ) -> ManagedHttpToolRecord:
        try:
            endpoint = validate_custom_http_endpoint(
                str(payload.get("endpoint") or "")
            )
            values = dict(payload)
            values.update(
                endpoint=endpoint,
                version=version,
                created_at=created_at or _placeholder_time(),
                updated_at=_placeholder_time(),
            )
            return ManagedHttpToolRecord(**values)
        except (TypeError, ValueError) as exc:
            raise AppError(
                ErrorCode.INVALID_REQUEST,
                "自定义 Tool 配置无效，请检查 Endpoint、JSON Schema、Header 与预算限制",
                status_code=400,
                category="capability",
                stage="validation",
            ) from exc

    @staticmethod
    def _skill_record(**values: Any) -> ManagedSkillRecord:
        try:
            return ManagedSkillRecord(
                **{
                    **values,
                    "allowed_tools": _unique(values["allowed_tools"]),
                    "required_roles": _unique(values["required_roles"]),
                    "required_secrets": _unique(values["required_secrets"]),
                }
            )
        except (TypeError, ValueError) as exc:
            raise AppError(
                ErrorCode.INVALID_REQUEST,
                "Skill 配置无效，请检查名称、Role、Secret 与 Tool 标识符",
                status_code=400,
                category="capability",
                stage="validation",
            ) from exc

    def _sql_settings(self, record: SqlAssistantConfigRecord) -> SqlAssistantSettings:
        dsn = self._secret_value(record.dsn_secret_name)
        base = self.settings.sql_assistant
        values = base.model_dump()
        values.update(
            {
                "enabled": record.enabled,
                "dsn": SecretStr(dsn),
                "expected_role": record.expected_role,
                "allowed_schemas_raw": ",".join(record.allowed_schemas),
                "allowed_tables_raw": ",".join(record.allowed_tables),
                "sensitive_columns_raw": ",".join(record.sensitive_columns),
                "statement_timeout_seconds": record.statement_timeout_seconds,
                "max_rows": record.max_rows,
                "max_result_bytes": record.max_result_bytes,
                "max_estimated_cost": record.max_estimated_cost,
                "max_estimated_rows": record.max_estimated_rows,
                "max_estimated_bytes": record.max_estimated_bytes,
                "catalog_cache_ttl_seconds": record.catalog_cache_ttl_seconds,
            }
        )
        try:
            return SqlAssistantSettings.model_validate(values)
        except (TypeError, ValueError) as exc:
            raise AppError(
                ErrorCode.INVALID_REQUEST,
                "SQL Assistant 配置无效，请检查角色、allowlist 与查询预算",
                status_code=400,
                category="capability",
                stage="sql_configuration",
            ) from exc

    def _validate_sql_settings(self, sql_settings: SqlAssistantSettings) -> None:
        candidate = self.settings.model_copy(update={"sql_assistant": sql_settings})
        try:
            candidate.validate_startup()
        except ValueError as exc:
            raise AppError(
                ErrorCode.INVALID_REQUEST,
                str(exc),
                status_code=400,
                category="capability",
                stage="sql_configuration",
            ) from exc

    def _secret_value(self, name: str) -> str:
        value = os.getenv(name, "").strip()
        if value:
            return value
        if name == "SQL_ASSISTANT_DSN":
            return self.settings.sql_assistant.dsn.get_secret_value().strip()
        return ""

    def _sql_with_availability(
        self,
        record: SqlAssistantConfigRecord,
    ) -> SqlAssistantConfigRecord:
        return record.model_copy(
            update={"dsn_configured": bool(self._secret_value(record.dsn_secret_name))}
        )

    def _skill(self, name: str) -> ManagedSkillRecord:
        for item in self.repository.list_skills():
            if item.name == name:
                return item
        raise AppError(
            ErrorCode.NOT_FOUND,
            "Skill 不存在",
            status_code=404,
            category="capability",
            stage="catalog",
        )

    def _http_tool(self, name: str) -> ManagedHttpToolRecord:
        for item in self.repository.list_http_tools():
            if item.name == name:
                return item
        raise AppError(
            ErrorCode.NOT_FOUND,
            "Tool 不存在",
            status_code=404,
            category="capability",
            stage="catalog",
        )

    def _skills_referencing(self, tool_name: str, *, only_enabled: bool) -> list[str]:
        return sorted(
            item.name
            for item in self.repository.list_skills()
            if tool_name in item.allowed_tools and (item.enabled or not only_enabled)
        )


def _next_patch(version: str) -> str:
    match = _SEMVER.fullmatch(version)
    if match is None:
        raise ValueError("version must be semantic version text")
    return f"{match.group(1)}.{match.group(2)}.{int(match.group(3)) + 1}"


def _unique(values: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(str(value).strip() for value in values if str(value).strip()))


def _placeholder_time():
    from datetime import UTC, datetime

    return datetime.now(UTC)


def _close_quietly(runtime: object | None) -> None:
    if runtime is None:
        return
    try:
        runtime.close()  # type: ignore[attr-defined]
    except BaseException:
        pass




capability_control_service = CapabilityControlService()


__all__ = [
    "CapabilityRuntime",
    "CapabilityControlService",
    "capability_control_service",
]
