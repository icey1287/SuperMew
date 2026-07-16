from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import backend.chat as chat_package
from backend.api.routes.chat import router
from backend.core.errors import install_exception_handlers
from backend.infra.auth import get_current_user


def _app(*, authenticated: bool = True) -> FastAPI:
    app = FastAPI()
    install_exception_handlers(app)
    app.include_router(router)
    if authenticated:
        app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
            username="alice",
            role="user",
        )
    return app


@pytest.mark.parametrize("path", ["/chat", "/chat/stream"])
@pytest.mark.parametrize(
    "payload", [{}, {"message": "旧客户端请求", "session_id": "s"}]
)
def test_legacy_chat_routes_return_typed_gone_for_any_legacy_body(
    path: str,
    payload: dict[str, str],
) -> None:
    with TestClient(_app()) as client:
        response = client.post(path, json=payload)

    assert response.status_code == 410
    assert response.headers["content-type"].startswith("application/json")
    assert response.json() == {
        "error": {
            "code": "ENDPOINT_RETIRED",
            "message": "旧 Chat 接口已退役，请使用持久化 Run/Event 接口。",
            "retryable": False,
            "category": "contract",
            "stage": "routing",
            "provider": None,
            "retry_after": None,
            "request_id": None,
            "details": {
                "create_run": "/v1/threads/{thread_id}/runs",
                "stream_run": "/v1/runs/{run_id}/stream",
                "resume_run": "/v1/runs/{run_id}/resume",
                "cancel_run": "/v1/runs/{run_id}/cancel",
            },
        }
    }


@pytest.mark.parametrize("path", ["/chat", "/chat/stream"])
def test_legacy_chat_openapi_marks_route_deprecated_and_gone(path: str) -> None:
    operation = _app().openapi()["paths"][path]["post"]

    assert operation["deprecated"] is True
    assert "410" in operation["responses"]
    assert "200" not in operation["responses"]


@pytest.mark.parametrize("path", ["/chat", "/chat/stream"])
def test_legacy_chat_tombstones_still_require_authentication(path: str) -> None:
    with TestClient(_app(authenticated=False)) as client:
        response = client.post(path, json={"message": "旧客户端请求"})

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "AUTHENTICATION_REQUIRED"
    assert response.headers["www-authenticate"] == "Bearer"


def test_chat_package_does_not_reexport_legacy_execution_helpers() -> None:
    assert not hasattr(chat_package, "chat_with_agent")
    assert not hasattr(chat_package, "chat_with_agent_stream")
