from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from threading import Event
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import backend.documents.worker as worker_module
import backend.documents.catalog as catalog_module
from backend.core.errors import AppError, ErrorCode
from backend.db.models import (
    Base,
    DocumentCleanupJob,
    DocumentVersion,
    IndexJob,
    IndexManifest,
    User,
    utcnow,
)
from backend.documents.catalog import (
    BuildProfile,
    CleanupBuild,
    CleanupJobExecution,
    DocumentCatalog,
    IndexJobExecution,
    IndexJobStatus,
    ManifestEntry,
    VersionBuild,
)
from backend.documents.publication import (
    DocumentPublication,
    DocumentPublicationConfig,
)
from backend.documents.worker import (
    IndexingWorker,
    IndexingWorkerConfig,
    default_indexing_worker_id,
)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def test_configured_worker_id_is_only_a_prefix_and_each_process_is_unique(monkeypatch):
    monkeypatch.setattr(
        worker_module,
        "get_settings",
        lambda: SimpleNamespace(
            worker=SimpleNamespace(
                indexing_worker_id="catalog-worker",
                worker_id="shared-worker",
            )
        ),
    )

    first = default_indexing_worker_id()
    second = default_indexing_worker_id()

    assert first.startswith("catalog-worker-")
    assert second.startswith("catalog-worker-")
    assert first != second
    assert len(first) <= 128


@pytest.fixture
def catalog_env(tmp_path):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    with session_factory.begin() as db:
        db.add(User(id=1, username="alice", password_hash="hash", role="admin"))

    catalog = DocumentCatalog(session_factory)
    knowledge_base = catalog.ensure_knowledge_base(
        tenant_id="tenant-a",
        owner_id=1,
        name="default",
        knowledge_base_id="kb-default",
    )
    yield SimpleNamespace(
        catalog=catalog,
        knowledge_base=knowledge_base,
        Session=session_factory,
        upload_dir=tmp_path,
    )
    engine.dispose()


def _reserve(
    env,
    content: str,
    *,
    max_attempts: int = 3,
    profile: BuildProfile | None = None,
):
    digest = _digest(content)
    return env.catalog.reserve_upload(
        tenant_id="tenant-a",
        knowledge_base_id=env.knowledge_base.id,
        canonical_name="guide.pdf",
        owner_id=1,
        content_sha256=digest,
        source_object_key=f"objects/{digest}",
        media_type="application/pdf",
        size_bytes=len(content),
        processing_profile=profile or BuildProfile(embedding_model="embed-v1"),
        vector_collection="documents_v2",
        max_attempts=max_attempts,
    )


def _manifest(version_id: str) -> list[ManifestEntry]:
    return [
        ManifestEntry(
            chunk_id=f"{version_id}:leaf:0",
            content_hash=_digest(f"{version_id}:leaf"),
            store_kind="vector",
            section_id="section-1",
            chunk_level=3,
        ),
        ManifestEntry(
            chunk_id=f"{version_id}:parent:0",
            content_hash=_digest(f"{version_id}:parent"),
            store_kind="parent",
            section_id="section-1",
            chunk_level=2,
        ),
    ]


def _stage(env, reservation, *, execution: IndexJobExecution | None = None):
    return env.catalog.record_manifest(
        job_id=reservation.job.id,
        publication_fence=reservation.publication_fence,
        entries=_manifest(reservation.version.id),
        vector_chunk_count=1,
        parent_chunk_count=1,
        execution=execution,
    )


def _publish(env, reservation, *, cleanup_grace: timedelta = timedelta(0)):
    _stage(env, reservation)
    return env.catalog.publish(
        job_id=reservation.job.id,
        publication_fence=reservation.publication_fence,
        expected_current_version_id=reservation.expected_current_version_id,
        cleanup_grace=cleanup_grace,
    )


def _index_execution(build) -> IndexJobExecution:
    return IndexJobExecution(
        worker_id=build.job.owner_worker_id,
        execution_fence=build.job.execution_fence,
    )


def _cleanup_execution(build) -> CleanupJobExecution:
    return CleanupJobExecution(
        worker_id=build.job.owner_worker_id,
        execution_fence=build.job.execution_fence,
    )


def _expire_index_lease(env, job_id: str) -> None:
    with env.Session.begin() as db:
        row = db.get(IndexJob, job_id)
        row.lease_expires_at = utcnow() - timedelta(seconds=1)


def _expire_cleanup_lease(env, job_id: str) -> None:
    with env.Session.begin() as db:
        row = db.get(DocumentCleanupJob, job_id)
        row.lease_expires_at = utcnow() - timedelta(seconds=1)


def test_second_worker_cannot_claim_live_job_but_reclaims_expired_lease(catalog_env):
    reservation = _reserve(catalog_env, "version-one")

    first = catalog_env.catalog.claim_index_job(
        worker_id="index-worker-a",
        lease_seconds=30,
    )

    assert first is not None
    assert first.job.id == reservation.job.id
    assert first.job.status == IndexJobStatus.RUNNING
    assert first.job.owner_worker_id == "index-worker-a"
    assert first.job.execution_fence > 0
    assert (
        catalog_env.catalog.claim_index_job(
            worker_id="index-worker-b",
            lease_seconds=30,
        )
        is None
    )

    _expire_index_lease(catalog_env, first.job.id)
    reclaimed = catalog_env.catalog.claim_index_job(
        worker_id="index-worker-b",
        lease_seconds=30,
    )

    assert reclaimed is not None
    assert reclaimed.job.id == first.job.id
    assert reclaimed.job.owner_worker_id == "index-worker-b"
    assert reclaimed.job.execution_fence > first.job.execution_fence
    assert reclaimed.job.publication_fence == first.job.publication_fence


