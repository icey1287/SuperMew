from __future__ import annotations

import hashlib
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from backend.core.errors import AppError, ErrorCode
from backend.documents.catalog import (
    BuildProfile,
    IndexJobExecution,
    IndexJobRecord,
    IndexJobStatus,
)
from backend.documents.publication import (
    DocumentPublication,
    DocumentPublicationConfig,
    index_job_compatibility_view,
)
from backend.indexing.milvus_writer import IndexVersionScope
from backend.security.uploads import StoredUpload


def _chunk(version_id: str, level: int, index: int) -> dict:
    text = f"level-{level}-{index}"
    return {
        "text": text,
        "filename": "guide.pdf",
        "file_type": "PDF",
        "file_path": "/objects/object.pdf",
        "page_number": 1,
        "chunk_idx": index,
        "chunk_id": f"{version_id}::guide.pdf::p1::l{level}::{index}",
        "parent_chunk_id": "",
        "root_chunk_id": "",
        "chunk_level": level,
        "tenant_id": "default",
        "knowledge_base_id": "kb-1",
        "document_id": "doc-1",
        "document_version_id": version_id,
        "section_id": "page:1",
        "acl_tags": [],
        "index_version": "catalog-v1",
        "content_hash": hashlib.sha256(text.encode()).hexdigest(),
    }


def _build(*, status: str = IndexJobStatus.PENDING):
    profile = BuildProfile(
        parser_version="parser-v1",
        chunker_version="chunker-v1",
        embedding_model="embedding-v1",
        index_version="catalog-v1",
    )
    current = SimpleNamespace(id="version-v1")
    document = SimpleNamespace(
        id="doc-1",
        tenant_id="default",
        knowledge_base_id="kb-1",
        canonical_name="guide.pdf",
        current_version=current,
    )
    version = SimpleNamespace(
        id="version-v2",
        version_number=2,
        source_object_key="object.pdf",
        build_fingerprint=profile.fingerprint,
        parser_version=profile.parser_version,
        chunker_version=profile.chunker_version,
        embedding_model=profile.embedding_model,
        index_version="catalog-v1",
        vector_collection="embeddings_collection_catalog_v1",
        parent_chunk_count=0,
        chunk_count=0,
    )
    document.pending_version = version
    job = SimpleNamespace(
        id="job-1",
        status=status,
        progress=0,
        publication_fence=2,
        expected_current_version_id="version-v1",
    )
    if status == IndexJobStatus.COMPLETED:
        document.current_version = version
        document.pending_version = None
    return SimpleNamespace(document=document, version=version, job=job)


class FakeCatalog:
    def __init__(self, build=None) -> None:
        self.build = build or _build()
        self.current_id = "version-v1"
        self.updates: list[dict] = []
        self.manifests: list[dict] = []
        self.failed: list[dict] = []
        self.cleaned: list[dict] = []
        self.lease_assertions: list[dict] = []
        self.publish_calls: list[dict] = []
        self.publish_error: Exception | None = None
        self.fail_error: Exception | None = None
        self.stale_on_publish_error = False
        self.load_calls: list[dict] = []

    def load_build(self, **kwargs):
        self.load_calls.append(kwargs)
        return self.build

    def assert_index_lease(self, **kwargs):
        self.lease_assertions.append(kwargs)
        return self.build

    def update_job(self, **kwargs):
        self.updates.append(kwargs)
        self.build.job.status = kwargs["status"]
        return self.build.job

    def record_manifest(self, **kwargs):
        self.manifests.append(kwargs)
        self.build.job.status = IndexJobStatus.STAGED
        return self.build

    def publish(self, **kwargs):
        self.publish_calls.append(kwargs)
        if self.publish_error:
            if self.stale_on_publish_error:
                self.build.job.status = IndexJobStatus.CANCELLED
                self.build.document.pending_version = None
            raise self.publish_error
        previous = SimpleNamespace(id=self.current_id)
        self.current_id = self.build.version.id
        self.build.job.status = IndexJobStatus.COMPLETED
        self.build.document.current_version = self.build.version
        self.build.document.pending_version = None
        return SimpleNamespace(
            document=self.build.document,
            version=self.build.version,
            previous_version=previous,
            published=True,
        )

    def fail(self, **kwargs):
        if self.fail_error:
            raise self.fail_error
        self.failed.append(kwargs)
        self.build.job.status = IndexJobStatus.FAILED
        return self.build.job

    def record_cleanup(self, **kwargs):
        self.cleaned.append(kwargs)
        return self.build.version


