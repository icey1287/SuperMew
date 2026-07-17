import unittest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.core.errors import AppError, ErrorCode
from backend.db.models import Base, Message, Thread, Run, User
from backend.runs.repository import RunRepository, hash_run_request
from backend.runs.state import MultitaskStrategy, RunStatus


class RunReservationTests(unittest.TestCase):
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
        self.repository = RunRepository(self.Session)

    def tearDown(self):
        self.engine.dispose()

    def reserve(self, key: str, message: str = "hello", **kwargs):
        return self.repository.reserve(
            username="alice",
            thread_id=kwargs.pop("thread_id", "thread-1"),
            message=message,
            idempotency_key=key,
            request_hash=hash_run_request(message),
            _allow_implicit_thread=True,
            **kwargs,
        )

    def test_same_key_and_hash_returns_same_run_without_duplicate_messages(self):
        first = self.reserve("request-1")
        second = self.reserve("request-1")

        self.assertTrue(first.created)
        self.assertFalse(second.created)
        self.assertEqual(first.run.id, second.run.id)
        with self.Session() as db:
            self.assertEqual(1, db.query(Run).count())
            self.assertEqual(2, db.query(Message).count())

    def test_same_key_with_different_hash_is_rejected(self):
        self.reserve("request-1", "first")
        with self.assertRaises(AppError) as raised:
            self.reserve("request-1", "second")
        self.assertEqual(ErrorCode.IDEMPOTENCY_CONFLICT, raised.exception.code)

    def test_reject_strategy_prevents_second_active_run(self):
        self.reserve("request-1")
        with self.assertRaises(AppError) as raised:
            self.reserve("request-2", "second")
        self.assertEqual(ErrorCode.RUN_ACTIVE, raised.exception.code)
        with self.Session() as db:
            self.assertEqual(2, db.query(Message).count())

    def test_enqueue_reserves_monotonic_messages_without_overwrite(self):
        first = self.reserve("request-1")
        second = self.reserve(
            "request-2",
            "second",
            multitask_strategy=MultitaskStrategy.ENQUEUE,
        )

        self.assertEqual(RunStatus.PENDING, first.run.status)
        self.assertEqual(RunStatus.QUEUED, second.run.status)
        with self.Session() as db:
            rows = db.query(Message).order_by(Message.sequence).all()
            thread = db.query(Thread).one()
            self.assertEqual([1, 2, 3, 4], [row.sequence for row in rows])
            self.assertEqual(4, thread.message_count)
            self.assertEqual(4, thread.version)

    def test_cancel_previous_records_superseded_run(self):
        first = self.reserve("request-1")
        second = self.reserve(
            "request-2",
            "second",
            multitask_strategy=MultitaskStrategy.CANCEL_PREVIOUS,
        )
        self.assertEqual(RunStatus.QUEUED, second.run.status)
        self.assertEqual(first.run.id, second.run.supersedes_run_id)

    def test_stale_thread_version_is_rejected(self):
        self.reserve("request-1")
        with self.assertRaises(AppError) as raised:
            self.reserve(
                "request-2",
                "second",
                expected_thread_version=0,
                multitask_strategy=MultitaskStrategy.ENQUEUE,
            )
        self.assertEqual(ErrorCode.THREAD_VERSION_CONFLICT, raised.exception.code)

    def test_different_threads_can_reserve_active_runs(self):
        first = self.reserve("request-1", thread_id="thread-1")
        second = self.reserve("request-2", thread_id="thread-2")
        self.assertEqual(RunStatus.PENDING, first.run.status)
        self.assertEqual(RunStatus.PENDING, second.run.status)

    def test_security_context_participates_in_request_idempotency_hash(self):
        base = hash_run_request("hello")
        approved = hash_run_request(
            "hello",
            tenant_id="tenant-a",
            channel="run",
            approved_tools=frozenset({"sandbox_execute"}),
        )
        other_channel = hash_run_request(
            "hello",
            tenant_id="tenant-a",
            channel="worker",
            approved_tools=frozenset({"sandbox_execute"}),
        )

        self.assertNotEqual(base, approved)
        self.assertNotEqual(approved, other_channel)


if __name__ == "__main__":
    unittest.main()
