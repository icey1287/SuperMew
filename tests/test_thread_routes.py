from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import backend.api.routes.runs as run_routes
import backend.api.routes.sessions as session_routes
import backend.api.routes.threads as thread_routes
from backend.api.router import router as application_router
from backend.chat.repository import MessageRecord
from backend.core.errors import AppError, ErrorCode, install_exception_handlers
from backend.infra.auth import get_current_user
from backend.threads.service import (
    LegacyMessagePage,
    ThreadMessage,
    ThreadMessagePage,
    ThreadSummary,
)


NOW = datetime(2026, 7, 16, 8, 0, tzinfo=UTC)


def _app(*, authenticated: bool = True, include_legacy: bool = False) -> FastAPI:
    app = FastAPI()
    install_exception_handlers(app)
    app.include_router(thread_routes.router)
    if include_legacy:
        app.include_router(session_routes.router)
    if authenticated:
        app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
            username="alice",
            role="user",
        )
    return app


def _thread_summary(
    thread_id: str,
    *,
    updated_at: datetime = NOW,
    active_run_id: str | None = None,
    active_run_status: str | None = None,
) -> ThreadSummary:
    return ThreadSummary(
        thread_id=thread_id,
        title=f"标题 {thread_id}",
        created_at=NOW,
        updated_at=updated_at,
        message_count=2,
        version=2,
        thread_status="active",
        active_run_id=active_run_id,
        active_run_status=active_run_status,
    )


def _thread_message(
    sequence: int,
    role: str,
    content: str,
    *,
    run_id: str | None = "run_1",
) -> ThreadMessage:
    return ThreadMessage(
        id=sequence,
        run_id=run_id,
        sequence=sequence,
        status="completed",
        role=role,
        content=content,
        timestamp=NOW,
        rag_trace=None,
    )


def test_canonical_create_generates_identity_in_application_module() -> None:
    created = _thread_summary("thread_generated")
    create_thread = Mock(return_value=created)

    with (
        patch.object(thread_routes.thread_service, "create_thread", create_thread),
        TestClient(_app()) as client,
    ):
        response = client.post("/v1/threads", json={"title": "新 Thread"})

    assert response.status_code == 201
    assert response.json() == {
        "thread_id": "thread_generated",
        "title": "标题 thread_generated",
        "created_at": "2026-07-16T08:00:00Z",
        "updated_at": "2026-07-16T08:00:00Z",
        "message_count": 2,
        "version": 2,
        "thread_status": "active",
        "active_run_id": None,
        "active_run_status": None,
    }
    create_thread.assert_called_once_with(username="alice", title="新 Thread")


def test_canonical_create_rejects_client_supplied_thread_id() -> None:
    create_thread = Mock()
    with (
        patch.object(thread_routes.thread_service, "create_thread", create_thread),
        TestClient(_app()) as client,
    ):
        response = client.post(
            "/v1/threads",
            json={"thread_id": "client-owned", "title": "bad"},
        )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_REQUEST"
    create_thread.assert_not_called()


def test_canonical_thread_list_publishes_run_projection_without_adapter_sorting() -> (
    None
):
    records = [
        _thread_summary(
            "thread_new",
            active_run_id="run_active",
            active_run_status="running",
        ),
        _thread_summary(
            "thread_old",
            updated_at=datetime(2026, 7, 15, 8, 0, tzinfo=UTC),
        ),
    ]
    list_threads = Mock(return_value=records)

    with (
        patch.object(thread_routes.thread_service, "list_threads", list_threads),
        TestClient(_app()) as client,
    ):
        response = client.get("/v1/threads")

    assert response.status_code == 200
    assert [item["thread_id"] for item in response.json()["threads"]] == [
        "thread_new",
        "thread_old",
    ]
    assert response.json()["threads"][0]["active_run_id"] == "run_active"
    assert response.json()["threads"][0]["active_run_status"] == "running"
    assert "status" not in response.json()["threads"][0]
    list_threads.assert_called_once_with(username="alice")


def test_canonical_messages_use_recent_page_contract_and_canonical_roles() -> None:
    page = ThreadMessagePage(
        messages=(
            _thread_message(4, "user", "问题", run_id=None),
            _thread_message(5, "assistant", "回答"),
        ),
        previous_cursor=4,
    )
    recent_messages = Mock(return_value=page)

    with (
        patch.object(
            thread_routes.thread_service,
            "recent_messages",
            recent_messages,
        ),
        TestClient(_app()) as client,
    ):
        response = client.get(
            "/v1/threads/thread-1/messages",
            params={"before": 6, "limit": 2},
        )

    assert response.status_code == 200
    assert response.json() == {
        "messages": [
            {
                "id": 4,
                "run_id": None,
                "sequence": 4,
                "status": "completed",
                "role": "user",
                "content": "问题",
                "timestamp": "2026-07-16T08:00:00Z",
                "rag_trace": None,
            },
            {
                "id": 5,
                "run_id": "run_1",
                "sequence": 5,
                "status": "completed",
                "role": "assistant",
                "content": "回答",
                "timestamp": "2026-07-16T08:00:00Z",
                "rag_trace": None,
            },
        ],
        "previous_cursor": 4,
    }
    recent_messages.assert_called_once_with(
        username="alice",
        thread_id="thread-1",
        before=6,
        limit=2,
    )


