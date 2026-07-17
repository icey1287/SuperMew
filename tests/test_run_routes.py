from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import backend.api.routes.runs as routes
from backend.core.errors import install_exception_handlers
from backend.events.contracts import new_run_event
from backend.infra.auth import get_current_user
from backend.runs.repository import RunRecord, RunReservation


def _run_record(
    *,
    run_id: str = "run_123",
    thread_id: str = "thread-1",
) -> RunRecord:
    return RunRecord(
        id=run_id,
        thread_id=thread_id,
        status="queued",
        idempotency_key="request-1",
        request_hash="request-hash",
        multitask_strategy="reject",
        fencing_token=1,
        user_message_id=10,
        assistant_message_id=11,
        supersedes_run_id=None,
        model_name="test-model",
        on_disconnect="continue",
        owner_worker_id=None,
        lease_expires_at=None,
        deadline_at=None,
        started_at=None,
        finished_at=None,
        error_code=None,
        skill_name=None,
        skill_version=None,
        skill_content_hash=None,
        skill_activation_source=None,
        input_tokens=0,
        output_tokens=0,
        cost="0",
        created_at="2026-07-16T00:00:00+00:00",
        updated_at="2026-07-16T00:00:00+00:00",
    )


def _reservation(*, run_id: str = "run_123") -> RunReservation:
    return RunReservation(
        run=_run_record(run_id=run_id),
        created=True,
        thread_version=2,
    )


def _app(*, authenticated: bool = True) -> tuple[FastAPI, SimpleNamespace]:
    app = FastAPI()
    install_exception_handlers(app)
    app.include_router(routes.router)
    user = SimpleNamespace(username="alice", role="user")
    if authenticated:
        app.dependency_overrides[get_current_user] = lambda: user
    return app, user


def _run_request() -> dict[str, str]:
    return {
        "message": "run code",
        "idempotency_key": "request-1",
    }


def test_authenticated_create_run_uses_user_identity() -> None:
    app, _ = _app()
    reservation = _reservation()
    create_run = Mock(return_value=reservation)
    spawn_once = AsyncMock(return_value=None)

    with (
        patch.object(routes.service, "create_run", create_run),
        patch.object(routes.run_agent_executor, "spawn_once", spawn_once),
        patch.object(
            routes,
            "get_settings",
            return_value=SimpleNamespace(
                app=SimpleNamespace(default_tenant_id="tenant-test")
            ),
        ),
        TestClient(app) as client,
    ):
        response = client.post(
            "/v1/threads/thread-1/runs",
            json=_run_request(),
        )

    assert response.status_code == 201
    assert response.json()["run"]["id"] == "run_123"
    assert response.json()["created"] is True
    assert response.json()["thread_version"] == 2
    create_run.assert_called_once_with(
        username="alice",
        thread_id="thread-1",
        message="run code",
        idempotency_key="request-1",
        expected_thread_version=None,
        multitask_strategy=None,
        on_disconnect=None,
        tenant_id="tenant-test",
        channel="run",
        approved_tools=frozenset(),
    )
    spawn_once.assert_awaited_once_with(username="alice", run_id="run_123")


def test_get_run_passes_username_to_service() -> None:
    app, _ = _app()
    get_run = Mock(return_value=_run_record())

    with (
        patch.object(routes.service, "get_run", get_run),
        TestClient(app) as client,
    ):
        response = client.get("/v1/runs/run_123")

    assert response.status_code == 200
    assert response.json()["id"] == "run_123"
    get_run.assert_called_once_with(username="alice", run_id="run_123")


def test_get_run_events_passes_username_and_returns_next_cursor() -> None:
    app, _ = _app()
    event = new_run_event(
        sequence=4,
        run_id="run_123",
        thread_id="thread-1",
        event_type="run.started",
    )
    read_after = Mock(return_value=[event])

    with (
        patch.object(routes.journal, "read_after", read_after),
        TestClient(app) as client,
    ):
        response = client.get("/v1/runs/run_123/events?after=2&limit=25")

    assert response.status_code == 200
    assert response.json()["events"][0]["event_id"] == event.event_id
    assert response.json()["next_after"] == 4
    read_after.assert_called_once_with(
        username="alice",
        run_id="run_123",
        after=2,
        limit=25,
    )


@pytest.mark.parametrize(
    ("after", "last_event_id", "expected_cursor"),
    [
        (7, "11", 11),
        (11, "7", 11),
        (7, None, 7),
    ],
)
def test_stream_cursor_uses_maximum_available_cursor(
    after: int,
    last_event_id: str | None,
    expected_cursor: int,
) -> None:
    app, _ = _app()
    subscriptions: list[dict[str, object]] = []

    async def subscribe(*, username: str, run_id: str, after: int):
        subscriptions.append({"username": username, "run_id": run_id, "after": after})
        if False:
            yield None

    headers = {"Last-Event-ID": last_event_id} if last_event_id else {}
    with (
        patch.object(routes.event_bus, "subscribe", subscribe),
        TestClient(app) as client,
    ):
        response = client.get(
            f"/v1/runs/run_123/stream?after={after}",
            headers=headers,
        )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert subscriptions == [
        {
            "username": "alice",
            "run_id": "run_123",
            "after": expected_cursor,
        }
    ]


