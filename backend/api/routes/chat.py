from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from starlette.concurrency import run_in_threadpool

from backend.chat import chat_with_agent, chat_with_agent_stream
from backend.chat.service import (
    legacy_public_error_from_exception,
    legacy_sse_error_chunk,
)
from backend.db.models import User
from backend.infra.auth import get_current_user
from backend.schemas import ChatRequest, ChatResponse

router = APIRouter(tags=["chat"])


@router.post("/chat", response_model=ChatResponse)
async def chat_endpoint(
    request: ChatRequest, current_user: User = Depends(get_current_user)
):
    session_id = request.session_id or "default_session"
    resp = await run_in_threadpool(
        chat_with_agent,
        request.message,
        current_user.username,
        session_id,
        current_user.role,
    )
    if isinstance(resp, dict):
        return ChatResponse(**resp)
    return ChatResponse(response=resp)


@router.post("/chat/stream")
async def chat_stream_endpoint(
    request: ChatRequest, current_user: User = Depends(get_current_user)
):
    async def event_generator():
        try:
            session_id = request.session_id or "default_session"
            async for chunk in chat_with_agent_stream(
                request.message,
                current_user.username,
                session_id,
                current_user.role,
            ):
                yield chunk
        except Exception as exc:
            public = legacy_public_error_from_exception(exc)
            yield legacy_sse_error_chunk(public)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
