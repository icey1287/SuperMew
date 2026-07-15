from __future__ import annotations

import hashlib
import json

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.db.models import (
    Base,
    Document,
    DocumentCatalogState,
    DocumentVersion,
    IndexJob,
    KnowledgeBase,
    ParentChunk,
    User,
)
from backend.documents.catalog import BuildProfile, DocumentCatalog, ManifestEntry
from scripts.adopt_legacy_document_catalog import (
    LegacyDocumentCatalogAdopter,
    main,
    scan_legacy_documents,
)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _leaf(
    filename: str,
    text: str,
    index: int,
    *,
    file_type: str = "PDF",
    parent_chunk_id: str = "",
) -> dict:
    return {
        "filename": filename,
        "file_type": file_type,
        "chunk_id": f"{filename}::p0::l3::{index}",
        "chunk_level": 3,
        "parent_chunk_id": parent_chunk_id,
        "text": text,
    }


class FakeMilvusStore:
    def __init__(
        self,
        rows: list[dict],
        *,
        collection_name: str = "legacy_docs",
        exists: bool = True,
    ):
        self.rows = list(rows)
        self.collection_name = collection_name
        self.exists = exists
        self.calls: list[tuple[str, tuple[str, ...]]] = []

    def has_collection(self):
        return self.exists

    def query_all(self, filter_expr: str = "", output_fields=None):
        self.calls.append((filter_expr, tuple(output_fields or ())))
        return list(self.rows)


class FailingMilvusStore(FakeMilvusStore):
    def query_all(self, filter_expr: str = "", output_fields=None):
        raise RuntimeError(
            "http://private-milvus:19530 token=super-secret raw document body"
        )


def _database():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    return engine, session_factory


def _adopter(session_factory, store: FakeMilvusStore):
    return LegacyDocumentCatalogAdopter(
        store=store,
        catalog=DocumentCatalog(session_factory),
        session_factory=session_factory,
    )


def _run(
    adopter: LegacyDocumentCatalogAdopter,
    *,
    dry_run: bool = False,
    create_target_knowledge_base: bool = False,
):
    return adopter.run(
        owner_username="alice",
        tenant_id="tenant-a",
        knowledge_base_name="main-kb",
        dry_run=dry_run,
        create_target_knowledge_base=create_target_knowledge_base,
    )


def _add_owner(session_factory):
    with session_factory.begin() as db:
        db.add_all(
            [
                User(id=1, username="alice", password_hash="hash", role="admin"),
                KnowledgeBase(
                    id="kb-main",
                    tenant_id="tenant-a",
                    name="main-kb",
                    owner_id=1,
                    status="active",
                ),
            ]
        )


def _add_parent(
    session_factory,
    *,
    chunk_id: str,
    filename: str,
    document_version_id: str = "",
    parent_chunk_id: str = "",
    chunk_level: int = 2,
    text: str | None = None,
):
    with session_factory.begin() as db:
        db.add(
            ParentChunk(
                chunk_id=chunk_id,
                filename=filename,
                text=text or f"parent body {chunk_id}",
                document_version_id=document_version_id,
                parent_chunk_id=parent_chunk_id,
                chunk_level=chunk_level,
            )
        )


def _reserve(catalog: DocumentCatalog, kb_id: str, name: str, content: str):
    return catalog.reserve_upload(
        tenant_id="tenant-a",
        knowledge_base_id=kb_id,
        canonical_name=name,
        owner_id=1,
        content_sha256=_digest(content),
        source_object_key=f"object-{_digest(content)}",
        media_type="application/pdf",
        size_bytes=len(content),
        processing_profile=BuildProfile(embedding_model="embed-v1"),
        vector_collection="catalog_docs",
    )


def _publish(catalog: DocumentCatalog, reservation):
    catalog.record_manifest(
        job_id=reservation.job.id,
        publication_fence=reservation.publication_fence,
        entries=[
            ManifestEntry(
                chunk_id=f"{reservation.version.id}:leaf:0",
                content_hash=_digest("leaf"),
                store_kind="vector",
                chunk_level=3,
            )
        ],
        vector_chunk_count=1,
        parent_chunk_count=0,
    )
    return catalog.publish(
        job_id=reservation.job.id,
        publication_fence=reservation.publication_fence,
        expected_current_version_id=reservation.expected_current_version_id,
    )