def test_index_claim_only_selects_jobs_matching_worker_build_capability(catalog_env):
    expected_profile = BuildProfile(
        parser_version="parser-v2",
        chunker_version="chunker-v2",
        embedding_model="embed-v2",
        index_version="catalog-v2",
    )
    wrong_profile = BuildProfile(
        parser_version="parser-v1",
        chunker_version="chunker-v1",
        embedding_model="embed-v1",
        index_version="catalog-v1",
    )
    reservation = _reserve(
        catalog_env,
        "version-one",
        profile=expected_profile,
    )

    assert (
        catalog_env.catalog.claim_index_job(
            worker_id="old-profile-worker",
            lease_seconds=30,
            build_fingerprint=wrong_profile.fingerprint,
        )
        is None
    )
    claimed = catalog_env.catalog.claim_index_job(
        worker_id="matching-profile-worker",
        lease_seconds=30,
        build_fingerprint=expected_profile.fingerprint,
    )

    assert claimed is not None
    assert claimed.job.id == reservation.job.id
    assert claimed.version.build_fingerprint == expected_profile.fingerprint


def test_same_worker_repeated_claim_only_renews_lease(catalog_env):
    _reserve(catalog_env, "version-one")
    started = utcnow()
    first = catalog_env.catalog.claim_index_job(
        worker_id="index-worker-a",
        lease_seconds=30,
        now=started,
    )

    repeated = catalog_env.catalog.claim_index_job(
        worker_id="index-worker-a",
        lease_seconds=30,
        now=started + timedelta(seconds=20),
    )

    assert repeated.job.id == first.job.id
    assert repeated.job.attempts == first.job.attempts
    assert repeated.job.execution_fence == first.job.execution_fence
    assert repeated.job.lease_expires_at == started + timedelta(seconds=50)


def test_reclaimed_job_rejects_every_stale_execution_write(catalog_env):
    reservation = _reserve(catalog_env, "version-one")
    first = catalog_env.catalog.claim_index_job(
        worker_id="index-worker-a",
        lease_seconds=30,
    )
    stale_execution = _index_execution(first)
    _expire_index_lease(catalog_env, first.job.id)
    reclaimed = catalog_env.catalog.claim_index_job(
        worker_id="index-worker-b",
        lease_seconds=30,
    )

    with pytest.raises(AppError) as missing_execution:
        catalog_env.catalog.update_job(
            job_id=reservation.job.id,
            publication_fence=reservation.publication_fence,
            current_step="parse",
            progress=10,
        )
    with pytest.raises(AppError) as update_error:
        catalog_env.catalog.update_job(
            job_id=reservation.job.id,
            publication_fence=reservation.publication_fence,
            current_step="parse",
            progress=20,
            execution=stale_execution,
        )
    with pytest.raises(AppError) as manifest_error:
        _stage(catalog_env, reservation, execution=stale_execution)
    with pytest.raises(AppError) as failure_error:
        catalog_env.catalog.fail(
            job_id=reservation.job.id,
            publication_fence=reservation.publication_fence,
            error_code="VECTOR_STORE_UNAVAILABLE",
            execution=stale_execution,
        )

    assert missing_execution.value.code == ErrorCode.CONFLICT
    assert update_error.value.code == ErrorCode.CONFLICT
    assert manifest_error.value.code == ErrorCode.CONFLICT
    assert failure_error.value.code == ErrorCode.CONFLICT
    current = catalog_env.catalog.get_job(job_id=reservation.job.id)
    assert current.owner_worker_id == "index-worker-b"
    assert current.execution_fence == reclaimed.job.execution_fence
    assert current.error_code is None


def test_retry_wait_is_not_claimable_early_and_exhaustion_dead_letters(catalog_env):
    current = _reserve(catalog_env, "version-one")
    _publish(catalog_env, current)
    candidate = _reserve(catalog_env, "version-two", max_attempts=2)
    first = catalog_env.catalog.claim_index_job(
        worker_id="index-worker-a",
        lease_seconds=30,
    )
    first_execution = _index_execution(first)
    retry_clock = utcnow()
    retry_at = retry_clock + timedelta(minutes=5)

    retry = catalog_env.catalog.schedule_index_retry(
        job_id=candidate.job.id,
        execution=first_execution,
        retry_delay_seconds=300,
        error_code="VECTOR_STORE_UNAVAILABLE",
        now=retry_clock,
    )

    assert retry.status == IndexJobStatus.RETRY_WAIT
    assert retry.owner_worker_id is None
    assert retry.next_retry_at == retry_at
    assert (
        catalog_env.catalog.claim_index_job(
            worker_id="index-worker-b",
            lease_seconds=30,
            now=retry_at - timedelta(seconds=1),
        )
        is None
    )

    second = catalog_env.catalog.claim_index_job(
        worker_id="index-worker-b",
        lease_seconds=30,
        now=retry_at + timedelta(seconds=1),
    )
    exhausted = catalog_env.catalog.schedule_index_retry(
        job_id=candidate.job.id,
        execution=_index_execution(second),
        retry_delay_seconds=300,
        error_code="VECTOR_STORE_UNAVAILABLE",
        now=retry_at + timedelta(seconds=1),
    )

    assert second.job.attempts == 2
    assert exhausted.status == IndexJobStatus.DEAD_LETTER
    assert exhausted.owner_worker_id is None
    visible = catalog_env.catalog.get_current(
        tenant_id="tenant-a",
        knowledge_base_id=catalog_env.knowledge_base.id,
        canonical_name="guide.pdf",
    )
    assert visible.current_version.id == current.version.id
    assert visible.pending_version is None


