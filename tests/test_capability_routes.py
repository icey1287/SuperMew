from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api.routes.capabilities import (
    get_capability_catalog,
    router,
)
from backend.capabilities.catalog import (
    CapabilitySkill,
    CapabilitySnapshot,
    CapabilityTool,
)
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


def _app(*, authenticated: bool) -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_capability_catalog] = _Catalog
    if authenticated:
        app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
            username="admin",
            role="admin",
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


def test_capability_openapi_publishes_one_canonical_interface() -> None:
    app = _app(authenticated=True)
    schema = app.openapi()

    assert set(schema["paths"]) == {"/v1/capabilities"}
    response_schema = schema["paths"]["/v1/capabilities"]["get"]["responses"]["200"][
        "content"
    ]["application/json"]["schema"]
    assert response_schema == {"$ref": "#/components/schemas/CapabilityResponse"}
