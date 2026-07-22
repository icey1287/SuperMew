import asyncio

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from backend.api.resources import (
    document_catalog,
    document_publication,
)
from backend.capabilities.control_service import capability_control_service
from backend.providers.runtime import provider_runtime
from backend.sandbox import get_sandbox_runtime
from backend.sql_assistant.runtime import get_sql_assistant_runtime
from backend.web_research.runtime import get_web_research_runtime


router = APIRouter(tags=["health"])


@router.get("/health/live")
async def live() -> dict[str, str]:
    return {"status": "live"}


@router.get("/health/ready")
async def ready() -> JSONResponse:
    snapshot = provider_runtime.readiness()
    worker_settings = provider_runtime.settings.worker
    capability_settings = (
        capability_control_service.active_settings or provider_runtime.settings
    )
    sql_enabled = bool(
        getattr(
            getattr(capability_settings, "sql_assistant", None),
            "enabled",
            False,
        )
    )
    sql_ready = False
    sql_catalog_hash = None
    if sql_enabled:
        try:
            sql_snapshot = get_sql_assistant_runtime().readiness()
            sql_ready = bool(sql_snapshot.ready)
            sql_catalog_hash = sql_snapshot.catalog_hash
        except Exception:
            sql_ready = False
    web_enabled = bool(
        getattr(
            getattr(capability_settings, "web_research", None),
            "enabled",
            False,
        )
    )
    web_ready = False
    web_search_ready = False
    if web_enabled:
        try:
            web_snapshot = get_web_research_runtime().readiness()
            web_ready = bool(web_snapshot.get("ready"))
            web_search_ready = bool(web_snapshot.get("search_ready"))
        except Exception:
            web_ready = False
    sandbox_enabled = bool(
        getattr(
            getattr(capability_settings, "sandbox", None),
            "enabled",
            False,
        )
    )
    sandbox_snapshot = None
    sandbox_ready = False
    if sandbox_enabled:
        try:
            sandbox_snapshot = get_sandbox_runtime().readiness()
            sandbox_ready = bool(sandbox_snapshot.ready)
        except Exception:
            sandbox_ready = False
    try:
        catalog_fingerprint = await asyncio.to_thread(
            document_catalog.current_index_fingerprint,
            tenant_id=document_publication.config.tenant_id,
        )
        catalog_available = True
    except Exception:
        catalog_fingerprint = None
        catalog_available = False
    try:
        worker_state = await asyncio.to_thread(
            document_catalog.worker_readiness,
            worker_kind="indexing",
            stale_after_seconds=worker_settings.indexing_readiness_ttl_seconds,
            expected_build_fingerprint=(
                document_publication.config.build_profile.fingerprint
            ),
        )
        worker_available = True
    except Exception:
        worker_state = None
        worker_available = False
    warmup_required = provider_runtime.settings.embedding.warmup_on_start
    is_ready = (
        snapshot.running
        and snapshot.embedding.ready
        and catalog_available
        and (not sql_enabled or sql_ready)
        and (not web_enabled or web_ready)
        and (not sandbox_enabled or sandbox_ready)
        and (
            not worker_settings.indexing_worker_required
            or bool(worker_state and worker_state.ready)
        )
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
            "sql_assistant": {
                "enabled": sql_enabled,
                "ready": sql_ready,
                "catalog_hash": sql_catalog_hash,
            },
            "web_research": {
                "enabled": web_enabled,
                "ready": web_ready,
                "search_ready": web_search_ready,
            },
            "sandbox": {
                "enabled": sandbox_enabled,
                "ready": sandbox_ready,
                "adapter": (
                    sandbox_snapshot.adapter
                    if sandbox_snapshot is not None
                    else (None if sandbox_enabled else "disabled")
                ),
                "daemon_reachable": bool(
                    sandbox_snapshot and sandbox_snapshot.daemon_reachable
                ),
                "image_available": bool(
                    sandbox_snapshot and sandbox_snapshot.image_available
                ),
                "active_executions": (
                    sandbox_snapshot.active_executions
                    if sandbox_snapshot is not None
                    else 0
                ),
            },
            "document_catalog": {
                "available": catalog_available,
                "fingerprint": catalog_fingerprint,
            },
            "indexing_worker": {
                "required": worker_settings.indexing_worker_required,
                "available": worker_available,
                "ready": bool(worker_state and worker_state.ready),
                "fresh_workers": (worker_state.fresh_workers if worker_state else 0),
                "incompatible_fresh_workers": (
                    worker_state.incompatible_fresh_workers if worker_state else 0
                ),
                "expected_build_fingerprint": (
                    worker_state.expected_build_fingerprint
                    if worker_state
                    else document_publication.config.build_profile.fingerprint
                ),
                "latest_heartbeat_at": (
                    worker_state.latest_heartbeat_at.isoformat()
                    if worker_state and worker_state.latest_heartbeat_at
                    else None
                ),
                "queue_counts": (worker_state.queue_counts if worker_state else {}),
                "oldest_ready_at": (
                    worker_state.oldest_ready_at.isoformat()
                    if worker_state and worker_state.oldest_ready_at
                    else None
                ),
            },
        },
    )


__all__ = ["router"]
