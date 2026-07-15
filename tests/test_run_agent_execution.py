import asyncio
import sys
import unittest
from contextlib import contextmanager
from unittest.mock import patch

from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.agent.runtime import (
    AgentRuntimeEvent,
    AgentRuntimeResult,
)
from backend.db.models import Base, ChatMessage, Run, User
from backend.events.bus import PersistentEventBus
from backend.events.journal import RunEventJournal
from backend.rag.checkpoint_runner import (
    CheckpointedRagRunner,
    HitlCheckpointRepository,
)
from backend.runs.agent_executor import RunAgentExecutor
from backend.runs.cancellation import CancellationRegistry, RunExecutionManager
from backend.runs.repository import RunRepository
from backend.runs.resume import RunResumeCoordinator
from backend.runs.service import RunService
from backend.runs.state import MultitaskStrategy
from test_native_checkpoint_hitl import NativeCheckpointGraphTests


class FakeRuntime:
    def __init__(self, factory, trace_queue=None):
        self.factory = factory
        self.trace_queue = trace_queue

    async def astream(self, request):
        self.factory.requests.append(request)
        self.factory.active += 1
        self.factory.max_active = max(self.factory.max_active, self.factory.active)
        try:
            if self.factory.emit_tool_trace and self.trace_queue is not None:
                await self.trace_queue.put(
                    {
                        "stage": "tool.completed",
                        "tool_name": "fake_tool",
                        "elapsed_ms": 1,
                    }
                )
                await asyncio.sleep(0)
            if self.factory.delay_seconds:
                await asyncio.sleep(self.factory.delay_seconds)
            for index, chunk in enumerate(("你", "好")):
                yield AgentRuntimeEvent(type="content", content=chunk)
                if index == 0 and self.factory.release_after_first is not None:
                    self.factory.first_chunk_published.set()
                    await self.factory.release_after_first.wait()
            yield AgentRuntimeEvent(
                type="completed",
                result=AgentRuntimeResult(
                    content="你好",
                    rag_trace=None,
                    hitl_resume_state=None,
                    runtime_trace=(),
                ),
            )
        finally:
            self.factory.active -= 1


class FakeRuntimeFactory:
    def __init__(self):
        self.requests = []
        self.create_kwargs = []
        self.delay_seconds = 0.0
        self.first_chunk_published = asyncio.Event()
        self.release_after_first: asyncio.Event | None = None
        self.active = 0
        self.max_active = 0
        self.emit_tool_trace = False

    def create(self, request_context, **kwargs):
        self.create_kwargs.append({"request_context": request_context, **kwargs})
        return FakeRuntime(self, kwargs.get("trace_queue"))


class CheckpointRuntime:
    def __init__(self, factory, request_context, knowledge_tool):
        self.factory = factory
        self.request_context = request_context
        self.knowledge_tool = knowledge_tool

    async def astream(self, request):
        self.factory.requests.append(request)
        if self.knowledge_tool is not None:
            tool_result = await asyncio.to_thread(
                self.knowledge_tool.invoke,
                {"query": request.user_text},
            )
            self.factory.pause_recorded.set()
            if self.factory.release_initial is not None:
                await self.factory.release_initial.wait()
            stored = self.request_context.take_rag_trace() or {}
            yield AgentRuntimeEvent(
                type="completed",
                result=AgentRuntimeResult(
                    content=str(tool_result),
                    rag_trace=stored.get("rag_trace"),
                    hitl_resume_state=None,
                    runtime_trace=(),
                    checkpoint_pause=self.request_context.take_checkpoint_pause(),
                ),
            )
            return

        for chunk in ("恢复后的", "答案"):
            yield AgentRuntimeEvent(type="content", content=chunk)
        stored = self.request_context.take_rag_trace() or {}
        yield AgentRuntimeEvent(
            type="completed",
            result=AgentRuntimeResult(
                content="恢复后的答案",
                rag_trace=stored.get("rag_trace"),
                hitl_resume_state=None,
                runtime_trace=(),
            ),
        )


class CheckpointRuntimeFactory:
    def __init__(self):
        self.requests = []
        self.pause_recorded = asyncio.Event()
        self.release_initial: asyncio.Event | None = None

    def create(self, request_context, **kwargs):
        return CheckpointRuntime(
            self,
            request_context,
            kwargs.get("knowledge_tool"),
        )


