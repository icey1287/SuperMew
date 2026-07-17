from __future__ import annotations

from fastapi import APIRouter, Depends, status
from starlette.concurrency import run_in_threadpool

from backend.core.errors import AppError, ErrorCode
from backend.db.models import User
from backend.infra.auth import get_current_user
from backend.model_control import ModelControlService, ModelRole, model_control_service
from backend.schemas.models import (
    ModelAssignmentRequest,
    ModelControlPlaneResponse,
    ModelDeleteResponse,
    ModelProfileCreateRequest,
    ModelProfileUpdateRequest,
)


router = APIRouter(prefix="/v1/models", tags=["models"])


def get_model_control_service() -> ModelControlService:
    return model_control_service


def _require_admin(user: User) -> None:
    if user.role != "admin":
        raise AppError(
            ErrorCode.PERMISSION_DENIED,
            "只有管理员可以管理模型配置。",
            status_code=403,
            category="model",
            stage="authorization",
        )


@router.get("", response_model=ModelControlPlaneResponse)
async def get_models(
    current_user: User = Depends(get_current_user),
    service: ModelControlService = Depends(get_model_control_service),
) -> ModelControlPlaneResponse:
    _require_admin(current_user)
    snapshot = await run_in_threadpool(service.control_plane)
    return ModelControlPlaneResponse.model_validate(snapshot)


@router.post(
    "",
    response_model=ModelControlPlaneResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_model(
    request: ModelProfileCreateRequest,
    current_user: User = Depends(get_current_user),
    service: ModelControlService = Depends(get_model_control_service),
) -> ModelControlPlaneResponse:
    _require_admin(current_user)
    await run_in_threadpool(
        service.create_profile,
        username=current_user.username,
        **request.model_dump(),
    )
    return ModelControlPlaneResponse.model_validate(
        await run_in_threadpool(service.control_plane)
    )


@router.put("/{profile_id}", response_model=ModelControlPlaneResponse)
async def update_model(
    profile_id: str,
    request: ModelProfileUpdateRequest,
    current_user: User = Depends(get_current_user),
    service: ModelControlService = Depends(get_model_control_service),
) -> ModelControlPlaneResponse:
    _require_admin(current_user)
    await run_in_threadpool(
        service.update_profile,
        username=current_user.username,
        profile_id=profile_id,
        **request.model_dump(),
    )
    return ModelControlPlaneResponse.model_validate(
        await run_in_threadpool(service.control_plane)
    )


@router.delete("/{profile_id}", response_model=ModelDeleteResponse)
async def delete_model(
    profile_id: str,
    current_user: User = Depends(get_current_user),
    service: ModelControlService = Depends(get_model_control_service),
) -> ModelDeleteResponse:
    _require_admin(current_user)
    await run_in_threadpool(
        service.delete_profile,
        username=current_user.username,
        profile_id=profile_id,
    )
    return ModelDeleteResponse(profile_id=profile_id, deleted=True)


@router.put(
    "/assignments/{role}",
    response_model=ModelControlPlaneResponse,
)
async def assign_model(
    role: ModelRole,
    request: ModelAssignmentRequest,
    current_user: User = Depends(get_current_user),
    service: ModelControlService = Depends(get_model_control_service),
) -> ModelControlPlaneResponse:
    _require_admin(current_user)
    await run_in_threadpool(
        service.assign_role,
        username=current_user.username,
        role=role,
        profile_id=request.profile_id,
    )
    return ModelControlPlaneResponse.model_validate(
        await run_in_threadpool(service.control_plane)
    )


__all__ = ["get_model_control_service", "router"]