def test_index_heartbeat_extends_lease_and_stale_heartbeat_is_rejected(catalog_env):
    reservation = _reserve(catalog_env, "version-one")
    started = utcnow()
    first = catalog_env.catalog.claim_index_job(
        worker_id="index-worker-a",
        lease_seconds=30,
        now=started,
    )
    first_execution = _index_execution(first)

    heartbeat = catalog_env.catalog.heartbeat_index_job(
        job_id=reservation.job.id,
        execution=first_execution,
        lease_seconds=30,
        now=started + timedelta(seconds=20),
    )

    assert heartbeat.lease_expires_at == started + timedelta(seconds=50)
    assert (
        catalog_env.catalog.claim_index_job(
            worker_id="index-worker-b",
            lease_seconds=30,
            now=started + timedelta(seconds=31),
        )
        is None
    )
    reclaimed = catalog_env.catalog.claim_index_job(
        worker_id="index-worker-b",
        lease_seconds=30,
        now=started + timedelta(seconds=51),
    )

    with pytest.raises(AppError) as stale_heartbeat:
        catalog_env.catalog.heartbeat_index_job(
            job_id=reservation.job.id,
            execution=first_execution,
            lease_seconds=30,
            now=started + timedelta(seconds=52),
        )

    assert reclaimed.job.execution_fence > first.job.execution_fence
    assert stale_heartbeat.value.code == ErrorCode.CONFLICT


def test_lease_methods_normalize_timezone_aware_database_clock(catalog_env):
    _reserve(catalog_env, "version-one")
    aware = datetime(2026, 7, 16, 3, 0, tzinfo=UTC)

    claimed = catalog_env.catalog.claim_index_job(
        worker_id="index-worker-a",
        lease_seconds=30,
        now=aware,
    )
    heartbeat = catalog_env.catalog.heartbeat_index_job(
        job_id=claimed.job.id,
        execution=_index_execution(claimed),
        lease_seconds=30,
        now=aware + timedelta(seconds=10),
    )

    assert heartbeat.lease_expires_at.tzinfo is None
    assert heartbeat.lease_expires_at == datetime(2026, 7, 16, 3, 0, 40)


def test_retry_delay_is_anchored_to_the_database_clock(catalog_env):
    _reserve(catalog_env, "version-one")
    aware = datetime(2026, 7, 16, 3, 0, tzinfo=UTC)
    claimed = catalog_env.catalog.claim_index_job(
        worker_id="index-worker-a",
        lease_seconds=30,
        now=aware,
    )

    retry = catalog_env.catalog.schedule_index_retry(
        job_id=claimed.job.id,
        execution=_index_execution(claimed),
        retry_delay_seconds=45,
        error_code="STORAGE_UNAVAILABLE",
        now=aware + timedelta(seconds=10),
    )

    assert retry.next_retry_at.tzinfo is None
    assert retry.next_retry_at == datetime(2026, 7, 16, 3, 0, 55)


def test_worker_readiness_uses_process_heartbeat_even_when_queue_is_empty(catalog_env):
    clock = utcnow()

    missing = catalog_env.catalog.worker_readiness(
        worker_kind="indexing",
        stale_after_seconds=30,
        now=clock,
    )
    catalog_env.catalog.record_worker_heartbeat(
        worker_id="index-worker-a",
        worker_kind="indexing",
        status="running",
        now=clock,
    )
    fresh = catalog_env.catalog.worker_readiness(
        worker_kind="indexing",
        stale_after_seconds=30,
        now=clock + timedelta(seconds=29),
    )
    stale = catalog_env.catalog.worker_readiness(
        worker_kind="indexing",
        stale_after_seconds=30,
        now=clock + timedelta(seconds=31),
    )

    assert missing.ready is False
    assert fresh.ready is True
    assert fresh.fresh_workers == 1
    assert fresh.queue_counts == {}
    assert stale.ready is False


def test_worker_readiness_requires_a_matching_build_capability(catalog_env):
    clock = utcnow()
    old_profile = BuildProfile(embedding_model="embed-v1")
    new_profile = BuildProfile(embedding_model="embed-v2")
    reservation = _reserve(catalog_env, "version-one", profile=old_profile)
    catalog_env.catalog.record_worker_heartbeat(
        worker_id="old-profile-worker",
        worker_kind="indexing",
        status="running",
        metadata={"build_fingerprint": old_profile.fingerprint},
        now=clock,
    )

    incompatible = catalog_env.catalog.worker_readiness(
        expected_build_fingerprint=new_profile.fingerprint,
        now=clock,
    )
    catalog_env.catalog.record_worker_heartbeat(
        worker_id="new-profile-worker",
        worker_kind="indexing",
        status="running",
        metadata={"build_fingerprint": new_profile.fingerprint},
        now=clock,
    )
    matching = catalog_env.catalog.worker_readiness(
        expected_build_fingerprint=new_profile.fingerprint,
        now=clock,
    )

    assert incompatible.ready is False
    assert incompatible.fresh_workers == 0
    assert incompatible.incompatible_fresh_workers == 1
    assert incompatible.oldest_ready_at is None

    _stage(catalog_env, reservation)
    staged = catalog_env.catalog.worker_readiness(
        expected_build_fingerprint=new_profile.fingerprint,
        now=clock,
    )
    assert staged.oldest_ready_at is not None
    assert matching.ready is True
    assert matching.fresh_workers == 1
    assert matching.incompatible_fresh_workers == 1
    assert matching.expected_build_fingerprint == new_profile.fingerprint


def test_cleanup_readiness_only_reports_currently_claimable_backlog(catalog_env):
    first = _reserve(catalog_env, "version-one")
    _publish(catalog_env, first)
    second = _reserve(catalog_env, "version-two")
    _publish(catalog_env, second, cleanup_grace=timedelta(minutes=5))
    with catalog_env.Session.begin() as db:
        version = db.get(DocumentVersion, first.version.id)
        cleanup_job = (
            db.query(DocumentCleanupJob)
            .filter(DocumentCleanupJob.document_version_id == first.version.id)
            .one()
        )
        due = version.cleanup_after
        cleanup_job.next_retry_at = due + timedelta(minutes=2)

    before_grace = catalog_env.catalog.worker_readiness(now=due - timedelta(seconds=1))
    in_retry_wait = catalog_env.catalog.worker_readiness(now=due + timedelta(minutes=1))
    claimable = catalog_env.catalog.worker_readiness(
        now=due + timedelta(minutes=2, seconds=1)
    )

    assert before_grace.oldest_ready_at is None
    assert in_retry_wait.oldest_ready_at is None
    assert claimable.oldest_ready_at is not None