class FakeLoader:
    def __init__(self, documents=None, error: Exception | None = None) -> None:
        self.documents = documents or [
            _chunk("version-v2", 1, 0),
            _chunk("version-v2", 2, 0),
            _chunk("version-v2", 3, 0),
            _chunk("version-v2", 3, 1),
        ]
        self.error = error

    def load_document(self, *_args, **_kwargs):
        if self.error:
            raise self.error
        return list(self.documents)


class FakeParentStore:
    def __init__(self, *, exact: bool = True) -> None:
        self.exact = exact
        self.calls: list[str] = []

    def delete_by_version(self, **_kwargs):
        self.calls.append("delete")
        return 0

    def upsert_documents(self, documents):
        self.calls.append("upsert")
        return len(documents)

    def verify_version(self, **_kwargs):
        self.calls.append("verify")
        return SimpleNamespace(exact=self.exact)


class FakeWriter:
    def __init__(self, *, exact: bool = True, write_error: Exception | None = None):
        self.exact = exact
        self.write_error = write_error
        self.deleted: list[IndexVersionScope] = []
        self.write_calls = 0

    def build_version_scope(self, **kwargs):
        return IndexVersionScope(
            collection_name=kwargs["collection_name"],
            tenant_id=kwargs["tenant_id"],
            knowledge_base_id=kwargs["knowledge_base_id"],
            document_id=kwargs["document_id"],
            document_version_id=kwargs["document_version_id"],
            index_version=kwargs["index_version"],
        )

    def write_versioned_documents(self, documents, *, progress_callback, **_kwargs):
        self.write_calls += 1
        if self.write_error:
            raise self.write_error
        progress_callback(len(documents), len(documents))
        return SimpleNamespace(chunk_ids=tuple(doc["chunk_id"] for doc in documents))

    def verify_receipt(self, _receipt):
        return SimpleNamespace(exact=self.exact)

    def delete_by_version(self, scope):
        self.deleted.append(scope)
        return 0


def _publication(tmp_path, *, catalog=None, loader=None, parent=None, writer=None):
    (tmp_path / "object.pdf").write_bytes(b"placeholder")
    return DocumentPublication(
        catalog=catalog or FakeCatalog(),
        loader=loader or FakeLoader(),
        parent_store=parent or FakeParentStore(),
        writer=writer or FakeWriter(),
        config=DocumentPublicationConfig(
            tenant_id="default",
            knowledge_base_name="default",
            parser_version="parser-v1",
            chunker_version="chunker-v1",
            embedding_model="embedding-v1",
            index_version="catalog-v1",
            vector_collection="embeddings_collection_catalog_v1",
            upload_dir=tmp_path,
            max_attempts=3,
            cleanup_grace=timedelta(hours=1),
        ),
    )


def test_publication_stages_exact_artifacts_then_atomically_publishes(tmp_path):
    catalog = FakeCatalog()
    parent = FakeParentStore()
    writer = FakeWriter()
    publication = _publication(
        tmp_path,
        catalog=catalog,
        parent=parent,
        writer=writer,
    )

    outcome = publication.run("job-1")

    assert outcome.published is True
    assert outcome.previous_version.id == "version-v1"
    assert catalog.current_id == "version-v2"
    assert parent.calls[:3] == ["delete", "upsert", "verify"]
    assert writer.write_calls == 1
    assert len(catalog.manifests) == 1
    entries = catalog.manifests[0]["entries"]
    assert sum(entry.store_kind == "parent" for entry in entries) == 2
    assert sum(entry.store_kind == "vector" for entry in entries) == 2
    assert catalog.failed == []


def test_worker_execution_fence_is_applied_to_every_durable_publication_write(
    tmp_path,
):
    catalog = FakeCatalog()
    publication = _publication(tmp_path, catalog=catalog)
    execution = IndexJobExecution(
        worker_id="index-worker-a",
        execution_fence=7,
    )

    outcome = publication.run("job-1", execution=execution)

    assert outcome.published is True
    assert catalog.lease_assertions
    assert all(
        assertion["execution"] == execution for assertion in catalog.lease_assertions
    )
    assert catalog.updates
    assert all(update["execution"] == execution for update in catalog.updates)
    assert catalog.manifests[0]["execution"] == execution
    assert catalog.publish_calls[0]["execution"] == execution
    assert catalog.failed == []


