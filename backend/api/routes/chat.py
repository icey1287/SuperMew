from fastapi import APIRouter, Depends, status

from backend.core.errors import AppError, ErrorCode
from backend.db.models import User
from backend.infra.auth import get_current_user

router = APIRouter(tags=["chat"])


def _retire_legacy_chat() -> None:
    raise AppError(
        ErrorCode.ENDPOINT_RETIRED,
        "旧 Chat 接口已退役，请使用持久化 Run/Event 接口。",
        status_code=410,
        retryable=False,
        safe_details={
            "create_run": "/v1/threads/{thread_id}/runs",
            "stream_run": "/v1/runs/{run_id}/stream",
            "resume_run": "/v1/runs/{run_id}/resume",
            "cancel_run": "/v1/runs/{run_id}/cancel",
        },
        category="contract",
        stage="routing",
    )


@router.post("/chat", deprecated=True, status_code=status.HTTP_410_GONE)
async def chat_endpoint(
    _current_user: User = Depends(get_current_user),
) -> None:
    _retire_legacy_chat()


@router.post("/chat/stream", deprecated=True, status_code=status.HTTP_410_GONE)
async def chat_stream_endpoint(
    _current_user: User = Depends(get_current_user),
) -> None:
    _retire_legacy_chat()
