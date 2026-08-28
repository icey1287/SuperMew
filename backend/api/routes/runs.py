from fastapi import APIRouter, Depends, Header, Query, status
from fastapi.responses import StreamingResponse
from starlette.concurrency import run_in_threadpool

from backend.core.errors import AppError, ErrorCode
from backend.db.models import User
from backend.events.bus import event_bus
from backend.events.generated.run_event_v1 import RunEventV1
from backend.events.journal import journal
from backend.events.sse import format_sse_event, format_sse_heartbeat
from backend.infra.auth import get_current_user
from backend.core.settings import get_settings
from backend.capabilities.control_service import capability_control_service
from backend.runs.agent_executor import run_agent_executor
from backend.runs.cancellation import cancellation_registry
from backend.runs.resume import resume_coordinator
from backend.runs.service import service
from backend.runs.state import RunStatus
from backend.schemas.events import RunEventsResponse
from backend.schemas.runs import (
    RunCreateRequest,
    RunCreateResponse,
    RunResumeRequest,
    RunResumeResponse,
    RunResponse,
)
from backend.threads.contracts import ThreadId


router = APIRouter(prefix="/v1", tags=["runs"])


async def _reserve_run(*, user: User, thread_id: ThreadId, request: RunCreateRequest):
    tool_registry = capability_control_service.tools
    approved_tools = frozenset(request.approved_tools)
    if approved_tools and user.role != "admin":
        raise AppError(
            ErrorCode.POLICY_DENIED,
            "只有管理员可以为 Run 预先批准高风险工具。",
            status_code=403,
            category="guardrail",
            stage="approval",
        )
    invalid_approvals = {
        name
        for name in approved_tools
        if (
            (descriptor := tool_registry.descriptor(name)) is None
            or not descriptor.requires_approval
        )
    }
    if invalid_approvals:
        raise AppError(
            ErrorCode.INVALID_REQUEST,
            "approved_tools 只能包含已声明需审批的工具。",
            status_code=400,
            category="guardrail",
            stage="approval",
        )
    settings = get_settings()
    reservation = await run_in_threadpool(
        service.create_run,
        username=user.username,
        thread_id=thread_id,
        message=request.message,
        idempotency_key=request.idempotency_key,
        expected_thread_version=request.expected_thread_version,
        multitask_strategy=request.multitask_strategy,
        on_disconnect=request.on_disconnect,
        tenant_id=settings.app.default_tenant_id,
        channel="run",
        approved_tools=approved_tools,
    )
    if reservation.run.supersedes_run_id:
        await run_in_threadpool(
            service.request_cancel,
            username=user.username,
            run_id=reservation.run.supersedes_run_id,
        )
        await cancellation_registry.request_cancel(reservation.run.supersedes_run_id)
    await run_agent_executor.spawn_once(
        username=user.username,
        run_id=reservation.run.id,
    )
    return reservation


def _event_cursor(after: int, last_event_id: str | None) -> int:
    if not last_event_id:
        return after
    try:
        return max(after, int(last_event_id))
    except ValueError as exc:
        raise AppError(
            ErrorCode.INVALID_REQUEST,
            "Last-Event-ID 必须是整数 sequence",
            status_code=400,
        ) from exc


def _stream_response(
    *,
    username: str,
    run_id: str,
    after: int,
    reservation_headers: dict[str, str] | None = None,
    initial_events: tuple[RunEventV1, ...] = (),
) -> StreamingResponse:
    async def generate():
        cursor = after
        for event in initial_events:
            if event.sequence <= cursor:
                continue
            yield format_sse_event(event)
            cursor = event.sequence
        async for event in event_bus.subscribe(
            username=username,
            run_id=run_id,
            after=cursor,
        ):
            yield (
                format_sse_event(event) if event is not None else format_sse_heartbeat()
            )

    headers = {
        "Cache-Control": "no-cache, no-store, must-revalidate",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
        "X-Run-ID": run_id,
    }
    if reservation_headers:
        headers.update(reservation_headers)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers=headers,
    )


@router.post(
    "/threads/{thread_id}/runs",
    response_model=RunCreateResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_run(
    thread_id: ThreadId,
    request: RunCreateRequest,
    current_user: User = Depends(get_current_user),
):
    reservation = await _reserve_run(
        user=current_user,
        thread_id=thread_id,
        request=request,
    )
    return RunCreateResponse(
        run=RunResponse(**reservation.run.__dict__),
        created=reservation.created,
        thread_version=reservation.thread_version,
    )


@router.get("/runs/{run_id}", response_model=RunResponse)
async def get_run(run_id: str, current_user: User = Depends(get_current_user)):
    run = await run_in_threadpool(
        service.get_run,
        username=current_user.username,
        run_id=run_id,
    )
    return RunResponse(**run.__dict__)


@router.post("/runs/{run_id}/cancel", response_model=RunResponse)
async def cancel_run(run_id: str, current_user: User = Depends(get_current_user)):
    requested = await run_in_threadpool(
        service.request_cancel,
        username=current_user.username,
        run_id=run_id,
    )
    if requested.status in {
        RunStatus.CANCELLING.value,
        RunStatus.CANCELLED.value,
    }:
        await cancellation_registry.request_cancel(run_id)
    run = await run_in_threadpool(
        service.get_run,
        username=current_user.username,
        run_id=run_id,
    )
    return RunResponse(**run.__dict__)


@router.post(
    "/runs/{run_id}/resume",
    response_model=RunResumeResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def resume_run(
    run_id: str,
    request: RunResumeRequest,
    current_user: User = Depends(get_current_user),
):
    acceptance = await run_in_threadpool(
        resume_coordinator.accept,
        username=current_user.username,
        run_id=run_id,
        hitl_token=request.hitl_token,
        answer=request.answer,
        idempotency_key=request.idempotency_key,
    )
    await run_agent_executor.resume_once(
        username=current_user.username,
        run_id=run_id,
        hitl_token=request.hitl_token,
        answer=request.answer,
        idempotency_key=request.idempotency_key,
    )
    return RunResumeResponse(
        run=RunResponse(**acceptance.run.__dict__),
        checkpoint_id=acceptance.checkpoint_id,
        created=acceptance.created,
    )


@router.get("/runs/{run_id}/events", response_model=RunEventsResponse)
async def get_run_events(
    run_id: str,
    after: int = Query(default=0, ge=0),
    limit: int = Query(default=500, ge=1, le=1000),
    current_user: User = Depends(get_current_user),
):
    events = await run_in_threadpool(
        journal.read_after,
        username=current_user.username,
        run_id=run_id,
        after=after,
        limit=limit,
    )
    return RunEventsResponse(
        events=events,
        next_after=events[-1].sequence if events else after,
    )


@router.get("/runs/{run_id}/stream")
async def stream_run_events(
    run_id: str,
    after: int = Query(default=0, ge=0),
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    current_user: User = Depends(get_current_user),
):
    return _stream_response(
        username=current_user.username,
        run_id=run_id,
        after=_event_cursor(after, last_event_id),
    )


@router.post("/threads/{thread_id}/runs/stream")
async def create_run_stream(
    thread_id: ThreadId,
    request: RunCreateRequest,
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    current_user: User = Depends(get_current_user),
):
    reservation = await _reserve_run(
        user=current_user,
        thread_id=thread_id,
        request=request,
    )
    return _stream_response(
        username=current_user.username,
        run_id=reservation.run.id,
        after=_event_cursor(0, last_event_id),
        reservation_headers={
            "X-Thread-Version": str(reservation.thread_version),
        },
        initial_events=(reservation.created_event,)
        if reservation.created_event
        else (),
    )
