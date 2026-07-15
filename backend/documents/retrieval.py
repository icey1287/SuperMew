from __future__ import annotations

import re
from dataclasses import dataclass

from backend.core.errors import AppError, ErrorCode
from backend.documents.catalog import (
    DocumentCatalog,
    DocumentRecord,
    DocumentVersionStatus,
    StorageLayout,
)
from backend.indexing.milvus_client import MilvusSettings
from backend.security.milvus_filters import (
    and_filter,
    eq_filter,
    in_filter,
    version_identity_filter,
)


_COLLECTION_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,159}$")


@dataclass(frozen=True, slots=True)
class RetrievalTarget:
    """一次查询可读取的单个 Milvus collection 与不可变 filter。"""

    collection_name: str
    filter_expr: str
    storage_layout: str
    document_version_ids: tuple[str, ...] = ()
    canonical_names: tuple[str, ...] = ()
    required: bool = True


@dataclass(frozen=True, slots=True)
class RetrievalSnapshot:
    """由 PostgreSQL current pointer 投影出的查询快照。"""

    tenant_id: str
    index_id: str
    targets: tuple[RetrievalTarget, ...]
    current_document_count: int
    catalog_document_count: int
    suppressed_legacy_names: tuple[str, ...]


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
        *,
        legacy_collection: str | None = None,
    ) -> None:
        self._catalog = catalog or DocumentCatalog()
        self._legacy_collection = _collection_name(
            legacy_collection or MilvusSettings.from_env().collection_name
        )

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
        tenant_id: str = "default",
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
        if not catalog_snapshot.legacy_adoption_complete:
            raise AppError(
                ErrorCode.STORAGE_UNAVAILABLE,
                "legacy 文档目录尚未完成安全接管",
                status_code=503,
                retryable=True,
                stage="document_catalog",
            )
        if catalog_snapshot.legacy_collection != self._legacy_collection:
            raise AppError(
                ErrorCode.STORAGE_UNAVAILABLE,
                "legacy 文档目录与当前向量集合配置不一致",
                status_code=503,
                retryable=False,
                stage="document_catalog",
            )

        versioned: dict[str, list[tuple[DocumentRecord, str]]] = {}
        explicit_legacy: dict[str, set[str]] = {}
        suppressed_legacy_names = set(catalog_snapshot.suppressed_legacy_names)
        current_document_count = 0

        for document in documents:
            if document.deleted_at is not None:
                suppressed_legacy_names.add(document.canonical_name)
                continue
            version = self._ready_current(document)
            if version is None:
                # A pending first catalog build must not hide an unadopted legacy file.
                continue
            current_document_count += 1
            if version.storage_layout == StorageLayout.VERSIONED:
                collection = _collection_name(version.vector_collection)
                versioned.setdefault(collection, []).append((document, version.id))
                suppressed_legacy_names.add(document.canonical_name)
                continue
            if version.storage_layout == StorageLayout.LEGACY_FILENAME:
                if (
                    not catalog_snapshot.legacy_knowledge_base_id
                    or document.knowledge_base_id
                    != catalog_snapshot.legacy_knowledge_base_id
                ):
                    raise AppError(
                        ErrorCode.STORAGE_UNAVAILABLE,
                        "legacy 文档目录目标知识库不一致",
                        status_code=503,
                        retryable=False,
                        stage="document_catalog",
                    )
                collection = _collection_name(
                    version.vector_collection or self._legacy_collection
                )
                explicit_legacy.setdefault(collection, set()).add(
                    document.canonical_name
                )
                continue
            raise ValueError("unsupported document storage_layout")

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
                    storage_layout=StorageLayout.VERSIONED,
                    document_version_ids=version_ids,
                    canonical_names=tuple(
                        sorted({document.canonical_name for document, _ in entries})
                    ),
                    required=True,
                )
            )

        # Legacy visibility is an explicit Catalog allowlist. Unadopted rows are
        # fail-closed: otherwise malformed filenames skipped by the adoption scan
        # would still be queryable through a broad "all except tombstones" target.
        base_legacy_names = tuple(
            sorted(explicit_legacy.pop(self._legacy_collection, set()))
        )
        if base_legacy_names:
            targets.append(
                RetrievalTarget(
                    collection_name=self._legacy_collection,
                    filter_expr=and_filter(
                        eq_filter("chunk_level", leaf_chunk_level),
                        in_filter("filename", base_legacy_names),
                    ),
                    storage_layout=StorageLayout.LEGACY_FILENAME,
                    canonical_names=base_legacy_names,
                    required=True,
                )
            )
        for collection, names in sorted(explicit_legacy.items()):
            canonical_names = tuple(sorted(names))
            targets.append(
                RetrievalTarget(
                    collection_name=collection,
                    filter_expr=and_filter(
                        eq_filter("chunk_level", leaf_chunk_level),
                        in_filter("filename", canonical_names),
                    ),
                    storage_layout=StorageLayout.LEGACY_FILENAME,
                    canonical_names=canonical_names,
                    required=True,
                )
            )

        return RetrievalSnapshot(
            tenant_id=tenant,
            index_id=index_id,
            targets=tuple(targets),
            current_document_count=current_document_count,
            catalog_document_count=len(documents),
            suppressed_legacy_names=tuple(sorted(suppressed_legacy_names)),
        )


__all__ = [
    "DocumentRetrievalScope",
    "RetrievalSnapshot",
    "RetrievalTarget",
]