@pytest.mark.parametrize(
    "path",
    [
        "/v1/threads/bad$id/messages",
        "/v1/threads/-leading-dash/messages",
        "/v1/threads/space%20id/messages",
    ],
)
def test_canonical_thread_paths_enforce_shared_thread_id_contract(path: str) -> None:
    recent_messages = Mock()
    with (
        patch.object(
            thread_routes.thread_service,
            "recent_messages",
            recent_messages,
        ),
        TestClient(_app()) as client,
    ):
        response = client.get(path)

    assert response.status_code == 422
    recent_messages.assert_not_called()


def test_canonical_thread_delete_preserves_active_run_guard() -> None:
    active = AppError(
        ErrorCode.RUN_ACTIVE,
        "Thread 仍有活跃 Run，请先取消或等待运行结束",
        status_code=409,
    )
    delete_thread = Mock(side_effect=active)

    with (
        patch.object(thread_routes.thread_service, "delete_thread", delete_thread),
        TestClient(_app()) as client,
    ):
        response = client.delete("/v1/threads/thread-1")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "RUN_ACTIVE"
    delete_thread.assert_called_once_with(username="alice", thread_id="thread-1")


@pytest.mark.parametrize(
    ("method", "path", "payload"),
    [
        ("POST", "/v1/threads", {}),
        ("GET", "/v1/threads", None),
        ("GET", "/v1/threads/thread-1/messages", None),
        ("DELETE", "/v1/threads/thread-1", None),
    ],
)
def test_canonical_thread_routes_require_authentication(
    method: str,
    path: str,
    payload: dict[str, object] | None,
) -> None:
    with TestClient(_app(authenticated=False)) as client:
        response = client.request(method, path, json=payload)

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "AUTHENTICATION_REQUIRED"


def test_legacy_session_adapter_keeps_old_shape_and_after_cursor() -> None:
    record = MessageRecord(
        id=1,
        thread_id="legacy id",
        sequence=1,
        role="human",
        content="旧消息",
        status="completed",
        run_id=None,
        client_message_id=None,
        timestamp=datetime(2026, 7, 16, 8, 0),
        updated_at=datetime(2026, 7, 16, 8, 0),
        rag_trace=None,
    )
    legacy_messages = Mock(
        return_value=LegacyMessagePage(messages=(record,), next_cursor=1)
    )
    list_threads = Mock(return_value=[_thread_summary("thread-1")])

    with (
        patch.object(
            session_routes.thread_service,
            "legacy_messages",
            legacy_messages,
        ),
        patch.object(
            session_routes.thread_service,
            "list_threads",
            list_threads,
        ),
        TestClient(_app(include_legacy=True)) as client,
    ):
        messages = client.get("/sessions/legacy%20id?after=0&limit=1")
        sessions = client.get("/sessions")

    assert messages.status_code == 200
    assert messages.json() == {
        "messages": [
            {
                "id": 1,
                "run_id": None,
                "sequence": 1,
                "status": "completed",
                "type": "human",
                "content": "旧消息",
                "timestamp": "2026-07-16T08:00:00",
                "rag_trace": None,
            }
        ],
        "next_cursor": 1,
    }
    assert sessions.json()["sessions"][0]["session_id"] == "thread-1"
    legacy_messages.assert_called_once_with(
        username="alice",
        session_id="legacy id",
        after=0,
        limit=1,
    )


def test_legacy_routes_are_deprecated_without_importing_canonical_adapter() -> None:
    app = _app(include_legacy=True)
    schema = app.openapi()

    assert schema["paths"]["/v1/threads"]["get"].get("deprecated") is not True
    for path, method in (
        ("/sessions", "get"),
        ("/sessions/{session_id}", "get"),
        ("/sessions/{session_id}", "delete"),
    ):
        assert schema["paths"][path][method]["deprecated"] is True


def test_thread_create_route_belongs_only_to_thread_adapter() -> None:
    thread_app = FastAPI()
    thread_app.include_router(thread_routes.router)
    run_app = FastAPI()
    run_app.include_router(run_routes.router)

    assert "post" in thread_app.openapi()["paths"]["/v1/threads"]
    assert "/v1/threads" not in run_app.openapi()["paths"]


def test_openapi_publishes_canonical_thread_and_message_contracts() -> None:
    app = FastAPI()
    app.include_router(thread_routes.router)
    app.include_router(run_routes.router)
    schema = app.openapi()

    assert set(
        schema["components"]["schemas"]["ThreadCreateRequest"]["properties"]
    ) == {"title"}
    assert "created_at" in schema["components"]["schemas"]["ThreadResponse"]["required"]
    assert (
        "created_at" not in schema["components"]["schemas"]["ThreadInfo"]["properties"]
    )
    assert set(schema["components"]["schemas"]["ThreadMessageInfo"]["required"]) == {
        "id",
        "run_id",
        "sequence",
        "status",
        "role",
        "content",
        "timestamp",
        "rag_trace",
    }
    expected_pattern = r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,119}$"
    assert (
        schema["paths"]["/v1/threads/{thread_id}/messages"]["get"]["parameters"][0][
            "schema"
        ]["pattern"]
        == expected_pattern
    )
    assert (
        schema["paths"]["/v1/threads/{thread_id}/runs"]["post"]["parameters"][0][
            "schema"
        ]["pattern"]
        == expected_pattern
    )


def test_application_router_combines_thread_history_and_run_creation() -> None:
    app = FastAPI()
    app.include_router(application_router)
    paths = app.openapi()["paths"]

    assert {"get", "post"}.issubset(paths["/v1/threads"])
    assert "get" in paths["/v1/threads/{thread_id}/messages"]
    assert "delete" in paths["/v1/threads/{thread_id}"]
