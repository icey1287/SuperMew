from fastapi import APIRouter, Depends, Query
from starlette.concurrency import run_in_threadpool

from backend.core.errors import AppError, ErrorCode
from backend.db.models import User
from backend.infra.auth import get_current_user
from backend.schemas import (
    MessageInfo,
    SessionDeleteResponse,
    SessionInfo,
    SessionListResponse,
    SessionMessagesResponse,
)
from backend.threads.service import thread_service


router = APIRouter(tags=["sessions-compatibility"])


@router.get(
    "/sessions/{session_id}",
    response_model=SessionMessagesResponse,
    deprecated=True,
    description="兼容旧客户端；新客户端应使用 /v1/threads/{thread_id}/messages。",
)
async def get_session_messages(
    session_id: str,
    after: int = Query(default=0, ge=0),
    limit: int = Query(default=200, ge=1, le=500),
    current_user: User = Depends(get_current_user),
) -> SessionMessagesResponse:
    page = await run_in_threadpool(
        thread_service.legacy_messages,
        username=current_user.username,
        session_id=session_id,
        after=after,
        limit=limit,
    )
    return SessionMessagesResponse(
        messages=[
            MessageInfo(
                id=record.id,
                run_id=record.run_id,
                sequence=record.sequence,
                status=record.status,
                type=record.role,
                content=record.content,
                timestamp=record.timestamp.isoformat(),
                rag_trace=record.rag_trace,
            )
            for record in page.messages
        ],
        next_cursor=page.next_cursor,
    )


@router.get(
    "/sessions",
    response_model=SessionListResponse,
    deprecated=True,
    description="兼容旧客户端；新客户端应使用 /v1/threads。",
)
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
                session_id=item.thread_id,
                title=item.title,
                updated_at=item.updated_at.isoformat(),
                message_count=item.message_count,
                version=item.version,
                status=item.thread_status,
            )
            for item in threads
        ]
    )


@router.delete(
    "/sessions/{session_id}",
    response_model=SessionDeleteResponse,
    deprecated=True,
    description="兼容旧客户端；新客户端应使用 DELETE /v1/threads/{thread_id}。",
)
async def delete_session(
    session_id: str,
    current_user: User = Depends(get_current_user),
) -> SessionDeleteResponse:
    deleted = await run_in_threadpool(
        thread_service.delete_session,
        username=current_user.username,
        session_id=session_id,
    )
    if not deleted:
        raise AppError(ErrorCode.NOT_FOUND, "会话不存在", status_code=404)
    return SessionDeleteResponse(session_id=session_id, message="成功删除会话")
