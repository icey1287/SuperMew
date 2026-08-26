from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from uuid import uuid4

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from starlette.concurrency import run_in_threadpool

from backend.core.errors import AppError, ErrorCode, public_error_from_exception
from backend.core.settings import get_settings
from backend.db.models import User
from backend.events.bus import event_bus
from backend.events.generated.run_event_v1 import RunEventType, RunEventV1
from backend.events.journal import journal
from backend.infra.auth import get_current_user
from backend.runs.agent_executor import run_agent_executor
from backend.runs.cancellation import cancellation_registry
from backend.runs.resume import resume_coordinator
from backend.runs.service import RunService
from backend.runs.state import MultitaskStrategy, RunStatus
from backend.schemas.legacy_chat import (
    ChatRequest,
    ChatResponse,
    MessageInfo,
    SessionDeleteResponse,
    SessionInfo,
    SessionListResponse,
    SessionMessagesResponse,
)
from backend.schemas.rag import normalize_rag_trace
from backend.threads.repository import thread_repository
from backend.threads.service import thread_service


router = APIRouter(tags=["legacy-chat"])
_legacy_run_service = RunService(_allow_implicit_threads=True)


@dataclass(frozen=True, slots=True)
class _LegacyRun:
    run_id: str
    after: int
    new_thread: bool


def _sse(payload: dict[str, object] | str) -> str:
    if isinstance(payload, str):
        return f"data: {payload}\n\n"
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _title(message: str) -> str:
    compact = " ".join(message.split())
    return compact if len(compact) <= 10 else compact[:10] + "..."


def _session_id(value: str | None) -> str:
    return value or "default_session"


def _hitl_trace(data: dict) -> dict | None:
    return normalize_rag_trace(
        {
            "route": data.get("route"),
            "retrieval_status": data.get("retrieval_status"),
            "hitl_prompt": data.get("prompt"),
            "hitl_options": data.get("options") or [],
        }
    )


def _hitl_content(data: dict) -> str:
    prompt = str(data.get("prompt") or "请补充信息后继续。")
    options = [str(item) for item in (data.get("options") or [])]
    if not options:
        return prompt
    return prompt + "\n\n可选方向：\n" + "\n".join(f"- {item}" for item in options)


def _legacy_hitl_payload(data: dict) -> dict[str, object]:
    return {
        "id": str(data.get("checkpoint_id") or data.get("hitl_token") or ""),
        "prompt": str(data.get("prompt") or "请补充信息后继续。"),
        "options": [str(item) for item in (data.get("options") or [])],
        "route": data.get("route"),
        "retrieval_status": data.get("retrieval_status"),
        "original_question": data.get("original_question"),
    }


def _event_error(data: dict) -> dict:
    error = data.get("error")
    return error if isinstance(error, dict) else {}


def _run_error(data: dict) -> AppError:
    error = _event_error(data)
    code = str(error.get("code") or ErrorCode.RUN_EXECUTION_FAILED)
    if code in {str(ErrorCode.RATE_LIMITED), str(ErrorCode.MODEL_RATE_LIMITED)}:
        status_code = 429
    elif "TIMEOUT" in code or "DEADLINE" in code:
        status_code = 504
    elif "UNAVAILABLE" in code:
        status_code = 503
    elif code in {str(ErrorCode.PERMISSION_DENIED), str(ErrorCode.POLICY_DENIED)}:
        status_code = 403
    elif code == str(ErrorCode.RUN_CANCELLED):
        status_code = 409
    else:
        status_code = 500
    return AppError(
        code,
        error.get("message") or "运行失败，请稍后重试。",
        status_code=status_code,
        retryable=bool(error.get("retryable")),
        retry_after=error.get("retry_after"),
    )


def _latest_hitl_event(username: str, run_id: str) -> RunEventV1:
    events = journal.read_after(
        username=username,
        run_id=run_id,
        after=0,
        limit=1000,
    )
    for event in reversed(events):
        if event.type == RunEventType.HITL_REQUIRED:
            return event
    raise AppError(
        ErrorCode.RUN_STATE_CONFLICT,
        "等待输入的 Run 缺少 HITL 状态",
        status_code=409,
    )


