from fastapi import APIRouter

from backend.api.routes.auth import router as auth_router
from backend.api.routes.capabilities import router as capabilities_router
from backend.api.routes.documents import router as documents_router
from backend.api.routes.evaluations import router as evaluations_router
from backend.api.routes.health import router as health_router
from backend.api.routes.legacy_chat import router as legacy_chat_router
from backend.api.routes.models import router as models_router
from backend.api.routes.runs import router as runs_router
from backend.api.routes.threads import router as threads_router

router = APIRouter()
router.include_router(health_router)
router.include_router(auth_router)
router.include_router(capabilities_router)
router.include_router(models_router)
router.include_router(evaluations_router)
router.include_router(threads_router)
router.include_router(documents_router)
router.include_router(runs_router)
router.include_router(legacy_chat_router)
