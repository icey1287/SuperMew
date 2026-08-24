from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import socket
import time
from collections.abc import Callable
from contextlib import AbstractContextManager, contextmanager
from datetime import UTC, datetime
from typing import Iterator, cast
from uuid import uuid4

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage

from backend.agent.factory import AgentRuntimeFactory
from backend.agent.runtime import AgentRuntimeInput, AgentRuntimeResult
from backend.capabilities.control_service import capability_control_service
from backend.runs.request_context import RunRequestContext
from backend.core.errors import AppError, ErrorCode
from backend.core.settings import get_settings
from backend.guardrails import RunToolApprovalGrant
from backend.events.bus import PersistentEventBus, event_bus
from backend.events.generated.run_event_v1 import RunEventType
from backend.rag.checkpoint_runner import (
    CheckpointedRagRunner,
    ConsumedResume,
    RagRunOutcome,
    ResumeAccessState,
    checkpointed_rag_runner,
)
from backend.rag.evidence import agent_evidence_character_budget, pack_evidence
from backend.rag.outcomes import (
    partial_evidence_instruction,
    retrieval_user_message,
)
from backend.runs.cancellation import (
    CancellationToken,
    Runner,
    RunExecutionManager,
    RunExecutionOutcome,
    RunLease,
    RunOwnership,
    execution_manager,
)
from backend.runs.repository import RunExecutionSnapshot, RunRecord, RunRepository
from backend.runs.service import RunService, service
from backend.runs.state import RunStatus
from backend.skills import ActivatedSkill, SkillPin
from backend.tools.contracts import ToolArtifactV1
from backend.tools.knowledge import make_checkpointed_search_knowledge_base


logger = logging.getLogger(__name__)


_TRACE_EVENT_TYPES = {
    "tool.started": RunEventType.TOOL_STARTED,
    "tool.completed": RunEventType.TOOL_COMPLETED,
    "tool.failed": RunEventType.TOOL_FAILED,
    "tool.denied": RunEventType.TOOL_DENIED,
}
_WARNING_TRACE_STAGES = {
    "context.trimmed",
    "model.failed",
    "terminal.fallback",
    "tool.loop_blocked",
    "web.citation_rejected",
    "web.context_rejected",
}

_EXPLICIT_WEB_INTENT = re.compile(
    r"(?:联网|上网|网上|网页|网络).{0,12}(?:查|查询|搜索|检索|搜|找)"
    r"|(?:search|look\s+up|find).{0,16}(?:the\s+)?web"
    r"|(?:web|online)\s+(?:search|lookup)",
    re.IGNORECASE,
)
_CURRENT_WEB_LOOKUP = re.compile(
    r"(?:查(?:一?下)?|查询|搜索|搜(?:一?下)?|找(?:一?下)?).{0,48}"
    r"(?:目前|当前|现在|最新|今日|今天).{0,24}"
    r"(?:版本|发布|发行|新闻|消息|价格|天气|状态|资料|信息)"
    r"|(?:search|look\s+up|check|find).{0,48}"
    r"(?:latest|current|today).{0,24}"
    r"(?:version|release|news|price|weather|status|information)",
    re.IGNORECASE,
)

_PUBLIC_ARTIFACT_FIELDS = frozenset(
    {
        "artifact_id",
        "name",
        "media_type",
        "uri",
        "size_bytes",
        "sha256",
    }
)


def _public_tool_event_data(stage: str, item: dict) -> dict:
    data: dict[str, object] = {"stage": stage}
    for field in ("tool_name", "tool_call_id", "error_code"):
        value = item.get(field)
        if isinstance(value, str) and value:
            data[field] = value
    for field in ("elapsed_ms", "duration_ms", "result_size"):
        value = item.get(field)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            data[field] = value
    for field in ("retryable", "fallback_applied"):
        value = item.get(field)
        if isinstance(value, bool):
            data[field] = value

    guardrail_audit = item.get("guardrail_audit")
    if isinstance(guardrail_audit, dict):
        decision = guardrail_audit.get("decision")
        reason_code = guardrail_audit.get("reason_code")
        if isinstance(decision, str) and decision:
            data["guardrail_decision"] = decision
        if isinstance(reason_code, str) and reason_code:
            data["reason_code"] = reason_code
    return data


