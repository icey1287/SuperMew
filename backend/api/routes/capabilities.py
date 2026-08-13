from fastapi import APIRouter, Depends, status
from starlette.concurrency import run_in_threadpool

from backend.capabilities.catalog import CapabilityCatalog
from backend.capabilities.control_service import (
    CapabilityControlService,
    capability_control_service,
)
from backend.capabilities.runtime import runtime_capability_catalog
from backend.core.errors import AppError, ErrorCode
from backend.db.models import User
from backend.infra.auth import get_current_user
from backend.schemas.capabilities import (
    CapabilityControlPlaneResponse,
    CapabilityDeleteResponse,
    CapabilityResponse,
    ManagedHttpToolCreateRequest,
    ManagedHttpToolUpdateRequest,
    ManagedSkillCreateRequest,
    ManagedSkillUpdateRequest,
    SqlAssistantConfigUpdateRequest,
    WebResearchConfigUpdateRequest,
)


router = APIRouter(prefix="/v1", tags=["capabilities"])


def get_capability_catalog() -> CapabilityCatalog:
    return runtime_capability_catalog


def get_capability_control_service() -> CapabilityControlService:
    return capability_control_service


def _require_admin(user: User) -> None:
    if user.role != "admin":
        raise AppError(
            ErrorCode.PERMISSION_DENIED,
            "只有管理员可以管理 Skill 与 Tool。",
            status_code=403,
            category="capability",
            stage="authorization",
        )


async def _apply(service: CapabilityControlService) -> None:
    await run_in_threadpool(service.apply_runtime)


async def _apply_skills(service: CapabilityControlService) -> None:
    await run_in_threadpool(service.apply_skills)


@router.get("/capabilities", response_model=CapabilityResponse)
def get_capabilities(
    current_user: User = Depends(get_current_user),
    catalog: CapabilityCatalog = Depends(get_capability_catalog),
) -> CapabilityResponse:
    return CapabilityResponse.model_validate(
        catalog.snapshot(role=current_user.role),
    )


@router.get(
    "/capabilities/control-plane",
    response_model=CapabilityControlPlaneResponse,
)
async def get_capability_control_plane(
    current_user: User = Depends(get_current_user),
    service: CapabilityControlService = Depends(get_capability_control_service),
) -> CapabilityControlPlaneResponse:
    _require_admin(current_user)
    return CapabilityControlPlaneResponse.model_validate(
        await run_in_threadpool(service.control_plane)
    )


@router.post(
    "/capabilities/skills",
    response_model=CapabilityControlPlaneResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_managed_skill(
    request: ManagedSkillCreateRequest,
    current_user: User = Depends(get_current_user),
    service: CapabilityControlService = Depends(get_capability_control_service),
) -> CapabilityControlPlaneResponse:
    _require_admin(current_user)
    await run_in_threadpool(
        service.create_skill,
        username=current_user.username,
        **request.model_dump(),
    )
    await _apply_skills(service)
    return CapabilityControlPlaneResponse.model_validate(
        await run_in_threadpool(service.control_plane)
    )


@router.put(
    "/capabilities/skills/{name}",
    response_model=CapabilityControlPlaneResponse,
)
async def update_managed_skill(
    name: str,
    request: ManagedSkillUpdateRequest,
    current_user: User = Depends(get_current_user),
    service: CapabilityControlService = Depends(get_capability_control_service),
) -> CapabilityControlPlaneResponse:
    _require_admin(current_user)
    await run_in_threadpool(
        service.update_skill,
        username=current_user.username,
        name=name,
        **request.model_dump(),
    )
    await _apply_skills(service)
    return CapabilityControlPlaneResponse.model_validate(
        await run_in_threadpool(service.control_plane)
    )


@router.delete(
    "/capabilities/skills/{name}",
    response_model=CapabilityDeleteResponse,
)
async def delete_managed_skill(
    name: str,
    current_user: User = Depends(get_current_user),
    service: CapabilityControlService = Depends(get_capability_control_service),
) -> CapabilityDeleteResponse:
    _require_admin(current_user)
    await run_in_threadpool(
        service.delete_skill,
        username=current_user.username,
        name=name,
    )
    await _apply_skills(service)
    return CapabilityDeleteResponse(name=name)


@router.post(
    "/capabilities/tools",
    response_model=CapabilityControlPlaneResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_managed_tool(
    request: ManagedHttpToolCreateRequest,
    current_user: User = Depends(get_current_user),
    service: CapabilityControlService = Depends(get_capability_control_service),
) -> CapabilityControlPlaneResponse:
    _require_admin(current_user)
    await run_in_threadpool(
        service.create_http_tool,
        username=current_user.username,
        **request.model_dump(),
    )
    await _apply(service)
    return CapabilityControlPlaneResponse.model_validate(
        await run_in_threadpool(service.control_plane)
    )


@router.put(
    "/capabilities/tools/{name}",
    response_model=CapabilityControlPlaneResponse,
)
async def update_managed_tool(
    name: str,
    request: ManagedHttpToolUpdateRequest,
    current_user: User = Depends(get_current_user),
    service: CapabilityControlService = Depends(get_capability_control_service),
) -> CapabilityControlPlaneResponse:
    _require_admin(current_user)
    await run_in_threadpool(
        service.update_http_tool,
        username=current_user.username,
        name=name,
        **request.model_dump(),
    )
    await _apply(service)
    return CapabilityControlPlaneResponse.model_validate(
        await run_in_threadpool(service.control_plane)
    )


@router.delete(
    "/capabilities/tools/{name}",
    response_model=CapabilityDeleteResponse,
)
async def delete_managed_tool(
    name: str,
    current_user: User = Depends(get_current_user),
    service: CapabilityControlService = Depends(get_capability_control_service),
) -> CapabilityDeleteResponse:
    _require_admin(current_user)
    await run_in_threadpool(
        service.delete_http_tool,
        username=current_user.username,
        name=name,
    )
    await _apply(service)
    return CapabilityDeleteResponse(name=name)


@router.put(
    "/capabilities/sql-assistant",
    response_model=CapabilityControlPlaneResponse,
)
async def update_sql_assistant(
    request: SqlAssistantConfigUpdateRequest,
    current_user: User = Depends(get_current_user),
    service: CapabilityControlService = Depends(get_capability_control_service),
) -> CapabilityControlPlaneResponse:
    _require_admin(current_user)
    await run_in_threadpool(
        service.update_sql_assistant,
        username=current_user.username,
        **request.model_dump(),
    )
    await _apply(service)
    return CapabilityControlPlaneResponse.model_validate(
        await run_in_threadpool(service.control_plane)
    )


@router.put(
    "/capabilities/web-research",
    response_model=CapabilityControlPlaneResponse,
)
async def update_web_research(
    request: WebResearchConfigUpdateRequest,
    current_user: User = Depends(get_current_user),
    service: CapabilityControlService = Depends(get_capability_control_service),
) -> CapabilityControlPlaneResponse:
    _require_admin(current_user)
    await run_in_threadpool(
        service.update_web_research,
        username=current_user.username,
        enabled=request.enabled,
    )
    await _apply(service)
    return CapabilityControlPlaneResponse.model_validate(
        await run_in_threadpool(service.control_plane)
    )


__all__ = [
    "get_capability_catalog",
    "get_capability_control_service",
    "router",
]
