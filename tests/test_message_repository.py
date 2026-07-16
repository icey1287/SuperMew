import unittest
from unittest.mock import patch

from langchain_core.messages import AIMessage, HumanMessage
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.chat.repository import ConversationRepository, MessageAppend
import backend.chat.storage as storage_module
from backend.chat.storage import ConversationStorage
from backend.core.errors import AppError, ErrorCode
from backend.db.models import Base, ChatMessage, ChatSession, User
from backend.runs.repository import RunRepository
from backend.runs.state import RunStatus


class _MemoryCache:
    def __init__(self):
        self.values = {}

    def get_json(self, key):
        return self.values.get(key)

    def set_json(self, key, value):
        self.values[key] = value

    def delete(self, key):
        self.values.pop(key, None)


class MessageRepositoryTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)
        with self.Session.begin() as db:
            db.add(User(username="alice", password_hash="hash", role="user"))
        self.repository = ConversationRepository(self.Session)

    def tearDown(self):
        self.engine.dispose()

    def test_legacy_snapshot_appends_without_rewriting_existing_rows(self):
        storage = ConversationStorage(self.repository)
        storage.save("alice", "thread-1", [HumanMessage(content="hello")])
        with self.Session() as db:
            first = db.query(ChatMessage).one()
            first_id = first.id
            first_timestamp = first.timestamp

        statements = []

        def capture(_conn, _cursor, statement, _parameters, _context, _executemany):
            statements.append(statement.lower().strip())

        event.listen(self.engine, "before_cursor_execute", capture)
        try:
            storage.save(
                "alice",
                "thread-1",
                [HumanMessage(content="hello"), AIMessage(content="world")],
            )
        finally:
            event.remove(self.engine, "before_cursor_execute", capture)

        with self.Session() as db:
            rows = db.query(ChatMessage).order_by(ChatMessage.sequence).all()
            thread = db.query(ChatSession).one()
            self.assertEqual([1, 2], [row.sequence for row in rows])
            self.assertEqual(first_id, rows[0].id)
            self.assertEqual(first_timestamp, rows[0].timestamp)
            self.assertEqual(2, thread.message_count)
            self.assertEqual(2, thread.last_sequence)
        self.assertFalse(
            any(statement.startswith("delete") for statement in statements)
        )

    def test_current_user_access_reloads_role_from_database(self):
        first = self.repository.current_user_access("alice")
        with self.Session.begin() as db:
            db.query(User).filter(User.username == "alice").one().role = "admin"
        second = self.repository.current_user_access("alice")

        self.assertEqual("user", first.role)
        self.assertEqual("admin", second.role)
        self.assertEqual(first.user_db_id, second.user_db_id)

        with self.assertRaises(AppError) as missing:
            self.repository.current_user_access("missing")
        self.assertEqual(ErrorCode.AUTHENTICATION_REQUIRED, missing.exception.code)

    def test_client_message_id_makes_append_idempotent(self):
        message = MessageAppend(
            role="human",
            content="hello",
            client_message_id="request-1:user",
        )
        first = self.repository.append_message("alice", "thread-1", message)
        second = self.repository.append_message("alice", "thread-1", message)

        self.assertEqual(first.id, second.id)
        with self.Session() as db:
            self.assertEqual(1, db.query(ChatMessage).count())

    def test_placeholder_and_finalize_are_idempotent(self):
        placeholder = self.repository.create_assistant_placeholder(
            "alice", "thread-1", "run-1"
        )
        duplicate = self.repository.create_assistant_placeholder(
            "alice", "thread-1", "run-1"
        )
        self.assertEqual(placeholder.id, duplicate.id)

        completed = self.repository.finalize_message(
            "alice",
            "thread-1",
            placeholder.id,
            content="answer",
            status="completed",
        )
        repeated = self.repository.finalize_message(
            "alice",
            "thread-1",
            placeholder.id,
            content="answer",
            status="completed",
        )
        self.assertEqual(completed.id, repeated.id)
        self.assertEqual("completed", repeated.status)

    def test_expected_version_rejects_stale_writer(self):
        self.repository.append_message(
            "alice", "thread-1", MessageAppend(role="human", content="one")
        )
        with self.assertRaises(AppError) as raised:
            self.repository.append_message(
                "alice",
                "thread-1",
                MessageAppend(role="human", content="two"),
                expected_version=0,
            )
        self.assertEqual(ErrorCode.CONFLICT, raised.exception.code)

    def test_cursor_pagination_is_stable(self):
        for index in range(5):
            self.repository.append_message(
                "alice",
                "thread-1",
                MessageAppend(role="human", content=str(index)),
            )
        first_page = self.repository.list_messages("alice", "thread-1", limit=2)
        second_page = self.repository.list_messages(
            "alice", "thread-1", after=first_page[-1].sequence, limit=2
        )
        self.assertEqual(["0", "1"], [item.content for item in first_page])
        self.assertEqual(["2", "3"], [item.content for item in second_page])

    def test_thread_listing_has_no_message_count_query(self):
        self.repository.append_message(
            "alice", "thread-1", MessageAppend(role="human", content="one")
        )
        statements = []

        def capture(_conn, _cursor, statement, _parameters, _context, _executemany):
            statements.append(statement.lower())

        event.listen(self.engine, "before_cursor_execute", capture)
        try:
            rows = self.repository.list_threads("alice")
        finally:
            event.remove(self.engine, "before_cursor_execute", capture)

        self.assertEqual(1, rows[0]["message_count"])
        self.assertFalse(
            any(
                "count(" in statement and "chat_messages" in statement
                for statement in statements
            )
        )

    def test_session_reads_reflect_durable_run_writes_after_list_warmup(self):
        storage = ConversationStorage(self.repository)
        run_repository = RunRepository(self.Session)
        cache = _MemoryCache()

        with patch.object(storage_module, "cache", cache, create=True):
            storage.save(
                "alice",
                "thread-1",
                [HumanMessage(content="legacy message")],
            )
            warmed = storage.list_session_infos("alice")
            self.assertEqual(1, warmed[0]["message_count"])

            reservation = run_repository.reserve(
                username="alice",
                thread_id="thread-1",
                message="durable question",
                idempotency_key="request-1",
            )
            claimed = run_repository.claim(
                run_id=reservation.run.id,
                worker_id="worker-1",
            )
            run_repository.finalize(
                run_id=reservation.run.id,
                target_status=RunStatus.SUCCEEDED,
                content="durable answer",
                fencing_token=claimed.fencing_token,
            )

            messages = storage.get_session_messages("alice", "thread-1")
            sessions = storage.list_session_infos("alice")

        self.assertEqual(
            ["legacy message", "durable question", "durable answer"],
            [message["content"] for message in messages],
        )
        self.assertEqual("completed", messages[-1]["status"])
        self.assertEqual(3, sessions[0]["message_count"])
        self.assertEqual(4, sessions[0]["version"])

    def test_thread_delete_rejects_nonterminal_run_and_allows_terminal_history(self):
        storage = ConversationStorage(self.repository)
        run_repository = RunRepository(self.Session)
        reservation = run_repository.reserve(
            username="alice",
            thread_id="thread-1",
            message="durable question",
            idempotency_key="request-1",
        )

        with self.assertRaises(AppError) as raised:
            storage.delete_session("alice", "thread-1")
        self.assertEqual(ErrorCode.RUN_ACTIVE, raised.exception.code)

        claimed = run_repository.claim(
            run_id=reservation.run.id,
            worker_id="worker-1",
        )
        run_repository.finalize(
            run_id=reservation.run.id,
            target_status=RunStatus.SUCCEEDED,
            content="durable answer",
            fencing_token=claimed.fencing_token,
        )

        self.assertTrue(storage.delete_session("alice", "thread-1"))


if __name__ == "__main__":
    unittest.main()
