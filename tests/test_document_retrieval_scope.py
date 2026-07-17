from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pytest

from backend.documents.catalog import DocumentVersionStatus
from backend.documents.retrieval import DocumentRetrievalScope


def _version(
    version_id: str,
    *,
    collection: str = "embeddings_collection_catalog_v1",
    status: str = DocumentVersionStatus.READY,
):
    return SimpleNamespace(
        id=version_id,
        status=status,
        vector_collection=collection,
    )


def _document(
    name: str,
    *,
    current=None,
    pending=None,
    deleted: bool = False,
):
    return SimpleNamespace(
        canonical_name=name,
        current_version=current,
        pending_version=pending,
        deleted_at=datetime(2026, 7, 15) if deleted else None,
    )


class FakeCatalog:
    def __init__(self, documents, *, index_id: str = "index-a") -> None:
        self.documents = list(documents)
        self.index_id = index_id
        self.snapshot_calls = 0

    def load_retrieval_snapshot(self, **_kwargs):
        self.snapshot_calls += 1
        return SimpleNamespace(
            documents=tuple(self.documents),
            index_id=self.index_id,
        )


def test_candidate_is_invisible_and_snapshot_keeps_old_current():
    v1 = _version("version-v1")
    v2 = _version("version-v2", status=DocumentVersionStatus.INDEXING)
    document = _document("guide.pdf", current=v1, pending=v2)
    catalog = FakeCatalog([document], index_id="index-v1")
    scope = DocumentRetrievalScope(catalog)

    before = scope.resolve()
    before_target = before.targets[0]
    assert before_target.document_version_ids == ("version-v1",)
    assert "version-v2" not in before_target.filter_expr

    document.current_version = _version("version-v2")
    document.pending_version = None
    catalog.index_id = "index-v2"
    after = scope.resolve()

    assert before.index_id == "index-v1"
    assert before_target.document_version_ids == ("version-v1",)
    assert after.targets[0].document_version_ids == ("version-v2",)
    assert after.index_id == "index-v2"


def test_targets_are_grouped_by_collection_and_exact_version_identity():
    catalog = FakeCatalog(
        [
            _document("one.pdf", current=_version("version-1")),
            _document("two.pdf", current=_version("version-2")),
            _document(
                "archive.pdf",
                current=_version("version-3", collection="archive_catalog_v1"),
            ),
            _document("deleted.pdf", current=_version("version-4"), deleted=True),
            _document(
                "building.pdf",
                pending=_version(
                    "version-5",
                    status=DocumentVersionStatus.INDEXING,
                ),
            ),
        ]
    )

    snapshot = DocumentRetrievalScope(catalog).resolve(
        tenant_id="tenant-a",
        leaf_chunk_level=3,
    )

    assert [target.collection_name for target in snapshot.targets] == [
        "archive_catalog_v1",
        "embeddings_collection_catalog_v1",
    ]
    archive, current = snapshot.targets
    assert archive.document_version_ids == ("version-3",)
    assert current.document_version_ids == ("version-1", "version-2")
    assert 'tenant_id == "tenant-a"' in current.filter_expr
    assert "chunk_level == 3" in current.filter_expr
    assert "version-4" not in current.filter_expr
    assert "version-5" not in current.filter_expr


def test_catalog_scope_and_index_identity_come_from_one_atomic_snapshot():
    documents = [
        _document(f"doc-{index}.pdf", current=_version(f"version-{index}"))
        for index in range(3)
    ]
    catalog = FakeCatalog(documents)

    snapshot = DocumentRetrievalScope(catalog).resolve()

    assert catalog.snapshot_calls == 1
    assert snapshot.catalog_document_count == 3
    assert snapshot.current_document_count == 3


def test_catalog_failure_is_not_converted_to_empty_scope():
    class BrokenCatalog(FakeCatalog):
        def load_retrieval_snapshot(self, **_kwargs):
            raise RuntimeError("postgres unavailable")

    with pytest.raises(RuntimeError, match="postgres unavailable"):
        DocumentRetrievalScope(BrokenCatalog([])).resolve()


def test_invalid_collection_name_fails_closed():
    catalog = FakeCatalog(
        [_document("guide.pdf", current=_version("version-1", collection="bad-name"))]
    )

    with pytest.raises(ValueError, match="invalid Milvus collection"):
        DocumentRetrievalScope(catalog).resolve()
