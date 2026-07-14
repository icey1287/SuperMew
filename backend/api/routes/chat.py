import json

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse

from backend.chat import chat_with_agent, chat_with_agent_stream
from backend.db.models import User
from backend.core.errors import error_payload, public_error_from_exception
from backend.infra.auth import get_current_user
from backend.schemas import ChatRequest, ChatResponse

router = APIRouter(tags=["chat"])


@router.post("/chat", response_model=ChatResponse)
async def chat_endpoint(
    request: ChatRequest, current_user: User = Depends(get_current_user)
):
    session_id = request.session_id or "default_session"
    resp = chat_with_agent(request.message, current_user.username, session_id)
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
            ):
                yield chunk
        except Exception as exc:
            public = public_error_from_exception(exc)
            error_data = {"type": "error", **error_payload(public)["error"]}
            yield f"data: {json.dumps(error_data)}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