def _public_artifact_descriptor(value: object) -> dict | None:
    if not isinstance(value, dict):
        return None
    candidate = {
        field: value[field] for field in _PUBLIC_ARTIFACT_FIELDS if field in value
    }
    try:
        artifact = ToolArtifactV1.model_validate(candidate)
    except ValueError:
        return None

    descriptor = artifact.model_dump(
        include=_PUBLIC_ARTIFACT_FIELDS,
        exclude_none=True,
    )
    # A display name is public metadata, never a host or container path.
    safe_name = artifact.name.replace("\\", "/").rsplit("/", 1)[-1]
    descriptor["name"] = safe_name or artifact.artifact_id
    if artifact.uri not in {
        None,
        f"artifact://{artifact.artifact_id}",
        f"/api/artifacts/{artifact.artifact_id}",
    }:
        descriptor.pop("uri", None)
    return descriptor


def _public_artifact_descriptors(value: object) -> list[dict]:
    if not isinstance(value, list):
        return []
    descriptors = (_public_artifact_descriptor(item) for item in value)
    return [item for item in descriptors if item is not None]


def _history_messages(snapshot: RunExecutionSnapshot) -> list[BaseMessage]:
    messages: list[BaseMessage] = []
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


def _routed_skill_for_user_text(user_text: str) -> str | None:
    text = " ".join(str(user_text or "").split())
    if not text or text.startswith("/"):
        return None
    if _EXPLICIT_WEB_INTENT.search(text) or _CURRENT_WEB_LOOKUP.search(text):
        return "web-research"
    return None


def _pinned_skill(run: RunRecord) -> SkillPin | None:
    values = (
        run.skill_name,
        run.skill_version,
        run.skill_content_hash,
        run.skill_activation_source,
    )
    if not any(values):
        return None
    if not all(values):
        raise RuntimeError("Run contains an incomplete Skill snapshot")
    return SkillPin(
        name=str(run.skill_name),
        version=str(run.skill_version),
        content_hash=str(run.skill_content_hash),
    )


def _resume_access_state(snapshot: RunExecutionSnapshot) -> ResumeAccessState:
    run = snapshot.run
    return ResumeAccessState(
        user_db_id=snapshot.user_db_id,
        username=snapshot.username,
        role=snapshot.role,
        skill_name=run.skill_name,
        skill_version=run.skill_version,
        skill_content_hash=run.skill_content_hash,
        skill_activation_source=run.skill_activation_source,
    )


def _resume_answer_prompt(result: dict, answer: str) -> str | None:
    docs = list(result.get("docs") or [])
    if not docs:
        return None
    evidence = pack_evidence(
        docs,
        maximum_characters=agent_evidence_character_budget(),
    )
    original_question = result.get("original_question") or result.get("question") or ""
    evidence_instruction = partial_evidence_instruction(result)
    return (
        "请只根据下面的检索片段回答原始问题，并使用 [1]、[2] 形式引用。"
        "不要再次调用工具，也不要提及内部 HITL 或 RAG 实现。\n\n"
        f"证据约束：{evidence_instruction or '检索证据覆盖完整。'}\n\n"
        f"原始问题：\n{original_question}\n\n"
        f"用户补充：\n{answer}\n\n"
        "检索片段：\n" + evidence.text
    )


