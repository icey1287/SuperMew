import asyncio

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from backend.api.resources import (
    document_catalog,
    document_publication,
    milvus_manager,
)
from backend.providers.runtime import provider_runtime


router = APIRouter(tags=["health"])


@router.get("/health/live")
async def live() -> dict[str, str]:
    return {"status": "live"}


@router.get("/health/ready")
async def ready() -> JSONResponse:
    snapshot = provider_runtime.readiness()
    try:
        catalog_state = await asyncio.to_thread(
            document_catalog.legacy_adoption_state,
            tenant_id=document_publication.config.tenant_id,
        )
        catalog_available = True
    except Exception:
        catalog_state = None
        catalog_available = False
    catalog_collection_matches = bool(
        catalog_state
        and catalog_state.legacy_collection == milvus_manager.collection_name
    )
    catalog_target_matches = bool(
        catalog_state
        and catalog_state.knowledge_base_name
        == document_publication.config.knowledge_base_name
    )
    warmup_required = provider_runtime.settings.embedding.warmup_on_start
    is_ready = (
        snapshot.running
        and snapshot.embedding.ready
        and catalog_available
        and catalog_state.complete
        and catalog_collection_matches
        and catalog_target_matches
    )
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
            "document_catalog": {
                "available": catalog_available,
                "legacy_adoption_complete": (
                    catalog_state.complete if catalog_state else False
                ),
                "state_exists": catalog_state.state_exists if catalog_state else False,
                "legacy_collection_matches": catalog_collection_matches,
                "legacy_target_matches": catalog_target_matches,
                "fingerprint": catalog_state.fingerprint if catalog_state else None,
            },
        },
    )


__all__ = ["router"]