def test_dry_run_scans_all_leaf_chunks_without_any_database_write():
    engine, session_factory = _database()
    try:
        _add_owner(session_factory)
        _add_parent(
            session_factory,
            chunk_id="guide-parent-1",
            filename="guide.pdf",
        )
        _add_parent(
            session_factory,
            chunk_id="guide-parent-2",
            filename="guide.pdf",
        )
        _add_parent(
            session_factory,
            chunk_id="new-version-parent",
            filename="guide.pdf",
            document_version_id="docver-new",
        )
        store = FakeMilvusStore(
            [
                _leaf(
                    "guide.pdf",
                    "private leaf body one",
                    0,
                    parent_chunk_id="guide-parent-1",
                ),
                _leaf(
                    "guide.pdf",
                    "private leaf body two",
                    1,
                    parent_chunk_id="guide-parent-2",
                ),
            ]
        )

        summary = _run(_adopter(session_factory, store), dry_run=True)

        assert summary["status"] == "ok"
        assert summary["mode"] == "dry_run"
        assert summary["leaf_chunks_scanned"] == 2
        assert summary["documents_eligible"] == 1
        assert summary["documents_adopted"] == 0
        assert summary["results"][0]["status"] == "planned"
        assert summary["results"][0]["parent_chunk_count"] == 2
        assert store.calls == [
            (
                "chunk_level == 3",
                (
                    "filename",
                    "file_type",
                    "chunk_id",
                    "chunk_level",
                    "parent_chunk_id",
                    "text",
                ),
            )
        ]
        serialized = json.dumps(summary, ensure_ascii=False)
        assert "guide.pdf" not in serialized
        assert "private leaf body" not in serialized
        with session_factory() as db:
            assert db.query(KnowledgeBase).count() == 1
            assert db.query(DocumentCatalogState).count() == 0
            assert db.query(Document).count() == 0
            assert db.query(DocumentVersion).count() == 0
            assert db.query(IndexJob).count() == 0
    finally:
        engine.dispose()


def test_first_adoption_creates_current_legacy_version_with_exact_counts():
    engine, session_factory = _database()
    try:
        _add_owner(session_factory)
        _add_parent(
            session_factory,
            chunk_id="guide-parent-1",
            filename="guide.pdf",
        )
        _add_parent(
            session_factory,
            chunk_id="guide-parent-2",
            filename="guide.pdf",
        )
        store = FakeMilvusStore(
            [
                _leaf("guide.pdf", "leaf A", 0, parent_chunk_id="guide-parent-1"),
                _leaf("guide.pdf", "leaf B", 1, parent_chunk_id="guide-parent-2"),
            ]
        )

        summary = _run(_adopter(session_factory, store))

        assert summary["status"] == "ok"
        assert summary["documents_adopted"] == 1
        assert summary["adoption_complete"] is True
        assert summary["results"][0]["reason"] == "adopted"
        serialized = json.dumps(summary, ensure_ascii=False)
        assert "guide.pdf" not in serialized
        assert "leaf A" not in serialized
        with session_factory() as db:
            state = db.query(DocumentCatalogState).one()
            document = db.query(Document).one()
            version = db.query(DocumentVersion).one()
            job = db.query(IndexJob).one()
            assert state.tenant_id == "tenant-a"
            assert state.legacy_adoption_completed_at is not None
            assert len(state.legacy_corpus_fingerprint) == 64
            assert state.legacy_knowledge_base_id == "kb-main"
            assert document.canonical_name == "guide.pdf"
            assert document.current_version_id == version.id
            assert document.pending_version_id is None
            assert version.storage_layout == "legacy_filename"
            assert version.vector_collection == "legacy_docs"
            assert version.legacy_identity.startswith("legacy:source:v1:")
            assert version.chunk_count == 2
            assert version.parent_chunk_count == 2
            assert version.source_object_key == "guide.pdf"
            assert job.status == "completed"
    finally:
        engine.dispose()


def test_repeated_adoption_is_idempotent_with_order_independent_identity():
    engine, session_factory = _database()
    try:
        _add_owner(session_factory)
        store = FakeMilvusStore(
            [
                _leaf("guide.pdf", "leaf A", 0),
                _leaf("guide.pdf", "leaf B", 1),
            ]
        )
        adopter = _adopter(session_factory, store)
        first_scan = scan_legacy_documents(store)
        first = _run(adopter)
        store.rows.reverse()
        second_scan = scan_legacy_documents(store)
        second = _run(adopter)

        assert first["documents_adopted"] == 1
        assert second["status"] == "no_changes"
        assert second["documents_adopted"] == 0
        assert second["documents_skipped"] == 0
        assert second["adoption_complete"] is True
        assert second["results"][0]["status"] == "satisfied"
        assert second["results"][0]["reason"] == "already_adopted"
        assert (
            first_scan.candidates[0].legacy_identity
            == second_scan.candidates[0].legacy_identity
        )
        with session_factory() as db:
            assert db.query(Document).count() == 1
            assert db.query(DocumentVersion).count() == 1
            assert db.query(IndexJob).count() == 1
    finally:
        engine.dispose()