def test_staged_retry_preserves_exact_manifest_and_only_retries_publish(catalog_env):
    reservation = _reserve(catalog_env, "version-one")
    _stage(catalog_env, reservation)
    first = catalog_env.catalog.claim_index_job(
        worker_id="index-worker-a",
        lease_seconds=30,
    )
    retry_clock = utcnow()
    retry_at = retry_clock + timedelta(minutes=5)

    retry = catalog_env.catalog.schedule_index_retry(
        job_id=reservation.job.id,
        execution=_index_execution(first),
        retry_delay_seconds=300,
        error_code="STORAGE_UNAVAILABLE",
        now=retry_clock,
    )

    assert retry.status == IndexJobStatus.STAGED
    assert retry.current_step == "publish"
    assert retry.owner_worker_id is None
    assert retry.next_retry_at == retry_at
    assert (
        catalog_env.catalog.worker_readiness(
            now=retry_at - timedelta(seconds=1)
        ).oldest_ready_at
        is None
    )
    with catalog_env.Session() as db:
        assert (
            db.query(IndexManifest)
            .filter(IndexManifest.document_version_id == reservation.version.id)
            .count()
            == 2
        )
    assert (
        catalog_env.catalog.claim_index_job(
            worker_id="index-worker-b",
            lease_seconds=30,
            now=retry_at - timedelta(seconds=1),
        )
        is None
    )
    resumed = catalog_env.catalog.claim_index_job(
        worker_id="index-worker-b",
        lease_seconds=30,
        now=retry_at + timedelta(seconds=1),
    )
    assert resumed.job.status == IndexJobStatus.STAGED


def test_staged_crash_gets_one_publish_only_recovery_after_attempt_budget(catalog_env):
    reservation = _reserve(catalog_env, "version-one", max_attempts=1)
    first = catalog_env.catalog.claim_index_job(
        worker_id="index-worker-a",
        lease_seconds=30,
    )
    execution = _index_execution(first)
    _stage(catalog_env, reservation, execution=execution)
    _expire_index_lease(catalog_env, first.job.id)

    recovery = catalog_env.catalog.claim_index_job(
        worker_id="index-worker-b",
        lease_seconds=30,
    )

    assert recovery is not None
    assert recovery.job.status == IndexJobStatus.STAGED
    assert recovery.job.attempts == 2
    assert recovery.job.execution_fence > first.job.execution_fence


def test_staged_job_can_be_reclaimed_by_a_new_worker_profile(catalog_env):
    old_profile = BuildProfile(
        parser_version="parser-v1",
        chunker_version="chunker-v1",
        embedding_model="embed-v1",
        index_version="catalog-v1",
    )
    new_profile = BuildProfile(
        parser_version="parser-v2",
        chunker_version="chunker-v2",
        embedding_model="embed-v2",
        index_version="catalog-v2",
    )
    reservation = _reserve(catalog_env, "version-one", profile=old_profile)
    _stage(catalog_env, reservation)

    claimed = catalog_env.catalog.claim_index_job(
        worker_id="new-profile-worker",
        lease_seconds=30,
        build_fingerprint=new_profile.fingerprint,
    )

    assert claimed is not None
    assert claimed.job.status == IndexJobStatus.STAGED
    assert claimed.version.build_fingerprint == old_profile.fingerprint


class _ExplodingLoader:
    def __init__(self) -> None:
        self.calls = 0

    def load_document(self, *_args, **_kwargs):
        self.calls += 1
        raise AssertionError("a staged job must not be parsed again")


class _StagedWriter:
    def __init__(self) -> None:
        self.write_calls = 0

    @staticmethod
    def build_version_scope(**kwargs):
        return SimpleNamespace(**kwargs)

    def write_versioned_documents(self, *_args, **_kwargs):
        self.write_calls += 1
        raise AssertionError("a staged job must not write vectors again")


class _ExplodingParentStore:
    def __getattr__(self, name):
        raise AssertionError(f"a staged job must not call parent_store.{name}")


def test_staged_reclaim_only_publishes_and_old_execution_cannot_commit(catalog_env):
    reservation = _reserve(catalog_env, "version-one")
    _stage(catalog_env, reservation)
    first = catalog_env.catalog.claim_index_job(
        worker_id="index-worker-a",
        lease_seconds=30,
    )
    assert first.job.status == IndexJobStatus.STAGED
    stale_execution = _index_execution(first)
    _expire_index_lease(catalog_env, first.job.id)
    reclaimed = catalog_env.catalog.claim_index_job(
        worker_id="index-worker-b",
        lease_seconds=30,
    )
    assert reclaimed.job.status == IndexJobStatus.STAGED

    loader = _ExplodingLoader()
    writer = _StagedWriter()
    publication = DocumentPublication(
        catalog=catalog_env.catalog,
        loader=loader,
        parent_store=_ExplodingParentStore(),
        writer=writer,
        config=DocumentPublicationConfig(
            tenant_id="tenant-a",
            knowledge_base_name="default",
            parser_version="parser-v1",
            chunker_version="chunker-v1",
            embedding_model="embed-v1",
            index_version="catalog-v1",
            vector_collection="documents_v2",
            upload_dir=catalog_env.upload_dir,
            max_attempts=3,
            cleanup_grace=timedelta(0),
        ),
    )

    with pytest.raises(AppError) as stale_publish:
        publication.run(reservation.job.id, execution=stale_execution)

    assert stale_publish.value.code in {
        ErrorCode.CONFLICT,
        ErrorCode.STORAGE_UNAVAILABLE,
    }
    outcome = publication.run(
        reservation.job.id,
        execution=_index_execution(reclaimed),
    )

    assert outcome.published is True
    assert outcome.version.id == reservation.version.id
    assert loader.calls == 0
    assert writer.write_calls == 0
    current = catalog_env.catalog.get_current(
        tenant_id="tenant-a",
        knowledge_base_id=catalog_env.knowledge_base.id,
        canonical_name="guide.pdf",
    )
    assert current.current_version.id == reservation.version.id


