import unittest

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.core.errors import AppError, ErrorCode
from backend.db.models import Base, Message, User
from backend.runs.repository import RunRepository
from backend.runs.state import RunStatus
from backend.threads.repository import MessageAppend, ThreadRepository


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
        self.repository = ThreadRepository(self.Session)

    def tearDown(self):
        self.engine.dispose()

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
            self.assertEqual(1, db.query(Message).count())

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
        first_page = self.repository.list_messages_before(
            "alice", "thread-1", before=None, limit=2
        )
        second_page = self.repository.list_messages_before(
            "alice", "thread-1", before=first_page[-1].sequence, limit=2
        )
        self.assertEqual(["4", "3"], [item.content for item in first_page])
        self.assertEqual(["2", "1"], [item.content for item in second_page])

    def test_thread_listing_has_no_message_count_query(self):
        self.repository.append_message(
            "alice", "thread-1", MessageAppend(role="human", content="one")
        )
        statements = []

        def capture(_conn, _cursor, statement, _parameters, _context, _executemany):
            statements.append(statement.lower())

        event.listen(self.engine, "before_cursor_execute", capture)
        try:
            rows = self.repository.list_thread_summaries("alice")
        finally:
            event.remove(self.engine, "before_cursor_execute", capture)

        self.assertEqual(1, rows[0].message_count)
        self.assertFalse(
            any(
                "count(" in statement and "messages" in statement
                for statement in statements
            )
        )

    def test_thread_reads_reflect_durable_run_writes(self):
        run_repository = RunRepository(self.Session)
        self.repository.create_thread(username="alice", thread_id="thread-1")
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

        messages = self.repository.list_messages_before(
            "alice", "thread-1", before=None, limit=10
        )
        threads = self.repository.list_thread_summaries("alice")
        self.assertEqual(
            ["durable answer", "durable question"],
            [message.content for message in messages],
        )
        self.assertEqual("completed", messages[0].status)
        self.assertEqual(2, threads[0].message_count)
        self.assertEqual(2, threads[0].version)
        self.assertEqual("thread-1", threads[0].thread_id)

    def test_thread_delete_rejects_nonterminal_run_and_allows_terminal_history(self):
        run_repository = RunRepository(self.Session)
        self.repository.create_thread(username="alice", thread_id="thread-1")
        reservation = run_repository.reserve(
            username="alice",
            thread_id="thread-1",
            message="durable question",
            idempotency_key="request-1",
        )

        with self.assertRaises(AppError) as raised:
            self.repository.delete_thread("alice", "thread-1")
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

        self.assertTrue(self.repository.delete_thread("alice", "thread-1"))


if __name__ == "__main__":
    unittest.main()