def test_legacy_corpus_drift_reopens_gate_until_reconciled():
    engine, session_factory = _database()
    try:
        _add_owner(session_factory)
        store = FakeMilvusStore([_leaf("guide.pdf", "leaf A", 0)])
        adopter = _adopter(session_factory, store)
        first = _run(adopter)
        store.rows = [_leaf("guide.pdf", "leaf changed", 0)]

        second = _run(adopter)

        assert first["adoption_complete"] is True
        assert second["status"] == "error"
        assert second["error_code"] == "LEGACY_ADOPTION_INCOMPLETE"
        assert second["adoption_complete"] is False
        with session_factory() as db:
            state_row = db.query(DocumentCatalogState).one()
            assert state_row.legacy_adoption_completed_at is None
        state = DocumentCatalog(session_factory).legacy_adoption_state(
            tenant_id="tenant-a"
        )
        assert state.complete is False
    finally:
        engine.dispose()


def test_existing_current_and_pending_catalog_versions_are_never_overwritten():
    engine, session_factory = _database()
    try:
        _add_owner(session_factory)
        catalog = DocumentCatalog(session_factory)
        kb = catalog.ensure_knowledge_base(
            tenant_id="tenant-a",
            owner_id=1,
            name="main-kb",
            knowledge_base_id="kb-main",
        )
        current = _reserve(catalog, kb.id, "current.pdf", "versioned current")
        published = _publish(catalog, current)
        pending = _reserve(catalog, kb.id, "pending.pdf", "versioned pending")
        store = FakeMilvusStore(
            [
                _leaf("current.pdf", "legacy current", 0),
                _leaf("pending.pdf", "legacy pending", 0),
            ]
        )

        summary = _run(
            LegacyDocumentCatalogAdopter(
                store=store,
                catalog=catalog,
                session_factory=session_factory,
            )
        )

        assert summary["status"] == "error"
        assert summary["error_code"] == "LEGACY_ADOPTION_INCOMPLETE"
        assert summary["documents_adopted"] == 0
        assert summary["documents_skipped"] == 1
        assert {item["reason"] for item in summary["results"]} == {
            "catalog_current_suppresses_legacy",
            "catalog_not_empty",
        }
        with session_factory() as db:
            rows = {row.canonical_name: row for row in db.query(Document).all()}
            assert rows["current.pdf"].current_version_id == published.version.id
            assert rows["current.pdf"].pending_version_id is None
            assert rows["pending.pdf"].current_version_id is None
            assert rows["pending.pdf"].pending_version_id == pending.version.id
            assert db.query(DocumentVersion).count() == 3
    finally:
        engine.dispose()


def test_malicious_filename_is_skipped_without_leaking_name_or_body():
    engine, session_factory = _database()
    try:
        _add_owner(session_factory)
        malicious_name = "../../private/secret.pdf"
        secret_body = "TOP-SECRET-DOCUMENT-CONTENT"
        summary = _run(
            _adopter(
                session_factory,
                FakeMilvusStore(
                    [
                        _leaf(malicious_name, secret_body, 0),
                        _leaf(malicious_name, "SECOND-SECRET-BODY", 1),
                    ]
                ),
            )
        )

        assert summary["status"] == "error"
        assert summary["error_code"] == "LEGACY_ADOPTION_REQUIRES_CLEANUP"
        assert summary["documents_eligible"] == 0
        assert summary["unsafe_documents_skipped"] == 1
        serialized = json.dumps(summary, ensure_ascii=False)
        assert malicious_name not in serialized
        assert secret_body not in serialized
        with session_factory() as db:
            state = db.query(DocumentCatalogState).one()
            assert state.legacy_adoption_completed_at is None
            assert db.query(Document).count() == 0
    finally:
        engine.dispose()


