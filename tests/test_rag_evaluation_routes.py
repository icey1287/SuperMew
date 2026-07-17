from __future__ import annotations

from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.api.routes.evaluations import (
    get_rag_evaluation_service,
    router,
)
from backend.core.errors import install_exception_handlers
from backend.db.models import Base, User
from backend.evaluation.repository import RagEvaluationRepository
from backend.evaluation.service import RagEvaluationService
from backend.infra.auth import get_current_user
from tests.support import static_model_control


def _dataset_payload():
    return {
        "dataset": {
            "schema_version": 1,
            "name": "route_eval_v1",
            "cases": [
                {
                    "id": "case-1",
                    "question": "知识库之外的问题",
                    "expected": {
                        "route": "no_knowledge",
                        "outcome": "NO_KNOWLEDGE",
                        "acceptable_abstention": True,
                    },
                }
            ],
        }
    }


def _app(role: str = "admin"):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    Base.metadata.create_all(engine)
    with factory.begin() as db:
        db.add(User(username="admin", password_hash="hash", role="admin"))
    service = RagEvaluationService(
        RagEvaluationRepository(factory),
        model_control=static_model_control,
        settings=SimpleNamespace(worker=SimpleNamespace(evaluation_max_attempts=3)),
    )
    app = FastAPI()
    install_exception_handlers(app)
    app.include_router(router)
    app.dependency_overrides[get_current_user] = lambda: User(
        id=1,
        username="admin",
        password_hash="hash",
        role=role,
    )
    app.dependency_overrides[get_rag_evaluation_service] = lambda: service
    return app, engine


def test_admin_can_create_dataset_start_job_inspect_cases_and_cancel():
    app, engine = _app()
    try:
        with TestClient(app) as client:
            dataset_response = client.post(
                "/v1/rag-evaluations/datasets",
                json=_dataset_payload(),
            )
            assert dataset_response.status_code == 201
            dataset = dataset_response.json()
            assert dataset["case_count"] == 1

            job_response = client.post(
                "/v1/rag-evaluations/jobs",
                json={"dataset_id": dataset["id"]},
            )
            assert job_response.status_code == 202
            job = job_response.json()
            assert job["status"] == "queued"
            assert set(job["models"]) == {
                "answer",
                "fast",
                "grader",
                "evaluator",
            }
            serialized = job_response.text
            assert "models.test" not in serialized
            assert "api_key" not in serialized.lower()

            cases_response = client.get(f"/v1/rag-evaluations/jobs/{job['id']}/cases")
            assert cases_response.status_code == 200
            assert cases_response.json()["cases"][0]["question"]

            cancelled = client.post(f"/v1/rag-evaluations/jobs/{job['id']}/cancel")
            assert cancelled.status_code == 200
            assert cancelled.json()["status"] == "cancelled"
    finally:
        engine.dispose()


def test_non_admin_cannot_access_rag_evaluation_workbench():
    app, engine = _app(role="user")
    try:
        with TestClient(app) as client:
            response = client.get("/v1/rag-evaluations/jobs")
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "PERMISSION_DENIED"
    finally:
        engine.dispose()
