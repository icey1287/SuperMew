from fastapi import APIRouter
from fastapi.responses import JSONResponse

from backend.providers.runtime import provider_runtime


router = APIRouter(tags=["health"])


@router.get("/health/live")
async def live() -> dict[str, str]:
    return {"status": "live"}


@router.get("/health/ready")
async def ready() -> JSONResponse:
    snapshot = provider_runtime.readiness()
    warmup_required = provider_runtime.settings.embedding.warmup_on_start
    is_ready = snapshot.running and snapshot.embedding.ready
    return JSONResponse(
        status_code=200 if is_ready else 503,
        content={
            "status": "ready" if is_ready else "not_ready",
            "provider_runtime": {
                "running": snapshot.running,
                "embedding": {
                    "ready": snapshot.embedding.ready,
                    "model_loaded": snapshot.embedding.model_loaded,
                    "warmup_required": warmup_required,
                    "dimension": snapshot.embedding.dimension,
                    "queue_depth": snapshot.embedding.queue_depth,
                    "inflight": snapshot.embedding.inflight,
                },
                "rerank": {
                    "enabled": snapshot.rerank_enabled,
                    "model": snapshot.rerank_model,
                },
            },
        },
    )


__all__ = ["router"]
