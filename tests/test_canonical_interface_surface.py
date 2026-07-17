from inspect import signature

from fastapi import FastAPI

from backend.api.router import router
from backend.documents.catalog import DocumentCatalog


def test_application_exposes_only_canonical_public_interfaces() -> None:
    app = FastAPI()
    app.include_router(router)
    assert set(app.openapi()["paths"]) == {
        "/auth/login",
        "/auth/logout",
        "/auth/logout-all",
        "/auth/me",
        "/auth/refresh",
        "/auth/register",
        "/documents",
        "/documents/delete/async/{filename}",
        "/documents/delete/jobs",
        "/documents/delete/jobs/{job_id}",
        "/documents/upload/async",
        "/documents/upload/jobs",
        "/documents/upload/jobs/{job_id}",
        "/health/live",
        "/health/ready",
        "/v1/runs/{run_id}",
        "/v1/runs/{run_id}/cancel",
        "/v1/runs/{run_id}/events",
        "/v1/runs/{run_id}/resume",
        "/v1/runs/{run_id}/stream",
        "/v1/threads",
        "/v1/threads/{thread_id}",
        "/v1/threads/{thread_id}/messages",
        "/v1/threads/{thread_id}/runs",
        "/v1/threads/{thread_id}/runs/stream",
    }


def test_cleanup_grace_is_not_exposed_to_callers() -> None:
    assert "cleanup_grace" not in signature(DocumentCatalog.publish).parameters
    assert "cleanup_grace" not in signature(DocumentCatalog.reserve_upload).parameters
    assert "cleanup_grace" not in signature(DocumentCatalog.retire).parameters
