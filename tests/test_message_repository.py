import unittest

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.core.errors import AppError, ErrorCode
from backend.db.models import Base, Message, Thread, User
from backend.runs.repository import RunRepository
from backend.runs.state import RunStatus
from backend.threads.repository import ThreadRepository
from tests.support import TEST_MODEL_SNAPSHOT


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

    def test_cursor_pagination_is_stable(self):
        self.repository.create_thread(username="alice", thread_id="thread-1")
        with self.Session.begin() as db:
            thread = db.query(Thread).filter(Thread.thread_id == "thread-1").one()
            db.add_all(
                Message(
                    thread_ref_id=thread.id,
                    sequence=index + 1,
                    message_type="human",
                    content=str(index),
                    status="completed",
                )
                for index in range(5)
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
        self.repository.create_thread(username="alice", thread_id="thread-1")
        RunRepository(self.Session).reserve(
            username="alice",
            thread_id="thread-1",
            message="one",
            idempotency_key="request-1",
            model_snapshot=TEST_MODEL_SNAPSHOT,
        )
        statements = []

        def capture(_conn, _cursor, statement, _parameters, _context, _executemany):
            statements.append(statement.lower())

        event.listen(self.engine, "before_cursor_execute", capture)
        try:
            rows = self.repository.list_thread_summaries("alice")
        finally:
            event.remove(self.engine, "before_cursor_execute", capture)

        self.assertEqual(2, rows[0].message_count)
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
            model_snapshot=TEST_MODEL_SNAPSHOT,
        )
        claimed = run_repository.claim(
            run_id=reservation.run.id,
            worker_id="worker-1",
        )
        run_repository.pin_skill_activation(
            run_id=reservation.run.id,
            worker_id="worker-1",
            fencing_token=claimed.fencing_token,
            name="web-research",
            version="1.0.0",
            content_hash="a" * 64,
            source="slash",
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
        self.assertEqual(
            ["web-research", "web-research"],
            [message.skill_name for message in messages],
        )
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
            model_snapshot=TEST_MODEL_SNAPSHOT,
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
