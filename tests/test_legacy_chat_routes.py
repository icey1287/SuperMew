import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from urllib.parse import quote
from unittest.mock import AsyncMock, Mock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

import backend.api.routes.legacy_chat as routes
from backend.core.errors import install_exception_handlers
from backend.events.contracts import new_run_event
from backend.infra.auth import get_current_user
from backend.threads.repository import MessageRecord
from backend.threads.service import ThreadSummary


NOW = datetime(2026, 8, 26, 8, 0, tzinfo=UTC)


def _app() -> FastAPI:
    app = FastAPI()
    install_exception_handlers(app)
    app.include_router(routes.router)
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
        username="alice",
        role="user",
    )
    return app


def _event(sequence: int, event_type: str, data: dict | None = None):
    return new_run_event(
        sequence=sequence,
        run_id="run_legacy",
        thread_id="session_legacy",
        event_type=event_type,
        data=data,
    )


def test_legacy_stream_projects_new_run_events_without_duplicate_content() -> None:
    async def subscribe(**_kwargs):
        yield _event(1, "run.created", {"status": "queued"})
        yield _event(2, "tool.progress", {"step": {"label": "检索"}})
        yield _event(3, "message.delta", {"content": "答案"})
        yield _event(
            4,
            "message.completed",
            {"content": "答案", "rag_trace": {"tool_used": True}},
        )
        yield _event(5, "run.completed", {"status": "succeeded"})

    start = AsyncMock(
        return_value=routes._LegacyRun(
            run_id="run_legacy",
            after=0,
            new_thread=True,
        )
    )
    cancel = AsyncMock()
    with (
        patch.object(routes, "_start_or_resume", start),
        patch.object(routes.event_bus, "subscribe", subscribe),
        patch.object(routes, "_cancel_run", cancel),
        TestClient(_app()) as client,
    ):
        response = client.post(
            "/chat/stream",
            json={"message": "知识库问题", "session_id": "session_legacy"},
        )

    assert response.status_code == 200
    assert response.text.count('"type": "content"') == 1
    assert '"type": "session_title"' in response.text
    assert '"type": "rag_step"' in response.text
    assert '"type": "trace"' in response.text
    assert "data: [DONE]" in response.text
    cancel.assert_not_awaited()


def test_legacy_stream_returns_hitl_and_closes_for_the_next_answer() -> None:
    async def subscribe(**_kwargs):
        yield _event(
            1,
            "hitl.required",
            {
                "hitl_token": "hitl_1",
                "prompt": "请选择范围",
                "options": ["A", "B"],
                "route": "scope_select",
                "retrieval_status": "needs_scope_selection",
            },
        )

    with (
        patch.object(
            routes,
            "_start_or_resume",
            AsyncMock(
                return_value=routes._LegacyRun(
                    run_id="run_legacy",
                    after=0,
                    new_thread=False,
                )
            ),
        ),
        patch.object(routes.event_bus, "subscribe", subscribe),
        patch.object(routes, "_cancel_run", AsyncMock()) as cancel,
        TestClient(_app()) as client,
    ):
        response = client.post(
            "/chat/stream",
            json={"message": "问题", "session_id": "session_legacy"},
        )

    assert response.status_code == 200
    assert '"type": "hitl_request"' in response.text
    assert '"prompt": "请选择范围"' in response.text
    assert "data: [DONE]" in response.text
    cancel.assert_not_awaited()


def test_legacy_sync_chat_returns_final_content_and_trace() -> None:
    async def subscribe(**_kwargs):
        yield _event(1, "message.delta", {"content": "答"})
        yield _event(
            2,
            "message.completed",
            {"content": "答案", "rag_trace": {"tool_used": True}},
        )
        yield _event(3, "run.completed", {"status": "succeeded"})

    with (
        patch.object(
            routes,
            "_start_or_resume",
            AsyncMock(
                return_value=routes._LegacyRun(
                    run_id="run_legacy",
                    after=0,
                    new_thread=False,
                )
            ),
        ),
        patch.object(routes.event_bus, "subscribe", subscribe),
        TestClient(_app()) as client,
    ):
        response = client.post(
            "/chat",
            json={"message": "问题", "session_id": "session_legacy"},
        )

    assert response.status_code == 200
    assert response.json()["response"] == "答案"
    assert response.json()["rag_trace"]["tool_used"] is True


