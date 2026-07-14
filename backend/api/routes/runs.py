from fastapi import APIRouter, Depends, status

from backend.db.models import User
from backend.infra.auth import get_current_user
from backend.runs.service import service
from backend.schemas.runs import (
    RunCreateRequest,
    RunCreateResponse,
    RunResponse,
    ThreadCreateRequest,
    ThreadResponse,
)


router = APIRouter(prefix="/v1", tags=["runs"])


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