def test_invalid_or_duplicate_leaf_identity_taints_the_whole_document():
    for rows, unsafe_count in (
        ([{**_leaf("guide.pdf", "leaf", 0), "chunk_id": ""}], 1),
        ([{**_leaf("guide.pdf", "leaf", 0), "content_hash": "a" * 64}], 1),
        (
            [
                {
                    **_leaf("guide.pdf", "", 0),
                    "content_hash": _digest("different"),
                }
            ],
            1,
        ),
        (
            [
                _leaf("guide.pdf", "leaf one", 0),
                _leaf("guide.pdf", "leaf two", 0),
            ],
            1,
        ),
        (
            [
                _leaf("guide.pdf", "leaf one", 0),
                {
                    **_leaf("other.pdf", "leaf two", 1),
                    "chunk_id": "guide.pdf::p0::l3::0",
                },
            ],
            2,
        ),
    ):
        engine, session_factory = _database()
        try:
            _add_owner(session_factory)
            summary = _run(_adopter(session_factory, FakeMilvusStore(rows)))

            assert summary["status"] == "error"
            assert summary["error_code"] == "LEGACY_ADOPTION_REQUIRES_CLEANUP"
            assert summary["documents_eligible"] == 0
            assert summary["unsafe_documents_skipped"] == unsafe_count
            with session_factory() as db:
                assert db.query(Document).count() == 0
                assert (
                    db.query(DocumentCatalogState).one().legacy_adoption_completed_at
                    is None
                )
        finally:
            engine.dispose()


def test_parent_graph_mismatch_fails_closed_without_claiming_the_filename():
    engine, session_factory = _database()
    try:
        _add_owner(session_factory)
        _add_parent(
            session_factory,
            chunk_id="foreign-parent",
            filename="other.pdf",
        )
        store = FakeMilvusStore(
            [
                _leaf(
                    "guide.pdf",
                    "leaf",
                    0,
                    parent_chunk_id="foreign-parent",
                )
            ]
        )

        summary = _run(_adopter(session_factory, store))

        assert summary["status"] == "error"
        assert summary["error_code"] == "LEGACY_ADOPTION_REQUIRES_CLEANUP"
        assert summary["documents_eligible"] == 0
        assert summary["unsafe_documents_skipped"] == 1
        with session_factory() as db:
            assert db.query(DocumentVersion).count() == 0
    finally:
        engine.dispose()


def test_stale_parent_content_hash_cannot_hide_parent_text_drift():
    engine, session_factory = _database()
    try:
        _add_owner(session_factory)
        _add_parent(
            session_factory,
            chunk_id="guide-parent",
            filename="guide.pdf",
            text="actual parent text",
        )
        with session_factory.begin() as db:
            db.get(ParentChunk, "guide-parent").content_hash = "a" * 64
        summary = _run(
            _adopter(
                session_factory,
                FakeMilvusStore(
                    [
                        _leaf(
                            "guide.pdf",
                            "leaf",
                            0,
                            parent_chunk_id="guide-parent",
                        )
                    ]
                ),
            )
        )

        assert summary["status"] == "error"
        assert summary["error_code"] == "LEGACY_ADOPTION_REQUIRES_CLEANUP"
        assert summary["documents_eligible"] == 0
    finally:
        engine.dispose()


def test_parent_content_drift_reopens_gate_and_cannot_report_already_adopted():
    engine, session_factory = _database()
    try:
        _add_owner(session_factory)
        _add_parent(
            session_factory,
            chunk_id="guide-parent",
            filename="guide.pdf",
            text="parent v1",
        )
        store = FakeMilvusStore(
            [
                _leaf(
                    "guide.pdf",
                    "leaf",
                    0,
                    parent_chunk_id="guide-parent",
                )
            ]
        )
        adopter = _adopter(session_factory, store)
        first = _run(adopter)
        with session_factory.begin() as db:
            parent = db.get(ParentChunk, "guide-parent")
            parent.text = "parent v2"
            parent.content_hash = ""

        second = _run(adopter)

        assert first["adoption_complete"] is True
        assert second["status"] == "error"
        assert second["error_code"] == "LEGACY_ADOPTION_INCOMPLETE"
        assert second["results"][0]["reason"] == "legacy_content_drift"
        assert (
            DocumentCatalog(session_factory)
            .legacy_adoption_state(tenant_id="tenant-a")
            .complete
            is False
        )
    finally:
        engine.dispose()


def test_empty_collection_marks_the_knowledge_base_adoption_complete():
    engine, session_factory = _database()
    try:
        _add_owner(session_factory)
        summary = _run(_adopter(session_factory, FakeMilvusStore([])))

        assert summary["status"] == "no_changes"
        assert summary["leaf_chunks_scanned"] == 0
        assert summary["documents_eligible"] == 0
        assert summary["adoption_complete"] is True
        assert "error_code" not in summary
        with session_factory() as db:
            state = db.query(DocumentCatalogState).one()
            assert state.legacy_adoption_completed_at is not None
            assert len(state.legacy_corpus_fingerprint) == 64
            assert db.query(Document).count() == 0
    finally:
        engine.dispose()


