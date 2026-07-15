from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pytest

from backend.core.errors import AppError, ErrorCode
from backend.documents.catalog import DocumentVersionStatus, StorageLayout
from backend.documents.retrieval import DocumentRetrievalScope


def _version(
    version_id: str,
    *,
    layout: str = StorageLayout.VERSIONED,
    collection: str = "embeddings_collection_catalog_v1",
    status: str = DocumentVersionStatus.READY,
):
    return SimpleNamespace(
        id=version_id,
        status=status,
        storage_layout=layout,
        vector_collection=collection,
    )


def _document(
    name: str,
    *,
    current=None,
    pending=None,
    deleted: bool = False,
    knowledge_base_id: str = "kb-default",
):
    return SimpleNamespace(
        canonical_name=name,
        knowledge_base_id=knowledge_base_id,
        current_version=current,
        pending_version=pending,
        deleted_at=datetime(2026, 7, 15) if deleted else None,
    )


class FakeCatalog:
    def __init__(
        self,
        documents,
        *,
        index_id: str = "index-a",
        suppressed_legacy_names=(),
        legacy_adoption_complete: bool = True,
        legacy_collection: str = "embeddings_collection",
        legacy_knowledge_base_id: str | None = "kb-default",
    ) -> None:
        self.documents = list(documents)
        self.index_id = index_id
        self.suppressed_legacy_names = tuple(suppressed_legacy_names)
        self.legacy_adoption_complete = legacy_adoption_complete
        self.legacy_collection = legacy_collection
        self.legacy_knowledge_base_id = legacy_knowledge_base_id
        self.snapshot_calls = 0

    def load_retrieval_snapshot(self, **_kwargs):
        self.snapshot_calls += 1
        return SimpleNamespace(
            documents=tuple(self.documents),
            index_id=self.index_id,
            suppressed_legacy_names=self.suppressed_legacy_names,
            legacy_adoption_complete=self.legacy_adoption_complete,
            legacy_collection=self.legacy_collection,
            legacy_knowledge_base_id=self.legacy_knowledge_base_id,
            legacy_knowledge_base_name="默认知识库",
        )


def _versioned_target(snapshot):
    return next(
        target
        for target in snapshot.targets
        if target.storage_layout == StorageLayout.VERSIONED
    )


def _legacy_target(snapshot, collection="embeddings_collection"):
    return next(
        target
        for target in snapshot.targets
        if target.storage_layout == StorageLayout.LEGACY_FILENAME
        and target.collection_name == collection
    )


def test_candidate_is_invisible_and_snapshot_keeps_old_current():
    v1 = _version("version-v1")
    v2 = _version("version-v2", status=DocumentVersionStatus.INDEXING)
    document = _document("guide.pdf", current=v1, pending=v2)
    catalog = FakeCatalog([document], index_id="index-v1")
    scope = DocumentRetrievalScope(catalog, legacy_collection="embeddings_collection")

    before = scope.resolve()
    before_target = _versioned_target(before)
    assert before_target.document_version_ids == ("version-v1",)
    assert "version-v2" not in before_target.filter_expr

    document.current_version = _version("version-v2")
    document.pending_version = None
    catalog.index_id = "index-v2"
    after = scope.resolve()

    assert before.index_id == "index-v1"
    assert before_target.document_version_ids == ("version-v1",)
    assert _versioned_target(after).document_version_ids == ("version-v2",)
    assert after.index_id == "index-v2"


