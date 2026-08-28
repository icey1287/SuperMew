import asyncio
import importlib
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.runs.request_context import RunRequestContext
from backend.core.errors import AppError, ErrorCode
from backend.db.models import Base, Message, Run, RunEvent, User
from backend.providers import ProviderCode, ProviderError, ProviderOperation
from backend.runs.cancellation import CancellationRegistry, RunExecutionManager
from backend.runs.repository import RunRepository
from backend.runs.service import RunService
from backend.runs.state import RunStatus
from backend.schemas.runs import RunResponse
from tests.support import static_model_control


class FakeCancellationTransport:
    def __init__(self):
        self.requested = set()
        self.is_requested_calls = []

    async def request(self, run_id):
        self.requested.add(run_id)

    async def is_requested(self, run_id):
        self.is_requested_calls.append(run_id)
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
        self.service = RunService(
            self.repository,
            model_control=static_model_control,
            _allow_implicit_threads=True,
        )
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
                db.query(Message).filter(Message.id == run.assistant_message_id).one()
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

    async def test_checkpoint_uses_local_signal_without_repolling_transport(self):
        transport = FakeCancellationTransport()
        registry = CancellationRegistry(transport=transport)
        token = await registry.register("run_local_hot_path")

        await token.checkpoint()
        await token.checkpoint()
        await token.checkpoint()

        self.assertEqual(["run_local_hot_path"], transport.is_requested_calls)

    async def test_cancellation_reason_keeps_the_highest_priority(self):
        token = await self.registry.register("run_priority")

        token.request("shutdown")
        token.request("user")
        token.request("shutdown")
        token.request("ownership_lost")
        token.request("user")

        self.assertTrue(token.cancelled)
        self.assertEqual("ownership_lost", token.reason)

    async def test_completed_run_wins_cancel_cleanup_without_bubbling_conflict(self):
        reservation = self.create()
        claimed = self.service.claim_run(
            run_id=reservation.run.id,
            worker_id="worker-1",
        )
        completed_in_db = asyncio.Event()

        async def runner(_token):
            self.service.complete_run(
                run_id=claimed.id,
                content="already committed",
                fencing_token=claimed.fencing_token,
            )
            completed_in_db.set()
            await asyncio.sleep(60)
            return "late"

        task = asyncio.create_task(self.manager.execute(run=claimed, runner=runner))
        await completed_in_db.wait()

        self.assertTrue(await self.registry.request_cancel(claimed.id, propagate=False))
        await task
        completed = self.service.get_run(username="alice", run_id=claimed.id)
        self.assertEqual(RunStatus.SUCCEEDED, completed.status)

    async def test_cancel_route_does_not_signal_a_terminal_run(self):
        reservation = self.create()
        claimed = self.service.claim_run(
            run_id=reservation.run.id,
            worker_id="worker-1",
        )
        self.service.complete_run(
            run_id=claimed.id,
            content="done",
            fencing_token=claimed.fencing_token,
        )
        routes = importlib.import_module("backend.api.routes.runs")
        request_cancel = AsyncMock()

        with (
            patch.object(routes, "service", self.service),
            patch.object(
                routes.cancellation_registry, "request_cancel", request_cancel
            ),
        ):
            response = await routes.cancel_run(
                claimed.id,
                SimpleNamespace(username="alice"),
            )

        self.assertEqual(RunStatus.SUCCEEDED, response.status)
        request_cancel.assert_not_awaited()

    async def test_cancel_route_stops_active_task_even_if_db_is_already_cancelled(self):
        reservation = self.create()
        claimed = self.service.claim_run(
            run_id=reservation.run.id,
            worker_id="worker-1",
        )
        runner_started = asyncio.Event()

        async def runner(_token):
            runner_started.set()
            await asyncio.sleep(60)
            return "late"

        task = asyncio.create_task(self.manager.execute(run=claimed, runner=runner))
        await runner_started.wait()
        self.repository.finalize(
            run_id=claimed.id,
            target_status=RunStatus.CANCELLED,
            content="cancelled",
            fencing_token=claimed.fencing_token,
            error_code="RUN_CANCELLED",
            partial=True,
        )
        routes = importlib.import_module("backend.api.routes.runs")

        with (
            patch.object(routes, "service", self.service),
            patch.object(routes, "cancellation_registry", self.registry),
        ):
            response = await routes.cancel_run(
                claimed.id,
                SimpleNamespace(username="alice"),
            )

        await task
        self.assertTrue(task.done())
        self.assertEqual(RunStatus.CANCELLED, response.status)

    async def test_closed_request_context_keeps_provider_cancellation_probe(self):
        context = RunRequestContext.for_sync(user_id="alice", thread_id="thread-1")
        context.configure_provider_runtime(cancellation_probe=lambda: True)

        context.close()
        _, cancellation_probe = context.provider_runtime()

        self.assertIsNotNone(cancellation_probe)
        self.assertTrue(cancellation_probe())

    async def test_provider_failure_survives_run_persistence_and_terminal_event(self):
        reservation = self.create()
        claimed = self.service.claim_run(
            run_id=reservation.run.id,
            worker_id="worker-1",
        )

        async def runner(_token):
            try:
                raise RuntimeError("secret milvus response body")
            except RuntimeError as cause:
                raise ProviderError.from_code(
                    ProviderCode.VECTOR_STORE_UNAVAILABLE,
                    provider="milvus",
                    operation=ProviderOperation.VECTOR_SEARCH,
                    retry_after_seconds=1.25,
                    attempts=3,
                    max_attempts=3,
                ) from cause

        with patch("backend.runs.cancellation.logger.warning") as warning:
            await self.manager.execute(run=claimed, runner=runner)

        warning.assert_called_once_with(
            "Run execution failed run_id=%s error_code=%s",
            claimed.id,
            ErrorCode.VECTOR_STORE_UNAVAILABLE,
        )
        self.assertNotIn("secret milvus response body", str(warning.call_args))

        failed = self.service.get_run(username="alice", run_id=claimed.id)
        response = RunResponse.model_validate(failed.__dict__)
        self.assertEqual(RunStatus.FAILED, failed.status)
        self.assertEqual("VECTOR_STORE_UNAVAILABLE", failed.error_code)
        self.assertIsNotNone(response.error)
        self.assertEqual("向量检索服务暂时不可用，请稍后重试", response.error.message)
        self.assertTrue(response.error.retryable)
        self.assertEqual("provider", response.error.category)
        self.assertEqual("vector_search", response.error.stage)
        self.assertEqual("milvus", response.error.provider)
        self.assertEqual(1.25, response.error.retry_after)

        with self.Session() as db:
            run = db.query(Run).filter(Run.id == claimed.id).one()
            message = (
                db.query(Message).filter(Message.id == run.assistant_message_id).one()
            )
            terminal = (
                db.query(RunEvent)
                .filter(
                    RunEvent.run_id == run.id,
                    RunEvent.event_type == "run.failed",
                )
                .one()
            )
            self.assertNotIn("secret", message.content)
            self.assertNotIn("secret", str(terminal.payload_json))
            self.assertEqual(
                "VECTOR_STORE_UNAVAILABLE",
                terminal.payload_json["error_code"],
            )
            self.assertEqual("milvus", terminal.payload_json["error"]["provider"])

    async def test_durable_cancelling_wins_provider_failure_race(self):
        reservation = self.create()
        claimed = self.service.claim_run(
            run_id=reservation.run.id,
            worker_id="worker-1",
        )
        started = asyncio.Event()
        release = asyncio.Event()

        async def runner(_token):
            started.set()
            await release.wait()
            try:
                raise RuntimeError("secret provider failure after cancellation")
            except RuntimeError as cause:
                raise ProviderError.from_code(
                    ProviderCode.MODEL_UNAVAILABLE,
                    provider="answer-model",
                    operation=ProviderOperation.MODEL,
                ) from cause

        task = self.manager.spawn(run=claimed, runner=runner)
        await started.wait()
        cancelling = self.service.request_cancel(
            username="alice",
            run_id=claimed.id,
        )
        self.assertEqual(RunStatus.CANCELLING, cancelling.status)

        with patch("backend.runs.cancellation.logger.warning") as warning:
            release.set()
            await task

        terminal = self.service.get_run(username="alice", run_id=claimed.id)
        self.assertEqual(RunStatus.CANCELLED, terminal.status)
        self.assertEqual("RUN_CANCELLED", terminal.error_code)
        self.assertEqual("运行已由用户取消。", terminal.error["message"])
        warning.assert_called_once_with(
            "Run execution failed run_id=%s error_code=%s",
            claimed.id,
            ErrorCode.MODEL_UNAVAILABLE,
        )
        self.assertNotIn("secret provider failure", str(warning.call_args))
        with self.Session() as db:
            self.assertEqual(
                1,
                db.query(RunEvent)
                .filter(
                    RunEvent.run_id == claimed.id,
                    RunEvent.event_type == "run.cancelled",
                )
                .count(),
            )
            self.assertEqual(
                0,
                db.query(RunEvent)
                .filter(
                    RunEvent.run_id == claimed.id,
                    RunEvent.event_type == "run.failed",
                )
                .count(),
            )

    async def test_unknown_failure_uses_canonical_run_execution_failed_code(self):
        reservation = self.create()
        claimed = self.service.claim_run(
            run_id=reservation.run.id,
            worker_id="worker-1",
        )

        async def runner(_token):
            raise RuntimeError("secret internal implementation detail")

        await self.manager.execute(run=claimed, runner=runner)

        failed = self.service.get_run(username="alice", run_id=claimed.id)
        self.assertEqual("RUN_EXECUTION_FAILED", failed.error_code)
        self.assertEqual("运行失败，请稍后重试。", failed.error["message"])
        self.assertNotIn("secret", str(failed.error))


if __name__ == "__main__":
    unittest.main()