async def _start_or_resume(
    *,
    username: str,
    session_id: str,
    message: str,
) -> _LegacyRun:
    summary = await run_in_threadpool(
        thread_repository.get_thread_summary,
        username,
        session_id,
    )
    if (
        summary is not None
        and summary.active_run_status == RunStatus.WAITING_INPUT.value
    ):
        if summary.active_run_id is None:
            raise AppError(
                ErrorCode.RUN_STATE_CONFLICT,
                "等待输入的 Thread 缺少 active Run",
                status_code=409,
            )
        hitl_event = await run_in_threadpool(
            _latest_hitl_event,
            username,
            summary.active_run_id,
        )
        hitl_token = str((hitl_event.data or {}).get("hitl_token") or "")
        if not hitl_token:
            raise AppError(
                ErrorCode.RUN_STATE_CONFLICT,
                "等待输入的 Run 缺少 HITL token",
                status_code=409,
            )
        idempotency_key = f"legacy_resume_{uuid4().hex}"
        await run_in_threadpool(
            resume_coordinator.accept,
            username=username,
            run_id=summary.active_run_id,
            hitl_token=hitl_token,
            answer=message,
            idempotency_key=idempotency_key,
        )
        await run_agent_executor.resume_once(
            username=username,
            run_id=summary.active_run_id,
            hitl_token=hitl_token,
            answer=message,
            idempotency_key=idempotency_key,
        )
        return _LegacyRun(
            run_id=summary.active_run_id,
            after=hitl_event.sequence,
            new_thread=False,
        )

    reservation = await run_in_threadpool(
        _legacy_run_service.create_run,
        username=username,
        thread_id=session_id,
        message=message,
        idempotency_key=f"legacy_run_{uuid4().hex}",
        on_disconnect="cancel",
        multitask_strategy=MultitaskStrategy.CANCEL_PREVIOUS,
        tenant_id=get_settings().app.default_tenant_id,
        channel="run",
    )
    if reservation.run.supersedes_run_id:
        await _cancel_run(username, reservation.run.supersedes_run_id)
    await run_agent_executor.spawn_once(username=username, run_id=reservation.run.id)
    return _LegacyRun(
        run_id=reservation.run.id,
        after=0,
        new_thread=summary is None,
    )


async def _cancel_run(username: str, run_id: str) -> None:
    requested = await run_in_threadpool(
        _legacy_run_service.request_cancel,
        username=username,
        run_id=run_id,
    )
    if requested.status in {
        RunStatus.CANCELLING.value,
        RunStatus.CANCELLED.value,
    }:
        await cancellation_registry.request_cancel(run_id)


async def _collect_response(username: str, run: _LegacyRun) -> ChatResponse:
    content_parts: list[str] = []
    final_content = ""
    rag_trace = None
    try:
        async for event in event_bus.subscribe(
            username=username,
            run_id=run.run_id,
            after=run.after,
        ):
            if event is None:
                continue
            data = event.data or {}
            if event.type == RunEventType.MESSAGE_DELTA:
                content_parts.append(str(data.get("content") or ""))
            elif event.type == RunEventType.MESSAGE_COMPLETED:
                final_content = str(data.get("content") or "")
                rag_trace = normalize_rag_trace(data.get("rag_trace"))
            elif event.type == RunEventType.HITL_REQUIRED:
                return ChatResponse(
                    response=_hitl_content(data),
                    rag_trace=_hitl_trace(data),
                )
            elif event.type in {
                RunEventType.RUN_FAILED,
                RunEventType.RUN_CANCELLED,
            }:
                raise _run_error(data)
    except asyncio.CancelledError:
        await _cancel_run(username, run.run_id)
        raise
    return ChatResponse(
        response=final_content or "".join(content_parts), rag_trace=rag_trace
    )


@router.post("/chat", response_model=ChatResponse, deprecated=True)
async def chat_endpoint(
    request: ChatRequest,
    current_user: User = Depends(get_current_user),
) -> ChatResponse:
    session_id = _session_id(request.session_id)
    run = await _start_or_resume(
        username=current_user.username,
        session_id=session_id,
        message=request.message,
    )
    return await _collect_response(current_user.username, run)


