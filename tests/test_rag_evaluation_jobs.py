from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.db.models import Base, User, utcnow
from backend.evaluation import (
    RagEvalCase,
    RagEvalDataset,
    RagEvalGatePolicy,
    RagEvalObservation,
    RagExpectedBehavior,
    RagJudgeMetrics,
    RagOutcome,
    RagRoute,
)
from backend.evaluation.contracts import (
    RagEvaluationCaseStatus,
    RagEvaluationJobStatus,
)
from backend.evaluation.repository import RagEvaluationRepository
from backend.evaluation.runtime import RagEvaluationCaseExecution
from backend.evaluation.service import RagEvaluationService
from backend.evaluation.worker import RagEvaluationWorker
from tests.support import TEST_MODEL_SNAPSHOT, static_model_control


def _dataset() -> RagEvalDataset:
    return RagEvalDataset(
        name="automatic_eval_v1",
        cases=(
            RagEvalCase(
                id="no-knowledge",
                tags=("abstention",),
                critical=True,
                question="知识库之外的问题",
                expected=RagExpectedBehavior(
                    route=RagRoute.NO_KNOWLEDGE,
                    outcome=RagOutcome.NO_KNOWLEDGE,
                    acceptable_abstention=True,
                ),
            ),
        ),
    )


def _settings():
    return SimpleNamespace(
        worker=SimpleNamespace(
            evaluation_worker_id="",
            evaluation_poll_seconds=0.01,
            evaluation_lease_seconds=60,
            evaluation_heartbeat_seconds=10,
            evaluation_case_timeout_seconds=30.0,
            evaluation_max_attempts=3,
        )
    )


def _environment():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    Base.metadata.create_all(engine)
    with factory.begin() as db:
        db.add(User(username="admin", password_hash="hash", role="admin"))
    repository = RagEvaluationRepository(factory)
    service = RagEvaluationService(
        repository,
        model_control=static_model_control,
        settings=_settings(),
    )
    return engine, factory, repository, service


def test_dataset_job_case_and_report_are_persisted_behind_one_interface():
    engine, _, repository, service = _environment()
    try:
        first = service.create_dataset(username="admin", dataset=_dataset())
        repeated = service.create_dataset(username="admin", dataset=_dataset())
        assert repeated.id == first.id

        job = service.create_job(username="admin", dataset_id=first.id)
        assert job.status is RagEvaluationJobStatus.QUEUED
        assert job.model_catalog_hash == TEST_MODEL_SNAPSHOT.catalog_hash
        assert set(job.model_snapshot.assignments) == set(
            TEST_MODEL_SNAPSHOT.assignments
        )
        cases = service.list_cases(job.id)
        assert len(cases) == 1
        assert cases[0].status is RagEvaluationCaseStatus.QUEUED

        claimed = repository.claim_next(worker_id="worker-1", lease_seconds=60)
        assert claimed is not None
        assert claimed.job.id == job.id
        case = repository.claim_case(
            job_id=job.id,
            worker_id="worker-1",
            fencing_token=claimed.job.fencing_token,
        )
        assert case is not None
        observation = RagEvalObservation(
            case_id=case.case_id,
            route=RagRoute.NO_KNOWLEDGE,
            outcome=RagOutcome.NO_KNOWLEDGE,
            duration_ms=12,
            judge=RagJudgeMetrics(
                answer_correctness=1,
                groundedness=1,
                answer_relevance=1,
                completeness=1,
                context_relevance=1,
                unsupported_claim_rate=0,
                conflict_disclosure_rate=1,
            ),
        )
        repository.complete_case(
            job_id=job.id,
            case_id=case.case_id,
            worker_id="worker-1",
            fencing_token=claimed.job.fencing_token,
            observation=observation,
            generated_answer="知识库中没有足够可靠的信息。",
            judge_reason="正确拒答",
            judge=observation.judge.model_dump(mode="json"),
            retrieved_identities=[],
            duration_ms=12,
        )
        from backend.evaluation import RagEvalObservationBundle, evaluate_rag

        report = evaluate_rag(
            first.dataset,
            RagEvalObservationBundle(
                dataset_fingerprint=first.fingerprint,
                observations=(observation,),
            ),
            RagEvalGatePolicy(required_provenance="live_rag"),
            metadata={"provenance": "live_rag"},
        )
        repository.finish_job(
            job_id=job.id,
            worker_id="worker-1",
            fencing_token=claimed.job.fencing_token,
            report=report,
        )

        completed = service.get_job(job.id)
        assert completed.status is RagEvaluationJobStatus.SUCCEEDED
        assert completed.report is not None
        assert completed.report.metrics["answer_correctness"].value == 1
        stored_case = service.list_cases(job.id)[0]
        assert stored_case.metrics["answer_correctness"] == 1
        assert stored_case.judge_reason == "正确拒答"
    finally:
        engine.dispose()