@pytest.mark.parametrize(
    ("loader_error", "writer_error", "writer_exact", "expected_code"),
    [
        (ValueError("parse failed"), None, True, ErrorCode.DOCUMENT_PARSE_FAILED),
        (None, RuntimeError("insert failed"), True, ErrorCode.VECTOR_STORE_UNAVAILABLE),
        (None, None, False, ErrorCode.VECTOR_STORE_UNAVAILABLE),
    ],
)
def test_failure_keeps_old_current_and_cleans_unpublished_candidate(
    tmp_path,
    loader_error,
    writer_error,
    writer_exact,
    expected_code,
):
    catalog = FakeCatalog()
    writer = FakeWriter(exact=writer_exact, write_error=writer_error)
    parent = FakeParentStore()
    publication = _publication(
        tmp_path,
        catalog=catalog,
        loader=FakeLoader(error=loader_error),
        parent=parent,
        writer=writer,
    )

    with pytest.raises(AppError) as caught:
        publication.run("job-1")

    assert caught.value.public_error.code == expected_code.value
    assert catalog.current_id == "version-v1"
    assert catalog.failed[0]["publication_fence"] == 2
    assert len(writer.deleted) == 1
    assert parent.calls[-1] == "delete"
    assert not (tmp_path / "object.pdf").exists()
    assert catalog.cleaned == [{"document_version_id": "version-v2"}]


def test_candidate_cleanup_does_not_mark_complete_when_object_unlink_fails(
    tmp_path,
    monkeypatch,
):
    catalog = FakeCatalog()
    publication = _publication(
        tmp_path,
        catalog=catalog,
        loader=FakeLoader(error=ValueError("parse failed")),
    )
    monkeypatch.setattr(
        publication,
        "_unlink_version_object",
        lambda _version: (_ for _ in ()).throw(OSError("unlink failed")),
    )

    with pytest.raises(AppError):
        publication.run("job-1")

    assert (tmp_path / "object.pdf").exists()
    assert catalog.cleaned == []


def test_failure_state_uncertainty_preserves_source_and_candidate_artifacts(tmp_path):
    catalog = FakeCatalog()
    catalog.fail_error = ConnectionError("postgres unavailable")
    writer = FakeWriter()
    parent = FakeParentStore()
    publication = _publication(
        tmp_path,
        catalog=catalog,
        loader=FakeLoader(error=ValueError("parse failed")),
        writer=writer,
        parent=parent,
    )

    with pytest.raises(AppError) as caught:
        publication.run("job-1")

    assert caught.value.public_error.retryable is True
    assert (tmp_path / "object.pdf").exists()
    assert writer.deleted == []
    assert parent.calls == []
    assert catalog.cleaned == []


def test_publish_cas_failure_never_exposes_candidate(tmp_path):
    catalog = FakeCatalog()
    catalog.publish_error = AppError(
        ErrorCode.CONFLICT,
        "stale publication fence",
        status_code=409,
    )
    catalog.stale_on_publish_error = True
    writer = FakeWriter()
    publication = _publication(tmp_path, catalog=catalog, writer=writer)

    with pytest.raises(AppError) as caught:
        publication.run("job-1")

    assert caught.value.public_error.code == ErrorCode.CONFLICT.value
    assert catalog.current_id == "version-v1"
    assert len(writer.deleted) == 1


def test_transient_publish_failure_preserves_exact_verified_candidate(tmp_path):
    catalog = FakeCatalog()
    catalog.publish_error = RuntimeError("database connection dropped")
    writer = FakeWriter()
    publication = _publication(tmp_path, catalog=catalog, writer=writer)

    with pytest.raises(AppError) as caught:
        publication.run("job-1")

    assert caught.value.public_error.retryable is True
    assert catalog.current_id == "version-v1"
    assert catalog.build.job.status == IndexJobStatus.STAGED
    assert writer.deleted == []
    assert catalog.failed == []


def test_worker_reconciliation_uses_the_claimed_document_tenant(tmp_path):
    build = _build()
    build.document.tenant_id = "tenant-b"
    catalog = FakeCatalog(build)
    catalog.publish_error = RuntimeError("database connection dropped")
    publication = _publication(tmp_path, catalog=catalog)

    with pytest.raises(AppError) as caught:
        publication.run(
            "job-1",
            execution=IndexJobExecution(
                worker_id="index-worker-a",
                execution_fence=7,
            ),
        )

    assert caught.value.retryable is True
    assert catalog.load_calls[-1]["tenant_id"] == "tenant-b"


def test_staged_job_resume_only_publishes_without_rebuilding(tmp_path):
    build = _build(status=IndexJobStatus.STAGED)
    build.job.progress = 95
    build.version.parent_chunk_count = 2
    build.version.chunk_count = 2
    catalog = FakeCatalog(build)
    parent = FakeParentStore()
    writer = FakeWriter()
    publication = _publication(
        tmp_path,
        catalog=catalog,
        parent=parent,
        writer=writer,
    )

    outcome = publication.run("job-1")

    assert outcome.published is True
    assert catalog.current_id == "version-v2"
    assert parent.calls == []
    assert writer.write_calls == 0


