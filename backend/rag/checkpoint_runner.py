from __future__ import annotations

import json
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import timedelta
from uuid import uuid4

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.types import Command
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.chat.request_context import ChatRequestContext
from backend.core.errors import AppError, ErrorCode
from backend.core.settings import get_settings
from backend.db.models import ChatMessage, ChatSession, Run, RunCheckpoint, User, utcnow
from backend.events.generated.run_event_v1 import RunEventType
from backend.events.journal import append_event_in_session
from backend.infra.database import SessionLocal
from backend.rag.checkpointing import postgres_saver_factory
from backend.rag.runtime_context import bind_rag_runtime_context
from backend.runs.state import RunStatus


SessionFactory = Callable[[], Session]
SaverFactory = Callable[[], AbstractContextManager[BaseCheckpointSaver]]


def _jsonable(value):
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


@dataclass(frozen=True)
class CheckpointPause:
    run_id: str
    checkpoint_id: str
    interrupt_id: str
    hitl_token: str
    prompt: str
    options: list[str]
    route: str | None
    retrieval_status: str | None


@dataclass(frozen=True)
class ConsumedResume:
    checkpoint_id: str
    answer: str
    fencing_token: int
    already_consumed: bool
    should_resume: bool


@dataclass(frozen=True)
class PendingResume:
    username: str
    run_id: str
    hitl_token: str
    answer: str
    idempotency_key: str


@dataclass(frozen=True)
class RagRunOutcome:
    result: dict
    fencing_token: int
    pause: CheckpointPause | None = None


