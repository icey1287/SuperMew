import asyncio
import unittest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.core.errors import AppError, ErrorCode
from backend.db.models import Base, ChatMessage, Run, RunEvent, User
from backend.runs.cancellation import CancellationRegistry, RunExecutionManager
from backend.runs.repository import RunRepository
from backend.runs.service import RunService
from backend.runs.state import RunStatus


class FakeCancellationTransport:
    def __init__(self):
        self.requested = set()

    async def request(self, run_id):
        self.requested.add(run_id)

    async def is_requested(self, run_id):
        return run_id in self.requested

    async def close(self):
        return None


class CancellationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)
        with self.Session.begin() as db:
            db.add_all(
                [
                    User(username="alice", password_hash="hash", role="user"),
                    User(username="bob", password_hash="hash", role="user"),
                ]
            )
        self.repository = RunRepository(self.Session)
        self.service = RunService(self.repository)
        self.registry = CancellationRegistry(transport=None)
        self.manager = RunExecutionManager(self.service, self.registry)

    def tearDown(self):
        self.engine.dispose()

    def create(self):
        return self.service.create_run(
            username="alice",
            thread_id="thread-1",
            message="hello",
            idempotency_key="request-1",
        )

    async def test_running_task_is_cancelled_and_partial_output_is_persisted(self):
        reservation = self.create()
        claimed = self.service.claim_run(
            run_id=reservation.run.id,
            worker_id="worker-1",
        )
        started = asyncio.Event()

        async def runner(token):
            token.append_partial("partial answer")
            started.set()
            await asyncio.sleep(60)
            return "unreachable"

        task = self.manager.spawn(run=claimed, runner=runner)
        await started.wait()
        cancelling = self.service.request_cancel(
            username="alice",
            run_id=claimed.id,
        )
        self.assertEqual(RunStatus.CANCELLING, cancelling.status)
        self.assertTrue(await self.registry.request_cancel(claimed.id, propagate=False))
        await task

        with self.Session() as db:
            run = db.query(Run).filter(Run.id == claimed.id).one()
            message = (
                db.query(ChatMessage)
                .filter(ChatMessage.id == run.assistant_message_id)
                .one()
            )
            terminal_count = (
                db.query(RunEvent)
                .filter(
                    RunEvent.run_id == run.id,
                    RunEvent.event_type == "run.cancelled",
                )
                .count()
            )
            self.assertEqual("cancelled", run.status)
            self.assertEqual("partial answer", message.content)
            self.assertEqual("incomplete", message.status)
            self.assertEqual(1, terminal_count)

    async def test_pending_run_cancels_immediately_and_is_idempotent(self):
        reservation = self.create()
        first = self.service.request_cancel(
            username="alice",
            run_id=reservation.run.id,
        )
        second = self.service.request_cancel(
            username="alice",
            run_id=reservation.run.id,
        )
        self.assertEqual(RunStatus.CANCELLED, first.status)
        self.assertEqual(first.id, second.id)
        with self.Session() as db:
            self.assertEqual(
                1,
                db.query(RunEvent)
                .filter(
                    RunEvent.run_id == first.id,
                    RunEvent.event_type == "run.cancelled",
                )
                .count(),
            )

    async def test_other_user_cannot_cancel_run(self):
        reservation = self.create()
        with self.assertRaises(AppError) as raised:
            self.service.request_cancel(username="bob", run_id=reservation.run.id)
        self.assertEqual(ErrorCode.RUN_NOT_FOUND, raised.exception.code)

    async def test_cancel_unknown_local_task_is_safe(self):
        self.assertFalse(
            await self.registry.request_cancel("run_unknown", propagate=False)
        )

    async def test_transport_propagates_and_pre_requested_token_starts_cancelled(self):
        transport = FakeCancellationTransport()
        registry = CancellationRegistry(transport=transport)
        self.assertFalse(await registry.request_cancel("run_remote", propagate=True))
        token = await registry.register("run_remote")
        self.assertTrue(token.cancelled)


if __name__ == "__main__":
    unittest.main()
