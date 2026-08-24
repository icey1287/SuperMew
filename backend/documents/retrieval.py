from __future__ import annotations

import re
from dataclasses import dataclass

from backend.documents.catalog import (
    DocumentCatalog,
    DocumentRecord,
    DocumentVersionStatus,
)
from backend.security.milvus_filters import (
    and_filter,
    eq_filter,
    version_identity_filter,
)


_COLLECTION_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,159}$")


@dataclass(frozen=True, slots=True)
class RetrievalTarget:
    """一次查询可读取的单个 Milvus collection 与不可变 filter。"""

    collection_name: str
    filter_expr: str
    document_version_ids: tuple[str, ...] = ()
    required: bool = True


@dataclass(frozen=True, slots=True)
class RetrievalSnapshot:
    """由 PostgreSQL current pointer 投影出的查询快照。"""

    tenant_id: str
    index_id: str
    targets: tuple[RetrievalTarget, ...]
    current_document_count: int
    catalog_document_count: int


def _collection_name(value: str) -> str:
    normalized = str(value or "").strip()
    if not _COLLECTION_RE.fullmatch(normalized):
        raise ValueError("invalid Milvus collection name in document catalog")
    return normalized


class DocumentRetrievalScope:
    """把 Catalog current pointer 转换为 RAG 可消费的深只读 Interface。"""

    def __init__(
        self,
        catalog: DocumentCatalog | None = None,
    ) -> None:
        self._catalog = catalog or DocumentCatalog()

    @staticmethod
    def _ready_current(document: DocumentRecord):
        version = document.current_version
        if (
            document.deleted_at is not None
            or version is None
            or version.status != DocumentVersionStatus.READY
        ):
            return None
        return version

    def resolve(
        self,
        *,
        tenant_id: str,
        knowledge_base_id: str | None = None,
        leaf_chunk_level: int = 3,
    ) -> RetrievalSnapshot:
        tenant = str(tenant_id or "").strip()
        if not tenant:
            raise ValueError("tenant_id must not be empty")
        if leaf_chunk_level < 0:
            raise ValueError("leaf_chunk_level must not be negative")

        catalog_snapshot = self._catalog.load_retrieval_snapshot(
            tenant_id=tenant,
            knowledge_base_id=knowledge_base_id,
        )
        documents = list(catalog_snapshot.documents)
        index_id = catalog_snapshot.index_id

        versioned: dict[str, list[tuple[DocumentRecord, str]]] = {}
        current_document_count = 0

        for document in documents:
            if document.deleted_at is not None:
                continue
            version = self._ready_current(document)
            if version is None:
                continue
            current_document_count += 1
            collection = _collection_name(version.vector_collection)
            versioned.setdefault(collection, []).append((document, version.id))

        targets: list[RetrievalTarget] = []
        for collection, entries in sorted(versioned.items()):
            version_ids = tuple(
                sorted({version_id for _document, version_id in entries})
            )
            targets.append(
                RetrievalTarget(
                    collection_name=collection,
                    filter_expr=and_filter(
                        eq_filter("tenant_id", tenant),
                        version_identity_filter(version_ids),
                        eq_filter("chunk_level", leaf_chunk_level),
                    ),
                    document_version_ids=version_ids,
                    required=True,
                )
            )

        return RetrievalSnapshot(
            tenant_id=tenant,
            index_id=index_id,
            targets=tuple(targets),
            current_document_count=current_document_count,
            catalog_document_count=len(documents),
        )


__all__ = [
    "DocumentRetrievalScope",
    "RetrievalSnapshot",
    "RetrievalTarget",
]