def test_cancel_and_orphan_recovery_preserve_durable_job_state():
    engine, _, repository, service = _environment()
    try:
        dataset = service.create_dataset(username="admin", dataset=_dataset())
        cancelled = service.create_job(username="admin", dataset_id=dataset.id)
        cancelled = service.cancel_job(cancelled.id)
        assert cancelled.status is RagEvaluationJobStatus.CANCELLED
        assert (
            service.list_cases(cancelled.id)[0].status
            is RagEvaluationCaseStatus.CANCELLED
        )

        orphan = service.create_job(username="admin", dataset_id=dataset.id)
        claimed = repository.claim_next(worker_id="worker-a", lease_seconds=10)
        assert claimed is not None and claimed.job.id == orphan.id
        recovered = repository.reconcile_expired(now=utcnow() + timedelta(seconds=11))
        assert recovered == (orphan.id,)
        assert service.get_job(orphan.id).status is RagEvaluationJobStatus.QUEUED
        reclaimed = repository.claim_next(worker_id="worker-b", lease_seconds=60)
        assert reclaimed is not None
        assert reclaimed.job.fencing_token == claimed.job.fencing_token + 1
        assert reclaimed.job.attempts == 2
    finally:
        engine.dispose()


class _SuccessfulRuntime:
    def execute_case(self, *, case, **_kwargs):
        judge = RagJudgeMetrics(
            answer_correctness=0.95,
            groundedness=0.9,
            answer_relevance=1,
            completeness=0.9,
            context_relevance=0.8,
            unsupported_claim_rate=0.05,
            conflict_disclosure_rate=1,
        )
        observation = RagEvalObservation(
            case_id=case.id,
            route=RagRoute.NO_KNOWLEDGE,
            outcome=RagOutcome.NO_KNOWLEDGE,
            duration_ms=18,
            judge=judge,
        )
        return RagEvaluationCaseExecution(
            observation=observation,
            generated_answer="知识库中没有足够可靠的信息。",
            judge_reason="拒答与证据状态一致",
            judge={**judge.model_dump(mode="json"), "reason": "拒答与证据状态一致"},
            retrieved_identities=[],
            duration_ms=18,
        )


def test_worker_runs_answer_judge_and_report_to_completion():
    engine, _, repository, service = _environment()
    try:
        dataset = service.create_dataset(username="admin", dataset=_dataset())
        job = service.create_job(username="admin", dataset_id=dataset.id)
        worker = RagEvaluationWorker(
            repository=repository,
            runtime=_SuccessfulRuntime(),
            settings=_settings(),
            worker_id="evaluation-worker-1",
        )

        assert worker.run_once() is True

        completed = service.get_job(job.id)
        assert completed.status is RagEvaluationJobStatus.SUCCEEDED
        assert completed.completed_cases == completed.total_cases == 1
        assert completed.report is not None
        assert completed.report.metadata["provenance"] == "live_rag"
        assert completed.report.metrics["groundedness"].value == 0.9
        case = service.list_cases(job.id)[0]
        assert case.status is RagEvaluationCaseStatus.COMPLETED
        assert case.generated_answer
        assert case.retrieved_identities == ()
    finally:
        engine.dispose()


def test_evaluation_worker_entrypoint_initializes_model_control_and_provider_runtime():
    import backend.workers.evaluation as worker_entrypoint

    events: list[str] = []

    class Provider:
        def start_sync(self):
            events.append("provider.start")

        def close_sync(self):
            events.append("provider.close")

    class Worker:
        def __init__(self, *, settings):
            assert settings is configured_settings

        def run_forever(self, _stop_event):
            events.append("worker.run")

    configured_settings = SimpleNamespace(
        observability=SimpleNamespace(log_level="INFO"),
        validate_startup=lambda: events.append("settings.validate"),
    )
    with (
        patch.object(
            worker_entrypoint, "get_settings", return_value=configured_settings
        ),
        patch.object(
            worker_entrypoint, "init_db", side_effect=lambda: events.append("db.init")
        ),
        patch.object(
            worker_entrypoint,
            "model_control_service",
            SimpleNamespace(
                ensure_environment_defaults=lambda: events.append("models.seed")
            ),
        ),
        patch.object(worker_entrypoint, "provider_runtime", Provider()),
        patch.object(worker_entrypoint, "RagEvaluationWorker", Worker),
        patch.object(worker_entrypoint.signal, "signal"),
    ):
        assert worker_entrypoint.main([]) == 0

    assert events == [
        "settings.validate",
        "db.init",
        "models.seed",
        "provider.start",
        "worker.run",
        "provider.close",
    ]