def test_stream_rejects_malformed_last_event_id() -> None:
    app, _ = _app()
    stream_response = Mock()

    with (
        patch.object(routes, "_stream_response", stream_response),
        TestClient(app) as client,
    ):
        response = client.get(
            "/v1/runs/run_123/stream",
            headers={"Last-Event-ID": "not-a-sequence"},
        )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_REQUEST"
    stream_response.assert_not_called()


def test_run_events_reject_negative_after_before_reading_journal() -> None:
    app, _ = _app()
    read_after = Mock()

    with (
        patch.object(routes.journal, "read_after", read_after),
        TestClient(app) as client,
    ):
        response = client.get("/v1/runs/run_123/events?after=-1")

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_REQUEST"
    read_after.assert_not_called()


def test_create_run_rejects_invalid_thread_id_before_reservation() -> None:
    app, _ = _app()
    create_run = Mock()
    spawn_once = AsyncMock()

    with (
        patch.object(routes.service, "create_run", create_run),
        patch.object(routes.run_agent_executor, "spawn_once", spawn_once),
        TestClient(app) as client,
    ):
        response = client.post(
            "/v1/threads/bad$id/runs",
            json=_run_request(),
        )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_REQUEST"
    create_run.assert_not_called()
    spawn_once.assert_not_awaited()


def test_canonical_run_route_does_not_implicitly_create_thread() -> None:
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from backend.db.models import Base, Thread, User
    from backend.runs.repository import RunRepository
    from backend.runs.service import RunService

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    with session_factory.begin() as db:
        db.add(User(username="alice", password_hash="hash", role="user"))
    app, _ = _app()
    real_service = RunService(RunRepository(session_factory))
    spawn_once = AsyncMock()
    try:
        with (
            patch.object(routes, "service", real_service),
            patch.object(routes.run_agent_executor, "spawn_once", spawn_once),
            patch.object(
                routes,
                "get_settings",
                return_value=SimpleNamespace(
                    app=SimpleNamespace(default_tenant_id="tenant-test")
                ),
            ),
            TestClient(app) as client,
        ):
            response = client.post(
                "/v1/threads/thread-missing/runs",
                json=_run_request(),
            )

        assert response.status_code == 404
        assert response.json()["error"]["code"] == "NOT_FOUND"
        spawn_once.assert_not_awaited()
        with session_factory() as db:
            assert db.query(Thread).count() == 0
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    ("method", "path", "payload"),
    [
        ("POST", "/v1/threads/thread-1/runs", _run_request()),
        ("GET", "/v1/runs/run_123", None),
        ("GET", "/v1/runs/run_123/events", None),
        ("GET", "/v1/runs/run_123/stream", None),
        ("POST", "/v1/threads/thread-1/runs/stream", _run_request()),
    ],
)
def test_run_routes_require_authentication(
    method: str,
    path: str,
    payload: dict[str, str] | None,
) -> None:
    app, _ = _app(authenticated=False)

    with TestClient(app) as client:
        response = client.request(method, path, json=payload)

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "AUTHENTICATION_REQUIRED"
    assert response.headers["www-authenticate"] == "Bearer"


def test_create_run_stream_returns_reserved_run_identity() -> None:
    app, user = _app()
    reserve_run = AsyncMock(return_value=_reservation(run_id="run_created"))
    subscriptions: list[dict[str, object]] = []

    async def subscribe(*, username: str, run_id: str, after: int):
        subscriptions.append({"username": username, "run_id": run_id, "after": after})
        if False:
            yield None

    with (
        patch.object(routes, "_reserve_run", reserve_run),
        patch.object(routes.event_bus, "subscribe", subscribe),
        TestClient(app) as client,
    ):
        response = client.post(
            "/v1/threads/thread-1/runs/stream",
            json=_run_request(),
            headers={"Last-Event-ID": "9"},
        )

    assert response.status_code == 200
    assert response.headers["x-run-id"] == "run_created"
    assert response.headers["content-type"].startswith("text/event-stream")
    assert reserve_run.await_args.kwargs["user"] is user
    assert reserve_run.await_args.kwargs["thread_id"] == "thread-1"
    assert reserve_run.await_args.kwargs["request"].message == "run code"
    assert subscriptions == [{"username": "alice", "run_id": "run_created", "after": 9}]
