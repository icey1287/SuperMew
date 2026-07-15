from __future__ import annotations

import backend.rag.utils as rag_utils


class _Executor:
    @staticmethod
    def call(operation, **_kwargs):
        return operation()


class _ParentStore:
    def __init__(self, parent: dict):
        self.parent = parent

    def get_documents_by_ids(self, _chunk_ids):
        return [dict(self.parent)]


def _child(index: int, *, version_id: str = "") -> dict:
    return {
        "chunk_id": f"leaf-{index}",
        "parent_chunk_id": "parent-1",
        "filename": "guide.pdf",
        "text": f"leaf {index}",
        "tenant_id": "tenant-a" if version_id else "default",
        "knowledge_base_id": "kb-main" if version_id else "",
        "document_id": "doc-main" if version_id else "",
        "document_version_id": version_id,
        "index_version": "v1",
        "score": 1.0 - index / 10,
    }


def _parent(**overrides) -> dict:
    value = {
        "chunk_id": "parent-1",
        "parent_chunk_id": "",
        "filename": "guide.pdf",
        "text": "parent body",
        "tenant_id": "default",
        "knowledge_base_id": "",
        "document_id": "",
        "document_version_id": "",
        "index_version": "v1",
    }
    value.update(overrides)
    return value


def _merge(monkeypatch, children: list[dict], parent: dict):
    monkeypatch.setattr(rag_utils, "_provider_executor", _Executor())
    monkeypatch.setattr(rag_utils, "_parent_chunk_store", _ParentStore(parent))
    return rag_utils._merge_to_parent_level(children, threshold=2)


def test_legacy_parent_must_match_filename_and_have_empty_version_scope(monkeypatch):
    children = [_child(0), _child(1)]

    for parent in (
        _parent(filename="other.pdf"),
        _parent(document_version_id="foreign-version"),
        _parent(index_version="v2"),
        _parent(text=""),
    ):
        merged, count = _merge(monkeypatch, children, parent)
        assert count == 0
        assert merged == children

    merged, count = _merge(monkeypatch, children, _parent())
    assert count == 2
    assert len(merged) == 1
    assert merged[0]["merged_from_children"] is True


def test_versioned_parent_must_match_every_catalog_identity_field(monkeypatch):
    children = [_child(0, version_id="version-a"), _child(1, version_id="version-a")]
    valid = _parent(
        tenant_id="tenant-a",
        knowledge_base_id="kb-main",
        document_id="doc-main",
        document_version_id="version-a",
    )

    for field, value in (
        ("tenant_id", "tenant-b"),
        ("knowledge_base_id", "kb-other"),
        ("document_id", "doc-other"),
        ("document_version_id", "version-b"),
        ("index_version", "v2"),
    ):
        merged, count = _merge(
            monkeypatch,
            children,
            {**valid, field: value},
        )
        assert count == 0
        assert merged == children

    merged, count = _merge(monkeypatch, children, valid)
    assert count == 2
    assert len(merged) == 1
