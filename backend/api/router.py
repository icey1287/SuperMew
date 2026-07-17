from fastapi import APIRouter

from backend.api.routes.auth import router as auth_router
from backend.api.routes.chat import router as chat_router
from backend.api.routes.documents import router as documents_router
from backend.api.routes.health import router as health_router
from backend.api.routes.threads import router as threads_router
from backend.api.routes.sessions import router as sessions_router
from backend.api.routes.runs import router as runs_router

router = APIRouter()
router.include_router(health_router)
router.include_router(auth_router)
router.include_router(threads_router)
router.include_router(sessions_router)
router.include_router(chat_router)
router.include_router(documents_router)
router.include_router(runs_router)