class HitlCheckpointRepository:
    def __init__(self, session_factory: SessionFactory = SessionLocal):
        self._session_factory = session_factory

    def record_pause(
        self,
        *,
        run_id: str,
        worker_id: str,
        fencing_token: int,
        checkpoint_id: str,
        interrupt_id: str,
        interrupt_value: dict,
        state: dict,
        next_nodes: tuple[str, ...],
    ) -> CheckpointPause:
        db = self._session_factory()
        try:
            with db.begin():
                run = db.query(Run).filter(Run.id == run_id).with_for_update().first()
                if not run:
                    raise AppError(
                        ErrorCode.RUN_NOT_FOUND, "Run 不存在", status_code=404
                    )
                existing = (
                    db.query(RunCheckpoint)
                    .filter(
                        RunCheckpoint.run_id == run_id,
                        RunCheckpoint.checkpoint_id == checkpoint_id,
                    )
                    .first()
                )
                if existing and existing.consumed_at is None:
                    if run.fencing_token != fencing_token:
                        raise AppError(
                            ErrorCode.RUN_STATE_CONFLICT,
                            "Run fencing token 已失效",
                            status_code=409,
                        )
                    if existing.interrupt_id != interrupt_id:
                        raise AppError(
                            ErrorCode.RUN_STATE_CONFLICT,
                            "checkpoint interrupt 标识不匹配",
                            status_code=409,
                        )
                    return CheckpointPause(
                        run_id=run_id,
                        checkpoint_id=checkpoint_id,
                        interrupt_id=existing.interrupt_id or interrupt_id,
                        hitl_token=existing.hitl_token or "",
                        prompt=str(interrupt_value.get("prompt") or ""),
                        options=list(interrupt_value.get("options") or []),
                        route=interrupt_value.get("route"),
                        retrieval_status=interrupt_value.get("retrieval_status"),
                    )
                if existing:
                    raise AppError(
                        ErrorCode.RUN_STATE_CONFLICT,
                        "该 checkpoint 已完成恢复，不能再次暂停",
                        status_code=409,
                    )
                if (
                    run.owner_worker_id != worker_id
                    or run.fencing_token != fencing_token
                ):
                    raise AppError(
                        ErrorCode.RUN_STATE_CONFLICT,
                        "当前 worker 不再拥有该 Run",
                        status_code=409,
                    )
                if run.status != RunStatus.RUNNING.value:
                    raise AppError(
                        ErrorCode.RUN_STATE_CONFLICT,
                        f"状态为 {run.status} 的 Run 不能进入 HITL",
                        status_code=409,
                    )

                thread = (
                    db.query(ChatSession)
                    .filter(ChatSession.id == run.thread_ref_id)
                    .one()
                )
                hitl_token = f"hitl_{uuid4().hex}"
                checkpoint = RunCheckpoint(
                    run_id=run.id,
                    thread_ref_id=thread.id,
                    user_id=run.user_id,
                    checkpoint_id=checkpoint_id,
                    hitl_token=hitl_token,
                    interrupt_id=interrupt_id,
                    state_json=_jsonable(state),
                    next_nodes_json=list(next_nodes),
                    created_at=utcnow(),
                    updated_at=utcnow(),
                )
                db.add(checkpoint)
                run.status = RunStatus.WAITING_INPUT.value
                run.owner_worker_id = None
                run.lease_expires_at = None
                run.updated_at = utcnow()
                if run.assistant_message_id:
                    message = (
                        db.query(ChatMessage)
                        .filter(ChatMessage.id == run.assistant_message_id)
                        .first()
                    )
                    if message:
                        message.status = "waiting_input"
                        message.updated_at = utcnow()
                append_event_in_session(
                    db,
                    run=run,
                    thread_id=thread.session_id,
                    event_type=RunEventType.RUN_WAITING_INPUT,
                    data={"status": run.status},
                )
                append_event_in_session(
                    db,
                    run=run,
                    thread_id=thread.session_id,
                    event_type=RunEventType.HITL_REQUIRED,
                    data={
                        "hitl_token": hitl_token,
                        "checkpoint_id": checkpoint_id,
                        **_jsonable(interrupt_value),
                    },
                )
                return CheckpointPause(
                    run_id=run_id,
                    checkpoint_id=checkpoint_id,
                    interrupt_id=interrupt_id,
                    hitl_token=hitl_token,
                    prompt=str(interrupt_value.get("prompt") or ""),
                    options=[
                        str(item) for item in (interrupt_value.get("options") or [])
                    ],
                    route=interrupt_value.get("route"),
                    retrieval_status=interrupt_value.get("retrieval_status"),
                )
        finally:
            db.close()

    def consume_resume(
        self,
        *,
        username: str,
        run_id: str,
        hitl_token: str,
        answer: str,
        idempotency_key: str,
        worker_id: str | None,
    ) -> ConsumedResume:
        clean_answer = answer.strip()
        if not clean_answer:
            raise AppError(
                ErrorCode.INVALID_REQUEST,
                "HITL answer 不能为空",
                status_code=400,
            )
        db = self._session_factory()
        try:
            with db.begin():
                row = (
                    db.query(RunCheckpoint, Run, ChatSession)
                    .join(Run, Run.id == RunCheckpoint.run_id)
                    .join(ChatSession, ChatSession.id == Run.thread_ref_id)
                    .join(User, User.id == Run.user_id)
                    .filter(
                        Run.id == run_id,
                        RunCheckpoint.hitl_token == hitl_token,
                        User.username == username,
                    )
                    .with_for_update()
                    .first()
                )
                if not row:
                    raise AppError(
                        ErrorCode.RUN_NOT_FOUND,
                        "HITL checkpoint 不存在",
                        status_code=404,
                    )
                checkpoint, run, thread = row
                payload = {"answer": clean_answer}
                conflicting_key = (
                    db.query(RunCheckpoint.id)
                    .filter(
                        RunCheckpoint.run_id == run.id,
                        RunCheckpoint.resume_idempotency_key == idempotency_key,
                        RunCheckpoint.id != checkpoint.id,
                    )
                    .first()
                )
                if conflicting_key:
                    raise AppError(
                        ErrorCode.IDEMPOTENCY_CONFLICT,
                        "该恢复幂等键已用于当前 Run 的其他 HITL 请求",
                        status_code=409,
                    )
                if checkpoint.consumed_at is not None:
                    same_request = (
                        checkpoint.resume_idempotency_key == idempotency_key
                        and checkpoint.resume_payload_json == payload
                    )
                    if not same_request:
                        raise AppError(
                            ErrorCode.IDEMPOTENCY_CONFLICT,
                            "该 HITL token 已被消费",
                            status_code=409,
                        )
                    should_resume = False
                    started_now = False
                    if worker_id:
                        if run.status == RunStatus.PENDING.value:
                            run.status = RunStatus.RUNNING.value
                            run.owner_worker_id = worker_id
                            run.fencing_token += 1
                            run.lease_expires_at = utcnow() + timedelta(
                                seconds=get_settings().worker.lease_seconds
                            )
                            run.updated_at = utcnow()
                            should_resume = True
                            started_now = True
                        elif run.status == RunStatus.RUNNING.value:
                            if run.owner_worker_id != worker_id:
                                raise AppError(
                                    ErrorCode.RUN_ACTIVE,
                                    "HITL 恢复已被其他 worker 领取",
                                    status_code=409,
                                )
                            should_resume = True
                    if started_now:
                        append_event_in_session(
                            db,
                            run=run,
                            thread_id=thread.session_id,
                            event_type=RunEventType.RUN_STARTED,
                            data={
                                "status": run.status,
                                "worker_id": worker_id,
                                "fencing_token": run.fencing_token,
                                "resume_checkpoint_id": checkpoint.checkpoint_id,
                            },
                        )
                    return ConsumedResume(
                        checkpoint_id=checkpoint.checkpoint_id,
                        answer=clean_answer,
                        fencing_token=run.fencing_token,
                        already_consumed=True,
                        should_resume=should_resume,
                    )
                if run.status != RunStatus.WAITING_INPUT.value:
                    raise AppError(
                        ErrorCode.RUN_STATE_CONFLICT,
                        f"状态为 {run.status} 的 Run 不能恢复",
                        status_code=409,
                    )
                checkpoint.consumed_at = utcnow()
                checkpoint.resume_idempotency_key = idempotency_key
                checkpoint.resume_payload_json = payload
                checkpoint.updated_at = utcnow()
                if worker_id:
                    run.status = RunStatus.RUNNING.value
                    run.owner_worker_id = worker_id
                    run.fencing_token += 1
                    run.lease_expires_at = utcnow() + timedelta(
                        seconds=get_settings().worker.lease_seconds
                    )
                else:
                    run.status = RunStatus.PENDING.value
                    run.owner_worker_id = None
                    run.lease_expires_at = None
                run.updated_at = utcnow()
                if run.assistant_message_id:
                    message = (
                        db.query(ChatMessage)
                        .filter(ChatMessage.id == run.assistant_message_id)
                        .first()
                    )
                    if message:
                        message.status = "streaming"
                        message.updated_at = utcnow()
                append_event_in_session(
                    db,
                    run=run,
                    thread_id=thread.session_id,
                    event_type=RunEventType.HITL_RESUMED,
                    data={
                        "checkpoint_id": checkpoint.checkpoint_id,
                        "answer": clean_answer,
                        "status": run.status,
                    },
                )
                if worker_id:
                    append_event_in_session(
                        db,
                        run=run,
                        thread_id=thread.session_id,
                        event_type=RunEventType.RUN_STARTED,
                        data={
                            "status": run.status,
                            "worker_id": worker_id,
                            "fencing_token": run.fencing_token,
                            "resume_checkpoint_id": checkpoint.checkpoint_id,
                        },
                    )
                return ConsumedResume(
                    checkpoint_id=checkpoint.checkpoint_id,
                    answer=clean_answer,
                    fencing_token=run.fencing_token,
                    already_consumed=False,
                    should_resume=bool(worker_id),
                )
        except IntegrityError as exc:
            raise AppError(
                ErrorCode.IDEMPOTENCY_CONFLICT,
                "HITL 恢复请求发生幂等冲突",
                status_code=409,
            ) from exc
        finally:
            db.close()

    def list_pending_resumes(self, *, limit: int = 500) -> list[PendingResume]:
        db = self._session_factory()
        try:
            rows = (
                db.query(RunCheckpoint, Run, User)
                .join(Run, Run.id == RunCheckpoint.run_id)
                .join(User, User.id == Run.user_id)
                .filter(
                    Run.status == RunStatus.PENDING.value,
                    RunCheckpoint.consumed_at.is_not(None),
                    RunCheckpoint.resume_idempotency_key.is_not(None),
                )
                .order_by(RunCheckpoint.consumed_at.desc())
                .limit(max(1, min(limit, 5000)))
                .all()
            )
            pending: list[PendingResume] = []
            seen: set[str] = set()
            for checkpoint, run, user in rows:
                if run.id in seen:
                    continue
                payload = checkpoint.resume_payload_json or {}
                answer = str(payload.get("answer") or "").strip()
                if not answer or not checkpoint.resume_idempotency_key:
                    continue
                seen.add(run.id)
                pending.append(
                    PendingResume(
                        username=user.username,
                        run_id=run.id,
                        hitl_token=checkpoint.hitl_token or "",
                        answer=answer,
                        idempotency_key=checkpoint.resume_idempotency_key,
                    )
                )
            return pending
        finally:
            db.close()