def test_staged_publish_still_rejects_corrupted_recorded_profile(catalog_env):
    reservation = _reserve(catalog_env, "version-one")
    _stage(catalog_env, reservation)
    with catalog_env.Session.begin() as db:
        version = db.get(DocumentVersion, reservation.version.id)
        version.parser_version = "corrupted-parser"
    claimed = catalog_env.catalog.claim_index_job(
        worker_id="new-profile-worker",
        lease_seconds=30,
        build_fingerprint=BuildProfile(embedding_model="embed-v2").fingerprint,
    )
    loader = _ExplodingLoader()
    publication = DocumentPublication(
        catalog=catalog_env.catalog,
        loader=loader,
        parent_store=_ExplodingParentStore(),
        writer=_StagedWriter(),
        config=DocumentPublicationConfig(
            tenant_id="tenant-a",
            knowledge_base_name="default",
            parser_version="parser-v2",
            chunker_version="chunker-v2",
            embedding_model="embed-v2",
            index_version="catalog-v2",
            vector_collection="documents_v2",
            upload_dir=catalog_env.upload_dir,
            max_attempts=3,
            cleanup_grace=timedelta(0),
        ),
    )

    with pytest.raises(AppError) as corrupted:
        publication.run(
            reservation.job.id,
            execution=_index_execution(claimed),
        )

    assert corrupted.value.retryable is False
    assert corrupted.value.stage == "build_profile_integrity"
    assert loader.calls == 0


def test_publication_rejects_a_claimed_job_when_runtime_profile_drifted(catalog_env):
    reserved_profile = BuildProfile(
        parser_version="parser-v2",
        chunker_version="chunker-v2",
        embedding_model="embed-v2",
        index_version="catalog-v2",
    )
    reservation = _reserve(
        catalog_env,
        "version-one",
        profile=reserved_profile,
    )
    claimed = catalog_env.catalog.claim_index_job(
        worker_id="stale-worker",
        lease_seconds=30,
    )
    loader = _ExplodingLoader()
    publication = DocumentPublication(
        catalog=catalog_env.catalog,
        loader=loader,
        parent_store=_ExplodingParentStore(),
        writer=_StagedWriter(),
        config=DocumentPublicationConfig(
            tenant_id="tenant-a",
            knowledge_base_name="default",
            parser_version="parser-v1",
            chunker_version="chunker-v1",
            embedding_model="embed-v1",
            index_version="catalog-v1",
            vector_collection="documents_v2",
            upload_dir=catalog_env.upload_dir,
            max_attempts=3,
            cleanup_grace=timedelta(0),
        ),
    )

    with pytest.raises(AppError) as mismatch:
        publication.run(
            reservation.job.id,
            execution=_index_execution(claimed),
        )

    assert mismatch.value.code == ErrorCode.CONFLICT
    assert mismatch.value.retryable is True
    assert mismatch.value.stage == "worker_capability"
    assert loader.calls == 0


def test_cleanup_job_reclaim_is_fenced_and_only_current_owner_can_complete(catalog_env):
    first = _reserve(catalog_env, "version-one")
    _publish(catalog_env, first)
    second = _reserve(catalog_env, "version-two")
    _publish(catalog_env, second)

    claimed = catalog_env.catalog.claim_cleanup_job(
        worker_id="cleanup-worker-a",
        lease_seconds=30,
    )

    assert claimed is not None
    assert claimed.version.id == first.version.id
    assert claimed.job.status == "running"
    stale_execution = _cleanup_execution(claimed)
    assert (
        catalog_env.catalog.claim_cleanup_job(
            worker_id="cleanup-worker-b",
            lease_seconds=30,
        )
        is None
    )

    _expire_cleanup_lease(catalog_env, claimed.job.id)
    reclaimed = catalog_env.catalog.claim_cleanup_job(
        worker_id="cleanup-worker-b",
        lease_seconds=30,
    )
    assert reclaimed is not None
    assert reclaimed.job.id == claimed.job.id
    assert reclaimed.job.execution_fence > claimed.job.execution_fence

    with pytest.raises(AppError) as stale_complete:
        catalog_env.catalog.complete_cleanup_job(
            job_id=claimed.job.id,
            execution=stale_execution,
        )
    assert stale_complete.value.code == ErrorCode.CONFLICT

    completed = catalog_env.catalog.complete_cleanup_job(
        job_id=reclaimed.job.id,
        execution=_cleanup_execution(reclaimed),
    )

    assert completed.job.status == "completed"
    assert completed.job.execution_fence == reclaimed.job.execution_fence
    with catalog_env.Session() as db:
        version = db.get(DocumentVersion, first.version.id)
        assert version.index_cleaned_at is not None


def test_unfenced_cleanup_finalize_cannot_override_active_worker(catalog_env):
    reservation = _reserve(catalog_env, "version-one")
    catalog_env.catalog.fail(
        job_id=reservation.job.id,
        publication_fence=reservation.publication_fence,
        error_code="DOCUMENT_PARSE_FAILED",
    )
    claimed = catalog_env.catalog.claim_cleanup_job(
        worker_id="cleanup-worker-a",
        lease_seconds=30,
    )

    with pytest.raises(AppError) as conflict:
        catalog_env.catalog.record_cleanup(
            document_version_id=reservation.version.id,
        )

    assert conflict.value.code == ErrorCode.CONFLICT
    with catalog_env.Session() as db:
        cleanup_job = db.get(DocumentCleanupJob, claimed.job.id)
        version = db.get(DocumentVersion, reservation.version.id)
        assert cleanup_job.status == "running"
        assert version.index_cleaned_at is None


