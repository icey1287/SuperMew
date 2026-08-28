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


def _child(index: int, *, version_id: str = "version-a") -> dict:
    return {
        "chunk_id": f"leaf-{index}",
        "parent_chunk_id": "parent-1",
        "filename": "guide.pdf",
        "text": f"leaf {index}",
        "tenant_id": "tenant-a",
        "knowledge_base_id": "kb-main",
        "document_id": "doc-main",
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
        "tenant_id": "tenant-a",
        "knowledge_base_id": "kb-main",
        "document_id": "doc-main",
        "document_version_id": "version-a",
        "index_version": "v1",
    }
    value.update(overrides)
    return value


def _merge(monkeypatch, children: list[dict], parent: dict):
    monkeypatch.setattr(rag_utils, "_provider_executor", _Executor())
    monkeypatch.setattr(rag_utils, "_parent_chunk_store", _ParentStore(parent))
    return rag_utils._merge_to_parent_level(children, threshold=2)


def test_parent_must_match_every_document_version_identity_field(monkeypatch):
    children = [_child(0), _child(1)]
    valid = _parent()

    for field, value in (
        ("tenant_id", "tenant-b"),
        ("knowledge_base_id", "kb-other"),
        ("document_id", "doc-other"),
        ("document_version_id", "version-b"),
        ("index_version", "v2"),
        ("filename", "other.pdf"),
        ("text", ""),
        ("tenant_id", ""),
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
