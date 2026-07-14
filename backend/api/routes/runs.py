from fastapi import APIRouter, Depends, Header, Query, status
from fastapi.responses import StreamingResponse

from backend.core.errors import AppError, ErrorCode
from backend.db.models import User
from backend.events.bus import event_bus
from backend.events.journal import journal
from backend.events.sse import format_sse_event, format_sse_heartbeat
from backend.infra.auth import get_current_user
from backend.runs.service import service
from backend.schemas.events import RunEventsResponse
from backend.schemas.runs import (
    RunCreateRequest,
    RunCreateResponse,
    RunResponse,
    ThreadCreateRequest,
    ThreadResponse,
)


router = APIRouter(prefix="/v1", tags=["runs"])


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


def _stream_response(*, username: str, run_id: str, after: int) -> StreamingResponse:
    async def generate():
        async for event in event_bus.subscribe(
            username=username,
            run_id=run_id,
            after=after,
        ):
            yield (
                format_sse_event(event) if event is not None else format_sse_heartbeat()
            )

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
            "X-Run-ID": run_id,
        },
    )


@router.post(
    "/threads", response_model=ThreadResponse, status_code=status.HTTP_201_CREATED
)
async def create_thread(
    request: ThreadCreateRequest,
    current_user: User = Depends(get_current_user),
):
    return ThreadResponse(
        **service.create_thread(
            username=current_user.username,
            thread_id=request.thread_id,
            title=request.title,
        )
    )


@router.post(
    "/threads/{thread_id}/runs",
    response_model=RunCreateResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_run(
    thread_id: str,
    request: RunCreateRequest,
    current_user: User = Depends(get_current_user),
):
    reservation = service.create_run(
        username=current_user.username,
        thread_id=thread_id,
        message=request.message,
        idempotency_key=request.idempotency_key,
        expected_thread_version=request.expected_thread_version,
        multitask_strategy=request.multitask_strategy,
        on_disconnect=request.on_disconnect,
    )
    return RunCreateResponse(
        run=RunResponse(**reservation.run.__dict__),
        created=reservation.created,
        thread_version=reservation.thread_version,
    )


@router.get("/runs/{run_id}", response_model=RunResponse)
async def get_run(run_id: str, current_user: User = Depends(get_current_user)):
    return RunResponse(
        **service.get_run(username=current_user.username, run_id=run_id).__dict__
    )


@router.get("/runs/{run_id}/events", response_model=RunEventsResponse)
async def get_run_events(
    run_id: str,
    after: int = Query(default=0, ge=0),
    limit: int = Query(default=500, ge=1, le=1000),
    current_user: User = Depends(get_current_user),
):
    events = journal.read_after(
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
    thread_id: str,
    request: RunCreateRequest,
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    current_user: User = Depends(get_current_user),
):
    reservation = service.create_run(
        username=current_user.username,
        thread_id=thread_id,
        message=request.message,
        idempotency_key=request.idempotency_key,
        expected_thread_version=request.expected_thread_version,
        multitask_strategy=request.multitask_strategy,
        on_disconnect=request.on_disconnect,
    )
    return _stream_response(
        username=current_user.username,
        run_id=reservation.run.id,
        after=_event_cursor(0, last_event_id),
    )