def test_retirement_jobs_have_unique_operation_identity_across_reuploads(catalog_env):
    first = _reserve(catalog_env, "version-one")
    _publish(catalog_env, first)
    first_retirement = catalog_env.catalog.retire(
        tenant_id="tenant-a",
        knowledge_base_id=catalog_env.knowledge_base.id,
        canonical_name="guide.pdf",
        cleanup_grace=timedelta(0),
        retirement_job_id="retire-first",
    )
    with catalog_env.Session.begin() as db:
        old_cleanup = (
            db.query(DocumentCleanupJob)
            .filter(DocumentCleanupJob.document_version_id == first.version.id)
            .one()
        )
        old_cleanup.status = "dead_letter"

    second = _reserve(catalog_env, "version-two")
    _publish(catalog_env, second)
    second_retirement = catalog_env.catalog.retire(
        tenant_id="tenant-a",
        knowledge_base_id=catalog_env.knowledge_base.id,
        canonical_name="guide.pdf",
        cleanup_grace=timedelta(0),
        retirement_job_id="retire-second",
    )

    first_job = catalog_env.catalog.get_retirement_job(
        job_id=first_retirement.retirement_job_id,
        tenant_id="tenant-a",
    )
    second_job = catalog_env.catalog.get_retirement_job(
        job_id=second_retirement.retirement_job_id,
        tenant_id="tenant-a",
    )
    assert first_job.id != second_job.id
    assert first_job.cleanup_version_ids == (first.version.id,)
    assert second_job.cleanup_version_ids == (second.version.id,)
    with pytest.raises(AppError) as hidden:
        catalog_env.catalog.get_retirement_job(
            job_id=second_job.id,
            tenant_id="tenant-b",
        )
    assert hidden.value.code == ErrorCode.NOT_FOUND


def test_atomic_retirement_scope_is_revoked_before_cleanup_grace_allows_claim(
    catalog_env,
    monkeypatch,
):
    current = _reserve(catalog_env, "version-one")
    _publish(catalog_env, current)
    database_clock = datetime(2026, 7, 16, 3, 0, 0)
    monkeypatch.setattr(catalog_module, "_database_now", lambda _db: database_clock)
    monkeypatch.setattr(
        catalog_module,
        "utcnow",
        lambda: database_clock - timedelta(hours=2),
    )

    retirement = catalog_env.catalog.retire_with_legacy_suppression(
        tenant_id="tenant-a",
        knowledge_base_id=catalog_env.knowledge_base.id,
        canonical_name="guide.pdf",
        owner_id=1,
        vector_collection="legacy_documents",
        cleanup_grace=timedelta(minutes=5),
        retirement_job_id="retire-with-grace",
    )

    assert current.version.id in {version.id for version in retirement.cleanup_versions}
    operation = catalog_env.catalog.get_retirement_job(
        job_id="retire-with-grace",
        tenant_id="tenant-a",
    )
    assert set(operation.cleanup_version_ids) == {
        version.id for version in retirement.cleanup_versions
    }
    assert all(
        version.cleanup_after == database_clock + timedelta(minutes=5)
        for version in retirement.cleanup_versions
    )
    assert (
        catalog_env.catalog.get_current(
            tenant_id="tenant-a",
            knowledge_base_id=catalog_env.knowledge_base.id,
            canonical_name="guide.pdf",
        )
        is None
    )
    due = retirement.cleanup_versions[0].cleanup_after
    assert (
        catalog_env.catalog.claim_cleanup_job(
            worker_id="cleanup-worker-a",
            lease_seconds=30,
            now=due - timedelta(seconds=1),
        )
        is None
    )
    claimed = catalog_env.catalog.claim_cleanup_job(
        worker_id="cleanup-worker-a",
        lease_seconds=30,
        now=due + timedelta(seconds=1),
    )
    assert claimed is not None
    assert claimed.version.id in set(operation.cleanup_version_ids)


def test_atomic_retirement_rolls_back_scope_revoke_when_tombstone_fails(
    catalog_env,
    monkeypatch,
):
    current = _reserve(catalog_env, "version-one")
    _publish(catalog_env, current)

    def fail_tombstone(*_args, **_kwargs):
        raise RuntimeError("simulated crash before legacy tombstone commit")

    monkeypatch.setattr(
        DocumentCatalog,
        "_append_legacy_tombstone",
        staticmethod(fail_tombstone),
    )

    with pytest.raises(RuntimeError, match="simulated crash"):
        catalog_env.catalog.retire_with_legacy_suppression(
            tenant_id="tenant-a",
            knowledge_base_id=catalog_env.knowledge_base.id,
            canonical_name="guide.pdf",
            owner_id=1,
            vector_collection="legacy_documents",
            cleanup_grace=timedelta(minutes=5),
            retirement_job_id="retire-rollback",
        )

    visible = catalog_env.catalog.get_current(
        tenant_id="tenant-a",
        knowledge_base_id=catalog_env.knowledge_base.id,
        canonical_name="guide.pdf",
    )
    assert visible.current_version.id == current.version.id
    with pytest.raises(AppError) as missing_operation:
        catalog_env.catalog.get_retirement_job(
            job_id="retire-rollback",
            tenant_id="tenant-a",
        )
    assert missing_operation.value.code == ErrorCode.NOT_FOUND
    with catalog_env.Session() as db:
        assert db.query(DocumentCleanupJob).count() == 0


