"""文档向量化并写入 Milvus - 支持 legacy 与版本化候选索引。"""

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING

from backend.indexing.embedding import (
    EmbeddingService,
    embedding_service as _default_embedding_service,
)
from backend.indexing.milvus_client import MilvusStore, get_milvus_store

if TYPE_CHECKING:
    from backend.indexing.milvus_client import IndexVersionVerification


CATALOG_COLLECTION_SUFFIX = "_catalog_v1"
_VERSION_SCOPE_FIELDS = (
    "tenant_id",
    "knowledge_base_id",
    "document_id",
    "document_version_id",
    "index_version",
)
_VERSION_METADATA_FIELDS = (
    *_VERSION_SCOPE_FIELDS,
    "section_id",
    "acl_tags",
    "content_hash",
)
_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")


@dataclass(frozen=True)
class IndexVersionScope:
    """即使写入失败也足以精确定位候选版本的清理 scope。"""

    collection_name: str
    tenant_id: str
    knowledge_base_id: str
    document_id: str
    document_version_id: str
    index_version: str

    def __post_init__(self) -> None:
        for field in (
            "collection_name",
            "tenant_id",
            "knowledge_base_id",
            "document_id",
            "document_version_id",
            "index_version",
        ):
            value = getattr(self, field)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field} must be a non-empty string")
            object.__setattr__(self, field, value.strip())


@dataclass(frozen=True)
class IndexWriteReceipt(IndexVersionScope):
    """一次候选索引写入的可核验回执。"""

    chunk_ids: tuple[str, ...]
    attempted_count: int
    inserted_count: int
    batch_count: int

    def __post_init__(self) -> None:
        super().__post_init__()
        if not self.chunk_ids or any(not item for item in self.chunk_ids):
            raise ValueError("chunk_ids must contain non-empty IDs")
        if len(set(self.chunk_ids)) != len(self.chunk_ids):
            raise ValueError("chunk_ids must be unique")
        if self.attempted_count != len(self.chunk_ids):
            raise ValueError("attempted_count must equal the receipt chunk count")
        if self.inserted_count != self.attempted_count:
            raise ValueError("inserted_count must equal attempted_count")
        if self.batch_count <= 0:
            raise ValueError("batch_count must be positive")


def _required_string(document: Mapping, field: str) -> str:
    value = document.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"versioned document requires non-empty {field}")
    return value.strip()


def _normalize_acl_tags(value) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        raise ValueError("acl_tags must be a collection of strings")
    normalized: list[str] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, str):
            raise ValueError("acl_tags must contain strings")
        tag = item.strip()
        if not tag or tag in seen:
            continue
        seen.add(tag)
        normalized.append(tag)
    return normalized


def _insert_count(response, expected: int) -> int:
    """校验 Milvus 明确返回的 insert count；无 count 时按成功批次计数。"""

    count = None
    if isinstance(response, Mapping):
        for field in ("insert_count", "upsert_count"):
            if field in response:
                count = response[field]
                break
    else:
        for field in ("insert_count", "upsert_count"):
            if hasattr(response, field):
                count = getattr(response, field)
                break
    if count is None:
        return expected
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        raise RuntimeError("Milvus returned an invalid insert count")
    if count != expected:
        raise RuntimeError(
            f"Milvus inserted {count} records, expected {expected} for the batch"
        )
    return count


