from __future__ import annotations

from collections.abc import Callable, Iterable
from decimal import Decimal

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.capabilities.control_contracts import (
    ManagedHttpToolRecord,
    ManagedSkillRecord,
    SqlAssistantConfigRecord,
)
from backend.core.errors import AppError, ErrorCode
from backend.core.settings import SqlAssistantSettings
from backend.db.models import (
    CapabilityHttpToolProfile,
    CapabilitySkillProfile,
    CapabilityState,
    SqlAssistantProfile,
    User,
    utcnow,
)
from backend.infra.database import SessionLocal
from backend.skills import SkillDefinition


SessionFactory = Callable[[], Session]


class CapabilityControlRepository:
    """Persist desired Skill/Tool configuration behind one transaction seam."""

    def __init__(self, session_factory: SessionFactory = SessionLocal) -> None:
        self._session_factory = session_factory

    @staticmethod
    def _user_id(db: Session, username: str | None) -> int | None:
        if not username:
            return None
        row = db.query(User.id).filter(User.username == username).first()
        if row is None:
            raise AppError(
                ErrorCode.AUTHENTICATION_REQUIRED,
                "用户不存在或已失效",
                status_code=401,
            )
        return int(row[0])

    @staticmethod
    def _skill_record(row: CapabilitySkillProfile) -> ManagedSkillRecord:
        return ManagedSkillRecord(
            name=row.name,
            version=row.version,
            description=row.description,
            instructions=row.instructions,
            allowed_tools=tuple(row.allowed_tools_json or ()),
            required_roles=tuple(row.required_roles_json or ()),
            required_secrets=tuple(row.required_secrets_json or ()),
            enabled=row.enabled,
            source=row.source,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

    @staticmethod
    def _tool_record(row: CapabilityHttpToolProfile) -> ManagedHttpToolRecord:
        return ManagedHttpToolRecord(
            name=row.name,
            version=row.version,
            description=row.description,
            group=row.group,
            endpoint=row.endpoint,
            method=row.method,
            input_schema=dict(row.input_schema_json or {}),
            static_headers=dict(row.static_headers_json or {}),
            secret_headers=dict(row.secret_headers_json or {}),
            required_roles=tuple(row.required_roles_json or ()),
            requires_approval=row.requires_approval,
            idempotent=row.idempotent,
            timeout_seconds=float(row.timeout_seconds),
            max_response_bytes=row.max_response_bytes,
            enabled=row.enabled,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

    @staticmethod
    def _sql_record(row: SqlAssistantProfile) -> SqlAssistantConfigRecord:
        return SqlAssistantConfigRecord(
            enabled=row.enabled,
            dsn_secret_name=row.dsn_secret_name,
            dsn_configured=False,
            expected_role=row.expected_role,
            allowed_schemas=tuple(row.allowed_schemas_json or ()),
            allowed_tables=tuple(row.allowed_tables_json or ()),
            sensitive_columns=tuple(row.sensitive_columns_json or ()),
            statement_timeout_seconds=float(row.statement_timeout_seconds),
            max_rows=row.max_rows,
            max_result_bytes=row.max_result_bytes,
            max_estimated_cost=float(row.max_estimated_cost),
            max_estimated_rows=row.max_estimated_rows,
            max_estimated_bytes=row.max_estimated_bytes,
            catalog_cache_ttl_seconds=float(row.catalog_cache_ttl_seconds),
            updated_at=row.updated_at,
        )

    @staticmethod
    def _state(db: Session, *, lock: bool = False) -> CapabilityState:
        query = db.query(CapabilityState).filter(CapabilityState.id == "default")
        if lock:
            query = query.with_for_update()
        state = query.first()
        if state is None:
            raise RuntimeError("capability state has not been initialized")
        return state

    def ensure_defaults(
        self,
        *,
        default_skills: Iterable[SkillDefinition],
        sql_settings: SqlAssistantSettings,
        web_research_enabled: bool,
    ) -> None:
        db = self._session_factory()
        try:
            with db.begin():
                now = utcnow()
                state = (
                    db.query(CapabilityState)
                    .filter(CapabilityState.id == "default")
                    .first()
                )
                if state is None:
                    db.add(
                        CapabilityState(
                            id="default",
                            web_research_enabled=bool(web_research_enabled),
                            created_at=now,
                            updated_at=now,
                        )
                    )
                sql = (
                    db.query(SqlAssistantProfile)
                    .filter(SqlAssistantProfile.id == "default")
                    .first()
                )
                if sql is None:
                    db.add(
                        SqlAssistantProfile(
                            id="default",
                            enabled=sql_settings.enabled,
                            dsn_secret_name="SQL_ASSISTANT_DSN",
                            expected_role=sql_settings.expected_role,
                            allowed_schemas_json=list(sql_settings.allowed_schemas),
                            allowed_tables_json=list(sql_settings.allowed_tables),
                            sensitive_columns_json=list(sql_settings.sensitive_columns),
                            statement_timeout_seconds=Decimal(
                                str(sql_settings.statement_timeout_seconds)
                            ),
                            max_rows=sql_settings.max_rows,
                            max_result_bytes=sql_settings.max_result_bytes,
                            max_estimated_cost=Decimal(
                                str(sql_settings.max_estimated_cost)
                            ),
                            max_estimated_rows=sql_settings.max_estimated_rows,
                            max_estimated_bytes=sql_settings.max_estimated_bytes,
                            catalog_cache_ttl_seconds=Decimal(
                                str(sql_settings.catalog_cache_ttl_seconds)
                            ),
                            created_at=now,
                            updated_at=now,
                        )
                    )
                existing = {
                    str(name)
                    for (name,) in db.query(CapabilitySkillProfile.name).all()
                }
                for definition in default_skills:
                    manifest = definition.manifest
                    if manifest.name in existing:
                        continue
                    db.add(
                        CapabilitySkillProfile(
                            name=manifest.name,
                            version=manifest.version,
                            description=manifest.description,
                            instructions=definition.instructions,
                            allowed_tools_json=list(manifest.allowed_tools),
                            required_roles_json=list(manifest.required_roles),
                            required_secrets_json=list(manifest.required_secrets),
                            enabled=True,
                            source="builtin",
                            created_at=now,
                            updated_at=now,
                        )
                    )
        finally:
            db.close()

    def web_research_enabled(self) -> bool:
        db = self._session_factory()
        try:
            return bool(self._state(db).web_research_enabled)
        finally:
            db.close()

    def list_skills(self) -> tuple[ManagedSkillRecord, ...]:
        db = self._session_factory()
        try:
            rows = (
                db.query(CapabilitySkillProfile)
                .order_by(CapabilitySkillProfile.name.asc())
                .all()
            )
            return tuple(self._skill_record(row) for row in rows)
        finally:
            db.close()

    def list_http_tools(self) -> tuple[ManagedHttpToolRecord, ...]:
        db = self._session_factory()
        try:
            rows = (
                db.query(CapabilityHttpToolProfile)
                .order_by(CapabilityHttpToolProfile.name.asc())
                .all()
            )
            return tuple(self._tool_record(row) for row in rows)
        finally:
            db.close()

    def sql_config(self) -> SqlAssistantConfigRecord:
        db = self._session_factory()
        try:
            row = (
                db.query(SqlAssistantProfile)
                .filter(SqlAssistantProfile.id == "default")
                .first()
            )
            if row is None:
                raise RuntimeError("SQL Assistant profile has not been initialized")
            return self._sql_record(row)
        finally:
            db.close()

    def create_skill(
        self,
        *,
        record: ManagedSkillRecord,
        username: str,
    ) -> ManagedSkillRecord:
        db = self._session_factory()
        try:
            with db.begin():
                now = utcnow()
                row = CapabilitySkillProfile(
                    name=record.name,
                    version=record.version,
                    description=record.description,
                    instructions=record.instructions,
                    allowed_tools_json=list(record.allowed_tools),
                    required_roles_json=list(record.required_roles),
                    required_secrets_json=list(record.required_secrets),
                    enabled=record.enabled,
                    source="custom",
                    created_by_user_id=self._user_id(db, username),
                    created_at=now,
                    updated_at=now,
                )
                db.add(row)
                db.flush()
                return self._skill_record(row)
        except IntegrityError as exc:
            db.rollback()
            raise AppError(
                ErrorCode.CONFLICT,
                "Skill 名称已存在",
                status_code=409,
                category="capability",
                stage="catalog",
            ) from exc
        finally:
            db.close()

    def update_skill(
        self,
        *,
        name: str,
        version: str,
        description: str,
        instructions: str,
        allowed_tools: tuple[str, ...],
        required_roles: tuple[str, ...],
        required_secrets: tuple[str, ...],
        enabled: bool,
        username: str,
    ) -> ManagedSkillRecord:
        db = self._session_factory()
        try:
            with db.begin():
                self._user_id(db, username)
                row = (
                    db.query(CapabilitySkillProfile)
                    .filter(CapabilitySkillProfile.name == name)
                    .with_for_update()
                    .first()
                )
                if row is None:
                    raise AppError(
                        ErrorCode.NOT_FOUND,
                        "Skill 不存在",
                        status_code=404,
                        category="capability",
                        stage="catalog",
                    )
                row.version = version
                row.description = description
                row.instructions = instructions
                row.allowed_tools_json = list(allowed_tools)
                row.required_roles_json = list(required_roles)
                row.required_secrets_json = list(required_secrets)
                row.enabled = enabled
                row.updated_at = utcnow()
                db.flush()
                return self._skill_record(row)
        finally:
            db.close()

    def delete_skill(self, *, name: str, username: str) -> None:
        db = self._session_factory()
        try:
            with db.begin():
                self._user_id(db, username)
                row = (
                    db.query(CapabilitySkillProfile)
                    .filter(CapabilitySkillProfile.name == name)
                    .with_for_update()
                    .first()
                )
                if row is None:
                    raise AppError(
                        ErrorCode.NOT_FOUND,
                        "Skill 不存在",
                        status_code=404,
                        category="capability",
                        stage="catalog",
                    )
                if row.source != "custom":
                    raise AppError(
                        ErrorCode.CONFLICT,
                        "内建 Skill 只能停用，不能删除",
                        status_code=409,
                        category="capability",
                        stage="catalog",
                    )
                db.delete(row)
        finally:
            db.close()

    def create_http_tool(
        self,
        *,
        record: ManagedHttpToolRecord,
        username: str,
    ) -> ManagedHttpToolRecord:
        db = self._session_factory()
        try:
            with db.begin():
                now = utcnow()
                row = CapabilityHttpToolProfile(
                    name=record.name,
                    version=record.version,
                    description=record.description,
                    group=record.group,
                    endpoint=record.endpoint,
                    method=record.method,
                    input_schema_json=dict(record.input_schema),
                    static_headers_json=dict(record.static_headers),
                    secret_headers_json=dict(record.secret_headers),
                    required_roles_json=list(record.required_roles),
                    requires_approval=record.requires_approval,
                    idempotent=record.idempotent,
                    timeout_seconds=Decimal(str(record.timeout_seconds)),
                    max_response_bytes=record.max_response_bytes,
                    enabled=record.enabled,
                    created_by_user_id=self._user_id(db, username),
                    created_at=now,
                    updated_at=now,
                )
                db.add(row)
                db.flush()
                return self._tool_record(row)
        except IntegrityError as exc:
            db.rollback()
            raise AppError(
                ErrorCode.CONFLICT,
                "Tool 名称已存在",
                status_code=409,
                category="capability",
                stage="catalog",
            ) from exc
        finally:
            db.close()

    def update_http_tool(
        self,
        *,
        record: ManagedHttpToolRecord,
        username: str,
    ) -> ManagedHttpToolRecord:
        db = self._session_factory()
        try:
            with db.begin():
                self._user_id(db, username)
                row = (
                    db.query(CapabilityHttpToolProfile)
                    .filter(CapabilityHttpToolProfile.name == record.name)
                    .with_for_update()
                    .first()
                )
                if row is None:
                    raise AppError(
                        ErrorCode.NOT_FOUND,
                        "Tool 不存在",
                        status_code=404,
                        category="capability",
                        stage="catalog",
                    )
                row.version = record.version
                row.description = record.description
                row.group = record.group
                row.endpoint = record.endpoint
                row.method = record.method
                row.input_schema_json = dict(record.input_schema)
                row.static_headers_json = dict(record.static_headers)
                row.secret_headers_json = dict(record.secret_headers)
                row.required_roles_json = list(record.required_roles)
                row.requires_approval = record.requires_approval
                row.idempotent = record.idempotent
                row.timeout_seconds = Decimal(str(record.timeout_seconds))
                row.max_response_bytes = record.max_response_bytes
                row.enabled = record.enabled
                row.updated_at = utcnow()
                db.flush()
                return self._tool_record(row)
        finally:
            db.close()

    def delete_http_tool(self, *, name: str, username: str) -> None:
        db = self._session_factory()
        try:
            with db.begin():
                self._user_id(db, username)
                row = (
                    db.query(CapabilityHttpToolProfile)
                    .filter(CapabilityHttpToolProfile.name == name)
                    .with_for_update()
                    .first()
                )
                if row is None:
                    raise AppError(
                        ErrorCode.NOT_FOUND,
                        "Tool 不存在",
                        status_code=404,
                        category="capability",
                        stage="catalog",
                    )
                db.delete(row)
        finally:
            db.close()

    def update_sql_config(
        self,
        *,
        record: SqlAssistantConfigRecord,
        username: str,
    ) -> SqlAssistantConfigRecord:
        db = self._session_factory()
        try:
            with db.begin():
                user_id = self._user_id(db, username)
                row = (
                    db.query(SqlAssistantProfile)
                    .filter(SqlAssistantProfile.id == "default")
                    .with_for_update()
                    .first()
                )
                if row is None:
                    raise RuntimeError("SQL Assistant profile has not been initialized")
                row.enabled = record.enabled
                row.dsn_secret_name = record.dsn_secret_name
                row.expected_role = record.expected_role
                row.allowed_schemas_json = list(record.allowed_schemas)
                row.allowed_tables_json = list(record.allowed_tables)
                row.sensitive_columns_json = list(record.sensitive_columns)
                row.statement_timeout_seconds = Decimal(
                    str(record.statement_timeout_seconds)
                )
                row.max_rows = record.max_rows
                row.max_result_bytes = record.max_result_bytes
                row.max_estimated_cost = Decimal(str(record.max_estimated_cost))
                row.max_estimated_rows = record.max_estimated_rows
                row.max_estimated_bytes = record.max_estimated_bytes
                row.catalog_cache_ttl_seconds = Decimal(
                    str(record.catalog_cache_ttl_seconds)
                )
                row.updated_by_user_id = user_id
                row.updated_at = utcnow()
                db.flush()
                return self._sql_record(row)
        finally:
            db.close()

    def update_web_research(
        self,
        *,
        enabled: bool,
        username: str,
    ) -> None:
        db = self._session_factory()
        try:
            with db.begin():
                self._user_id(db, username)
                state = self._state(db, lock=True)
                state.web_research_enabled = bool(enabled)
                state.updated_at = utcnow()
                db.flush()
        finally:
            db.close()


__all__ = ["CapabilityControlRepository"]
