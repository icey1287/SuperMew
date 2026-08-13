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
from backend.runs.request_context import RunRequestContext
from backend.skills import SkillAccess
from backend.tools.contracts import ToolResultV1
from backend.tools.registry import ToolAccess
from backend.web_research.contracts import WebResearchResult


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

    capability_control.apply_runtime()
    state = capability_control.control_plane()

    assert state["web_research"]["enabled"] is True
    assert "revision" not in state
    assert capability_control.active_settings.web_research.enabled is True


def test_skill_changes_publish_one_snapshot_and_reuse_runtime_resources(
    capability_control,
):
    original = capability_control.apply_runtime()
    capability_control.create_skill(
        username="admin",
        name="release-summary",
        description="Summarize public release notes.",
        instructions="# Release Summary\nAnswer directly without tools.",
        allowed_tools=(),
    )

    updated = capability_control.apply_runtime()

    assert updated is capability_control.active_runtime
    assert updated is not original
    assert updated.resources is original.resources
    assert updated.tools is original.tools
    with capability_control.acquire_factory() as active_factory:
        assert updated.factory is active_factory
    assert "release-summary" in updated.skills.names
    assert original.resources._closed is False


def test_tool_changes_replace_runtime_resources(capability_control):
    original = capability_control.apply_runtime()
    with capability_control.acquire_factory():
        capability_control.create_http_tool(username="admin", **_http_payload())
        updated = capability_control.apply_runtime()

        assert original.resources._retired is True
        assert original.resources._closed is False

    assert updated is capability_control.active_runtime
    assert updated.resources is not original.resources
    assert updated.tools is not original.tools
    assert "release_lookup" in updated.tools.names
    assert original.resources._closed is True


def test_published_tool_runtime_stays_pinned_until_the_run_releases_it(
    capability_control,
    monkeypatch,
):
    class WebRuntime:
        def __init__(self, label):
            self.label = label
            self.closed = False
            self.searches = 0

        def start(self):
            return None

        def close(self):
            self.closed = True

        def search(self, query, *, limit, deadline_at, cancellation_probe):
            self.searches += 1
            return WebResearchResult(evidence=(), citations=(), truncated=False)

        def fetch(self, url, *, deadline_at, cancellation_probe):
            raise AssertionError(f"unexpected fetch from {self.label}: {url}")

    old_web = WebRuntime("old")
    new_web = WebRuntime("new")
    runtimes = iter((old_web, new_web))
    monkeypatch.setattr(
        "backend.capabilities.control_service.build_web_research_runtime",
        lambda _settings: next(runtimes),
    )
    capability_control.update_web_research(username="admin", enabled=True)
    capability_control.apply_runtime()

    with capability_control.acquire_factory() as old_factory:
        capability_control.create_http_tool(username="admin", **_http_payload())
        capability_control.apply_runtime()

        result = _invoke_web_search(old_factory.tools, thread_id="old-runtime")
        assert result.success is True
        assert old_web.searches == 1
        assert new_web.searches == 0
        assert old_web.closed is False

    assert old_web.closed is True
    with capability_control.acquire_factory() as new_factory:
        result = _invoke_web_search(new_factory.tools, thread_id="new-runtime")
        assert result.success is True
    assert new_web.searches == 1


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


def _invoke_web_search(registry, *, thread_id):
    context = RunRequestContext.for_sync(user_id="admin", thread_id=thread_id)
    try:
        session = registry.bind(
            context,
            ToolAccess(
                roles=frozenset({"admin"}),
                available_secrets=frozenset({"WEB_RESEARCH_RUNTIME"}),
                caller_allowed_tools=frozenset({"web_search"}),
                allowed_network_policies=frozenset({"restricted"}),
            ),
        )
        session.apply_skill({"web_search"})
        payload = session.resolve("web_search").invoke({"query": "public facts"})
        return ToolResultV1.model_validate_json(payload)
    finally:
        context.close()


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