class RunAgentExecutionTests(unittest.IsolatedAsyncioTestCase):
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
        self.service = RunService(self.repository)
        self.journal = RunEventJournal(self.Session)
        self.events = PersistentEventBus(self.journal, transport=None)
        self.registry = CancellationRegistry(transport=None)
        self.manager = RunExecutionManager(self.service, self.registry)
        self.saver = InMemorySaver()

        @contextmanager
        def saver_factory():
            yield self.saver

        self.checkpoints = HitlCheckpointRepository(self.Session)
        self.checkpoint_runner = CheckpointedRagRunner(
            saver_factory=saver_factory,
            checkpoint_repository=self.checkpoints,
        )
        self.runtime_factory = FakeRuntimeFactory()
        self.executor = RunAgentExecutor(
            run_service=self.service,
            runtime_builder=self.runtime_factory,
            events=self.events,
            manager=self.manager,
            worker_id="worker-agent-test",
            checkpoint_runner=self.checkpoint_runner,
        )

    async def asyncTearDown(self):
        await self.executor.close()
        self.engine.dispose()

    async def test_run_flows_through_runtime_events_and_atomic_finalize(self):
        reservation = self.service.create_run(
            username="alice",
            thread_id="thread-1",
            message="打个招呼",
            idempotency_key="request-1",
        )

        task = await self.executor.spawn_once(
            username="alice",
            run_id=reservation.run.id,
        )
        self.assertIsNotNone(task)
        await task

        with self.Session() as db:
            run = db.query(Run).filter(Run.id == reservation.run.id).one()
            assistant = (
                db.query(ChatMessage)
                .filter(ChatMessage.id == run.assistant_message_id)
                .one()
            )
            self.assertEqual("succeeded", run.status)
            self.assertEqual("你好", assistant.content)
            self.assertEqual("completed", assistant.status)

        events = self.journal.read_after(
            username="alice",
            run_id=reservation.run.id,
        )
        self.assertEqual(
            [
                "run.created",
                "run.started",
                "message.delta",
                "message.delta",
                "message.completed",
                "run.completed",
            ],
            [item.type.value for item in events],
        )
        self.assertEqual(list(range(1, 7)), [item.sequence for item in events])
        self.assertEqual(
            ["你", "好"],
            [item.data["content"] for item in events if item.type == "message.delta"],
        )
        self.assertEqual("打个招呼", self.runtime_factory.requests[0].user_text)
        self.assertEqual([], self.runtime_factory.requests[0].history)
        self.assertEqual(
            reservation.run.id,
            self.runtime_factory.create_kwargs[0]["run_id"],
        )
        self.assertGreater(
            self.runtime_factory.create_kwargs[0]["deadline_seconds"],
            0,
        )

        replay = self.service.create_run(
            username="alice",
            thread_id="thread-1",
            message="打个招呼",
            idempotency_key="request-1",
        )
        self.assertFalse(replay.created)
        repeated_task = await self.executor.spawn_once(
            username="alice",
            run_id=replay.run.id,
        )
        self.assertIsNotNone(repeated_task)
        await repeated_task
        self.assertEqual(
            6,
            len(
                self.journal.read_after(
                    username="alice",
                    run_id=reservation.run.id,
                )
            ),
        )

    async def test_process_worker_identity_is_unique_even_with_shared_prefix(self):
        first = RunAgentExecutor(
            run_service=self.service,
            runtime_builder=self.runtime_factory,
            events=self.events,
            manager=self.manager,
            checkpoint_runner=self.checkpoint_runner,
        )
        second = RunAgentExecutor(
            run_service=self.service,
            runtime_builder=self.runtime_factory,
            events=self.events,
            manager=self.manager,
            checkpoint_runner=self.checkpoint_runner,
        )
        try:
            self.assertNotEqual(first.worker_id, second.worker_id)
        finally:
            await first.close()
            await second.close()

    async def test_executor_drains_promoted_queued_runs_in_order(self):
        first = self.service.create_run(
            username="alice",
            thread_id="thread-queue",
            message="第一条",
            idempotency_key="queue-1",
        )
        second = self.service.create_run(
            username="alice",
            thread_id="thread-queue",
            message="第二条",
            idempotency_key="queue-2",
            multitask_strategy=MultitaskStrategy.ENQUEUE,
        )

        task = await self.executor.spawn_once(
            username="alice",
            run_id=first.run.id,
        )
        self.assertIsNotNone(task)
        await task
        second_task = await self.executor.spawn_once(
            username="alice",
            run_id=second.run.id,
        )
        self.assertIsNotNone(second_task)
        await second_task

        self.assertEqual(
            "succeeded",
            self.service.get_run(username="alice", run_id=first.run.id).status,
        )
        self.assertEqual(
            "succeeded",
            self.service.get_run(username="alice", run_id=second.run.id).status,
        )
        self.assertEqual(
            ["第一条", "第二条"],
            [request.user_text for request in self.runtime_factory.requests],
        )

    async def test_executor_renews_lease_while_runtime_is_active(self):
        self.runtime_factory.delay_seconds = 0.05
        self.executor.heartbeat_seconds = 0.01
        reservation = self.service.create_run(
            username="alice",
            thread_id="thread-heartbeat",
            message="慢一点回答",
            idempotency_key="heartbeat-1",
        )

        with patch.object(
            self.service,
            "heartbeat",
            wraps=self.service.heartbeat,
        ) as heartbeat:
            task = await self.executor.spawn_once(
                username="alice",
                run_id=reservation.run.id,
            )
            self.assertIsNotNone(task)
            await task

        self.assertGreaterEqual(heartbeat.call_count, 1)

    async def test_executor_limits_cross_thread_runtime_concurrency(self):
        self.runtime_factory.delay_seconds = 0.05
        self.executor._semaphore = asyncio.Semaphore(1)
        first = self.service.create_run(
            username="alice",
            thread_id="thread-limit-1",
            message="第一条并发任务",
            idempotency_key="limit-1",
        )
        second = self.service.create_run(
            username="alice",
            thread_id="thread-limit-2",
            message="第二条并发任务",
            idempotency_key="limit-2",
        )

        first_task = await self.executor.spawn_once(
            username="alice",
            run_id=first.run.id,
        )
        second_task = await self.executor.spawn_once(
            username="alice",
            run_id=second.run.id,
        )
        self.assertIsNotNone(first_task)
        self.assertIsNotNone(second_task)
        await asyncio.gather(first_task, second_task)

        self.assertEqual(1, self.runtime_factory.max_active)

    async def test_runtime_trace_event_precedes_answer_delta(self):
        self.runtime_factory.emit_tool_trace = True
        reservation = self.service.create_run(
            username="alice",
            thread_id="thread-trace-order",
            message="先调用工具",
            idempotency_key="trace-order-1",
        )
        task = await self.executor.spawn_once(
            username="alice",
            run_id=reservation.run.id,
        )
        self.assertIsNotNone(task)
        await task

        event_types = [
            item.type.value
            for item in self.journal.read_after(
                username="alice",
                run_id=reservation.run.id,
            )
        ]
        self.assertLess(
            event_types.index("tool.completed"),
            event_types.index("message.delta"),
        )

    async def test_owned_event_append_rejects_stale_writer_after_terminal(self):
        self.runtime_factory.release_after_first = asyncio.Event()
        reservation = self.service.create_run(
            username="alice",
            thread_id="thread-stale-writer",
            message="先输出一半",
            idempotency_key="stale-writer-1",
        )
        task = await self.executor.spawn_once(
            username="alice",
            run_id=reservation.run.id,
        )
        self.assertIsNotNone(task)
        await self.runtime_factory.first_chunk_published.wait()
        running = self.service.get_run(
            username="alice",
            run_id=reservation.run.id,
        )

        self.service.fail_run(
            run_id=running.id,
            error_code="ORPHAN_RUN",
            message="运行已由新 owner 回收。",
            fencing_token=running.fencing_token,
            partial=True,
        )
        self.runtime_factory.release_after_first.set()
        await task

        events = self.journal.read_after(
            username="alice",
            run_id=running.id,
        )
        event_types = [item.type.value for item in events]
        self.assertEqual("run.failed", event_types[-1])
        self.assertEqual(1, event_types.count("message.delta"))

    async def test_shutdown_is_interrupted_and_start_recovers_promoted_pending(self):
        self.runtime_factory.delay_seconds = 60
        first = self.service.create_run(
            username="alice",
            thread_id="thread-restart",
            message="第一条慢任务",
            idempotency_key="restart-1",
        )
        second = self.service.create_run(
            username="alice",
            thread_id="thread-restart",
            message="第二条待恢复",
            idempotency_key="restart-2",
            multitask_strategy=MultitaskStrategy.ENQUEUE,
        )
        first_task = await self.executor.spawn_once(
            username="alice",
            run_id=first.run.id,
        )
        self.assertIsNotNone(first_task)
        while not self.runtime_factory.requests:
            await asyncio.sleep(0)

        await self.executor.close()

        interrupted = self.service.get_run(username="alice", run_id=first.run.id)
        promoted = self.service.get_run(username="alice", run_id=second.run.id)
        self.assertEqual("failed", interrupted.status)
        self.assertEqual("RUN_INTERRUPTED", interrupted.error_code)
        self.assertEqual("pending", promoted.status)

        self.runtime_factory.delay_seconds = 0
        await self.executor.start()
        recovered_task = await self.executor.spawn_once(
            username="alice",
            run_id=second.run.id,
        )
        self.assertIsNotNone(recovered_task)
        await recovered_task
        self.assertEqual(
            "succeeded",
            self.service.get_run(username="alice", run_id=second.run.id).status,
        )

    async def test_run_hitl_resumes_same_checkpoint_and_finalizes(self):
        pipeline, calls = NativeCheckpointGraphTests._pipeline(clarify_rounds=1)
        runtime_factory = CheckpointRuntimeFactory()
        self.executor.runtime_builder = runtime_factory
        coordinator = RunResumeCoordinator(
            checkpoints=self.checkpoints,
            run_service=self.service,
        )
        reservation = self.service.create_run(
            username="alice",
            thread_id="thread-hitl-runtime",
            message=NativeCheckpointGraphTests.QUESTION,
            idempotency_key="hitl-runtime-1",
        )

        with patch.dict(sys.modules, {"backend.rag.pipeline": pipeline}):
            task = await self.executor.spawn_once(
                username="alice",
                run_id=reservation.run.id,
            )
            self.assertIsNotNone(task)
            await task

            waiting = self.service.get_run(
                username="alice",
                run_id=reservation.run.id,
            )
            self.assertEqual("waiting_input", waiting.status)
            events = self.journal.read_after(
                username="alice",
                run_id=reservation.run.id,
            )
            hitl_event = next(item for item in events if item.type == "hitl.required")
            hitl_token = hitl_event.data["hitl_token"]

            accepted = coordinator.accept(
                username="alice",
                run_id=reservation.run.id,
                hitl_token=hitl_token,
                answer="丹瑾",
                idempotency_key="hitl-resume-1",
            )
            self.assertEqual("pending", accepted.run.status)
            await self.executor.close()
            await self.executor.start()
            resume_task = await self.executor.resume_once(
                username="alice",
                run_id=reservation.run.id,
                hitl_token=hitl_token,
                answer="丹瑾",
                idempotency_key="hitl-resume-1",
            )
            self.assertIsNotNone(resume_task)
            await resume_task

        completed = self.service.get_run(
            username="alice",
            run_id=reservation.run.id,
        )
        self.assertEqual("succeeded", completed.status)
        with self.Session() as db:
            assistant = (
                db.query(ChatMessage)
                .filter(ChatMessage.id == completed.assistant_message_id)
                .one()
            )
            self.assertEqual("恢复后的答案", assistant.content)
            self.assertEqual("completed", assistant.status)
        final_events = self.journal.read_after(
            username="alice",
            run_id=reservation.run.id,
        )
        final_event_types = [item.type.value for item in final_events]
        self.assertEqual("run.completed", final_event_types[-1])
        self.assertEqual(2, final_event_types.count("run.started"))
        self.assertEqual(1, final_event_types.count("hitl.resumed"))
        self.assertEqual(1, calls["complexity"])
        self.assertEqual(2, len(calls["retrieve"]))
        self.assertEqual(2, len(runtime_factory.requests))

    async def test_fast_hitl_reply_is_queued_after_initial_task(self):
        pipeline, calls = NativeCheckpointGraphTests._pipeline(clarify_rounds=1)
        runtime_factory = CheckpointRuntimeFactory()
        runtime_factory.release_initial = asyncio.Event()
        self.executor.runtime_builder = runtime_factory
        coordinator = RunResumeCoordinator(
            checkpoints=self.checkpoints,
            run_service=self.service,
        )
        reservation = self.service.create_run(
            username="alice",
            thread_id="thread-fast-hitl",
            message=NativeCheckpointGraphTests.QUESTION,
            idempotency_key="fast-hitl-1",
        )

        with patch.dict(sys.modules, {"backend.rag.pipeline": pipeline}):
            initial_task = await self.executor.spawn_once(
                username="alice",
                run_id=reservation.run.id,
            )
            self.assertIsNotNone(initial_task)
            await runtime_factory.pause_recorded.wait()
            events = self.journal.read_after(
                username="alice",
                run_id=reservation.run.id,
            )
            hitl_token = next(
                item.data["hitl_token"]
                for item in events
                if item.type == "hitl.required"
            )
            coordinator.accept(
                username="alice",
                run_id=reservation.run.id,
                hitl_token=hitl_token,
                answer="丹瑾",
                idempotency_key="fast-hitl-resume-1",
            )
            reused = await self.executor.resume_once(
                username="alice",
                run_id=reservation.run.id,
                hitl_token=hitl_token,
                answer="丹瑾",
                idempotency_key="fast-hitl-resume-1",
            )
            self.assertIs(initial_task, reused)
            runtime_factory.release_initial.set()
            await initial_task

            for _ in range(200):
                current = self.service.get_run(
                    username="alice",
                    run_id=reservation.run.id,
                )
                if current.status == "succeeded":
                    break
                await asyncio.sleep(0.01)

        self.assertEqual("succeeded", current.status)
        self.assertEqual(1, calls["complexity"])
        self.assertEqual(2, len(calls["retrieve"]))


if __name__ == "__main__":
    unittest.main()
