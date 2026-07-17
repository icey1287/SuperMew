from __future__ import annotations

import re
from datetime import UTC

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.chat.repository import ConversationRepository, MessageAppend
from backend.core.errors import AppError, ErrorCode
from backend.db.models import Base, ChatSession, Run, User
from backend.runs.repository import RunRepository
from backend.runs.service import RunService
from backend.runs.state import RunStatus
from backend.threads.contracts import THREAD_ID_PATTERN
from backend.threads.service import ThreadService


@pytest.fixture()
def thread_environment():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    with session_factory.begin() as db:
        db.add_all(
            [
                User(username="alice", password_hash="hash", role="user"),
                User(username="bob", password_hash="hash", role="user"),
            ]
        )
    conversations = ConversationRepository(session_factory)
    threads = ThreadService(conversations)
    runs = RunRepository(session_factory)
    yield engine, session_factory, conversations, threads, runs
    engine.dispose()


def test_create_generates_server_identity_and_normalizes_title(
    thread_environment,
) -> None:
    _, _, _, threads, _ = thread_environment

    created = threads.create_thread(username="alice", title="  First   Thread  ")

    assert created.thread_id.startswith("thread_")
    assert re.fullmatch(THREAD_ID_PATTERN, created.thread_id)
    assert created.title == "First Thread"
    assert created.message_count == 0
    assert created.version == 0
    assert created.thread_status == "active"
    assert created.active_run_id is None
    assert created.active_run_status is None
    assert created.created_at.tzinfo is UTC
    assert created.updated_at.tzinfo is UTC


def test_internal_explicit_identity_uses_shared_validation(thread_environment) -> None:
    _, _, _, threads, _ = thread_environment

    created = threads.create_thread(
        username="alice",
        thread_id="customer.case:2026-01",
    )
    assert created.thread_id == "customer.case:2026-01"
    assert created.title == "customer.case:2026-01"

    for invalid in ("-leading", "space id", "bad$id", "x" * 121):
        with pytest.raises(ValueError):
            threads.create_thread(username="alice", thread_id=invalid)


def test_recent_messages_start_from_latest_and_page_backwards(
    thread_environment,
) -> None:
    _, _, conversations, threads, _ = thread_environment
    threads.create_thread(username="alice", thread_id="thread_recent")
    for index in range(1, 6):
        conversations.append_message(
            "alice",
            "thread_recent",
            MessageAppend(
                role="human" if index % 2 else "ai",
                content=str(index),
            ),
        )

    latest = threads.recent_messages(
        username="alice",
        thread_id="thread_recent",
        limit=2,
    )
    older = threads.recent_messages(
        username="alice",
        thread_id="thread_recent",
        before=latest.previous_cursor,
        limit=2,
    )
    oldest = threads.recent_messages(
        username="alice",
        thread_id="thread_recent",
        before=older.previous_cursor,
        limit=2,
    )

    assert [item.sequence for item in latest.messages] == [4, 5]
    assert [item.role for item in latest.messages] == ["assistant", "user"]
    assert latest.previous_cursor == 4
    assert [item.sequence for item in older.messages] == [2, 3]
    assert older.previous_cursor == 2
    assert [item.sequence for item in oldest.messages] == [1]
    assert oldest.previous_cursor is None
    assert all(item.timestamp.tzinfo is UTC for item in latest.messages)


def test_recent_messages_enforces_thread_ownership(thread_environment) -> None:
    _, _, _, threads, _ = thread_environment
    threads.create_thread(username="alice", thread_id="thread_private")

    with pytest.raises(AppError) as raised:
        threads.recent_messages(
            username="bob",
            thread_id="thread_private",
        )

    assert raised.value.code == ErrorCode.NOT_FOUND


def test_thread_list_aggregates_active_run_without_n_plus_one(
    thread_environment,
) -> None:
    engine, _, _, threads, run_repository = thread_environment
    threads.create_thread(username="alice", thread_id="thread_active")
    threads.create_thread(username="alice", thread_id="thread_idle")
    RunService(run_repository).create_run(
        username="alice",
        thread_id="thread_active",
        message="question",
        idempotency_key="request-1",
    )
    statements: list[str] = []

    def capture(_conn, _cursor, statement, _parameters, _context, _executemany):
        if statement.lstrip().lower().startswith("select"):
            statements.append(statement.lower())

    event.listen(engine, "before_cursor_execute", capture)
    try:
        summaries = threads.list_threads(username="alice")
    finally:
        event.remove(engine, "before_cursor_execute", capture)

    by_id = {item.thread_id: item for item in summaries}
    assert by_id["thread_active"].active_run_id is not None
    assert by_id["thread_active"].active_run_status == RunStatus.PENDING.value
    assert by_id["thread_idle"].active_run_id is None
    assert len(statements) == 2
    assert sum("join runs" in statement for statement in statements) == 1