def test_legacy_sync_chat_preserves_run_error_status_and_retry_after() -> None:
    async def subscribe(**_kwargs):
        yield _event(
            1,
            "run.failed",
            {
                "error": {
                    "code": "MODEL_RATE_LIMITED",
                    "message": "模型服务繁忙，请稍后重试",
                    "retryable": True,
                    "retry_after": 2.5,
                }
            },
        )

    with (
        patch.object(
            routes,
            "_start_or_resume",
            AsyncMock(
                return_value=routes._LegacyRun(
                    run_id="run_legacy",
                    after=0,
                    new_thread=False,
                )
            ),
        ),
        patch.object(routes.event_bus, "subscribe", subscribe),
        TestClient(_app(), raise_server_exceptions=False) as client,
    ):
        response = client.post(
            "/chat",
            json={"message": "问题", "session_id": "session_legacy"},
        )

    assert response.status_code == 429
    assert response.headers["retry-after"] == "2.5"
    assert response.json()["error"]["code"] == "MODEL_RATE_LIMITED"


def test_legacy_sync_chat_maps_timeout_to_gateway_timeout() -> None:
    error = routes._run_error(
        {
            "error": {
                "code": "MODEL_TIMEOUT",
                "message": "模型响应超时",
                "retryable": True,
            }
        }
    )

    assert error.status_code == 504
    assert error.retryable is True


def test_cancelled_sync_request_cancels_the_background_run() -> None:
    async def subscribe(**_kwargs):
        raise asyncio.CancelledError
        yield

    cancel = AsyncMock()
    with (
        patch.object(routes.event_bus, "subscribe", subscribe),
        patch.object(routes, "_cancel_run", cancel),
    ):
        try:
            asyncio.run(
                routes._collect_response(
                    "alice",
                    routes._LegacyRun(
                        run_id="run_legacy",
                        after=0,
                        new_thread=False,
                    ),
                )
            )
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("cancelled request should propagate cancellation")

    cancel.assert_awaited_once_with("alice", "run_legacy")


def test_legacy_null_session_id_maps_to_default_session() -> None:
    async def subscribe(**_kwargs):
        yield _event(1, "run.completed", {"status": "succeeded"})

    start = AsyncMock(
        return_value=routes._LegacyRun(
            run_id="run_legacy",
            after=0,
            new_thread=False,
        )
    )
    with (
        patch.object(routes, "_start_or_resume", start),
        patch.object(routes.event_bus, "subscribe", subscribe),
        TestClient(_app()) as client,
    ):
        response = client.post(
            "/chat",
            json={"message": "问题", "session_id": None},
        )

    assert response.status_code == 200
    assert start.await_args.kwargs["session_id"] == "default_session"


def test_waiting_legacy_session_resumes_the_existing_run() -> None:
    summary = SimpleNamespace(
        active_run_id="run_legacy",
        active_run_status="waiting_input",
    )
    hitl_event = _event(7, "hitl.required", {"hitl_token": "hitl_1"})
    accept = Mock()
    resume_once = AsyncMock()

    with (
        patch.object(
            routes.thread_repository, "get_thread_summary", return_value=summary
        ),
        patch.object(routes, "_latest_hitl_event", return_value=hitl_event),
        patch.object(routes.resume_coordinator, "accept", accept),
        patch.object(routes.run_agent_executor, "resume_once", resume_once),
    ):
        run = asyncio.run(
            routes._start_or_resume(
                username="alice",
                session_id="session_legacy",
                message="A",
            )
        )

    assert run == routes._LegacyRun(
        run_id="run_legacy",
        after=7,
        new_thread=False,
    )
    assert accept.call_args.kwargs["run_id"] == "run_legacy"
    assert accept.call_args.kwargs["hitl_token"] == "hitl_1"
    assert accept.call_args.kwargs["answer"] == "A"
    resume_once.assert_awaited_once()