def test_legacy_dual_read_is_exactly_allowlisted_to_adopted_names():
    catalog = FakeCatalog(
        [
            _document("published.pdf", current=_version("version-published")),
            _document("deleted.pdf", deleted=True),
            _document(
                "adopted.pdf",
                current=_version(
                    "version-legacy",
                    layout=StorageLayout.LEGACY_FILENAME,
                    collection="embeddings_collection",
                ),
            ),
            # A first candidate must not hide matching legacy vectors before publish.
            _document(
                "still-building.pdf",
                pending=_version(
                    "version-pending", status=DocumentVersionStatus.INDEXING
                ),
            ),
        ],
        suppressed_legacy_names=("published.pdf", "deleted.pdf", "adopted.pdf"),
    )

    snapshot = DocumentRetrievalScope(
        catalog,
        legacy_collection="embeddings_collection",
    ).resolve()
    target = _legacy_target(snapshot)

    assert 'filename in ["adopted.pdf"]' in target.filter_expr
    assert '"published.pdf"' not in target.filter_expr
    assert '"deleted.pdf"' not in target.filter_expr
    assert '"still-building.pdf"' not in target.filter_expr
    assert target.required is True
    assert snapshot.suppressed_legacy_names == (
        "adopted.pdf",
        "deleted.pdf",
        "published.pdf",
    )


def test_unadopted_legacy_rows_are_not_exposed_by_a_broad_base_target():
    catalog = FakeCatalog(
        [_document("still-building.pdf", pending=_version("version-pending"))],
        suppressed_legacy_names=("deleted.pdf",),
    )

    snapshot = DocumentRetrievalScope(
        catalog,
        legacy_collection="embeddings_collection",
    ).resolve()

    assert snapshot.targets == ()


def test_non_base_legacy_collection_is_scoped_to_adopted_names():
    catalog = FakeCatalog(
        [
            _document(
                "archive.pdf",
                current=_version(
                    "legacy-archive",
                    layout=StorageLayout.LEGACY_FILENAME,
                    collection="legacy_archive",
                ),
            )
        ]
    )

    snapshot = DocumentRetrievalScope(
        catalog,
        legacy_collection="embeddings_collection",
    ).resolve()
    target = _legacy_target(snapshot, "legacy_archive")

    assert target.required is True
    assert target.canonical_names == ("archive.pdf",)
    assert 'filename in ["archive.pdf"]' in target.filter_expr


def test_catalog_scope_and_index_identity_come_from_one_atomic_snapshot():
    documents = [
        _document(f"doc-{index}.pdf", current=_version(f"version-{index}"))
        for index in range(3)
    ]
    catalog = FakeCatalog(documents)

    snapshot = DocumentRetrievalScope(
        catalog,
        legacy_collection="embeddings_collection",
    ).resolve()

    assert catalog.snapshot_calls == 1
    assert snapshot.catalog_document_count == 3
    assert snapshot.current_document_count == 3


def test_catalog_failure_is_not_converted_to_empty_scope():
    class BrokenCatalog(FakeCatalog):
        def load_retrieval_snapshot(self, **_kwargs):
            raise RuntimeError("postgres unavailable")

    scope = DocumentRetrievalScope(
        BrokenCatalog([]),
        legacy_collection="embeddings_collection",
    )

    with pytest.raises(RuntimeError, match="postgres unavailable"):
        scope.resolve()


def test_incomplete_legacy_adoption_fails_closed_before_returning_targets():
    scope = DocumentRetrievalScope(
        FakeCatalog([], legacy_adoption_complete=False),
        legacy_collection="embeddings_collection",
    )

    with pytest.raises(AppError) as raised:
        scope.resolve()

    assert raised.value.code == ErrorCode.STORAGE_UNAVAILABLE


def test_legacy_target_fails_closed_when_catalog_state_points_to_another_kb():
    scope = DocumentRetrievalScope(
        FakeCatalog(
            [
                _document(
                    "legacy.pdf",
                    current=_version(
                        "legacy-version",
                        layout=StorageLayout.LEGACY_FILENAME,
                        collection="embeddings_collection",
                    ),
                    knowledge_base_id="kb-other",
                )
            ],
            legacy_knowledge_base_id="kb-default",
        ),
        legacy_collection="embeddings_collection",
    )

    with pytest.raises(AppError) as raised:
        scope.resolve()

    assert raised.value.code == ErrorCode.STORAGE_UNAVAILABLE
