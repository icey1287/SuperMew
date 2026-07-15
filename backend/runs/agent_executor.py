from __future__ import annotations

import asyncio
import logging
import os
import socket
from datetime import UTC, datetime
from uuid import uuid4

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from backend.agent.factory import AgentRuntimeFactory, runtime_factory
from backend.agent.runtime import AgentRuntimeInput, AgentRuntimeResult
from backend.chat.request_context import ChatRequestContext
from backend.core.errors import AppError, ErrorCode
from backend.core.settings import get_settings
from backend.events.bus import PersistentEventBus, event_bus
from backend.events.generated.run_event_v1 import RunEventType
from backend.rag.checkpoint_runner import (
    CheckpointedRagRunner,
    checkpointed_rag_runner,
)
from backend.runs.cancellation import (
    CancellationToken,
    RunExecutionManager,
    RunExecutionOutcome,
    execution_manager,
)
from backend.runs.repository import RunExecutionSnapshot, RunRecord, RunRepository
from backend.runs.service import RunService, service
from backend.runs.state import RunStatus
from backend.tools.knowledge import make_checkpointed_search_knowledge_base


logger = logging.getLogger(__name__)


_TRACE_EVENT_TYPES = {
    "tool.completed": RunEventType.TOOL_COMPLETED,
    "tool.failed": RunEventType.TOOL_FAILED,
    "tool.denied": RunEventType.TOOL_DENIED,
}
_WARNING_TRACE_STAGES = {
    "context.trimmed",
    "terminal.fallback",
    "tool.loop_blocked",
}


def _history_messages(snapshot: RunExecutionSnapshot) -> list:
    messages = []
    for item in snapshot.history:
        if item.role == "human":
            messages.append(HumanMessage(content=item.content))
        elif item.role == "ai":
            messages.append(AIMessage(content=item.content))
        elif item.role == "system":
            messages.append(SystemMessage(content=item.content))
    return messages


def _remaining_deadline(deadline_at: str | None) -> float | None:
    if not deadline_at:
        return None
    deadline = datetime.fromisoformat(deadline_at)
    if deadline.tzinfo is not None:
        deadline = deadline.astimezone(UTC).replace(tzinfo=None)
    now = datetime.now(UTC).replace(tzinfo=None)
    return max((deadline - now).total_seconds(), 0.0)


def _resume_answer_prompt(result: dict, answer: str) -> str | None:
    docs = list(result.get("docs") or [])
    trace = dict(result.get("rag_trace") or {})
    status = result.get("retrieval_status") or trace.get("retrieval_status")
    route = result.get("route") or trace.get("route")
    if status == "no_knowledge" or route == "no_knowledge" or not docs:
        return None
    chunks = []
    for index, item in enumerate(docs, 1):
        chunks.append(
            f"[{index}] {item.get('filename', 'Unknown')} "
            f"(Page {item.get('page_number', 'N/A')}):\n"
            f"{item.get('text', '')}"
        )
    original_question = result.get("original_question") or result.get("question") or ""
    return (
        "请只根据下面的检索片段回答原始问题，并使用 [1]、[2] 形式引用。"
        "不要再次调用工具，也不要提及内部 HITL 或 RAG 实现。\n\n"
        f"原始问题：\n{original_question}\n\n"
        f"用户补充：\n{answer}\n\n"
        "检索片段：\n" + "\n\n---\n\n".join(chunks)
    )