@router.post("/chat/stream", deprecated=True)
async def chat_stream_endpoint(
    request: ChatRequest,
    current_user: User = Depends(get_current_user),
) -> StreamingResponse:
    session_id = _session_id(request.session_id)
    run = await _start_or_resume(
        username=current_user.username,
        session_id=session_id,
        message=request.message,
    )

    async def generate() -> AsyncIterator[str]:
        finished = False
        if run.new_thread:
            yield _sse(
                {
                    "type": "session_title",
                    "title": _title(request.message),
                    "session_id": session_id,
                }
            )
        emitted_content = False
        try:
            async for event in event_bus.subscribe(
                username=current_user.username,
                run_id=run.run_id,
                after=run.after,
            ):
                if event is None:
                    yield ": heartbeat\n\n"
                    continue
                data = event.data or {}
                if event.type == RunEventType.MESSAGE_DELTA:
                    content = str(data.get("content") or "")
                    if content:
                        emitted_content = True
                        yield _sse({"type": "content", "content": content})
                elif event.type == RunEventType.TOOL_PROGRESS and isinstance(
                    data.get("step"), dict
                ):
                    yield _sse({"type": "rag_step", "step": data["step"]})
                elif event.type == RunEventType.MESSAGE_COMPLETED:
                    content = str(data.get("content") or "")
                    if content and not emitted_content:
                        yield _sse({"type": "content", "content": content})
                    trace = normalize_rag_trace(data.get("rag_trace"))
                    if trace:
                        yield _sse({"type": "trace", "rag_trace": trace})
                elif event.type == RunEventType.HITL_REQUIRED:
                    trace = _hitl_trace(data)
                    if trace:
                        yield _sse({"type": "trace", "rag_trace": trace})
                    yield _sse(
                        {
                            "type": "hitl_request",
                            "hitl": _legacy_hitl_payload(data),
                        }
                    )
                    yield _sse("[DONE]")
                    finished = True
                    return
                elif event.type in {
                    RunEventType.RUN_FAILED,
                    RunEventType.RUN_CANCELLED,
                }:
                    error = _event_error(data)
                    yield _sse(
                        {
                            "type": "error",
                            "content": error.get("message") or "运行未完成。",
                        }
                    )
                if event.type in {
                    RunEventType.RUN_COMPLETED,
                    RunEventType.RUN_FAILED,
                    RunEventType.RUN_CANCELLED,
                }:
                    yield _sse("[DONE]")
                    finished = True
                    return
        except Exception as exc:
            public = public_error_from_exception(exc)
            yield _sse({"type": "error", "content": public.message})
            yield _sse("[DONE]")
        finally:
            if not finished:
                await _cancel_run(current_user.username, run.run_id)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
            "X-Run-ID": run.run_id,
        },
    )


@router.get(
    "/sessions/{session_id}",
    response_model=SessionMessagesResponse,
    deprecated=True,
)
async def get_session_messages(
    session_id: str,
    current_user: User = Depends(get_current_user),
) -> SessionMessagesResponse:
    rows = await run_in_threadpool(
        thread_repository.list_messages_before,
        current_user.username,
        session_id,
        before=None,
        limit=500,
    )
    if rows is None:
        return SessionMessagesResponse(messages=[])
    messages: list[MessageInfo] = []
    for message in reversed(rows):
        content = message.content
        rag_trace = message.rag_trace
        if message.status == RunStatus.WAITING_INPUT.value and message.run_id:
            try:
                hitl = await run_in_threadpool(
                    _latest_hitl_event,
                    current_user.username,
                    message.run_id,
                )
            except AppError:
                pass
            else:
                data = hitl.data or {}
                content = _hitl_content(data)
                rag_trace = _hitl_trace(data)
        messages.append(
            MessageInfo(
                type=message.role,
                content=content,
                timestamp=message.timestamp,
                rag_trace=rag_trace,
            )
        )
    return SessionMessagesResponse(messages=messages)


@router.get("/sessions", response_model=SessionListResponse, deprecated=True)
async def list_sessions(
    current_user: User = Depends(get_current_user),
) -> SessionListResponse:
    threads = await run_in_threadpool(
        thread_service.list_threads,
        username=current_user.username,
    )
    return SessionListResponse(
        sessions=[
            SessionInfo(
                session_id=thread.thread_id,
                title=thread.title,
                updated_at=thread.updated_at,
                message_count=thread.message_count,
            )
            for thread in threads
        ]
    )


@router.delete(
    "/sessions/{session_id}",
    response_model=SessionDeleteResponse,
    deprecated=True,
)
async def delete_session(
    session_id: str,
    current_user: User = Depends(get_current_user),
) -> SessionDeleteResponse:
    deleted = await run_in_threadpool(
        thread_repository.delete_thread,
        current_user.username,
        session_id,
    )
    if not deleted:
        raise AppError(ErrorCode.NOT_FOUND, "会话不存在", status_code=404)
    return SessionDeleteResponse(session_id=session_id, message="成功删除会话")


__all__ = ["router"]