class CheckpointedRagRunner:
    def __init__(
        self,
        *,
        saver_factory: SaverFactory = postgres_saver_factory,
        checkpoint_repository: HitlCheckpointRepository | None = None,
    ) -> None:
        self.saver_factory = saver_factory
        self.checkpoints = checkpoint_repository or HitlCheckpointRepository()

    @staticmethod
    def _config(run_id: str) -> dict:
        return {"configurable": {"thread_id": run_id}}

    def _outcome(
        self,
        *,
        graph,
        result: dict,
        run_id: str,
        worker_id: str,
        fencing_token: int,
    ) -> RagRunOutcome:
        interrupts = list(result.pop("__interrupt__", []) or [])
        if not interrupts:
            return RagRunOutcome(result=result, fencing_token=fencing_token)
        snapshot = graph.get_state(self._config(run_id))
        interrupt_value = dict(interrupts[0].value or {})
        pause = self.checkpoints.record_pause(
            run_id=run_id,
            worker_id=worker_id,
            fencing_token=fencing_token,
            checkpoint_id=snapshot.config["configurable"]["checkpoint_id"],
            interrupt_id=interrupts[0].id,
            interrupt_value=interrupt_value,
            state=dict(snapshot.values),
            next_nodes=tuple(snapshot.next),
        )
        return RagRunOutcome(result=result, fencing_token=fencing_token, pause=pause)

    def start(
        self,
        *,
        run_id: str,
        question: str,
        context: ChatRequestContext,
        worker_id: str,
        fencing_token: int,
    ) -> RagRunOutcome:
        from backend.rag.pipeline import _initial_state, build_rag_graph

        with self.saver_factory() as saver:
            graph = build_rag_graph(checkpointer=saver)
            config = self._config(run_id)
            with bind_rag_runtime_context(context) as runtime_context_id:
                result = graph.invoke(
                    _initial_state(
                        question,
                        runtime_context_id=runtime_context_id,
                    ),
                    config=config,
                )
            return self._outcome(
                graph=graph,
                result=result,
                run_id=run_id,
                worker_id=worker_id,
                fencing_token=fencing_token,
            )

    def resume(
        self,
        *,
        username: str,
        run_id: str,
        hitl_token: str,
        answer: str,
        idempotency_key: str,
        context: ChatRequestContext,
        worker_id: str,
    ) -> RagRunOutcome:
        consumed = self.checkpoints.consume_resume(
            username=username,
            run_id=run_id,
            hitl_token=hitl_token,
            answer=answer,
            idempotency_key=idempotency_key,
            worker_id=worker_id,
        )
        return self.resume_consumed(
            run_id=run_id,
            consumed=consumed,
            context=context,
            worker_id=worker_id,
        )

    def resume_consumed(
        self,
        *,
        run_id: str,
        consumed: ConsumedResume,
        context: ChatRequestContext,
        worker_id: str,
    ) -> RagRunOutcome:
        from backend.rag.pipeline import build_rag_graph

        with self.saver_factory() as saver:
            graph = build_rag_graph(checkpointer=saver)
            config = self._config(run_id)
            snapshot = graph.get_state(config)
            if not snapshot.values:
                raise AppError(
                    ErrorCode.RUN_STATE_CONFLICT,
                    "HITL checkpoint 状态不存在",
                    status_code=409,
                )
            result = dict(snapshot.values)
            latest_checkpoint_id = snapshot.config["configurable"].get("checkpoint_id")
            if (
                not consumed.should_resume
                or latest_checkpoint_id != consumed.checkpoint_id
            ):
                return RagRunOutcome(
                    result=result,
                    fencing_token=consumed.fencing_token,
                )
            runtime_context_id = snapshot.values.get("runtime_context_id")
            with bind_rag_runtime_context(context, runtime_context_id):
                result = graph.invoke(
                    Command(resume=consumed.answer),
                    config=config,
                )
            return self._outcome(
                graph=graph,
                result=result,
                run_id=run_id,
                worker_id=worker_id,
                fencing_token=consumed.fencing_token,
            )


checkpoint_repository = HitlCheckpointRepository()
checkpointed_rag_runner = CheckpointedRagRunner(
    checkpoint_repository=checkpoint_repository
)
