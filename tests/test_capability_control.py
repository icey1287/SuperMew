from __future__ import annotations

import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.capabilities.control_repository import CapabilityControlRepository
from backend.capabilities.control_service import CapabilityControlService
from backend.core.errors import AppError, ErrorCode
from backend.core.settings import get_settings
from backend.db.models import Base, User
from backend.skills import SkillAccess


@pytest.fixture()
def capability_control():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    Base.metadata.create_all(engine)
    with factory.begin() as db:
        db.add(User(username="admin", password_hash="hash", role="admin"))
    service = CapabilityControlService(
        CapabilityControlRepository(factory),
        settings=get_settings(),
    )
    service.ensure_defaults()
    try:
        yield service
    finally:
        service.close_runtime()
        engine.dispose()


def _http_payload(**overrides):
    payload = {
        "name": "release_lookup",
        "description": "Look up public release metadata.",
        "group": "custom-http",
        "endpoint": "https://api.cloudflare.com/releases",
        "method": "POST",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string", "minLength": 1}},
            "required": ["query"],
            "additionalProperties": False,
        },
        "static_headers": {},
        "secret_headers": {},
        "required_roles": (),
        "requires_approval": False,
        "idempotent": True,
        "timeout_seconds": 20,
        "max_response_bytes": 65_536,
        "enabled": True,
    }
    payload.update(overrides)
    return payload


def test_defaults_seed_four_editable_skills_and_keyless_web(capability_control):
    state = capability_control.control_plane()

    assert {item.name for item in state["skills"]} == {
        "knowledge-base",
        "sandbox",
        "sql-assistant",
        "web-research",
    }
    assert all(item.source == "builtin" for item in state["skills"])
    assert state["web_research"]["provider"] == "tavily-keyless"
    assert state["web_research"]["api_key_required"] is False
    assert "BRAVE" not in json.dumps(state, default=str).upper()


def test_custom_http_tool_and_skill_are_loaded_into_runtime(capability_control):
    tool = capability_control.create_http_tool(
        username="admin",
        **_http_payload(),
    )
    skill = capability_control.create_skill(
        username="admin",
        name="release-research",
        description="Research public releases.",
        instructions="# Release Research\nUse release_lookup and cite returned facts.",
        allowed_tools=(tool.name,),
    )

    runtime = capability_control.build_runtime()
    try:
        assert tool.name in runtime.tools.names
        assert skill.name in runtime.skills.names
        activated = runtime.skills.activate(
            skill.name,
            access=SkillAccess(roles=frozenset({"user"})),
            source="test",
        )
        assert activated.allowed_tools == frozenset({tool.name})
    finally:
        runtime.close()


def test_saved_configuration_is_applied_to_current_runtime(capability_control):
    capability_control.update_web_research(username="admin", enabled=True)

    executor = type("Executor", (), {"runtime_builder": None})()
    runtime = capability_control.apply_runtime(executor=executor)
    state = capability_control.control_plane()

    assert state["web_research"]["enabled"] is True
    assert "revision" not in state
    assert executor.runtime_builder is runtime.factory
    assert capability_control.active_settings.web_research.enabled is True


def test_referenced_custom_tool_cannot_be_disabled_or_deleted(capability_control):
    tool = capability_control.create_http_tool(username="admin", **_http_payload())
    capability_control.create_skill(
        username="admin",
        name="release-research",
        description="Research public releases.",
        instructions="# Release Research\nUse the configured Tool.",
        allowed_tools=(tool.name,),
    )

    with pytest.raises(AppError) as disabled:
        capability_control.update_http_tool(
            username="admin",
            name=tool.name,
            **{key: value for key, value in _http_payload(enabled=False).items() if key != "name"},
        )
    assert disabled.value.code == ErrorCode.CONFLICT

    with pytest.raises(AppError) as deleted:
        capability_control.delete_http_tool(username="admin", name=tool.name)
    assert deleted.value.code == ErrorCode.CONFLICT


