from __future__ import annotations

from fastapi import APIRouter, Depends, Query, status
from starlette.concurrency import run_in_threadpool

from backend.core.errors import AppError, ErrorCode
from backend.db.models import User
from backend.infra.auth import get_current_user
from backend.schemas.threads import (
    ThreadCreateRequest,
    ThreadDeleteResponse,
    ThreadInfo,
    ThreadListResponse,
    ThreadMessagesResponse,
    ThreadResponse,
)
from backend.threads.contracts import ThreadId
from backend.threads.service import thread_service


router = APIRouter(prefix="/v1", tags=["threads"])


@router.post(
    "/threads",
    response_model=ThreadResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_thread(
    request: ThreadCreateRequest,
    current_user: User = Depends(get_current_user),
) -> ThreadResponse:
    thread = await run_in_threadpool(
        thread_service.create_thread,
        username=current_user.username,
        title=request.title,
    )
    return ThreadResponse.model_validate(thread)


@router.get("/threads", response_model=ThreadListResponse)
async def list_threads(
    current_user: User = Depends(get_current_user),
) -> ThreadListResponse:
    records = await run_in_threadpool(
        thread_service.list_threads,
        username=current_user.username,
    )
    return ThreadListResponse(
        threads=[ThreadInfo.model_validate(record) for record in records]
    )


@router.get(
    "/threads/{thread_id}/messages",
    response_model=ThreadMessagesResponse,
)
async def get_thread_messages(
    thread_id: ThreadId,
    before: int | None = Query(default=None, ge=1),
    limit: int = Query(default=200, ge=1, le=500),
    current_user: User = Depends(get_current_user),
) -> ThreadMessagesResponse:
    page = await run_in_threadpool(
        thread_service.recent_messages,
        username=current_user.username,
        thread_id=thread_id,
        before=before,
        limit=limit,
    )
    return ThreadMessagesResponse.model_validate(page)


@router.delete("/threads/{thread_id}", response_model=ThreadDeleteResponse)
async def delete_thread(
    thread_id: ThreadId,
    current_user: User = Depends(get_current_user),
) -> ThreadDeleteResponse:
    deleted = await run_in_threadpool(
        thread_service.delete_thread,
        username=current_user.username,
        thread_id=thread_id,
    )
    if not deleted:
        raise AppError(ErrorCode.NOT_FOUND, "Thread 不存在", status_code=404)
    return ThreadDeleteResponse(thread_id=thread_id, message="成功删除 Thread")