def test_missing_legacy_collection_is_a_safe_empty_corpus():
    engine, session_factory = _database()
    try:
        _add_owner(session_factory)
        store = FakeMilvusStore([], exists=False)

        summary = _run(_adopter(session_factory, store))

        assert summary["status"] == "no_changes"
        assert summary["adoption_complete"] is True
        assert store.calls == []
        with session_factory() as db:
            state = db.query(DocumentCatalogState).one()
            assert state.legacy_adoption_completed_at is not None
    finally:
        engine.dispose()


def test_missing_owner_returns_redacted_error_and_reopens_tenant_gate():
    engine, session_factory = _database()
    try:
        store = FakeMilvusStore([_leaf("guide.pdf", "private body", 0)])
        adopter = _adopter(session_factory, store)

        summary = _run(adopter)

        assert summary["status"] == "error"
        assert summary["error_code"] == "OWNER_NOT_FOUND"
        assert summary["documents_adopted"] == 0
        serialized = json.dumps(summary, ensure_ascii=False)
        assert "alice" not in serialized
        assert "guide.pdf" not in serialized
        assert "private body" not in serialized
        with session_factory() as db:
            assert db.query(KnowledgeBase).count() == 0
            assert db.query(Document).count() == 0
            state = db.query(DocumentCatalogState).one()
            assert state.legacy_adoption_completed_at is None
    finally:
        engine.dispose()


def test_nonempty_corpus_requires_an_explicit_existing_target_kb():
    engine, session_factory = _database()
    try:
        with session_factory.begin() as db:
            db.add(User(id=1, username="alice", password_hash="hash", role="admin"))
        summary = _run(
            _adopter(
                session_factory,
                FakeMilvusStore([_leaf("guide.pdf", "private body", 0)]),
            )
        )

        assert summary["status"] == "error"
        assert summary["error_code"] == "KNOWLEDGE_BASE_NOT_FOUND"
        assert summary["adoption_complete"] is False
        with session_factory() as db:
            assert db.query(KnowledgeBase).count() == 0
            assert db.query(DocumentVersion).count() == 0
            state = db.query(DocumentCatalogState).one()
            assert state.legacy_knowledge_base_name == "main-kb"
            assert state.legacy_adoption_completed_at is None
    finally:
        engine.dispose()


def test_zero_kb_upgrade_dry_run_reports_required_creation_without_writing():
    engine, session_factory = _database()
    try:
        with session_factory.begin() as db:
            db.add(User(id=1, username="alice", password_hash="hash", role="admin"))
        summary = _run(
            _adopter(
                session_factory,
                FakeMilvusStore([_leaf("guide.pdf", "legacy body", 0)]),
            ),
            dry_run=True,
        )

        assert summary["status"] == "ok"
        assert summary["adoption_ready"] is True
        assert summary["target_knowledge_base_exists"] is False
        assert summary["target_knowledge_base_creation_required"] is True
        with session_factory() as db:
            assert db.query(KnowledgeBase).count() == 0
            assert db.query(DocumentCatalogState).count() == 0
            assert db.query(DocumentVersion).count() == 0
    finally:
        engine.dispose()


def test_explicit_target_bootstrap_supports_the_zero_kb_upgrade_shape():
    engine, session_factory = _database()
    try:
        with session_factory.begin() as db:
            db.add(User(id=1, username="alice", password_hash="hash", role="admin"))
        summary = _run(
            _adopter(
                session_factory,
                FakeMilvusStore([_leaf("guide.pdf", "legacy body", 0)]),
            ),
            create_target_knowledge_base=True,
        )

        assert summary["status"] == "ok"
        assert summary["target_knowledge_base_created"] is True
        assert summary["adoption_complete"] is True
        with session_factory() as db:
            kb = db.query(KnowledgeBase).one()
            state = db.query(DocumentCatalogState).one()
            assert kb.name == "main-kb"
            assert state.legacy_knowledge_base_id == kb.id
            assert db.query(DocumentVersion).one().status == "ready"
    finally:
        engine.dispose()


def test_cli_redacts_raw_provider_exception(capsys):
    engine, session_factory = _database()
    try:
        adopter = _adopter(session_factory, FailingMilvusStore([]))

        exit_code = main(["--owner-username", "alice"], adopter=adopter)

        captured = capsys.readouterr()
        assert exit_code == 2
        assert captured.out == ""
        payload = json.loads(captured.err)
        assert payload["error_code"] == "VECTOR_STORE_UNAVAILABLE"
        assert "private-milvus" not in captured.err
        assert "super-secret" not in captured.err
        assert "raw document body" not in captured.err
    finally:
        engine.dispose()