class MilvusWriter:
    """文档向量化并写入 Milvus；legacy 与候选版本使用隔离 collection。"""

    def __init__(
        self,
        embedding_service: EmbeddingService | None = None,
        milvus_manager: MilvusStore | None = None,
        versioned_milvus_manager: MilvusStore | None = None,
    ):
        self.embedding_service = embedding_service or _default_embedding_service
        self.milvus_manager = milvus_manager or get_milvus_store()
        self.versioned_milvus_manager = versioned_milvus_manager

    def _store_for_collection(self, collection_name: str) -> MilvusStore:
        if self.versioned_milvus_manager is not None:
            if self.versioned_milvus_manager.collection_name == collection_name:
                return self.versioned_milvus_manager
        if self.milvus_manager.collection_name == collection_name:
            return self.milvus_manager
        return self.milvus_manager.with_collection(collection_name)

    def _versioned_store(self, collection_name: str | None) -> MilvusStore:
        if collection_name is not None:
            normalized = collection_name.strip()
            if not normalized:
                raise ValueError("collection_name must not be empty")
            return self._store_for_collection(normalized)
        if self.versioned_milvus_manager is not None:
            return self.versioned_milvus_manager
        return self.milvus_manager.with_collection(
            f"{self.milvus_manager.collection_name}{CATALOG_COLLECTION_SUFFIX}"
        )

    def build_version_scope(
        self,
        *,
        tenant_id: str,
        knowledge_base_id: str,
        document_id: str,
        document_version_id: str,
        index_version: str,
        collection_name: str | None = None,
    ) -> IndexVersionScope:
        """在写入前创建可供失败补偿使用的稳定 scope。"""

        store = self._versioned_store(collection_name)
        return IndexVersionScope(
            collection_name=store.collection_name,
            tenant_id=tenant_id,
            knowledge_base_id=knowledge_base_id,
            document_id=document_id,
            document_version_id=document_version_id,
            index_version=index_version,
        )

    def write_documents(
        self,
        documents: list[dict],
        batch_size: int = 50,
        progress_callback=None,
    ):
        """写入 legacy collection；保持旧调用与旧 metadata 形状不变。"""

        if not documents:
            return
        if batch_size <= 0:
            raise ValueError("batch_size must be positive")

        dense_dim = int(os.getenv("DENSE_EMBEDDING_DIM", "1024"))

        total = len(documents)
        self.milvus_manager.init_collection(dense_dim)

        for i in range(0, total, batch_size):
            batch = documents[i : i + batch_size]
            texts = [doc["text"] for doc in batch]
            dense_embeddings = self.embedding_service.get_embeddings(texts)
            if len(dense_embeddings) != len(batch):
                raise RuntimeError(
                    "embedding provider returned an unexpected vector count"
                )

            insert_data = [
                {
                    "dense_embedding": dense_emb,
                    "text": doc["text"],
                    "filename": doc["filename"],
                    "file_type": doc["file_type"],
                    "file_path": doc.get("file_path", ""),
                    "page_number": doc.get("page_number", 0),
                    "chunk_idx": doc.get("chunk_idx", 0),
                    "chunk_id": doc.get("chunk_id", ""),
                    "parent_chunk_id": doc.get("parent_chunk_id", ""),
                    "root_chunk_id": doc.get("root_chunk_id", ""),
                    "chunk_level": doc.get("chunk_level", 0),
                }
                for doc, dense_emb in zip(batch, dense_embeddings)
            ]

            self.milvus_manager.insert(insert_data)

            if progress_callback:
                processed = min(i + batch_size, total)
                progress_callback(processed, total)

    def write_versioned_documents(
        self,
        documents: list[dict],
        *,
        collection_name: str | None = None,
        batch_size: int = 50,
        progress_callback=None,
    ) -> IndexWriteReceipt:
        """写入隔离的候选 collection，并返回可做 exact verify 的回执。"""

        if not documents:
            raise ValueError("versioned documents must not be empty")
        if batch_size <= 0:
            raise ValueError("batch_size must be positive")

        normalized_documents: list[dict] = []
        expected_scope: tuple[str, ...] | None = None
        chunk_ids: list[str] = []
        seen_chunk_ids: set[str] = set()
        for document in documents:
            if not isinstance(document, Mapping):
                raise ValueError("versioned documents must be mappings")
            normalized = dict(document)
            normalized["text"] = _required_string(document, "text")
            normalized["filename"] = _required_string(document, "filename")
            normalized["file_type"] = _required_string(document, "file_type")
            for field in _VERSION_METADATA_FIELDS:
                if field == "acl_tags":
                    normalized[field] = _normalize_acl_tags(document.get(field))
                else:
                    normalized[field] = _required_string(document, field)
            if not _SHA256_RE.fullmatch(normalized["content_hash"]):
                raise ValueError("content_hash must be a SHA-256 hex digest")

            chunk_id = _required_string(document, "chunk_id")
            if chunk_id in seen_chunk_ids:
                raise ValueError(f"duplicate versioned chunk_id: {chunk_id}")
            seen_chunk_ids.add(chunk_id)
            normalized["chunk_id"] = chunk_id
            chunk_ids.append(chunk_id)

            scope = tuple(normalized[field] for field in _VERSION_SCOPE_FIELDS)
            if expected_scope is None:
                expected_scope = scope
            elif scope != expected_scope:
                raise ValueError("all versioned documents must share one version scope")
            normalized_documents.append(normalized)

        assert expected_scope is not None
        store = self._versioned_store(collection_name)
        dense_dim = int(os.getenv("DENSE_EMBEDDING_DIM", "1024"))
        store.init_versioned_collection(dense_dim)

        tenant_id, knowledge_base_id, document_id, version_id, index_version = (
            expected_scope
        )
        scope = IndexVersionScope(
            collection_name=store.collection_name,
            tenant_id=tenant_id,
            knowledge_base_id=knowledge_base_id,
            document_id=document_id,
            document_version_id=version_id,
            index_version=index_version,
        )
        store.delete_by_version(
            tenant_id=scope.tenant_id,
            knowledge_base_id=scope.knowledge_base_id,
            document_id=scope.document_id,
            document_version_id=scope.document_version_id,
            index_version=scope.index_version,
        )

        inserted_count = 0
        batch_count = 0
        total = len(normalized_documents)
        for index in range(0, total, batch_size):
            batch = normalized_documents[index : index + batch_size]
            texts = [document["text"] for document in batch]
            dense_embeddings = self.embedding_service.get_embeddings(texts)
            if len(dense_embeddings) != len(batch):
                raise RuntimeError(
                    "embedding provider returned an unexpected vector count"
                )
            insert_data = [
                {
                    "dense_embedding": dense_embedding,
                    "text": document["text"],
                    "filename": document["filename"],
                    "file_type": document["file_type"],
                    "file_path": document.get("file_path", ""),
                    "page_number": int(document.get("page_number", 0) or 0),
                    "chunk_idx": int(document.get("chunk_idx", 0) or 0),
                    "chunk_id": document["chunk_id"],
                    "parent_chunk_id": document.get("parent_chunk_id", ""),
                    "root_chunk_id": document.get("root_chunk_id", ""),
                    "chunk_level": int(document.get("chunk_level", 0) or 0),
                    **{field: document[field] for field in _VERSION_METADATA_FIELDS},
                }
                for document, dense_embedding in zip(batch, dense_embeddings)
            ]
            response = store.insert(insert_data)
            inserted_count += _insert_count(response, len(batch))
            batch_count += 1
            if progress_callback:
                progress_callback(min(index + batch_size, total), total)

        return IndexWriteReceipt(
            collection_name=store.collection_name,
            tenant_id=tenant_id,
            knowledge_base_id=knowledge_base_id,
            document_id=document_id,
            document_version_id=version_id,
            index_version=index_version,
            chunk_ids=tuple(chunk_ids),
            attempted_count=total,
            inserted_count=inserted_count,
            batch_count=batch_count,
        )

    def verify_receipt(self, receipt: IndexWriteReceipt) -> "IndexVersionVerification":
        store = self._store_for_collection(receipt.collection_name)
        return store.verify_version(
            tenant_id=receipt.tenant_id,
            knowledge_base_id=receipt.knowledge_base_id,
            document_id=receipt.document_id,
            document_version_id=receipt.document_version_id,
            index_version=receipt.index_version,
            expected_chunk_ids=receipt.chunk_ids,
        )

    def delete_by_version(self, scope: IndexVersionScope) -> int:
        store = self._store_for_collection(scope.collection_name)
        return store.delete_by_version(
            tenant_id=scope.tenant_id,
            knowledge_base_id=scope.knowledge_base_id,
            document_id=scope.document_id,
            document_version_id=scope.document_version_id,
            index_version=scope.index_version,
        )