class RunAgentExecutor:
    """Owns the complete Run → AgentRuntime → Event → finalize execution seam."""

    def __init__(
        self,
        *,
        run_service: RunService = service,
        runtime_builder: AgentRuntimeFactory
        | Callable[[], AbstractContextManager[AgentRuntimeFactory]] = (
            capability_control_service.acquire_factory
        ),
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
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._task_kinds: dict[str, str] = {}
        self._pending_resumes: dict[str, dict] = {}
        self._lock = asyncio.Lock()
        self._closing = False
        self._dispatcher_stop = asyncio.Event()
        self._dispatcher_task: asyncio.Task[None] | None = None

    @contextmanager
    def _runtime_scope(self) -> Iterator[AgentRuntimeFactory]:
        if hasattr(self.runtime_builder, "create"):
            yield cast(AgentRuntimeFactory, self.runtime_builder)
            return
        with self.runtime_builder() as runtime_factory:
            yield runtime_factory

    @staticmethod
    def _runtime_tool_ceiling(runtime_factory: AgentRuntimeFactory) -> frozenset[str]:
        ceiling: object = getattr(runtime_factory, "tool_ceiling", frozenset())
        if isinstance(ceiling, (set, frozenset, tuple, list)):
            return frozenset(str(name) for name in ceiling)
        return frozenset()

    async def spawn_once(
        self,
        *,
        username: str,
        run_id: str,
    ) -> asyncio.Task[None] | None:
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
    ) -> asyncio.Task[None] | None:
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
            with self._runtime_scope() as runtime_factory:
                snapshot = await self._load_execution_snapshot(
                    username=username,
                    run_id=claimed.id,
                    fencing_token=claimed.fencing_token,
                )
                return await self._run_runtime(
                    snapshot=snapshot,
                    token=token,
                    runtime_factory=runtime_factory,
                )

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
        with self._runtime_scope() as runtime_factory:
            consumed = await asyncio.to_thread(
                self.checkpoint_runner.checkpoints.consume_resume,
                username=username,
                run_id=run_id,
                hitl_token=hitl_token,
                answer=answer,
                idempotency_key=idempotency_key,
                worker_id=self.worker_id,
                preflight=runtime_factory.validate_resume_access,
            )
            if not consumed.should_resume:
                return
            lease = RunLease(id=run_id, fencing_token=consumed.fencing_token)

            async def runner(token: CancellationToken) -> RunExecutionOutcome:
                snapshot = await self._load_execution_snapshot(
                    username=username,
                    run_id=run_id,
                    fencing_token=consumed.fencing_token,
                )
                runtime_factory.validate_resume_access(_resume_access_state(snapshot))
                rag_outcome = await self._resume_checkpoint(
                    snapshot=snapshot,
                    consumed=consumed,
                    token=token,
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
                        content=retrieval_user_message(rag_outcome.result)
                        or "当前没有可用于回答的可靠证据。",
                        rag_trace=trace,
                        fencing_token=rag_outcome.fencing_token,
                    )
                return await self._run_runtime(
                    snapshot=snapshot,
                    token=token,
                    runtime_factory=runtime_factory,
                    user_text=prompt,
                    disable_tools=True,
                    initial_rag_trace=trace,
                )

            await self._execute_claimed(lease, runner)
        await self._dispatch_next(username=username, thread_id=consumed.thread_id)

    async def _load_execution_snapshot(
        self,
        *,
        username: str,
        run_id: str,
        fencing_token: int,
    ) -> RunExecutionSnapshot:
        return await asyncio.to_thread(
            self.repository.load_execution_snapshot,
            username=username,
            run_id=run_id,
            worker_id=self.worker_id,
            fencing_token=fencing_token,
        )

    async def _resume_checkpoint(
        self,
        *,
        snapshot: RunExecutionSnapshot,
        consumed: ConsumedResume,
        token: CancellationToken,
    ) -> RagRunOutcome:
        output_queue: asyncio.Queue = asyncio.Queue()
        context = RunRequestContext.for_stream(
            user_id=snapshot.username,
            thread_id=snapshot.run.thread_id,
            output_queue=output_queue,
            model_snapshot=snapshot.model_snapshot,
            tenant_id=snapshot.tenant_id,
        )
        remaining_deadline = _remaining_deadline(snapshot.run.deadline_at)
        context.configure_provider_runtime(
            deadline_at=(
                time.monotonic() + remaining_deadline
                if remaining_deadline is not None
                else None
            ),
            cancellation_probe=lambda: token.cancelled,
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

    async def _execute_claimed(self, run: RunOwnership, runner: Runner) -> None:
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
        run: RunOwnership,
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
                heartbeat = await asyncio.to_thread(
                    self.service.heartbeat,
                    run_id=run.id,
                    worker_id=self.worker_id,
                    fencing_token=run.fencing_token,
                )
                if heartbeat.status == RunStatus.CANCELLING.value:
                    await self.manager.registry.cancel_local(
                        run.id,
                        reason="user",
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
        runtime_factory: AgentRuntimeFactory,
        user_text: str | None = None,
        disable_tools: bool = False,
        initial_rag_trace: dict | None = None,
    ) -> RunExecutionOutcome:
        output_queue: asyncio.Queue = asyncio.Queue()
        trace_queue: asyncio.Queue = asyncio.Queue()
        request_context = RunRequestContext.for_stream(
            user_id=snapshot.username,
            thread_id=snapshot.run.thread_id,
            output_queue=output_queue,
            model_snapshot=snapshot.model_snapshot,
            tenant_id=snapshot.tenant_id,
        )
        remaining_deadline = _remaining_deadline(snapshot.run.deadline_at)
        request_context.configure_provider_runtime(
            deadline_at=(
                time.monotonic() + remaining_deadline
                if remaining_deadline is not None
                else None
            ),
            cancellation_probe=lambda: token.cancelled,
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

        def pin_skill(activated: ActivatedSkill) -> None:
            self.repository.pin_skill_activation(
                run_id=snapshot.run.id,
                worker_id=self.worker_id,
                fencing_token=snapshot.run.fencing_token,
                name=activated.name,
                version=activated.version,
                content_hash=activated.pin.content_hash,
                source=activated.source,
            )

        pinned_skill = _pinned_skill(snapshot.run)
        effective_user_text = user_text or snapshot.user_text
        tool_ceiling = self._runtime_tool_ceiling(runtime_factory)
        routed_skill = None
        if (
            not disable_tools
            and pinned_skill is None
            and "web_search" in tool_ceiling
        ):
            routed_skill = _routed_skill_for_user_text(effective_user_text)

        runtime = runtime_factory.create(
            request_context,
            persistent_note=snapshot.persistent_note,
            user_db_id=snapshot.user_db_id,
            roles=frozenset({snapshot.role}),
            tenant_id=snapshot.tenant_id,
            channel=snapshot.channel,
            run_id=snapshot.run.id,
            allowed_tools=(
                frozenset() if disable_tools else tool_ceiling
            ),
            deadline_seconds=remaining_deadline,
            approval_grant=(
                RunToolApprovalGrant(
                    user_id=snapshot.username,
                    tenant_id=snapshot.tenant_id,
                    thread_id=snapshot.run.thread_id,
                    run_id=snapshot.run.id,
                    tool_names=snapshot.approved_tools,
                )
                if snapshot.approved_tools
                else None
            ),
            tool_overrides=(
                {"search_knowledge_base": knowledge_tool}
                if knowledge_tool is not None
                else None
            ),
            pinned_skill=pinned_skill,
            pinned_skill_source=snapshot.run.skill_activation_source,
            routed_skill=routed_skill,
            on_skill_activate=pin_skill,
            trace_queue=trace_queue,
            model_snapshot=snapshot.model_snapshot,
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
                getattr(runtime_factory, "tools", None),
            ),
            name=f"run-trace-events:{snapshot.run.id}",
        )
        try:
            async for runtime_event in runtime.astream(
                AgentRuntimeInput(
                    history=_history_messages(snapshot),
                    user_text=effective_user_text,
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
                if item.get("type") == "rag_warning":
                    await self._publish_owned(
                        run,
                        event_type=RunEventType.WARNING_CREATED,
                        data=dict(item.get("warning") or {}),
                    )
                    continue
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
        registry: object | None,
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
                if stage in {"tool.completed", "tool.failed", "tool.denied"}:
                    if not (
                        stage == "tool.failed"
                        and item.get("error_code") == "TOOL_POLICY_DENIED"
                    ):
                        await self._record_tool_audit(run, stage, item, registry)
                if event_type is not None:
                    public_item = _public_tool_event_data(stage, item)
                    await self._publish_owned(
                        run,
                        event_type=event_type,
                        data=public_item,
                    )
                    if stage in {"tool.completed", "tool.failed"}:
                        for artifact in _public_artifact_descriptors(
                            item.get("artifacts")
                        ):
                            artifact_event = dict(artifact)
                            for field in ("tool_name", "tool_call_id"):
                                if field in public_item:
                                    artifact_event[field] = public_item[field]
                            await self._publish_owned(
                                run,
                                event_type=RunEventType.ARTIFACT_CREATED,
                                data=artifact_event,
                            )
                elif stage in _WARNING_TRACE_STAGES:
                    await self._publish_owned(
                        run,
                        event_type=RunEventType.WARNING_CREATED,
                        data={
                            "code": item.get("error_code")
                            or stage.upper().replace(".", "_"),
                            **item,
                        },
                    )
            except AppError as exc:
                if exc.code == ErrorCode.RUN_STATE_CONFLICT:
                    error_event.set()
                    return
                raise
            finally:
                trace_queue.task_done()

    async def _record_tool_audit(
        self,
        run: RunRecord,
        stage: str,
        item: dict,
        registry: object | None,
    ) -> None:
        tool_name = str(item.get("tool_name") or "unknown")
        descriptor = (
            registry.descriptor(tool_name)
            if registry is not None and hasattr(registry, "descriptor")
            else None
        )
        metadata = {
            "tool_version": getattr(descriptor, "version", ""),
            "tool_group": getattr(descriptor, "group", ""),
            "skill_name": run.skill_name or "",
            "skill_version": run.skill_version or "",
            "tool_catalog_hash": getattr(registry, "catalog_hash", ""),
        }
        audit_metadata = item.get("audit_metadata")
        if isinstance(audit_metadata, dict):
            metadata["tool_observability"] = dict(audit_metadata)
        guardrail_audit = item.get("guardrail_audit")
        if not isinstance(guardrail_audit, dict):
            guardrail_audit = {}
        guardrail_metadata = guardrail_audit.get("safe_metadata")
        if isinstance(guardrail_metadata, dict):
            metadata["guardrail_context"] = dict(guardrail_metadata)
        audit_key = str(item.get("tool_audit_key") or "")
        if len(audit_key) != 64:
            audit_key = hashlib.sha256(
                (
                    f"{run.id}\x00{run.fencing_token}\x00{stage}\x00"
                    f"{tool_name}\x00{item.get('tool_call_id') or ''}"
                ).encode("utf-8")
            ).hexdigest()
        await asyncio.to_thread(
            self.repository.record_tool_audit,
            run_id=run.id,
            worker_id=self.worker_id,
            fencing_token=run.fencing_token,
            audit_key=audit_key,
            tool_call_id=str(item.get("tool_call_id") or "") or None,
            tool_name=tool_name,
            tool_version=str(getattr(descriptor, "version", "")),
            decision=str(
                guardrail_audit.get("decision")
                or ("DENY" if stage == "tool.denied" else "ALLOW")
            ),
            reason_code=str(
                guardrail_audit.get("reason_code")
                or ("REGISTRY_POLICY_DENIED" if stage == "tool.denied" else "ALLOWED")
            ),
            policy_version=(str(guardrail_audit.get("policy_version") or "") or None),
            policy_hash=(str(guardrail_audit.get("policy_hash") or "") or None),
            success=stage == "tool.completed",
            error_code=(
                str(item.get("error_code") or "TOOL_POLICY_DENIED")
                if stage != "tool.completed"
                else None
            ),
            duration_ms=int(item.get("duration_ms") or 0),
            result_size=int(item.get("result_size") or 0),
            metadata=metadata,
        )

    @staticmethod
    async def _flush_event_queues(
        output_queue: asyncio.Queue,
        trace_queue: asyncio.Queue,
        error_event: asyncio.Event,
    ) -> None:
        async def wait_for_queues() -> None:
            await asyncio.gather(output_queue.join(), trace_queue.join())

        async def wait_for_error() -> None:
            await error_event.wait()

        joins = asyncio.create_task(wait_for_queues())
        failed = asyncio.create_task(wait_for_error())
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