def test_cleanup_retry_wait_and_attempt_exhaustion_are_durable(catalog_env):
    first = _reserve(catalog_env, "version-one")
    _publish(catalog_env, first)
    second = _reserve(catalog_env, "version-two")
    _publish(catalog_env, second)
    with catalog_env.Session.begin() as db:
        cleanup_job = (
            db.query(DocumentCleanupJob)
            .filter(DocumentCleanupJob.document_version_id == first.version.id)
            .one()
        )
        cleanup_job.max_attempts = 2

    first_attempt = catalog_env.catalog.claim_cleanup_job(
        worker_id="cleanup-worker-a",
        lease_seconds=30,
    )
    retry_clock = utcnow()
    retry_at = retry_clock + timedelta(minutes=5)
    retry = catalog_env.catalog.schedule_cleanup_retry(
        job_id=first_attempt.job.id,
        execution=_cleanup_execution(first_attempt),
        retry_delay_seconds=300,
        error_code="VECTOR_STORE_UNAVAILABLE",
        now=retry_clock,
    )

    assert retry.status == "retry_wait"
    assert retry.owner_worker_id is None
    assert (
        catalog_env.catalog.claim_cleanup_job(
            worker_id="cleanup-worker-b",
            lease_seconds=30,
            now=retry_at - timedelta(seconds=1),
        )
        is None
    )

    second_attempt = catalog_env.catalog.claim_cleanup_job(
        worker_id="cleanup-worker-b",
        lease_seconds=30,
        now=retry_at + timedelta(seconds=1),
    )
    exhausted = catalog_env.catalog.schedule_cleanup_retry(
        job_id=second_attempt.job.id,
        execution=_cleanup_execution(second_attempt),
        retry_delay_seconds=300,
        error_code="VECTOR_STORE_UNAVAILABLE",
        now=retry_at + timedelta(seconds=1),
    )

    assert second_attempt.job.attempts == 2
    assert exhausted.status == "dead_letter"
    assert exhausted.owner_worker_id is None
    with catalog_env.Session() as db:
        version = db.get(DocumentVersion, first.version.id)
        assert version.index_cleaned_at is None
        assert version.cleanup_error_code == "VECTOR_STORE_UNAVAILABLE"


def _worker_config() -> IndexingWorkerConfig:
    return IndexingWorkerConfig(
        poll_seconds=0.01,
        lease_seconds=30,
        heartbeat_seconds=60,
        retry_base_seconds=5,
        retry_max_seconds=60,
        retry_jitter_ratio=0,
    )


def _worker_index_build(*, status: str = IndexJobStatus.RUNNING) -> VersionBuild:
    return VersionBuild(
        job=SimpleNamespace(
            id="index-job-1",
            status=status,
            attempts=1,
            max_attempts=3,
            publication_fence=4,
            execution_fence=9,
        ),
        document=SimpleNamespace(id="document-1"),
        version=SimpleNamespace(id="version-2"),
    )


def _worker_cleanup_build() -> CleanupBuild:
    return CleanupBuild(
        job=SimpleNamespace(
            id="cleanup-job-1",
            status="running",
            attempts=1,
            max_attempts=3,
            execution_fence=6,
        ),
        document=SimpleNamespace(id="document-1"),
        version=SimpleNamespace(id="version-1"),
    )


class _WorkerCatalog:
    def __init__(
        self,
        *,
        index_build: VersionBuild | None = None,
        cleanup_build: CleanupBuild | None = None,
    ) -> None:
        self.index_build = index_build
        self.cleanup_build = cleanup_build
        self.index_lease_assertions: list[dict] = []
        self.index_claims: list[dict] = []
        self.index_retries: list[dict] = []
        self.index_failures: list[dict] = []
        self.cleanup_updates: list[dict] = []
        self.cleanup_retries: list[dict] = []
        self.cleanup_dead_letters: list[dict] = []
        self.cleanup_completions: list[dict] = []
        self.worker_heartbeats: list[dict] = []

    def record_worker_heartbeat(self, **kwargs) -> None:
        self.worker_heartbeats.append(kwargs)

    def claim_index_job(self, **kwargs):
        self.index_claims.append(kwargs)
        build, self.index_build = self.index_build, None
        return build

    def claim_cleanup_job(self, **_kwargs):
        build, self.cleanup_build = self.cleanup_build, None
        return build

    def assert_index_lease(self, **kwargs):
        self.index_lease_assertions.append(kwargs)
        return True

    def heartbeat_index_job(self, **_kwargs):
        return None

    def heartbeat_cleanup_job(self, **_kwargs):
        return None

    def schedule_index_retry(self, **kwargs):
        self.index_retries.append(kwargs)
        return None

    def fail(self, **kwargs):
        self.index_failures.append(kwargs)
        return None

    def update_cleanup_job(self, **kwargs):
        self.cleanup_updates.append(kwargs)
        return None

    def schedule_cleanup_retry(self, **kwargs):
        self.cleanup_retries.append(kwargs)
        return None

    def dead_letter_cleanup_job(self, **kwargs):
        self.cleanup_dead_letters.append(kwargs)
        return None

    def complete_cleanup_job(self, **kwargs):
        self.cleanup_completions.append(kwargs)
        return None


class _WorkerPublication:
    def __init__(
        self,
        *,
        index_error: BaseException | None = None,
        cleanup_error: BaseException | None = None,
    ) -> None:
        self.index_error = index_error
        self.cleanup_error = cleanup_error
        self.index_calls: list[dict] = []
        self.cleanup_calls: list[dict] = []

    def run(self, job_id: str, *, execution: IndexJobExecution):
        self.index_calls.append({"job_id": job_id, "execution": execution})
        if self.index_error is not None:
            raise self.index_error
        return None

    def cleanup_version(self, **kwargs):
        self.cleanup_calls.append(kwargs)
        if self.cleanup_error is not None:
            raise self.cleanup_error
        callback = kwargs.get("step_callback")
        if callback is not None:
            for step in ("milvus", "parent_store", "object_store", "finalize"):
                callback(step)
        return 1


def test_worker_schedules_retry_for_retryable_index_failure():
    catalog = _WorkerCatalog(index_build=_worker_index_build())
    publication = _WorkerPublication(
        index_error=AppError(
            ErrorCode.VECTOR_STORE_UNAVAILABLE,
            "向量服务暂时不可用",
            status_code=503,
            retryable=True,
            stage="vector_store",
        )
    )
    worker = IndexingWorker(
        catalog=catalog,
        publication=publication,
        worker_id="index-worker-a",
        config=_worker_config(),
    )

    worked = worker.run_once()

    assert worked is True
    assert publication.index_calls == [
        {
            "job_id": "index-job-1",
            "execution": IndexJobExecution(
                worker_id="index-worker-a",
                execution_fence=9,
            ),
        }
    ]
    assert catalog.index_retries
    assert catalog.index_retries[0]["error_code"] == "VECTOR_STORE_UNAVAILABLE"
    assert catalog.index_retries[0]["retry_delay_seconds"] == 5
    assert "retry_at" not in catalog.index_retries[0]
    assert catalog.index_retries[0]["error_detail_redacted"] == ("stage=vector_store")
    assert catalog.index_failures == []