def test_running_job_resume_never_moves_durable_progress_backwards(tmp_path):
    build = _build(status=IndexJobStatus.RUNNING)
    build.job.progress = 72
    catalog = FakeCatalog(build)
    publication = _publication(tmp_path, catalog=catalog)

    publication.run("job-1")

    durable_progress = [update["progress"] for update in catalog.updates]
    assert durable_progress == sorted(durable_progress)
    assert min(durable_progress) >= 72


def test_completed_job_short_circuits_without_touching_candidate_storage(tmp_path):
    catalog = FakeCatalog(_build(status=IndexJobStatus.COMPLETED))
    parent = FakeParentStore()
    writer = FakeWriter()
    publication = _publication(
        tmp_path,
        catalog=catalog,
        parent=parent,
        writer=writer,
    )

    outcome = publication.run("job-1")

    assert outcome.reused_current is True
    assert parent.calls == []
    assert writer.write_calls == 0


def test_compatibility_view_uses_candidate_publication_semantics():
    now = datetime(2026, 7, 15, 12, 0, 0)
    job = IndexJobRecord(
        id="job-1",
        document_id="doc-1",
        document_version_id="version-v2",
        canonical_name="guide.pdf",
        tenant_id="default",
        status=IndexJobStatus.RUNNING,
        current_step="vector_store",
        progress=60,
        attempts=1,
        max_attempts=3,
        publication_fence=2,
        expected_current_version_id="version-v1",
        owner_worker_id=None,
        lease_expires_at=None,
        heartbeat_at=None,
        next_retry_at=None,
        error_code=None,
        step_state={
            "message": "正在写入候选向量：50 / 100",
            "active_step_percent": 50,
            "total_chunks": 100,
            "processed_chunks": 50,
        },
        finished_at=None,
        created_at=now,
        updated_at=now,
    )

    view = index_job_compatibility_view(job)

    assert [step["key"] for step in view["steps"]] == [
        "upload",
        "reserve",
        "parse",
        "parent_store",
        "vector_store",
        "verify",
        "publish",
    ]
    assert all("清理旧版本" not in step["label"] for step in view["steps"])
    vector = next(step for step in view["steps"] if step["key"] == "vector_store")
    assert vector["status"] == "running"
    assert vector["percent"] == 50
    assert view["document_version_id"] == "version-v2"


@pytest.mark.parametrize("failed_stage", ["parse", "vector_store"])
def test_failed_compatibility_view_preserves_the_actual_failed_stage(failed_stage):
    now = datetime(2026, 7, 15, 12, 0, 0)
    job = IndexJobRecord(
        id="job-1",
        document_id="doc-1",
        document_version_id="version-v2",
        canonical_name="guide.pdf",
        tenant_id="default",
        status=IndexJobStatus.FAILED,
        current_step="failed",
        progress=40,
        attempts=1,
        max_attempts=3,
        publication_fence=2,
        expected_current_version_id="version-v1",
        owner_worker_id=None,
        lease_expires_at=None,
        heartbeat_at=None,
        next_retry_at=None,
        error_code="INDEX_BUILD_FAILED",
        step_state={
            "active_step": failed_stage,
            "active_step_percent": 35,
            "message": "构建失败",
        },
        finished_at=now,
        created_at=now,
        updated_at=now,
    )

    view = index_job_compatibility_view(job)

    assert view["current_step"] == failed_stage
    failed_step = next(step for step in view["steps"] if step["key"] == failed_stage)
    assert failed_step["status"] == "failed"
    assert failed_step["percent"] == 35
    assert all(
        step["status"] == "completed"
        for step in view["steps"][: view["steps"].index(failed_step)]
    )


def test_submit_removes_new_object_when_catalog_reuses_current(tmp_path):
    path = tmp_path / "new.pdf"
    path.write_bytes(b"same content")
    stored = StoredUpload(
        original_name="guide.pdf",
        object_key="new.pdf",
        path=path,
        extension=".pdf",
        media_type="application/pdf",
        size_bytes=12,
        content_sha256="a" * 64,
    )

    class SubmitCatalog:
        def ensure_knowledge_base(self, **_kwargs):
            return SimpleNamespace(id="kb-1")

        def reserve_upload(self, **_kwargs):
            return SimpleNamespace(
                version=SimpleNamespace(source_object_key="current.pdf"),
                already_current=True,
                created=False,
                requeued=False,
            )

    publication = _publication(tmp_path, catalog=SubmitCatalog())

    publication.submit(stored, owner_id=7)

    assert not path.exists()


