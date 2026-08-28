from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timedelta
from uuid import uuid4

from pydantic import ValidationError
from sqlalchemy.orm import Session, aliased

from backend.core.errors import (
    AppError,
    ErrorCode,
    deserialize_public_error,
)
from backend.db.models import (
    RagEvaluationCaseRecord as RagEvaluationCaseRow,
    RagEvaluationDataset as RagEvaluationDatasetRow,
    RagEvaluationJob as RagEvaluationJobRow,
    User,
    WorkerHeartbeat,
    utcnow,
)
from backend.evaluation.contracts import (
    ClaimedRagEvaluationJob,
    RagEvaluationCaseRecord,
    RagEvaluationCaseStatus,
    RagEvaluationDatasetRecord,
    RagEvaluationJobRecord,
    RagEvaluationJobStatus,
)
from backend.evaluation.rag import (
    RagEvalDataset,
    RagEvalGatePolicy,
    RagEvalObservation,
    RagEvalObservationBundle,
    RagEvalReport,
    dataset_fingerprint,
    evaluate_rag_partial,
)
from backend.infra.database import SessionLocal
from backend.model_control import ModelCatalogSnapshot


SessionFactory = Callable[[], Session]
logger = logging.getLogger(__name__)


class RagEvaluationRepository:
    """Persist Dataset, Evaluation Job, Case progress and worker ownership."""

    def __init__(self, session_factory: SessionFactory = SessionLocal) -> None:
        self._session_factory = session_factory

    @staticmethod
    def _user(db: Session, username: str) -> User:
        user = db.query(User).filter(User.username == username).first()
        if user is None:
            raise AppError(
                ErrorCode.AUTHENTICATION_REQUIRED,
                "用户不存在或已失效",
                status_code=401,
            )
        return user

    @staticmethod
    def _dataset_record(
        row: RagEvaluationDatasetRow,
        created_by: str | None,
    ) -> RagEvaluationDatasetRecord:
        try:
            dataset = RagEvalDataset.model_validate(row.payload_json)
        except ValidationError as exc:
            raise AppError(
                "RAG_EVALUATION_DATASET_INVALID",
                "持久化 RAG 评估数据集无效",
                status_code=409,
                category="evaluation",
                stage="dataset",
            ) from exc
        if dataset_fingerprint(dataset) != row.fingerprint:
            raise AppError(
                "RAG_EVALUATION_DATASET_INVALID",
                "RAG 评估数据集 fingerprint 不匹配",
                status_code=409,
                category="evaluation",
                stage="dataset",
            )
        return RagEvaluationDatasetRecord(
            id=row.id,
            name=row.name,
            fingerprint=row.fingerprint,
            case_count=row.case_count,
            dataset=dataset,
            created_by=created_by,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

    @staticmethod
    def _job_record(
        row: RagEvaluationJobRow,
        dataset: RagEvaluationDatasetRow,
        created_by: str | None,
    ) -> RagEvaluationJobRecord:
        try:
            policy = RagEvalGatePolicy.model_validate(row.gate_policy_json)
            model_snapshot = ModelCatalogSnapshot.model_validate(
                row.model_snapshot_json
            )
            report = (
                RagEvalReport.model_validate(row.report_json)
                if row.report_json is not None
                else None
            )
        except ValidationError as exc:
            raise AppError(
                "RAG_EVALUATION_JOB_INVALID",
                "持久化 RAG Evaluation Job 快照无效",
                status_code=409,
                category="evaluation",
                stage="snapshot",
            ) from exc
        if model_snapshot.catalog_hash != row.model_catalog_hash:
            raise AppError(
                "RAG_EVALUATION_JOB_INVALID",
                "RAG Evaluation Job 模型目录哈希不匹配",
                status_code=409,
                category="evaluation",
                stage="snapshot",
            )
        public_error = deserialize_public_error(row.error_detail_redacted)
        return RagEvaluationJobRecord(
            id=row.id,
            dataset_id=row.dataset_id,
            dataset_name=dataset.name,
            dataset_fingerprint=dataset.fingerprint,
            baseline_job_id=row.baseline_job_id,
            status=RagEvaluationJobStatus(row.status),
            completed_cases=row.completed_cases,
            total_cases=row.total_cases,
            gate_policy=policy,
            model_catalog_hash=row.model_catalog_hash,
            model_snapshot=model_snapshot,
            owner_worker_id=row.owner_worker_id,
            lease_expires_at=row.lease_expires_at,
            fencing_token=row.fencing_token,
            attempts=row.attempts,
            max_attempts=row.max_attempts,
            error_code=row.error_code,
            error=public_error.contract() if public_error else None,
            report=report,
            created_by=created_by,
            started_at=row.started_at,
            finished_at=row.finished_at,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

    @staticmethod
    def _case_record(row: RagEvaluationCaseRow) -> RagEvaluationCaseRecord:
        try:
            observation = (
                RagEvalObservation.model_validate(row.observation_json)
                if row.observation_json is not None
                else None
            )
        except ValidationError as exc:
            raise AppError(
                "RAG_EVALUATION_CASE_INVALID",
                "持久化 RAG Evaluation Case 观察结果无效",
                status_code=409,
                category="evaluation",
                stage="case",
            ) from exc
        public_error = deserialize_public_error(row.error_detail_redacted)
        return RagEvaluationCaseRecord(
            id=row.id,
            job_id=row.job_id,
            case_id=row.case_id,
            position=row.position,
            status=RagEvaluationCaseStatus(row.status),
            question=row.question,
            generated_answer=row.generated_answer,
            judge_reason=row.judge_reason,
            observation=observation,
            judge=dict(row.judge_json) if row.judge_json is not None else None,
            metrics=dict(row.metrics_json or {}),
            checks=dict(row.checks_json or {}),
            retrieved_identities=tuple(row.retrieved_identity_json or ()),
            provider_error_code=row.provider_error_code,
            provider_error_stage=row.provider_error_stage,
            duration_ms=row.duration_ms,
            error_code=row.error_code,
            error=public_error.contract() if public_error else None,
            started_at=row.started_at,
            finished_at=row.finished_at,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

    def create_dataset(
        self,
        *,
        username: str,
        dataset: RagEvalDataset,
    ) -> RagEvaluationDatasetRecord:
        fingerprint = dataset_fingerprint(dataset)
        db = self._session_factory()
        try:
            with db.begin():
                user = self._user(db, username)
                existing = (
                    db.query(RagEvaluationDatasetRow)
                    .filter(RagEvaluationDatasetRow.fingerprint == fingerprint)
                    .first()
                )
                if existing is not None:
                    creator = (
                        db.query(User.username)
                        .filter(User.id == existing.created_by_user_id)
                        .scalar()
                    )
                    return self._dataset_record(existing, creator)
                now = utcnow()
                row = RagEvaluationDatasetRow(
                    id=f"rag_dataset_{uuid4().hex}",
                    name=dataset.name,
                    fingerprint=fingerprint,
                    payload_json=dataset.model_dump(mode="json"),
                    case_count=len(dataset.cases),
                    created_by_user_id=user.id,
                    created_at=now,
                    updated_at=now,
                )
                db.add(row)
                db.flush()
                return self._dataset_record(row, user.username)
        finally:
            db.close()

    def list_datasets(
        self, *, limit: int = 100
    ) -> tuple[RagEvaluationDatasetRecord, ...]:
        db = self._session_factory()
        try:
            creator = aliased(User)
            rows = (
                db.query(RagEvaluationDatasetRow, creator.username)
                .outerjoin(
                    creator,
                    creator.id == RagEvaluationDatasetRow.created_by_user_id,
                )
                .order_by(RagEvaluationDatasetRow.created_at.desc())
                .limit(max(1, min(limit, 500)))
                .all()
            )
            return tuple(self._dataset_record(row, username) for row, username in rows)
        finally:
            db.close()

    def get_dataset(self, dataset_id: str) -> RagEvaluationDatasetRecord:
        db = self._session_factory()
        try:
            creator = aliased(User)
            result = (
                db.query(RagEvaluationDatasetRow, creator.username)
                .outerjoin(
                    creator,
                    creator.id == RagEvaluationDatasetRow.created_by_user_id,
                )
                .filter(RagEvaluationDatasetRow.id == dataset_id)
                .first()
            )
            if result is None:
                raise AppError(
                    ErrorCode.NOT_FOUND,
                    "RAG 评估数据集不存在",
                    status_code=404,
                    category="evaluation",
                    stage="dataset",
                )
            return self._dataset_record(*result)
        finally:
            db.close()

    def create_job(
        self,
        *,
        username: str,
        dataset_id: str,
        gate_policy: RagEvalGatePolicy,
        model_snapshot: ModelCatalogSnapshot,
        baseline_job_id: str | None = None,
        max_attempts: int = 3,
    ) -> RagEvaluationJobRecord:
        db = self._session_factory()
        try:
            with db.begin():
                user = self._user(db, username)
                dataset = (
                    db.query(RagEvaluationDatasetRow)
                    .filter(RagEvaluationDatasetRow.id == dataset_id)
                    .first()
                )
                if dataset is None:
                    raise AppError(
                        ErrorCode.NOT_FOUND,
                        "RAG 评估数据集不存在",
                        status_code=404,
                        category="evaluation",
                        stage="dataset",
                    )
                if baseline_job_id is not None:
                    baseline = (
                        db.query(RagEvaluationJobRow)
                        .filter(RagEvaluationJobRow.id == baseline_job_id)
                        .first()
                    )
                    if baseline is None:
                        raise AppError(
                            ErrorCode.NOT_FOUND,
                            "Baseline Evaluation Job 不存在",
                            status_code=404,
                            category="evaluation",
                            stage="baseline",
                        )
                    if (
                        baseline.dataset_id != dataset.id
                        or baseline.status != RagEvaluationJobStatus.SUCCEEDED.value
                        or baseline.report_json is None
                    ):
                        raise AppError(
                            ErrorCode.CONFLICT,
                            "Baseline 必须是同一数据集的成功 Evaluation Job",
                            status_code=409,
                            category="evaluation",
                            stage="baseline",
                        )
                validated_dataset = RagEvalDataset.model_validate(dataset.payload_json)
                now = utcnow()
                job = RagEvaluationJobRow(
                    id=f"rag_eval_{uuid4().hex}",
                    dataset_id=dataset.id,
                    baseline_job_id=baseline_job_id,
                    status=RagEvaluationJobStatus.QUEUED.value,
                    completed_cases=0,
                    total_cases=len(validated_dataset.cases),
                    gate_policy_json=gate_policy.model_dump(mode="json"),
                    model_catalog_hash=model_snapshot.catalog_hash,
                    model_snapshot_json=model_snapshot.model_dump(mode="json"),
                    fencing_token=0,
                    attempts=0,
                    max_attempts=max(1, max_attempts),
                    created_by_user_id=user.id,
                    created_at=now,
                    updated_at=now,
                )
                db.add(job)
                db.flush()
                for position, case in enumerate(validated_dataset.cases, 1):
                    db.add(
                        RagEvaluationCaseRow(
                            id=f"rag_case_{uuid4().hex}",
                            job_id=job.id,
                            case_id=case.id,
                            position=position,
                            status=RagEvaluationCaseStatus.QUEUED.value,
                            question=case.question,
                            metrics_json={},
                            checks_json={},
                            retrieved_identity_json=[],
                            created_at=now,
                            updated_at=now,
                        )
                    )
                db.flush()
                return self._job_record(job, dataset, user.username)
        finally:
            db.close()

    def list_jobs(
        self,
        *,
        status: RagEvaluationJobStatus | str | None = None,
        limit: int = 100,
    ) -> tuple[RagEvaluationJobRecord, ...]:
        db = self._session_factory()
        try:
            creator = aliased(User)
            query = (
                db.query(RagEvaluationJobRow, RagEvaluationDatasetRow, creator.username)
                .join(
                    RagEvaluationDatasetRow,
                    RagEvaluationDatasetRow.id == RagEvaluationJobRow.dataset_id,
                )
                .outerjoin(
                    creator,
                    creator.id == RagEvaluationJobRow.created_by_user_id,
                )
            )
            if status is not None:
                query = query.filter(
                    RagEvaluationJobRow.status == RagEvaluationJobStatus(status).value
                )
            rows = (
                query.order_by(RagEvaluationJobRow.created_at.desc())
                .limit(max(1, min(limit, 500)))
                .all()
            )
            return tuple(
                self._job_record(job, dataset, username)
                for job, dataset, username in rows
            )
        finally:
            db.close()

    def get_job(self, job_id: str) -> RagEvaluationJobRecord:
        db = self._session_factory()
        try:
            creator = aliased(User)
            result = (
                db.query(RagEvaluationJobRow, RagEvaluationDatasetRow, creator.username)
                .join(
                    RagEvaluationDatasetRow,
                    RagEvaluationDatasetRow.id == RagEvaluationJobRow.dataset_id,
                )
                .outerjoin(
                    creator,
                    creator.id == RagEvaluationJobRow.created_by_user_id,
                )
                .filter(RagEvaluationJobRow.id == job_id)
                .first()
            )
            if result is None:
                raise AppError(
                    ErrorCode.NOT_FOUND,
                    "RAG Evaluation Job 不存在",
                    status_code=404,
                    category="evaluation",
                    stage="job",
                )
            return self._job_record(*result)
        finally:
            db.close()

    def list_cases(self, job_id: str) -> tuple[RagEvaluationCaseRecord, ...]:
        db = self._session_factory()
        try:
            exists = (
                db.query(RagEvaluationJobRow.id)
                .filter(RagEvaluationJobRow.id == job_id)
                .first()
            )
            if exists is None:
                raise AppError(
                    ErrorCode.NOT_FOUND,
                    "RAG Evaluation Job 不存在",
                    status_code=404,
                    category="evaluation",
                    stage="job",
                )
            rows = (
                db.query(RagEvaluationCaseRow)
                .filter(RagEvaluationCaseRow.job_id == job_id)
                .order_by(RagEvaluationCaseRow.position.asc())
                .all()
            )
            return tuple(self._case_record(row) for row in rows)
        finally:
            db.close()

    def cancel(self, *, job_id: str) -> RagEvaluationJobRecord:
        db = self._session_factory()
        try:
            with db.begin():
                job = (
                    db.query(RagEvaluationJobRow)
                    .filter(RagEvaluationJobRow.id == job_id)
                    .with_for_update()
                    .first()
                )
                if job is None:
                    raise AppError(
                        ErrorCode.NOT_FOUND,
                        "RAG Evaluation Job 不存在",
                        status_code=404,
                        category="evaluation",
                        stage="job",
                    )
                if job.status == RagEvaluationJobStatus.QUEUED.value:
                    self._cancel_locked(db, job)
                elif job.status == RagEvaluationJobStatus.RUNNING.value:
                    job.status = RagEvaluationJobStatus.CANCELLING.value
                    job.updated_at = utcnow()
                dataset = db.get(RagEvaluationDatasetRow, job.dataset_id)
                creator = (
                    db.query(User.username)
                    .filter(User.id == job.created_by_user_id)
                    .scalar()
                )
                return self._job_record(job, dataset, creator)
        finally:
            db.close()

    def reconcile_expired(self, *, now: datetime | None = None) -> tuple[str, ...]:
        current = now or utcnow()
        db = self._session_factory()
        recovered: list[str] = []
        try:
            with db.begin():
                jobs = (
                    db.query(RagEvaluationJobRow)
                    .filter(
                        RagEvaluationJobRow.status.in_(
                            (
                                RagEvaluationJobStatus.RUNNING.value,
                                RagEvaluationJobStatus.CANCELLING.value,
                            )
                        ),
                        RagEvaluationJobRow.lease_expires_at.is_not(None),
                        RagEvaluationJobRow.lease_expires_at <= current,
                    )
                    .with_for_update()
                    .all()
                )
                for job in jobs:
                    recovered.append(job.id)
                    if job.status == RagEvaluationJobStatus.CANCELLING.value:
                        self._cancel_locked(db, job)
                        continue
                    if job.attempts >= job.max_attempts:
                        dataset = db.get(RagEvaluationDatasetRow, job.dataset_id)
                        case_rows = (
                            db.query(RagEvaluationCaseRow)
                            .filter(RagEvaluationCaseRow.job_id == job.id)
                            .order_by(RagEvaluationCaseRow.position.asc())
                            .all()
                        )
                        if dataset is not None:
                            try:
                                report = self._orphaned_partial_report(
                                    job=job,
                                    dataset=dataset,
                                    cases=case_rows,
                                )
                            except Exception:
                                logger.exception(
                                    "RAG evaluation orphan partial report failed job_id=%s",
                                    job.id,
                                )
                            else:
                                report_cases = {
                                    item.case_id: item for item in report.cases
                                }
                                for case_row in case_rows:
                                    scored = report_cases.get(case_row.case_id)
                                    if scored is None:
                                        continue
                                    case_row.metrics_json = scored.metrics
                                    case_row.checks_json = scored.checks
                                    case_row.updated_at = current
                                job.report_json = report.model_dump(mode="json")
                        job.status = RagEvaluationJobStatus.FAILED.value
                        job.error_code = "RAG_EVALUATION_ORPHANED"
                        job.error_detail_redacted = None
                        job.finished_at = current
                        job.owner_worker_id = None
                        job.lease_expires_at = None
                        job.updated_at = current
                        db.query(RagEvaluationCaseRow).filter(
                            RagEvaluationCaseRow.job_id == job.id,
                            RagEvaluationCaseRow.status.in_(
                                (
                                    RagEvaluationCaseStatus.QUEUED.value,
                                    RagEvaluationCaseStatus.RUNNING.value,
                                )
                            ),
                        ).update(
                            {
                                RagEvaluationCaseRow.status: RagEvaluationCaseStatus.FAILED.value,
                                RagEvaluationCaseRow.error_code: "RAG_EVALUATION_ORPHANED",
                                RagEvaluationCaseRow.finished_at: current,
                                RagEvaluationCaseRow.updated_at: current,
                            },
                            synchronize_session=False,
                        )
                        continue
                    job.status = RagEvaluationJobStatus.QUEUED.value
                    job.owner_worker_id = None
                    job.lease_expires_at = None
                    job.heartbeat_at = None
                    job.updated_at = current
                    db.query(RagEvaluationCaseRow).filter(
                        RagEvaluationCaseRow.job_id == job.id,
                        RagEvaluationCaseRow.status
                        == RagEvaluationCaseStatus.RUNNING.value,
                    ).update(
                        {
                            RagEvaluationCaseRow.status: RagEvaluationCaseStatus.QUEUED.value,
                            RagEvaluationCaseRow.started_at: None,
                            RagEvaluationCaseRow.updated_at: current,
                        },
                        synchronize_session=False,
                    )
            return tuple(recovered)
        finally:
            db.close()

    @staticmethod
    def _orphaned_partial_report(
        *,
        job: RagEvaluationJobRow,
        dataset: RagEvaluationDatasetRow,
        cases: list[RagEvaluationCaseRow],
    ) -> RagEvalReport:
        parsed_dataset = RagEvalDataset.model_validate(dataset.payload_json)
        policy = RagEvalGatePolicy.model_validate(job.gate_policy_json)
        observations = tuple(
            RagEvalObservation.model_validate(case.observation_json)
            for case in cases
            if case.observation_json is not None
        )
        return evaluate_rag_partial(
            parsed_dataset,
            RagEvalObservationBundle(
                dataset_fingerprint=dataset.fingerprint,
                observations=observations,
            ),
            policy,
            metadata={
                "adapter": "persistent_rag_evaluation_worker",
                "provenance": "live_rag",
                "job_id": job.id,
                "model_catalog_hash": job.model_catalog_hash,
                "completed_case_count": len(observations),
                "failure": {
                    "code": "RAG_EVALUATION_ORPHANED",
                    "stage": "lease_recovery",
                },
            },
        )

    def claim_next(
        self,
        *,
        worker_id: str,
        lease_seconds: int,
    ) -> ClaimedRagEvaluationJob | None:
        self.reconcile_expired()
        db = self._session_factory()
        try:
            with db.begin():
                job = (
                    db.query(RagEvaluationJobRow)
                    .filter(
                        RagEvaluationJobRow.status
                        == RagEvaluationJobStatus.QUEUED.value
                    )
                    .order_by(RagEvaluationJobRow.created_at.asc())
                    .with_for_update(skip_locked=True)
                    .first()
                )
                if job is None:
                    return None
                now = utcnow()
                job.status = RagEvaluationJobStatus.RUNNING.value
                job.owner_worker_id = worker_id
                job.lease_expires_at = now + timedelta(seconds=max(10, lease_seconds))
                job.heartbeat_at = now
                job.fencing_token += 1
                job.attempts += 1
                job.started_at = job.started_at or now
                job.updated_at = now
                dataset = db.get(RagEvaluationDatasetRow, job.dataset_id)
                cases = (
                    db.query(RagEvaluationCaseRow)
                    .filter(RagEvaluationCaseRow.job_id == job.id)
                    .order_by(RagEvaluationCaseRow.position.asc())
                    .all()
                )
                creator = (
                    db.query(User.username)
                    .filter(User.id == job.created_by_user_id)
                    .scalar()
                )
                dataset_creator = (
                    db.query(User.username)
                    .filter(User.id == dataset.created_by_user_id)
                    .scalar()
                )
                return ClaimedRagEvaluationJob(
                    job=self._job_record(job, dataset, creator),
                    dataset=self._dataset_record(dataset, dataset_creator),
                    cases=tuple(self._case_record(row) for row in cases),
                )
        finally:
            db.close()

    def heartbeat(
        self,
        *,
        job_id: str,
        worker_id: str,
        fencing_token: int,
        lease_seconds: int,
    ) -> None:
        db = self._session_factory()
        try:
            with db.begin():
                job = (
                    db.query(RagEvaluationJobRow)
                    .filter(RagEvaluationJobRow.id == job_id)
                    .with_for_update()
                    .first()
                )
                self._assert_owner(job, worker_id, fencing_token)
                now = utcnow()
                job.heartbeat_at = now
                job.lease_expires_at = now + timedelta(seconds=max(10, lease_seconds))
                job.updated_at = now
        finally:
            db.close()

    def claim_case(
        self,
        *,
        job_id: str,
        worker_id: str,
        fencing_token: int,
    ) -> RagEvaluationCaseRecord | None:
        db = self._session_factory()
        try:
            with db.begin():
                job = (
                    db.query(RagEvaluationJobRow)
                    .filter(RagEvaluationJobRow.id == job_id)
                    .with_for_update()
                    .first()
                )
                self._assert_owner(job, worker_id, fencing_token)
                if job.status == RagEvaluationJobStatus.CANCELLING.value:
                    return None
                case = (
                    db.query(RagEvaluationCaseRow)
                    .filter(
                        RagEvaluationCaseRow.job_id == job_id,
                        RagEvaluationCaseRow.status
                        == RagEvaluationCaseStatus.QUEUED.value,
                    )
                    .order_by(RagEvaluationCaseRow.position.asc())
                    .with_for_update()
                    .first()
                )
                if case is None:
                    return None
                now = utcnow()
                case.status = RagEvaluationCaseStatus.RUNNING.value
                case.started_at = now
                case.updated_at = now
                return self._case_record(case)
        finally:
            db.close()

    def complete_case(
        self,
        *,
        job_id: str,
        case_id: str,
        worker_id: str,
        fencing_token: int,
        observation: RagEvalObservation,
        generated_answer: str,
        judge_reason: str | None,
        judge: dict | None,
        retrieved_identities: list[dict],
        duration_ms: int,
    ) -> None:
        db = self._session_factory()
        try:
            with db.begin():
                job = (
                    db.query(RagEvaluationJobRow)
                    .filter(RagEvaluationJobRow.id == job_id)
                    .with_for_update()
                    .first()
                )
                self._assert_owner(job, worker_id, fencing_token)
                case = (
                    db.query(RagEvaluationCaseRow)
                    .filter(
                        RagEvaluationCaseRow.job_id == job_id,
                        RagEvaluationCaseRow.case_id == case_id,
                    )
                    .with_for_update()
                    .first()
                )
                if case is None:
                    raise AppError(
                        ErrorCode.NOT_FOUND,
                        "RAG Evaluation Case 不存在",
                        status_code=404,
                        category="evaluation",
                        stage="case",
                    )
                if case.status == RagEvaluationCaseStatus.COMPLETED.value:
                    return
                if case.status != RagEvaluationCaseStatus.RUNNING.value:
                    raise AppError(
                        ErrorCode.CONFLICT,
                        "RAG Evaluation Case 当前不可完成",
                        status_code=409,
                        category="evaluation",
                        stage="case",
                    )
                now = utcnow()
                case.status = RagEvaluationCaseStatus.COMPLETED.value
                case.generated_answer = generated_answer
                case.judge_reason = judge_reason
                case.observation_json = observation.model_dump(mode="json")
                case.judge_json = judge
                case.retrieved_identity_json = retrieved_identities
                case.provider_error_code = observation.provider_error_code
                case.provider_error_stage = (
                    observation.provider_error_stage.value
                    if observation.provider_error_stage is not None
                    else None
                )
                case.duration_ms = max(int(duration_ms), 0)
                case.finished_at = now
                case.updated_at = now
                job.completed_cases += 1
                job.updated_at = now
        finally:
            db.close()

    def fail_job(
        self,
        *,
        job_id: str,
        worker_id: str,
        fencing_token: int,
        error_code: str,
        error_detail_redacted: str,
        case_id: str | None = None,
        report: RagEvalReport | None = None,
    ) -> None:
        db = self._session_factory()
        try:
            with db.begin():
                job = (
                    db.query(RagEvaluationJobRow)
                    .filter(RagEvaluationJobRow.id == job_id)
                    .with_for_update()
                    .first()
                )
                self._assert_owner(job, worker_id, fencing_token)
                now = utcnow()
                job.status = RagEvaluationJobStatus.FAILED.value
                job.error_code = error_code
                job.error_detail_redacted = error_detail_redacted
                if report is not None:
                    report_cases = {item.case_id: item for item in report.cases}
                    completed_rows = (
                        db.query(RagEvaluationCaseRow)
                        .filter(RagEvaluationCaseRow.job_id == job_id)
                        .all()
                    )
                    for completed_case in completed_rows:
                        scored = report_cases.get(completed_case.case_id)
                        if scored is None:
                            continue
                        completed_case.metrics_json = scored.metrics
                        completed_case.checks_json = scored.checks
                        completed_case.updated_at = now
                    job.report_json = report.model_dump(mode="json")
                job.finished_at = now
                job.owner_worker_id = None
                job.lease_expires_at = None
                job.updated_at = now
                if case_id is not None:
                    case = (
                        db.query(RagEvaluationCaseRow)
                        .filter(
                            RagEvaluationCaseRow.job_id == job_id,
                            RagEvaluationCaseRow.case_id == case_id,
                        )
                        .first()
                    )
                    if case is not None:
                        case.status = RagEvaluationCaseStatus.FAILED.value
                        case.error_code = error_code
                        case.error_detail_redacted = error_detail_redacted
                        case.finished_at = now
                        case.updated_at = now
                db.query(RagEvaluationCaseRow).filter(
                    RagEvaluationCaseRow.job_id == job_id,
                    RagEvaluationCaseRow.status == RagEvaluationCaseStatus.QUEUED.value,
                ).update(
                    {
                        RagEvaluationCaseRow.status: RagEvaluationCaseStatus.CANCELLED.value,
                        RagEvaluationCaseRow.finished_at: now,
                        RagEvaluationCaseRow.updated_at: now,
                    },
                    synchronize_session=False,
                )
        finally:
            db.close()

    def finish_job(
        self,
        *,
        job_id: str,
        worker_id: str,
        fencing_token: int,
        report: RagEvalReport,
    ) -> None:
        db = self._session_factory()
        try:
            with db.begin():
                job = (
                    db.query(RagEvaluationJobRow)
                    .filter(RagEvaluationJobRow.id == job_id)
                    .with_for_update()
                    .first()
                )
                self._assert_owner(job, worker_id, fencing_token)
                if job.completed_cases != job.total_cases:
                    raise AppError(
                        ErrorCode.CONFLICT,
                        "RAG Evaluation Job 尚未完成全部 Case",
                        status_code=409,
                        category="evaluation",
                        stage="finalize",
                    )
                report_cases = {case.case_id: case for case in report.cases}
                case_rows = (
                    db.query(RagEvaluationCaseRow)
                    .filter(RagEvaluationCaseRow.job_id == job.id)
                    .all()
                )
                if set(report_cases) != {case.case_id for case in case_rows}:
                    raise AppError(
                        ErrorCode.CONFLICT,
                        "RAG Evaluation Report 未覆盖全部 Case",
                        status_code=409,
                        category="evaluation",
                        stage="finalize",
                    )
                now = utcnow()
                for case in case_rows:
                    scored = report_cases[case.case_id]
                    case.metrics_json = scored.metrics
                    case.checks_json = scored.checks
                    case.updated_at = now
                job.status = RagEvaluationJobStatus.SUCCEEDED.value
                job.report_json = report.model_dump(mode="json")
                job.error_code = None
                job.error_detail_redacted = None
                job.finished_at = now
                job.owner_worker_id = None
                job.lease_expires_at = None
                job.updated_at = now
        finally:
            db.close()

    def observations(self, job_id: str) -> tuple[RagEvalObservation, ...]:
        cases = self.list_cases(job_id)
        observations = tuple(
            case.observation for case in cases if case.observation is not None
        )
        if len(observations) != len(cases):
            raise AppError(
                ErrorCode.CONFLICT,
                "RAG Evaluation Job 仍有 Case 缺少 Observation",
                status_code=409,
                category="evaluation",
                stage="finalize",
            )
        return observations

    def baseline_report(self, job_id: str | None) -> RagEvalReport | None:
        if job_id is None:
            return None
        job = self.get_job(job_id)
        if job.status is not RagEvaluationJobStatus.SUCCEEDED or job.report is None:
            raise AppError(
                ErrorCode.CONFLICT,
                "Baseline Evaluation Job 尚未成功完成",
                status_code=409,
                category="evaluation",
                stage="baseline",
            )
        return job.report

    def cancel_requested(self, job_id: str) -> bool:
        db = self._session_factory()
        try:
            status = (
                db.query(RagEvaluationJobRow.status)
                .filter(RagEvaluationJobRow.id == job_id)
                .scalar()
            )
            return status in {
                RagEvaluationJobStatus.CANCELLING.value,
                RagEvaluationJobStatus.CANCELLED.value,
            }
        finally:
            db.close()

    def finish_cancelled(
        self,
        *,
        job_id: str,
        worker_id: str,
        fencing_token: int,
    ) -> None:
        db = self._session_factory()
        try:
            with db.begin():
                job = (
                    db.query(RagEvaluationJobRow)
                    .filter(RagEvaluationJobRow.id == job_id)
                    .with_for_update()
                    .first()
                )
                self._assert_owner(job, worker_id, fencing_token)
                self._cancel_locked(db, job)
        finally:
            db.close()

    def worker_heartbeat(
        self,
        *,
        worker_id: str,
        status: str,
        metadata: dict | None = None,
    ) -> None:
        db = self._session_factory()
        try:
            with db.begin():
                now = utcnow()
                row = (
                    db.query(WorkerHeartbeat)
                    .filter(WorkerHeartbeat.worker_id == worker_id)
                    .with_for_update()
                    .first()
                )
                if row is None:
                    row = WorkerHeartbeat(
                        worker_id=worker_id,
                        worker_kind="rag_evaluation",
                        status=status,
                        started_at=now,
                        heartbeat_at=now,
                        stopped_at=now if status == "stopped" else None,
                        metadata_json=dict(metadata or {}),
                        created_at=now,
                        updated_at=now,
                    )
                    db.add(row)
                    return
                row.status = status
                row.heartbeat_at = now
                row.stopped_at = now if status == "stopped" else None
                row.metadata_json = dict(metadata or {})
                row.updated_at = now
        finally:
            db.close()

    @staticmethod
    def _assert_owner(
        job: RagEvaluationJobRow | None,
        worker_id: str,
        fencing_token: int,
    ) -> None:
        if job is None:
            raise AppError(
                ErrorCode.NOT_FOUND,
                "RAG Evaluation Job 不存在",
                status_code=404,
                category="evaluation",
                stage="job",
            )
        if (
            job.owner_worker_id != worker_id
            or job.fencing_token != fencing_token
            or job.status
            not in {
                RagEvaluationJobStatus.RUNNING.value,
                RagEvaluationJobStatus.CANCELLING.value,
            }
        ):
            raise AppError(
                ErrorCode.CONFLICT,
                "当前 worker 不再拥有该 RAG Evaluation Job",
                status_code=409,
                category="evaluation",
                stage="ownership",
            )

    @staticmethod
    def _cancel_locked(db: Session, job: RagEvaluationJobRow) -> None:
        now = utcnow()
        job.status = RagEvaluationJobStatus.CANCELLED.value
        job.owner_worker_id = None
        job.lease_expires_at = None
        job.finished_at = now
        job.updated_at = now
        db.query(RagEvaluationCaseRow).filter(
            RagEvaluationCaseRow.job_id == job.id,
            RagEvaluationCaseRow.status.in_(
                (
                    RagEvaluationCaseStatus.QUEUED.value,
                    RagEvaluationCaseStatus.RUNNING.value,
                )
            ),
        ).update(
            {
                RagEvaluationCaseRow.status: RagEvaluationCaseStatus.CANCELLED.value,
                RagEvaluationCaseRow.finished_at: now,
                RagEvaluationCaseRow.updated_at: now,
            },
            synchronize_session=False,
        )


rag_evaluation_repository = RagEvaluationRepository()


__all__ = ["RagEvaluationRepository", "rag_evaluation_repository"]