@pytest.mark.parametrize(
    "run_status",
    [
        RunStatus.QUEUED.value,
        RunStatus.PENDING.value,
        RunStatus.RUNNING.value,
        RunStatus.WAITING_INPUT.value,
        RunStatus.CANCELLING.value,
        "paused_by_future_worker",
    ],
)
def test_delete_fails_closed_for_every_nonterminal_status(
    thread_environment,
    run_status: str,
) -> None:
    _, session_factory, _, threads, _ = thread_environment
    thread_id = f"thread_{run_status}"
    threads.create_thread(username="alice", thread_id=thread_id)
    with session_factory.begin() as db:
        user = db.query(User).filter(User.username == "alice").one()
        thread = db.query(ChatSession).filter(ChatSession.session_id == thread_id).one()
        db.add(
            Run(
                id=f"run_{run_status}",
                thread_ref_id=thread.id,
                user_id=user.id,
                status=run_status,
                idempotency_key=f"request-{run_status}",
                request_hash="a" * 64,
            )
        )

    with pytest.raises(AppError) as raised:
        threads.delete_thread(username="alice", thread_id=thread_id)

    assert raised.value.code == ErrorCode.RUN_ACTIVE
    assert raised.value.safe_details["active_run_status"] == run_status


def test_delete_is_owner_scoped_and_allows_terminal_history(
    thread_environment,
) -> None:
    _, session_factory, _, threads, _ = thread_environment
    threads.create_thread(username="alice", thread_id="thread_owned")

    assert threads.delete_thread(username="bob", thread_id="thread_owned") is False
    with session_factory() as db:
        assert (
            db.query(ChatSession)
            .filter(ChatSession.session_id == "thread_owned")
            .count()
            == 1
        )

    with session_factory.begin() as db:
        user = db.query(User).filter(User.username == "alice").one()
        thread = (
            db.query(ChatSession).filter(ChatSession.session_id == "thread_owned").one()
        )
        db.add(
            Run(
                id="run_terminal",
                thread_ref_id=thread.id,
                user_id=user.id,
                status=RunStatus.SUCCEEDED.value,
                idempotency_key="request-terminal",
                request_hash="b" * 64,
            )
        )

    assert threads.delete_thread(username="alice", thread_id="thread_owned") is True


def test_run_requires_owned_thread_and_versions_only_appended_messages(
    thread_environment,
) -> None:
    _, session_factory, _, threads, run_repository = thread_environment
    runs = RunService(run_repository)

    with pytest.raises(AppError) as missing:
        runs.create_run(
            username="alice",
            thread_id="thread_missing",
            message="first question",
            idempotency_key="request-missing",
        )
    assert missing.value.code == ErrorCode.NOT_FOUND

    threads.create_thread(username="alice", thread_id="thread_rounds")
    first = runs.create_run(
        username="alice",
        thread_id="thread_rounds",
        message="first question",
        idempotency_key="request-1",
    )
    claimed_first = runs.claim_run(run_id=first.run.id, worker_id="worker-1")
    runs.complete_run(
        run_id=first.run.id,
        content="first answer",
        fencing_token=claimed_first.fencing_token,
    )
    second = runs.create_run(
        username="alice",
        thread_id="thread_rounds",
        message="second question",
        idempotency_key="request-2",
    )
    claimed_second = runs.claim_run(run_id=second.run.id, worker_id="worker-1")
    runs.complete_run(
        run_id=second.run.id,
        content="second answer",
        fencing_token=claimed_second.fencing_token,
    )

    with session_factory() as db:
        thread = (
            db.query(ChatSession)
            .filter(ChatSession.session_id == "thread_rounds")
            .one()
        )
        assert thread.message_count == 4
        assert thread.last_sequence == 4
        assert thread.version == 4
        assert (thread.metadata_json or {})["title"] == "first question"

    with pytest.raises(AppError) as other_owner:
        runs.create_run(
            username="bob",
            thread_id="thread_rounds",
            message="intrusion",
            idempotency_key="request-bob",
        )
    assert other_owner.value.code == ErrorCode.NOT_FOUND