def test_sql_configuration_uses_secret_reference_without_returning_dsn(
    capability_control,
    monkeypatch,
):
    monkeypatch.setenv(
        "ANALYTICS_READER_DSN",
        "postgresql://analytics_reader:private-password@db/analytics",
    )

    record = capability_control.update_sql_assistant(
        username="admin",
        enabled=True,
        dsn_secret_name="ANALYTICS_READER_DSN",
        expected_role="analytics_reader",
        allowed_schemas=("analytics",),
        allowed_tables=("analytics.orders",),
        sensitive_columns=(),
        statement_timeout_seconds=10,
        max_rows=200,
        max_result_bytes=262_144,
        max_estimated_cost=100_000,
        max_estimated_rows=100_000,
        max_estimated_bytes=8_388_608,
        catalog_cache_ttl_seconds=300,
    )

    assert record.enabled is True
    assert record.dsn_configured is True
    serialized = json.dumps(
        capability_control.control_plane(), default=str, ensure_ascii=False
    )
    assert "private-password" not in serialized
    assert "ANALYTICS_READER_DSN" in serialized


@pytest.mark.parametrize(
    "overrides",
    [
        {"endpoint": "http://api.cloudflare.com/releases"},
        {"input_schema": {"type": "string"}},
        {"input_schema": {"type": "object", "$ref": "https://schemas.test/input"}},
        {"static_headers": {"Content-Type": "text/plain"}},
        {"static_headers": {"X-Auth-Token": "inline-secret"}},
    ],
)
def test_invalid_custom_http_configuration_is_a_safe_client_error(
    capability_control,
    overrides,
):
    with pytest.raises(AppError) as captured:
        capability_control.create_http_tool(
            username="admin",
            **_http_payload(**overrides),
        )

    assert captured.value.code == ErrorCode.INVALID_REQUEST
    assert "api.cloudflare.com" not in captured.value.message


def test_custom_skill_can_gate_activation_on_an_environment_secret(
    capability_control,
    monkeypatch,
):
    monkeypatch.setenv("RELEASE_POLICY_TOKEN", "configured")
    capability_control.create_skill(
        username="admin",
        name="release-policy",
        description="Use a separately enabled release policy.",
        instructions="# Release Policy\nFollow the configured policy.",
        allowed_tools=(),
        required_secrets=("RELEASE_POLICY_TOKEN",),
    )

    runtime = capability_control.build_runtime()
    try:
        snapshot = runtime.catalog.snapshot(role="user")
        skill = next(item for item in snapshot.skills if item.name == "release-policy")
        assert skill.available is True
    finally:
        runtime.close()


def test_sql_configuration_is_canonical_and_rejects_invalid_identifiers(
    capability_control,
):
    saved = capability_control.update_sql_assistant(
        username="admin",
        enabled=False,
        dsn_secret_name="SQL_ASSISTANT_DSN",
        expected_role="ANALYTICS_READER",
        allowed_schemas=("ANALYTICS",),
        allowed_tables=("ANALYTICS.ORDERS",),
        sensitive_columns=("ANALYTICS.ORDERS.EMAIL",),
        statement_timeout_seconds=10,
        max_rows=200,
        max_result_bytes=262_144,
        max_estimated_cost=100_000,
        max_estimated_rows=100_000,
        max_estimated_bytes=8_388_608,
        catalog_cache_ttl_seconds=300,
    )

    assert saved.expected_role == "analytics_reader"
    assert saved.allowed_schemas == ("analytics",)
    assert saved.allowed_tables == ("analytics.orders",)
    assert saved.sensitive_columns == ("analytics.orders.email",)

    with pytest.raises(AppError) as captured:
        capability_control.update_sql_assistant(
            username="admin",
            enabled=False,
            dsn_secret_name="SQL_ASSISTANT_DSN",
            expected_role="invalid role",
            allowed_schemas=("analytics",),
            allowed_tables=("analytics.orders",),
            sensitive_columns=(),
            statement_timeout_seconds=10,
            max_rows=200,
            max_result_bytes=262_144,
            max_estimated_cost=100_000,
            max_estimated_rows=100_000,
            max_estimated_bytes=8_388_608,
            catalog_cache_ttl_seconds=300,
        )

    assert captured.value.code == ErrorCode.INVALID_REQUEST