class RunAgentExecutor:
    """Owns the complete Run → AgentRuntime → Event → finalize execution seam."""

    def __init__(
        self,
        *,
        run_service: RunService = service,
        runtime_builder: AgentRuntimeFactory = runtime_factory,
        events: PersistentEventBus = event_bus,
        manager: RunExecutionManager = execution_manager,
        worker_id: str | None = None,
        heartbeat_seconds: float | None = None,
        max_concurrent_runs: int | None = None,
        checkpoint_runner: CheckpointedRagRunner = checkpointed_rag_runner,
    ) -> None:
        settings = get_settings().worker
        self.service = run_service
        self.repository: RunRepository = run_service.repository
        self.runtime_builder = runtime_builder
        self.events = events
        self.manager = manager
        self.checkpoint_runner = checkpoint_runner
        worker_prefix = settings.worker_id or "api"
        self.worker_id = worker_id or (
            f"{worker_prefix}-{socket.gethostname()}-{os.getpid()}-{uuid4().hex[:12]}"
        )
        self.heartbeat_seconds = heartbeat_seconds or settings.heartbeat_seconds
        self._semaphore = asyncio.Semaphore(
            max_concurrent_runs or settings.max_concurrent_runs
        )
        self._tasks: dict[str, asyncio.Task] = {}
        self._task_kinds: dict[str, str] = {}
        self._pending_resumes: dict[str, dict] = {}
        self._lock = asyncio.Lock()
        self._closing = False
        self._dispatcher_stop = asyncio.Event()
        self._dispatcher_task: asyncio.Task | None = None

    async def spawn_once(
        self,
        *,
        username: str,
        run_id: str,
    ) -> asyncio.Task | None:
        async with self._lock:
            if self._closing:
                return None
            existing = self._tasks.get(run_id)
            if existing is not None and not existing.done():
                return existing

            async def managed() -> None:
                pending_resume = None
                should_schedule_resume = False
                try:
                    async with self._semaphore:
                        await self.execute(username=username, run_id=run_id)
                except Exception:
                    logger.exception("Run agent executor failed run_id=%s", run_id)
                finally:
                    async with self._lock:
                        if self._tasks.get(run_id) is asyncio.current_task():
                            self._tasks.pop(run_id, None)
                            self._task_kinds.pop(run_id, None)
                            pending_resume = self._pending_resumes.pop(run_id, None)
                            should_schedule_resume = not self._closing
                    if pending_resume is not None and should_schedule_resume:
                        await self.resume_once(**pending_resume)

            task = asyncio.create_task(managed(), name=f"run-agent:{run_id}")
            self._tasks[run_id] = task
            self._task_kinds[run_id] = "execute"
            return task

    async def resume_once(
        self,
        *,
        username: str,
        run_id: str,
        hitl_token: str,
        answer: str,
        idempotency_key: str,
    ) -> asyncio.Task | None:
        async with self._lock:
            if self._closing:
                return None
            existing = self._tasks.get(run_id)
            if existing is not None and not existing.done():
                if self._task_kinds.get(run_id) == "execute":
                    self._pending_resumes[run_id] = {
                        "username": username,
                        "run_id": run_id,
                        "hitl_token": hitl_token,
                        "answer": answer,
                        "idempotency_key": idempotency_key,
                    }
                return existing

            async def managed() -> None:
                try:
                    async with self._semaphore:
                        await self.resume(
                            username=username,
                            run_id=run_id,
                            hitl_token=hitl_token,
                            answer=answer,
                            idempotency_key=idempotency_key,
                        )
                except Exception:
                    logger.exception("Run HITL resume failed run_id=%s", run_id)
                finally:
                    async with self._lock:
                        if self._tasks.get(run_id) is asyncio.current_task():
                            self._tasks.pop(run_id, None)
                            self._task_kinds.pop(run_id, None)

            task = asyncio.create_task(managed(), name=f"run-resume:{run_id}")
            self._tasks[run_id] = task
            self._task_kinds[run_id] = "resume"
            return task

    async def execute(self, *, username: str, run_id: str) -> None:
        current = await asyncio.to_thread(
            self.service.get_run,
            username=username,
            run_id=run_id,
        )
        if current.status != RunStatus.PENDING.value:
            return
        try:
            claimed = await asyncio.to_thread(
                self.service.claim_run,
                run_id=run_id,
                worker_id=self.worker_id,
            )
        except AppError as exc:
            if exc.code in {ErrorCode.RUN_ACTIVE, ErrorCode.RUN_STATE_CONFLICT}:
                return
            raise

        async def runner(token: CancellationToken) -> RunExecutionOutcome:
            snapshot = await asyncio.to_thread(
                self.repository.load_execution_snapshot,
                username=username,
                run_id=claimed.id,
                worker_id=self.worker_id,
                fencing_token=claimed.fencing_token,
            )
            return await self._run_runtime(snapshot=snapshot, token=token)

        await self._execute_claimed(claimed, runner)
        await self._dispatch_next(username=username, thread_id=claimed.thread_id)

    async def resume(
        self,
        *,
        username: str,
        run_id: str,
        hitl_token: str,
        answer: str,
        idempotency_key: str,
    ) -> None:
        consumed = await asyncio.to_thread(
            self.checkpoint_runner.checkpoints.consume_resume,
            username=username,
            run_id=run_id,
            hitl_token=hitl_token,
            answer=answer,
            idempotency_key=idempotency_key,
            worker_id=self.worker_id,
        )
        if not consumed.should_resume:
            return
        claimed = await asyncio.to_thread(
            self.repository.get_internal,
            run_id=run_id,
        )

        async def runner(token: CancellationToken) -> RunExecutionOutcome:
            snapshot = await asyncio.to_thread(
                self.repository.load_execution_snapshot,
                username=username,
                run_id=run_id,
                worker_id=self.worker_id,
                fencing_token=consumed.fencing_token,
            )
            rag_outcome = await self._resume_checkpoint(
                snapshot=snapshot,
                consumed=consumed,
            )
            if rag_outcome.pause is not None:
                return RunExecutionOutcome(
                    kind="waiting_input",
                    fencing_token=rag_outcome.fencing_token,
                )
            trace = dict(rag_outcome.result.get("rag_trace") or {})
            prompt = _resume_answer_prompt(rag_outcome.result, consumed.answer)
            if prompt is None:
                return RunExecutionOutcome(
                    kind="completed",
                    content=(
                        "知识库中没有找到可靠的相关信息，"
                        "暂时无法基于知识库回答这个问题。"
                    ),
                    rag_trace=trace,
                    fencing_token=rag_outcome.fencing_token,
                )
            return await self._run_runtime(
                snapshot=snapshot,
                token=token,
                user_text=prompt,
                disable_tools=True,
                initial_rag_trace=trace,
            )

        await self._execute_claimed(claimed, runner)
        await self._dispatch_next(username=username, thread_id=claimed.thread_id)

    async def _resume_checkpoint(self, *, snapshot, consumed):
        output_queue: asyncio.Queue = asyncio.Queue()
        context = ChatRequestContext.for_stream(
            user_id=snapshot.username,
            session_id=snapshot.run.thread_id,
            output_queue=output_queue,
        )
        pump_stop = asyncio.Event()
        pump_error = asyncio.Event()
        pump_task = asyncio.create_task(
            self._pump_rag_steps(
                snapshot.run,
                output_queue,
                pump_stop,
                pump_error,
            ),
            name=f"run-rag-resume-events:{snapshot.run.id}",
        )
        try:
            return await asyncio.to_thread(
                self.checkpoint_runner.resume_consumed,
                run_id=snapshot.run.id,
                consumed=consumed,
                context=context,
                worker_id=self.worker_id,
            )
        finally:
            context.close()
            pump_stop.set()
            await pump_task

    async def _execute_claimed(self, run: RunRecord, runner) -> None:
        heartbeat_stop = asyncio.Event()
        manager_task = asyncio.create_task(
            self.manager.execute(run=run, runner=runner),
            name=f"run-manager:{run.id}",
        )
        heartbeat_task = asyncio.create_task(
            self._heartbeat_loop(run, heartbeat_stop),
            name=f"run-heartbeat:{run.id}",
        )
        try:
            await manager_task
        finally:
            heartbeat_stop.set()
            await heartbeat_task

    async def _heartbeat_loop(
        self,
        run: RunRecord,
        stop_event: asyncio.Event,
    ) -> None:
        while not stop_event.is_set():
            try:
                await asyncio.wait_for(
                    stop_event.wait(),
                    timeout=self.heartbeat_seconds,
                )
                return
            except TimeoutError:
                pass
            try:
                await asyncio.to_thread(
                    self.service.heartbeat,
                    run_id=run.id,
                    worker_id=self.worker_id,
                    fencing_token=run.fencing_token,
                )
            except AppError as exc:
                if exc.code == ErrorCode.RUN_STATE_CONFLICT:
                    await self.manager.registry.cancel_local(
                        run.id,
                        reason="ownership_lost",
                    )
                    return
                logger.warning(
                    "Run heartbeat rejected run_id=%s code=%s",
                    run.id,
                    exc.code,
                )
            except Exception:
                logger.exception("Run heartbeat failed run_id=%s", run.id)

    async def _run_runtime(
        self,
        *,
        snapshot: RunExecutionSnapshot,
        token: CancellationToken,
        user_text: str | None = None,
        disable_tools: bool = False,
        initial_rag_trace: dict | None = None,
    ) -> RunExecutionOutcome:
        output_queue: asyncio.Queue = asyncio.Queue()
        trace_queue: asyncio.Queue = asyncio.Queue()
        request_context = ChatRequestContext.for_stream(
            user_id=snapshot.username,
            session_id=snapshot.run.thread_id,
            output_queue=output_queue,
        )
        if initial_rag_trace:
            request_context.store_rag_trace(initial_rag_trace)
        knowledge_tool = None
        if not disable_tools:
            knowledge_tool = make_checkpointed_search_knowledge_base(
                request_context,
                run_id=snapshot.run.id,
                worker_id=self.worker_id,
                fencing_token=snapshot.run.fencing_token,
                runner=self.checkpoint_runner,
            )
        runtime = self.runtime_builder.create(
            request_context,
            persistent_note=snapshot.persistent_note,
            run_id=snapshot.run.id,
            allowed_tools=frozenset() if disable_tools else None,
            deadline_seconds=_remaining_deadline(snapshot.run.deadline_at),
            knowledge_tool=knowledge_tool,
            trace_queue=trace_queue,
        )
        result: AgentRuntimeResult | None = None
        pump_stop = asyncio.Event()
        pump_error = asyncio.Event()
        pump_task = asyncio.create_task(
            self._pump_rag_steps(
                snapshot.run,
                output_queue,
                pump_stop,
                pump_error,
            ),
            name=f"run-rag-events:{snapshot.run.id}",
        )
        trace_pump_task = asyncio.create_task(
            self._pump_runtime_trace(
                snapshot.run,
                trace_queue,
                pump_stop,
                pump_error,
            ),
            name=f"run-trace-events:{snapshot.run.id}",
        )
        try:
            async for runtime_event in runtime.astream(
                AgentRuntimeInput(
                    history=_history_messages(snapshot),
                    user_text=user_text or snapshot.user_text,
                )
            ):
                await token.checkpoint()
                if runtime_event.type == "content" and runtime_event.content:
                    await asyncio.sleep(0)
                    await self._flush_event_queues(
                        output_queue,
                        trace_queue,
                        pump_error,
                    )
                    token.append_partial(runtime_event.content)
                    await self._publish_owned(
                        snapshot.run,
                        event_type=RunEventType.MESSAGE_DELTA,
                        data={
                            "message_id": snapshot.run.assistant_message_id,
                            "content": runtime_event.content,
                        },
                    )
                elif runtime_event.result is not None:
                    result = runtime_event.result
            if result is None:
                raise RuntimeError("AgentRuntime did not produce a completed result")
            if result.checkpoint_pause is not None:
                return RunExecutionOutcome(
                    kind="waiting_input",
                    fencing_token=snapshot.run.fencing_token,
                )
            return RunExecutionOutcome(
                kind="completed",
                content=result.content,
                rag_trace=result.rag_trace,
            )
        finally:
            request_context.close()
            pump_stop.set()
            await asyncio.gather(pump_task, trace_pump_task)

    async def _pump_rag_steps(
        self,
        run: RunRecord,
        output_queue: asyncio.Queue,
        stop_event: asyncio.Event,
        error_event: asyncio.Event,
    ) -> None:
        while True:
            try:
                item = await asyncio.wait_for(output_queue.get(), timeout=0.1)
            except TimeoutError:
                if stop_event.is_set() and output_queue.empty():
                    return
                continue
            try:
                if item.get("type") != "rag_step":
                    continue
                await self._publish_owned(
                    run,
                    event_type=RunEventType.TOOL_PROGRESS,
                    data={
                        "tool_name": "search_knowledge_base",
                        "step": item.get("step") or {},
                    },
                )
            except AppError as exc:
                if exc.code == ErrorCode.RUN_STATE_CONFLICT:
                    error_event.set()
                    return
                raise
            finally:
                output_queue.task_done()

    async def _pump_runtime_trace(
        self,
        run: RunRecord,
        trace_queue: asyncio.Queue,
        stop_event: asyncio.Event,
        error_event: asyncio.Event,
    ) -> None:
        while True:
            try:
                item = await asyncio.wait_for(trace_queue.get(), timeout=0.1)
            except TimeoutError:
                if stop_event.is_set() and trace_queue.empty():
                    return
                continue
            stage = str(item.get("stage") or "")
            event_type = _TRACE_EVENT_TYPES.get(stage)
            try:
                if event_type is not None:
                    await self._publish_owned(
                        run,
                        event_type=event_type,
                        data=dict(item),
                    )
                elif stage in _WARNING_TRACE_STAGES:
                    await self._publish_owned(
                        run,
                        event_type=RunEventType.WARNING_CREATED,
                        data={"code": stage.upper().replace(".", "_"), **item},
                    )
            except AppError as exc:
                if exc.code == ErrorCode.RUN_STATE_CONFLICT:
                    error_event.set()
                    return
                raise
            finally:
                trace_queue.task_done()

    @staticmethod
    async def _flush_event_queues(
        output_queue: asyncio.Queue,
        trace_queue: asyncio.Queue,
        error_event: asyncio.Event,
    ) -> None:
        joins = asyncio.gather(output_queue.join(), trace_queue.join())
        failed = asyncio.create_task(error_event.wait())
        done, _ = await asyncio.wait(
            {joins, failed},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if failed in done and error_event.is_set():
            joins.cancel()
            await asyncio.gather(joins, return_exceptions=True)
            raise AppError(
                ErrorCode.RUN_STATE_CONFLICT,
                "当前 worker 已失去运行事件写权限",
                status_code=409,
            )
        failed.cancel()
        await asyncio.gather(failed, return_exceptions=True)
        await joins

    async def _publish_owned(
        self,
        run: RunRecord,
        *,
        event_type: RunEventType,
        data: dict,
    ) -> None:
        await self.events.publish(
            run_id=run.id,
            event_type=event_type,
            data=data,
            worker_id=self.worker_id,
            fencing_token=run.fencing_token,
        )

    async def _dispatch_next(self, *, username: str, thread_id: str) -> None:
        next_run = await asyncio.to_thread(
            self.repository.find_pending,
            username=username,
            thread_id=thread_id,
        )
        if next_run is None:
            return
        task = await self.spawn_once(username=username, run_id=next_run.id)
        if task is None:
            logger.warning("Pending Run was not dispatched run_id=%s", next_run.id)

    async def close(self) -> None:
        async with self._lock:
            self._closing = True
            self._dispatcher_stop.set()
            dispatcher = self._dispatcher_task
            self._dispatcher_task = None
            active = [
                (run_id, task)
                for run_id, task in self._tasks.items()
                if not task.done()
            ]
        if dispatcher is not None and not dispatcher.done():
            dispatcher.cancel()
            await asyncio.gather(dispatcher, return_exceptions=True)
        for run_id, task in active:
            interrupted = await self.manager.registry.cancel_local(
                run_id,
                reason="shutdown",
            )
            if not interrupted and not task.done():
                task.cancel()
        tasks = [task for _, task in active]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def start(self) -> None:
        async with self._lock:
            self._closing = False
            if self._dispatcher_stop.is_set():
                self._dispatcher_stop = asyncio.Event()
            if self._dispatcher_task is None or self._dispatcher_task.done():
                self._dispatcher_task = asyncio.create_task(
                    self._dispatch_loop(),
                    name=f"run-dispatcher:{self.worker_id}",
                )
        await self._recover_once()

    async def _dispatch_loop(self) -> None:
        interval = max(min(float(self.heartbeat_seconds), 5.0), 1.0)
        while not self._dispatcher_stop.is_set():
            try:
                await asyncio.wait_for(
                    self._dispatcher_stop.wait(),
                    timeout=interval,
                )
                return
            except TimeoutError:
                pass
            try:
                await self._recover_once()
            except Exception:
                logger.exception("Run dispatcher recovery pass failed")

    async def _recover_once(self) -> None:
        await asyncio.to_thread(self.service.reconcile_orphans)
        resumes = await asyncio.to_thread(
            self.checkpoint_runner.checkpoints.list_pending_resumes
        )
        for item in resumes:
            await self.resume_once(
                username=item.username,
                run_id=item.run_id,
                hitl_token=item.hitl_token,
                answer=item.answer,
                idempotency_key=item.idempotency_key,
            )
        pending = await asyncio.to_thread(self.repository.list_pending)
        for username, run in pending:
            await self.spawn_once(username=username, run_id=run.id)


run_agent_executor = RunAgentExecutor()
