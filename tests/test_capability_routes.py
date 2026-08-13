from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api.routes.capabilities import (
    get_capability_catalog,
    get_capability_control_service,
    router,
)
from backend.capabilities.catalog import (
    CapabilitySkill,
    CapabilitySnapshot,
    CapabilityTool,
)
from backend.core.errors import install_exception_handlers
from backend.infra.auth import get_current_user
from backend.schemas.capabilities import CapabilityResponse


class _Catalog:
    def snapshot(self, *, role: str) -> CapabilitySnapshot:
        assert role == "admin"
        return CapabilitySnapshot(
            schema_version=1,
            catalog_hash="a" * 64,
            skills=(
                CapabilitySkill(
                    name="sandbox",
                    version="1.0.0",
                    description="Run isolated code.",
                    activation="/sandbox",
                    available=True,
                    availability_reason=None,
                    required_roles=("admin",),
                    tool_names=("sandbox_execute",),
                    approval_tools=("sandbox_execute",),
                    network_policies=("none",),
                    resource_scopes=("code-execution",),
                ),
            ),
            tools=(
                CapabilityTool(
                    name="sandbox_execute",
                    description="Execute isolated code.",
                    group="sandbox-execution",
                    version="1.0.0",
                    exposure="deferred",
                    available=True,
                    availability_reason=None,
                    required_roles=("admin",),
                    requires_approval=True,
                    network_policy="none",
                    resource_scope="code-execution",
                    idempotent=False,
                ),
            ),
        )


def _control_plane() -> dict:
    return {
        "schema_version": 1,
        "web_research": {
            "enabled": True,
            "provider": "tavily-keyless",
            "api_key_required": False,
        },
        "sql_assistant": {
            "enabled": False,
            "dsn_secret_name": "SQL_ASSISTANT_DSN",
            "dsn_configured": False,
            "expected_role": "",
            "allowed_schemas": [],
            "allowed_tables": [],
            "sensitive_columns": [],
            "statement_timeout_seconds": 10,
            "max_rows": 200,
            "max_result_bytes": 262_144,
            "max_estimated_cost": 100_000,
            "max_estimated_rows": 100_000,
            "max_estimated_bytes": 8_388_608,
            "catalog_cache_ttl_seconds": 300,
            "updated_at": "2026-07-21T00:00:00Z",
        },
        "skills": [],
        "custom_tools": [],
        "builtin_tools": [],
    }


class _ControlService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.apply_calls = 0
        self.web_enabled = True

    def control_plane(self) -> dict:
        payload = _control_plane()
        payload["web_research"]["enabled"] = self.web_enabled
        return payload

    def _mutate(self, operation: str, values: dict) -> None:
        self.calls.append((operation, values))

    def create_skill(self, **values) -> None:
        self._mutate("create_skill", values)

    def update_skill(self, **values) -> None:
        self._mutate("update_skill", values)

    def delete_skill(self, **values) -> None:
        self._mutate("delete_skill", values)

    def create_http_tool(self, **values) -> None:
        self._mutate("create_http_tool", values)

    def update_http_tool(self, **values) -> None:
        self._mutate("update_http_tool", values)

    def delete_http_tool(self, **values) -> None:
        self._mutate("delete_http_tool", values)

    def update_sql_assistant(self, **values) -> None:
        self._mutate("update_sql_assistant", values)

    def update_web_research(self, **values) -> None:
        self._mutate("update_web_research", values)
        self.web_enabled = bool(values["enabled"])

    def apply_runtime(self) -> None:
        self.apply_calls += 1


def _app(
    *,
    authenticated: bool,
    role: str = "admin",
    control_service: object | None = None,
) -> FastAPI:
    app = FastAPI()
    install_exception_handlers(app)
    app.include_router(router)
    app.dependency_overrides[get_capability_catalog] = _Catalog
    app.dependency_overrides[get_capability_control_service] = lambda: (
        control_service or SimpleNamespace()
    )
    if authenticated:
        app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
            username="admin",
            role=role,
        )
    return app


def test_capabilities_require_authentication() -> None:
    with TestClient(_app(authenticated=False)) as client:
        response = client.get("/v1/capabilities")

    assert response.status_code == 401


def test_capabilities_return_only_the_strict_public_projection() -> None:
    with TestClient(_app(authenticated=True)) as client:
        response = client.get("/v1/capabilities")

    assert response.status_code == 200
    payload = response.json()
    expected = CapabilityResponse.model_validate(
        _Catalog().snapshot(role="admin")
    ).model_dump(mode="json")
    assert payload == expected
    rendered = response.text
    for forbidden in (
        "required_secrets",
        "input_schema",
        "output_schema",
        "instructions",
        "content_hash",
        "SANDBOX_RUNTIME",
    ):
        assert forbidden not in rendered


