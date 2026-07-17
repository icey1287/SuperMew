import hashlib
import unittest
from datetime import timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.core.errors import AppError, ErrorCode
from backend.db.models import (
    Base,
    Document,
    DocumentCleanupJob,
    DocumentVersion,
    IndexJob,
    IndexManifest,
    KnowledgeBase,
    User,
)
from backend.documents.catalog import (
    BuildProfile,
    CleanupJobStatus,
    DocumentCatalog,
    DocumentVersionStatus,
    IndexJobStatus,
    ManifestEntry,
)


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class DocumentCatalogTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)
        with self.Session.begin() as db:
            db.add(User(id=1, username="alice", password_hash="hash", role="user"))
        self.catalog = DocumentCatalog(self.Session)
        self.knowledge_base = self.catalog.ensure_knowledge_base(
            tenant_id="tenant-a",
            owner_id=1,
            name="default",
            knowledge_base_id="kb-default",
        )

    def tearDown(self):
        self.engine.dispose()

    def reserve(
        self,
        content: str,
        *,
        name: str = "guide.pdf",
        profile: BuildProfile | None = None,
    ):
        return self.catalog.reserve_upload(
            tenant_id="tenant-a",
            knowledge_base_id=self.knowledge_base.id,
            canonical_name=name,
            owner_id=1,
            content_sha256=digest(content),
            source_object_key=f"objects/{digest(content)}",
            media_type="application/pdf",
            size_bytes=len(content),
            processing_profile=profile or BuildProfile(embedding_model="embed-v1"),
            vector_collection="documents_v2",
        )

    def stage(self, reservation):
        vector_hash = digest(f"{reservation.version.id}:vector")
        parent_hash = digest(f"{reservation.version.id}:parent")
        return self.catalog.record_manifest(
            job_id=reservation.job.id,
            publication_fence=reservation.publication_fence,
            entries=[
                ManifestEntry(
                    chunk_id=f"{reservation.version.id}:chunk:0",
                    content_hash=vector_hash,
                    store_kind="vector",
                    section_id="section-1",
                    chunk_level=1,
                ),
                ManifestEntry(
                    chunk_id=f"{reservation.version.id}:parent:0",
                    content_hash=parent_hash,
                    store_kind="parent",
                    section_id="section-1",
                    chunk_level=0,
                ),
            ],
            vector_chunk_count=1,
            parent_chunk_count=1,
        )

    def publish(self, reservation):
        self.stage(reservation)
        return self.catalog.publish(
            job_id=reservation.job.id,
            publication_fence=reservation.publication_fence,
            expected_current_version_id=reservation.expected_current_version_id,
        )

    def test_same_content_and_build_is_idempotent_but_new_build_is_new_version(self):
        first = self.reserve("version-one")
        repeated = self.reserve("version-one")

        self.assertTrue(first.created)
        self.assertFalse(repeated.created)
        self.assertEqual(first.version.id, repeated.version.id)
        self.assertEqual(first.job.id, repeated.job.id)
        self.assertEqual(first.publication_fence, repeated.publication_fence)

        rebuilt = self.reserve(
            "version-one",
            profile=BuildProfile(
                parser_version="v2",
                chunker_version="v1",
                embedding_model="embed-v1",
                index_version="v1",
            ),
        )
        self.assertNotEqual(first.version.id, rebuilt.version.id)
        self.assertEqual(2, rebuilt.version.version_number)
        with self.Session() as db:
            self.assertEqual(2, db.query(DocumentVersion).count())
            self.assertEqual(2, db.query(IndexJob).count())
            old = db.get(DocumentVersion, first.version.id)
            old_job = db.get(IndexJob, first.job.id)
            self.assertEqual(DocumentVersionStatus.SUPERSEDED, old.status)
            self.assertEqual(IndexJobStatus.CANCELLED, old_job.status)

    def test_retrieval_snapshot_excludes_documents_from_inactive_knowledge_bases(self):
        self.publish(self.reserve("version-one"))
        with self.Session.begin() as db:
            kb = db.get(KnowledgeBase, self.knowledge_base.id)
            kb.status = "inactive"

        snapshot = self.catalog.load_retrieval_snapshot(tenant_id="tenant-a")

        self.assertEqual((), snapshot.documents)

    def test_failed_version_retry_gets_new_identity_and_refreshes_source_object(self):
        failed = self.reserve("version-one")
        self.catalog.fail(
            job_id=failed.job.id,
            publication_fence=failed.publication_fence,
            error_code="DOCUMENT_PARSE_FAILED",
        )
        requeued = self.catalog.reserve_upload(
            tenant_id="tenant-a",
            knowledge_base_id=self.knowledge_base.id,
            canonical_name="guide.pdf",
            owner_id=1,
            content_sha256=digest("version-one"),
            source_object_key="objects/new-upload-copy",
            media_type="application/octet-stream",
            size_bytes=99,
            processing_profile=BuildProfile(embedding_model="embed-v1"),
            vector_collection="documents_v2_recreated",
        )
        self.assertTrue(requeued.created)
        self.assertTrue(requeued.requeued)
        self.assertNotEqual(failed.version.id, requeued.version.id)
        self.assertNotEqual(failed.job.id, requeued.job.id)
        self.assertEqual(2, requeued.version.version_number)
        self.assertEqual("objects/new-upload-copy", requeued.version.source_object_key)
        self.assertEqual("application/octet-stream", requeued.version.media_type)
        self.assertEqual(99, requeued.version.size_bytes)
        self.assertEqual("documents_v2_recreated", requeued.version.vector_collection)
        with self.Session() as db:
            immutable_failed = db.get(DocumentVersion, failed.version.id)
            self.assertEqual(DocumentVersionStatus.FAILED, immutable_failed.status)
            self.assertEqual(2, db.query(DocumentVersion).count())
            self.assertEqual(2, db.query(IndexJob).count())

    def test_failed_candidate_does_not_change_published_current(self):
        first = self.reserve("version-one")
        self.publish(first)
        candidate = self.reserve("version-two")

        while_building = self.catalog.get_current(
            tenant_id="tenant-a",
            knowledge_base_id=self.knowledge_base.id,
            canonical_name="guide.pdf",
        )
        self.assertEqual(first.version.id, while_building.current_version.id)

        self.catalog.fail(
            job_id=candidate.job.id,
            publication_fence=candidate.publication_fence,
            error_code="EMBEDDING_UNAVAILABLE",
            error_detail_redacted="provider unavailable",
        )
        current = self.catalog.get_current(
            tenant_id="tenant-a",
            knowledge_base_id=self.knowledge_base.id,
            canonical_name="guide.pdf",
        )
        self.assertEqual(first.version.id, current.current_version.id)
        self.assertIsNone(current.pending_version)
        with self.Session() as db:
            failed = db.get(DocumentVersion, candidate.version.id)
            self.assertEqual(DocumentVersionStatus.FAILED, failed.status)
            self.assertIsNotNone(failed.cleanup_after)

        cleanup = self.catalog.cleanup_candidates(tenant_id="tenant-a")
        self.assertEqual([candidate.version.id], [item.version.id for item in cleanup])
        cleanup_failed = self.catalog.record_cleanup(
            document_version_id=candidate.version.id,
            error_code="VECTOR_DELETE_FAILED",
        )
        self.assertEqual("EMBEDDING_UNAVAILABLE", cleanup_failed.error_code)
        self.assertEqual("VECTOR_DELETE_FAILED", cleanup_failed.cleanup_error_code)
        cleaned = self.catalog.record_cleanup(document_version_id=candidate.version.id)
        self.assertEqual("EMBEDDING_UNAVAILABLE", cleaned.error_code)
        self.assertIsNotNone(cleaned.index_cleaned_at)
        late_error = self.catalog.record_cleanup(
            document_version_id=candidate.version.id,
            error_code="LATE_CACHE_DELETE_FAILURE",
        )
        self.assertIsNotNone(late_error.index_cleaned_at)
        self.assertIsNone(late_error.cleanup_error_code)
        self.assertEqual([], self.catalog.cleanup_candidates(tenant_id="tenant-a"))

    def test_publish_uses_fence_and_marks_previous_version_for_cleanup(self):
        first = self.reserve("version-one")
        self.publish(first)
        second = self.reserve("version-two")
        result = self.publish(second)

        self.assertTrue(result.published)
        self.assertEqual(second.version.id, result.document.current_version.id)
        self.assertIsNone(result.document.pending_version)
        self.assertEqual(
            DocumentVersionStatus.SUPERSEDED, result.previous_version.status
        )
        self.assertIsNotNone(result.previous_version.cleanup_after)
        self.assertEqual(
            timedelta(hours=1),
            result.previous_version.cleanup_after
            - result.previous_version.superseded_at,
        )
        self.assertGreater(result.document.catalog_revision, 0)

        repeated = self.catalog.publish(
            job_id=second.job.id,
            publication_fence=second.publication_fence,
            expected_current_version_id=second.expected_current_version_id,
        )
        self.assertFalse(repeated.published)

    def test_stale_candidate_cannot_record_or_publish_after_new_reservation(self):
        current = self.reserve("version-one")
        self.publish(current)
        stale = self.reserve("version-two")
        self.stage(stale)
        latest = self.reserve("version-three")

        with self.assertRaises(AppError) as raised:
            self.catalog.publish(
                job_id=stale.job.id,
                publication_fence=stale.publication_fence,
                expected_current_version_id=stale.expected_current_version_id,
            )
        self.assertEqual(ErrorCode.CONFLICT, raised.exception.code)
        visible = self.catalog.get_current(
            tenant_id="tenant-a",
            canonical_name="guide.pdf",
            knowledge_base_id=self.knowledge_base.id,
        )
        self.assertEqual(current.version.id, visible.current_version.id)
        self.assertEqual(latest.version.id, visible.pending_version.id)

    def test_stale_failure_cannot_rewrite_cancelled_job_terminal_state(self):
        stale = self.reserve("version-one")
        latest = self.reserve("version-two")

        with self.assertRaises(AppError) as raised:
            self.catalog.fail(
                job_id=stale.job.id,
                publication_fence=stale.publication_fence,
                error_code="VECTOR_STORE_UNAVAILABLE",
            )

        self.assertEqual(ErrorCode.CONFLICT, raised.exception.code)
        with self.Session() as db:
            stale_job = db.get(IndexJob, stale.job.id)
            stale_version = db.get(DocumentVersion, stale.version.id)
            document = db.query(Document).one()
            self.assertEqual(IndexJobStatus.CANCELLED, stale_job.status)
            self.assertEqual(DocumentVersionStatus.SUPERSEDED, stale_version.status)
            self.assertEqual(latest.version.id, document.pending_version_id)

    def test_manifest_is_exact_and_rejects_duplicate_or_mismatched_counts(self):
        reservation = self.reserve("version-one")
        duplicate = ManifestEntry(
            chunk_id="duplicate",
            content_hash=digest("chunk"),
            store_kind="vector",
        )
        with self.assertRaises(AppError):
            self.catalog.record_manifest(
                job_id=reservation.job.id,
                publication_fence=reservation.publication_fence,
                entries=[duplicate, duplicate],
            )
        with self.assertRaises(AppError):
            self.catalog.record_manifest(
                job_id=reservation.job.id,
                publication_fence=reservation.publication_fence,
                entries=[duplicate],
                vector_chunk_count=2,
                parent_chunk_count=0,
            )

    def test_durable_progress_and_job_queries_are_fencing_aware(self):
        reservation = self.reserve("version-one")
        running = self.catalog.update_job(
            job_id=reservation.job.id,
            publication_fence=reservation.publication_fence,
            status=IndexJobStatus.RUNNING,
            current_step="parsing",
            progress=20,
            step_state_patch={"page": 2},
            increment_attempts=True,
        )
        self.assertEqual(IndexJobStatus.RUNNING, running.status)
        self.assertEqual(1, running.attempts)
        self.assertEqual(2, running.step_state["page"])
        self.assertEqual(running.id, self.catalog.get_job(job_id=running.id).id)
        self.assertEqual(running.id, self.catalog.load_build(job_id=running.id).job.id)
        self.assertEqual(
            [running.id],
            [job.id for job in self.catalog.list_jobs(tenant_id="tenant-a")],
        )
        with self.assertRaises(AppError) as raised:
            self.catalog.update_job(
                job_id=running.id,
                publication_fence=reservation.publication_fence + 1,
                progress=30,
            )
        self.assertEqual(ErrorCode.CONFLICT, raised.exception.code)

    def test_cancelled_and_dead_letter_jobs_clear_pending_and_cannot_publish(self):
        current = self.reserve("version-one")
        self.publish(current)
        cancelled = self.reserve("version-two")
        self.catalog.update_job(
            job_id=cancelled.job.id,
            publication_fence=cancelled.publication_fence,
            status=IndexJobStatus.CANCELLED,
            current_step="cancelled",
        )
        visible = self.catalog.get_current(
            tenant_id="tenant-a",
            knowledge_base_id=self.knowledge_base.id,
            canonical_name="guide.pdf",
        )
        self.assertEqual(current.version.id, visible.current_version.id)
        self.assertIsNone(visible.pending_version)
        with self.assertRaises(AppError):
            self.stage(cancelled)

        dead_letter = self.reserve("version-three")
        self.catalog.update_job(
            job_id=dead_letter.job.id,
            publication_fence=dead_letter.publication_fence,
            status=IndexJobStatus.RUNNING,
            current_step="indexing",
            progress=50,
        )
        self.catalog.update_job(
            job_id=dead_letter.job.id,
            publication_fence=dead_letter.publication_fence,
            status=IndexJobStatus.DEAD_LETTER,
            current_step="dead_letter",
            step_state_patch={"error_code": "INDEX_RETRIES_EXHAUSTED"},
        )
        visible = self.catalog.get_current(
            tenant_id="tenant-a",
            knowledge_base_id=self.knowledge_base.id,
            canonical_name="guide.pdf",
        )
        self.assertEqual(current.version.id, visible.current_version.id)
        self.assertIsNone(visible.pending_version)
        with self.assertRaises(AppError):
            self.catalog.publish(
                job_id=dead_letter.job.id,
                publication_fence=dead_letter.publication_fence,
                expected_current_version_id=dead_letter.expected_current_version_id,
            )
        with self.Session() as db:
            cancelled_version = db.get(DocumentVersion, cancelled.version.id)
            dead_version = db.get(DocumentVersion, dead_letter.version.id)
            self.assertEqual(DocumentVersionStatus.SUPERSEDED, cancelled_version.status)
            self.assertEqual(DocumentVersionStatus.FAILED, dead_version.status)

    def test_retire_is_atomic_logical_delete_and_idempotent(self):
        current = self.reserve("version-one")
        self.publish(current)
        pending = self.reserve("version-two")

        retired = self.catalog.retire(
            tenant_id="tenant-a",
            knowledge_base_id=self.knowledge_base.id,
            canonical_name="guide.pdf",
        )
        self.assertTrue(retired.found)
        self.assertFalse(retired.already_deleted)
        self.assertEqual(2, len(retired.cleanup_versions))
        self.assertIsNone(
            self.catalog.get_current(
                tenant_id="tenant-a",
                knowledge_base_id=self.knowledge_base.id,
                canonical_name="guide.pdf",
            )
        )
        self.assertEqual(
            [],
            self.catalog.list_documents(
                tenant_id="tenant-a",
                knowledge_base_id=self.knowledge_base.id,
            ),
        )
        included = self.catalog.list_documents(
            tenant_id="tenant-a",
            knowledge_base_id=self.knowledge_base.id,
            include_deleted=True,
        )
        self.assertEqual("deleted", included[0].status)
        with self.Session() as db:
            pending_job = db.get(IndexJob, pending.job.id)
            self.assertEqual(IndexJobStatus.CANCELLED, pending_job.status)
            document = db.query(Document).one()
            self.assertIsNone(document.current_version_id)
            self.assertIsNone(document.pending_version_id)

        repeated = self.catalog.retire(
            tenant_id="tenant-a",
            knowledge_base_id=self.knowledge_base.id,
            canonical_name="guide.pdf",
        )
        self.assertTrue(repeated.already_deleted)

    def test_repeated_retire_preserves_cleanup_retry_backoff(self):
        current = self.reserve("version-one")
        self.publish(current)
        retired = self.catalog.retire(
            tenant_id="tenant-a",
            knowledge_base_id=self.knowledge_base.id,
            canonical_name="guide.pdf",
        )
        retry_at = retired.cleanup_versions[0].cleanup_after + timedelta(minutes=5)
        with self.Session.begin() as db:
            cleanup_job = (
                db.query(DocumentCleanupJob)
                .filter(DocumentCleanupJob.document_version_id == current.version.id)
                .one()
            )
            cleanup_job.status = CleanupJobStatus.RETRY_WAIT
            cleanup_job.next_retry_at = retry_at

        self.catalog.retire(
            tenant_id="tenant-a",
            knowledge_base_id=self.knowledge_base.id,
            canonical_name="guide.pdf",
        )

        with self.Session() as db:
            cleanup_job = (
                db.query(DocumentCleanupJob)
                .filter(DocumentCleanupJob.document_version_id == current.version.id)
                .one()
            )
            self.assertEqual(CleanupJobStatus.RETRY_WAIT, cleanup_job.status)
            self.assertEqual(retry_at, cleanup_job.next_retry_at)

    def test_retired_cleanup_snapshot_cannot_alias_same_content_reupload(self):
        first = self.reserve("version-one")
        self.publish(first)
        retired = self.catalog.retire(
            tenant_id="tenant-a",
            knowledge_base_id=self.knowledge_base.id,
            canonical_name="guide.pdf",
        )
        stale_cleanup_version = retired.cleanup_versions[0]

        replacement = self.reserve("version-one")
        self.assertNotEqual(stale_cleanup_version.id, replacement.version.id)
        self.assertEqual(2, replacement.version.version_number)
        self.publish(replacement)

        self.catalog.record_cleanup(
            document_version_id=stale_cleanup_version.id,
        )
        current = self.catalog.get_current(
            tenant_id="tenant-a",
            knowledge_base_id=self.knowledge_base.id,
            canonical_name="guide.pdf",
        )
        self.assertEqual(replacement.version.id, current.current_version.id)
        with self.Session() as db:
            old = db.get(DocumentVersion, stale_cleanup_version.id)
            new = db.get(DocumentVersion, replacement.version.id)
            self.assertEqual(DocumentVersionStatus.SUPERSEDED, old.status)
            self.assertIsNotNone(old.index_cleaned_at)
            self.assertEqual(DocumentVersionStatus.READY, new.status)

    def test_current_index_fingerprint_is_stable_and_manifest_sensitive(self):
        empty_a = self.catalog.current_index_fingerprint(tenant_id="tenant-a")
        empty_b = self.catalog.current_index_fingerprint(tenant_id="tenant-a")
        self.assertEqual(empty_a, empty_b)

        reservation = self.reserve("version-one")
        self.assertEqual(
            empty_a,
            self.catalog.current_index_fingerprint(tenant_id="tenant-a"),
        )
        self.catalog.update_job(
            job_id=reservation.job.id,
            publication_fence=reservation.publication_fence,
            status=IndexJobStatus.RUNNING,
            current_step="parsing",
            progress=20,
        )
        self.assertEqual(
            empty_a,
            self.catalog.current_index_fingerprint(tenant_id="tenant-a"),
        )
        self.publish(reservation)
        published_a = self.catalog.current_index_fingerprint(tenant_id="tenant-a")
        published_b = self.catalog.current_index_fingerprint(tenant_id="tenant-a")
        self.assertNotEqual(empty_a, published_a)
        self.assertEqual(published_a, published_b)

        failed_candidate = self.reserve("version-two")
        self.catalog.update_job(
            job_id=failed_candidate.job.id,
            publication_fence=failed_candidate.publication_fence,
            status=IndexJobStatus.RUNNING,
            current_step="vector_store",
            progress=50,
        )
        self.assertEqual(
            published_a,
            self.catalog.current_index_fingerprint(tenant_id="tenant-a"),
        )
        self.catalog.fail(
            job_id=failed_candidate.job.id,
            publication_fence=failed_candidate.publication_fence,
            error_code="VECTOR_STORE_UNAVAILABLE",
        )
        self.assertEqual(
            published_a,
            self.catalog.current_index_fingerprint(tenant_id="tenant-a"),
        )
        self.catalog.record_cleanup(document_version_id=failed_candidate.version.id)
        snapshot = self.catalog.load_retrieval_snapshot(tenant_id="tenant-a")
        self.assertEqual(published_a, snapshot.index_id)
        with self.Session() as db:
            manifest = db.query(IndexManifest).filter_by(store_kind="vector").one()
            manifest.content_hash = digest("mutated")
            db.commit()
        mutated = self.catalog.current_index_fingerprint(tenant_id="tenant-a")
        self.assertNotEqual(published_a, mutated)


if __name__ == "__main__":
    unittest.main()
