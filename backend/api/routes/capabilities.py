from fastapi import APIRouter, Depends

from backend.capabilities.catalog import CapabilityCatalog
from backend.capabilities.runtime import runtime_capability_catalog
from backend.db.models import User
from backend.infra.auth import get_current_user
from backend.schemas.capabilities import CapabilityResponse


router = APIRouter(prefix="/v1", tags=["capabilities"])


def get_capability_catalog() -> CapabilityCatalog:
    return runtime_capability_catalog


@router.get("/capabilities", response_model=CapabilityResponse)
def get_capabilities(
    current_user: User = Depends(get_current_user),
    catalog: CapabilityCatalog = Depends(get_capability_catalog),
) -> CapabilityResponse:
    return CapabilityResponse.model_validate(
        catalog.snapshot(role=current_user.role),
    )


__all__ = ["get_capability_catalog", "router"]