def test_capability_openapi_publishes_public_and_admin_control_interfaces() -> None:
    app = _app(authenticated=True)
    schema = app.openapi()

    assert set(schema["paths"]) == {
        "/v1/capabilities",
        "/v1/capabilities/control-plane",
        "/v1/capabilities/skills",
        "/v1/capabilities/skills/{name}",
        "/v1/capabilities/tools",
        "/v1/capabilities/tools/{name}",
        "/v1/capabilities/sql-assistant",
        "/v1/capabilities/web-research",
    }
    response_schema = schema["paths"]["/v1/capabilities"]["get"]["responses"]["200"][
        "content"
    ]["application/json"]["schema"]
    assert response_schema == {"$ref": "#/components/schemas/CapabilityResponse"}


def test_capability_control_plane_requires_admin_role() -> None:
    service = _ControlService()
    with TestClient(
        _app(authenticated=True, role="user", control_service=service)
    ) as client:
        response = client.get("/v1/capabilities/control-plane")

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "PERMISSION_DENIED"
    assert service.calls == []


def test_admin_can_manage_skills_tools_sql_and_keyless_web() -> None:
    service = _ControlService()
    skill_payload = {
        "name": "release-research",
        "description": "Research public releases.",
        "instructions": "# Workflow\nUse release_lookup.",
        "allowed_tools": ["release_lookup"],
        "required_roles": [],
        "required_secrets": [],
        "enabled": True,
    }
    tool_payload = {
        "name": "release_lookup",
        "description": "Look up public release metadata.",
        "group": "custom-http",
        "endpoint": "https://api.cloudflare.com/releases",
        "method": "POST",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
            "additionalProperties": False,
        },
        "static_headers": {},
        "secret_headers": {},
        "required_roles": [],
        "requires_approval": False,
        "idempotent": True,
        "timeout_seconds": 20,
        "max_response_bytes": 65_536,
        "enabled": True,
    }
    sql_payload = {
        "enabled": False,
        "dsn_secret_name": "ANALYTICS_READER_DSN",
        "expected_role": "analytics_reader",
        "allowed_schemas": ["analytics"],
        "allowed_tables": ["analytics.orders"],
        "sensitive_columns": ["analytics.customers.email"],
        "statement_timeout_seconds": 10,
        "max_rows": 200,
        "max_result_bytes": 262_144,
        "max_estimated_cost": 100_000,
        "max_estimated_rows": 100_000,
        "max_estimated_bytes": 8_388_608,
        "catalog_cache_ttl_seconds": 300,
    }

    with TestClient(
        _app(authenticated=True, control_service=service)
    ) as client:
        control = client.get("/v1/capabilities/control-plane")
        created_tool = client.post("/v1/capabilities/tools", json=tool_payload)
        updated_tool = client.put(
            "/v1/capabilities/tools/release_lookup",
            json={key: value for key, value in tool_payload.items() if key != "name"},
        )
        created_skill = client.post("/v1/capabilities/skills", json=skill_payload)
        updated_skill = client.put(
            "/v1/capabilities/skills/release-research",
            json={key: value for key, value in skill_payload.items() if key != "name"},
        )
        sql = client.put("/v1/capabilities/sql-assistant", json=sql_payload)
        web = client.put(
            "/v1/capabilities/web-research",
            json={"enabled": False},
        )
        deleted_skill = client.delete(
            "/v1/capabilities/skills/release-research"
        )
        deleted_tool = client.delete("/v1/capabilities/tools/release_lookup")

    assert control.status_code == 200
    assert control.json()["web_research"] == {
        "enabled": True,
        "provider": "tavily-keyless",
        "api_key_required": False,
    }
    assert created_tool.status_code == 201
    assert "revision" not in created_tool.json()
    assert updated_tool.status_code == 200
    assert created_skill.status_code == 201
    assert updated_skill.status_code == 200
    assert sql.status_code == 200
    assert web.status_code == 200
    assert web.json()["web_research"]["enabled"] is False
    assert deleted_skill.json() == {
        "name": "release-research",
        "deleted": True,
    }
    assert deleted_tool.json() == {"name": "release_lookup", "deleted": True}
    assert [operation for operation, _ in service.calls] == [
        "create_http_tool",
        "update_http_tool",
        "create_skill",
        "update_skill",
        "update_sql_assistant",
        "update_web_research",
        "delete_skill",
        "delete_http_tool",
    ]
    assert service.apply_calls == 8
    assert service.calls[4][1]["dsn_secret_name"] == "ANALYTICS_READER_DSN"
    assert service.calls[5][1]["enabled"] is False