def test_worker_advertises_and_claims_with_its_build_fingerprint():
    profile = BuildProfile(
        parser_version="parser-v2",
        chunker_version="chunker-v2",
        embedding_model="embed-v2",
        index_version="catalog-v2",
    )
    catalog = _WorkerCatalog(index_build=_worker_index_build())
    publication = _WorkerPublication()
    publication.config = SimpleNamespace(build_profile=profile)
    worker = IndexingWorker(
        catalog=catalog,
        publication=publication,
        worker_id="index-worker-a",
        config=_worker_config(),
    )

    assert worker.run_once() is True
    assert catalog.index_claims[0]["build_fingerprint"] == profile.fingerprint
    assert any(
        heartbeat["metadata"].get("build_fingerprint") == profile.fingerprint
        for heartbeat in catalog.worker_heartbeats
    )


def test_worker_marks_nonretryable_index_failure_failed():
    catalog = _WorkerCatalog(index_build=_worker_index_build())
    publication = _WorkerPublication(
        index_error=AppError(
            ErrorCode.DOCUMENT_PARSE_FAILED,
            "文档无法解析",
            status_code=422,
            retryable=False,
            stage="parse",
        )
    )
    worker = IndexingWorker(
        catalog=catalog,
        publication=publication,
        worker_id="index-worker-a",
        config=_worker_config(),
    )

    worked = worker.run_once()

    assert worked is True
    assert catalog.index_retries == []
    assert catalog.index_failures == [
        {
            "job_id": "index-job-1",
            "publication_fence": 4,
            "error_code": "DOCUMENT_PARSE_FAILED",
            "error_detail_redacted": "stage=parse",
            "execution": IndexJobExecution(
                worker_id="index-worker-a",
                execution_fence=9,
            ),
        }
    ]


def test_worker_treats_unknown_index_exception_as_retryable_storage_failure():
    catalog = _WorkerCatalog(index_build=_worker_index_build())
    worker = IndexingWorker(
        catalog=catalog,
        publication=_WorkerPublication(index_error=RuntimeError("database dropped")),
        worker_id="index-worker-a",
        config=_worker_config(),
    )

    assert worker.run_once() is True
    assert catalog.index_failures == []
    assert catalog.index_retries[0]["error_code"] == "STORAGE_UNAVAILABLE"
    assert catalog.index_retries[0]["retry_delay_seconds"] == 5


def test_worker_finalizes_successful_cleanup_without_inline_catalog_finalize():
    catalog = _WorkerCatalog(cleanup_build=_worker_cleanup_build())
    publication = _WorkerPublication()
    worker = IndexingWorker(
        catalog=catalog,
        publication=publication,
        worker_id="index-worker-a",
        config=_worker_config(),
    )

    worked = worker.run_once()

    execution = CleanupJobExecution(
        worker_id="index-worker-a",
        execution_fence=6,
    )
    assert worked is True
    assert [item["current_step"] for item in catalog.cleanup_updates] == [
        "physical_cleanup",
        "milvus",
        "parent_store",
        "object_store",
        "finalize",
    ]
    cleanup_call = publication.cleanup_calls[0]
    assert cleanup_call["document"] == SimpleNamespace(id="document-1")
    assert cleanup_call["version"] == SimpleNamespace(id="version-1")
    assert cleanup_call["finalize"] is False
    assert callable(cleanup_call["step_callback"])
    assert catalog.cleanup_completions == [
        {
            "job_id": "cleanup-job-1",
            "execution": execution,
        }
    ]
    assert catalog.cleanup_retries == []


def test_worker_schedules_backoff_for_retryable_cleanup_failure():
    catalog = _WorkerCatalog(cleanup_build=_worker_cleanup_build())
    publication = _WorkerPublication(
        cleanup_error=AppError(
            ErrorCode.STORAGE_UNAVAILABLE,
            "对象存储暂时不可用",
            status_code=503,
            retryable=True,
            stage="object_store",
        )
    )
    worker = IndexingWorker(
        catalog=catalog,
        publication=publication,
        worker_id="index-worker-a",
        config=_worker_config(),
    )

    worked = worker.run_once()

    assert worked is True
    assert catalog.cleanup_completions == []
    assert catalog.cleanup_dead_letters == []
    assert catalog.cleanup_retries
    assert catalog.cleanup_retries[0]["error_code"] == "STORAGE_UNAVAILABLE"
    assert catalog.cleanup_retries[0]["retry_delay_seconds"] == 5
    assert catalog.cleanup_retries[0]["error_detail_redacted"] == ("stage=object_store")


def test_worker_treats_unknown_cleanup_exception_as_retryable_storage_failure():
    catalog = _WorkerCatalog(cleanup_build=_worker_cleanup_build())
    worker = IndexingWorker(
        catalog=catalog,
        publication=_WorkerPublication(cleanup_error=RuntimeError("database dropped")),
        worker_id="index-worker-a",
        config=_worker_config(),
    )

    assert worker.run_once() is True
    assert catalog.cleanup_dead_letters == []
    assert catalog.cleanup_retries[0]["error_code"] == "STORAGE_UNAVAILABLE"
    assert catalog.cleanup_retries[0]["retry_delay_seconds"] == 5


def test_run_forever_records_starting_draining_and_stopped_heartbeats():
    catalog = _WorkerCatalog()
    worker = IndexingWorker(
        catalog=catalog,
        publication=_WorkerPublication(),
        worker_id="index-worker-a",
        config=_worker_config(),
    )
    stop_event = Event()
    stop_event.set()

    worker.run_forever(stop_event)

    assert [str(item["status"]) for item in catalog.worker_heartbeats] == [
        "starting",
        "running",
        "draining",
        "stopped",
    ]
