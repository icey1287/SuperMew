from fastapi import APIRouter, Depends

from backend.chat.storage import storage
from backend.db.models import User
from backend.infra.auth import get_current_user
from backend.schemas import (
    MessageInfo,
    SessionDeleteResponse,
    SessionInfo,
    SessionListResponse,
    SessionMessagesResponse,
)
from backend.core.errors import AppError, ErrorCode

router = APIRouter(tags=["sessions"])


@router.get("/sessions/{session_id}", response_model=SessionMessagesResponse)
async def get_session_messages(
    session_id: str, current_user: User = Depends(get_current_user)
):
    messages = [
        MessageInfo(
            type=msg["type"],
            content=msg["content"],
            timestamp=msg["timestamp"],
            rag_trace=msg.get("rag_trace"),
        )
        for msg in storage.get_session_messages(current_user.username, session_id)
    ]
    return SessionMessagesResponse(messages=messages)


@router.get("/sessions", response_model=SessionListResponse)
async def list_sessions(current_user: User = Depends(get_current_user)):
    sessions = [
        SessionInfo(**item)
        for item in storage.list_session_infos(current_user.username)
    ]
    sessions.sort(key=lambda x: x.updated_at, reverse=True)
    return SessionListResponse(sessions=sessions)


@router.delete("/sessions/{session_id}", response_model=SessionDeleteResponse)
async def delete_session(
    session_id: str, current_user: User = Depends(get_current_user)
):
    deleted = storage.delete_session(current_user.username, session_id)
    if not deleted:
        raise AppError(ErrorCode.NOT_FOUND, "会话不存在", status_code=404)
    return SessionDeleteResponse(session_id=session_id, message="成功删除会话")