def test_new_legacy_message_cancels_superseded_run_before_spawn() -> None:
    reservation = SimpleNamespace(
        run=SimpleNamespace(
            id="run_new",
            supersedes_run_id="run_old",
        )
    )
    cancel = AsyncMock()
    spawn = AsyncMock()
    with (
        patch.object(routes.thread_repository, "get_thread_summary", return_value=None),
        patch.object(
            routes._legacy_run_service,
            "create_run",
            return_value=reservation,
        ) as create_run,
        patch.object(routes, "_cancel_run", cancel),
        patch.object(routes.run_agent_executor, "spawn_once", spawn),
    ):
        run = asyncio.run(
            routes._start_or_resume(
                username="alice",
                session_id="session_legacy",
                message="新问题",
            )
        )

    assert run.run_id == "run_new"
    cancel.assert_awaited_once_with("alice", "run_old")
    spawn.assert_awaited_once_with(username="alice", run_id="run_new")
    assert (
        create_run.call_args.kwargs["multitask_strategy"]
        == routes.MultitaskStrategy.CANCEL_PREVIOUS
    )


def test_legacy_sessions_project_canonical_threads_and_messages() -> None:
    thread = ThreadSummary(
        thread_id="session_legacy",
        title="旧会话",
        created_at=NOW,
        updated_at=NOW,
        message_count=2,
        version=2,
        thread_status="active",
        active_run_id=None,
        active_run_status=None,
    )
    rows = [
        MessageRecord(
            id=2,
            thread_id="session_legacy",
            run_id="run_legacy",
            sequence=2,
            status="completed",
            role="ai",
            content="答案",
            client_message_id="run_legacy:assistant",
            timestamp=NOW,
            updated_at=NOW,
            rag_trace={"tool_used": True},
        ),
        MessageRecord(
            id=1,
            thread_id="session_legacy",
            run_id="run_legacy",
            sequence=1,
            status="completed",
            role="human",
            content="问题",
            client_message_id="run_legacy:user",
            timestamp=NOW,
            updated_at=NOW,
            rag_trace=None,
        ),
    ]
    with (
        patch.object(routes.thread_service, "list_threads", return_value=[thread]),
        patch.object(
            routes.thread_repository, "list_messages_before", return_value=rows
        ),
        TestClient(_app()) as client,
    ):
        listed = client.get("/sessions")
        messages = client.get("/sessions/session_legacy")

    assert listed.status_code == 200
    assert listed.json()["sessions"][0]["session_id"] == "session_legacy"
    assert [item["type"] for item in messages.json()["messages"]] == ["human", "ai"]
    assert messages.json()["messages"][1]["rag_trace"]["tool_used"] is True


def test_waiting_session_message_is_rebuilt_from_durable_hitl_event() -> None:
    row = MessageRecord(
        id=2,
        thread_id="session_legacy",
        run_id="run_legacy",
        sequence=2,
        status="waiting_input",
        role="ai",
        content="",
        client_message_id="run_legacy:assistant",
        timestamp=NOW,
        updated_at=NOW,
        rag_trace=None,
    )
    hitl = _event(
        7,
        "hitl.required",
        {
            "hitl_token": "hitl_1",
            "prompt": "请选择范围",
            "options": ["A", "B"],
            "route": "scope_select",
            "retrieval_status": "needs_scope_selection",
        },
    )
    with (
        patch.object(
            routes.thread_repository, "list_messages_before", return_value=[row]
        ),
        patch.object(routes, "_latest_hitl_event", return_value=hitl),
        TestClient(_app()) as client,
    ):
        response = client.get("/sessions/session_legacy")

    assert response.status_code == 200
    message = response.json()["messages"][0]
    assert message["content"] == "请选择范围\n\n可选方向：\n- A\n- B"
    assert message["rag_trace"]["hitl_options"] == ["A", "B"]


def test_legacy_session_reads_historical_noncanonical_id() -> None:
    session_id = "历史 会话"
    with (
        patch.object(
            routes.thread_repository,
            "list_messages_before",
            return_value=[],
        ) as list_messages,
        TestClient(_app()) as client,
    ):
        response = client.get(f"/sessions/{quote(session_id, safe='')}")

    assert response.status_code == 200
    assert list_messages.call_args.args[1] == session_id