def test_retire_uses_publication_cleanup_grace_for_versioned_and_legacy_scopes(
    tmp_path,
):
    versioned = SimpleNamespace(id="version-v1")
    legacy = SimpleNamespace(id="version-legacy")
    document = SimpleNamespace(
        id="doc-1",
        tenant_id="default",
        knowledge_base_id="kb-1",
        canonical_name="guide.pdf",
        owner_id=7,
    )

    class RetirementCatalog:
        def __init__(self) -> None:
            self.retire_calls: list[dict] = []

        def list_documents(self, **_kwargs):
            return [document]

        def retire_with_legacy_suppression(self, **kwargs):
            self.retire_calls.append(kwargs)
            return SimpleNamespace(
                cleanup_versions=(versioned, legacy),
            )

    catalog = RetirementCatalog()
    writer = FakeWriter()
    writer.milvus_manager = SimpleNamespace(collection_name="embeddings_collection")
    publication = _publication(tmp_path, catalog=catalog, writer=writer)

    outcome = publication.retire("guide.pdf", owner_id=7)

    assert outcome.cleanup_pending is True
    assert catalog.retire_calls[0]["cleanup_grace"] == timedelta(hours=1)
    assert catalog.retire_calls[0]["retirement_job_id"] == outcome.retirement_job_id


def test_legacy_cleanup_uses_canonical_filename_not_composite_identity(tmp_path):
    class LegacyStore:
        collection_name = "embeddings_collection"

        def __init__(self):
            self.filter_expr = ""

        def with_collection(self, _name):
            return self

        def delete(self, filter_expr):
            self.filter_expr = filter_expr
            return {"delete_count": 3}

    class LegacyWriter(FakeWriter):
        def __init__(self):
            super().__init__()
            self.milvus_manager = LegacyStore()

    class CleanupCatalog(FakeCatalog):
        def record_cleanup(self, **_kwargs):
            return None

    class LegacyParent(FakeParentStore):
        def __init__(self):
            super().__init__()
            self.filename = ""

        def delete_legacy_by_filename(self, filename):
            self.filename = filename
            return 2

    catalog = CleanupCatalog()
    writer = LegacyWriter()
    parent = LegacyParent()
    publication = _publication(
        tmp_path,
        catalog=catalog,
        writer=writer,
        parent=parent,
    )
    document = SimpleNamespace(canonical_name="guide.pdf")
    version = SimpleNamespace(
        id="legacy-version",
        storage_layout="legacy_filename",
        legacy_identity="documents:guide.pdf",
        vector_collection="embeddings_collection",
    )

    deleted = publication.cleanup_version(document=document, version=version)

    assert deleted == 3
    assert 'filename == "guide.pdf"' == writer.milvus_manager.filter_expr
    assert parent.filename == "guide.pdf"


def test_unadopted_legacy_delete_persists_tombstone_before_physical_cleanup(
    tmp_path,
):
    class TombstoneCatalog:
        def __init__(self):
            self.suppressed = False
            self.cleanup_errors = []
            self.document = SimpleNamespace(
                id="doc-tombstone",
                tenant_id="default",
                knowledge_base_id="kb-1",
                canonical_name="legacy.pdf",
                owner_id=7,
            )

        def list_documents(self, **_kwargs):
            return [self.document] if self.suppressed else []

        def find_knowledge_base(self, **_kwargs):
            return SimpleNamespace(id="kb-1")

        def retire_with_legacy_suppression(self, **_kwargs):
            self.suppressed = True
            return SimpleNamespace(
                cleanup_versions=(
                    SimpleNamespace(
                        id="legacy-tombstone",
                        storage_layout="legacy_filename",
                        legacy_identity="legacy:tombstone:v1:redacted",
                        vector_collection="embeddings_collection",
                    ),
                )
            )

        def record_cleanup(self, **kwargs):
            self.cleanup_errors.append(kwargs)

    catalog = TombstoneCatalog()

    class FailingLegacyStore:
        collection_name = "embeddings_collection"

        def with_collection(self, _name):
            return self

        def delete(self, _filter_expr):
            assert catalog.suppressed is True
            raise ConnectionError("milvus unavailable")

    class LegacyWriter(FakeWriter):
        def __init__(self):
            super().__init__()
            self.milvus_manager = FailingLegacyStore()

    publication = _publication(
        tmp_path,
        catalog=catalog,
        writer=LegacyWriter(),
    )

    outcome = publication.retire("legacy.pdf", owner_id=7)

    assert outcome.cleanup_pending is True
    assert outcome.cleanup_step == "pending"
    assert catalog.suppressed is True
    assert catalog.cleanup_errors == []
