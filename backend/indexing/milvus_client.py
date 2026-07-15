"""Milvus 访问层：无状态 Store + 短生命周期 gRPC 连接（避免长期持有失效 channel）。"""

from __future__ import annotations

import math
import os
import re
from collections import Counter
from collections.abc import Iterable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, replace
from typing import Callable, Iterator, TypeVar

from pymilvus import (
    AnnSearchRequest,
    DataType,
    MilvusClient,
    RRFRanker,
    Function,
    FunctionType,
)
from pymilvus.exceptions import (
    DataTypeNotSupportException,
    ErrorCode as MilvusErrorCode,
    FunctionsTypeException,
    MilvusException,
    ServerVersionIncompatibleException,
)

from backend.security.milvus_filters import in_filter, version_scope_filter

QUERY_MAX_LIMIT = 16384
T = TypeVar("T")
_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")

CATALOG_METADATA_FIELDS = [
    "tenant_id",
    "knowledge_base_id",
    "document_id",
    "document_version_id",
    "section_id",
    "acl_tags",
    "index_version",
    "content_hash",
]
RETRIEVAL_OUTPUT_FIELDS = [
    "text",
    "filename",
    "file_type",
    "page_number",
    "chunk_id",
    "parent_chunk_id",
    "root_chunk_id",
    "chunk_level",
    "chunk_idx",
    *CATALOG_METADATA_FIELDS,
]
_CATALOG_SCHEMA_FIELDS = {
    "id",
    "dense_embedding",
    "sparse_embedding",
    "text",
    "filename",
    "file_type",
    "file_path",
    "page_number",
    "chunk_idx",
    "chunk_id",
    "parent_chunk_id",
    "root_chunk_id",
    "chunk_level",
    *CATALOG_METADATA_FIELDS,
}


class HybridRetrievalUnsupported(RuntimeError):
    """The connected Milvus deployment cannot execute sparse/hybrid recall."""


_HYBRID_CAPABILITY_EXCEPTIONS = (
    DataTypeNotSupportException,
    FunctionsTypeException,
    ServerVersionIncompatibleException,
)


def _is_hybrid_capability_error(exc: Exception) -> bool:
    if isinstance(exc, _HYBRID_CAPABILITY_EXCEPTIONS):
        return True
    return (
        isinstance(exc, MilvusException) and exc.code == MilvusErrorCode.INDEX_NOT_FOUND
    )


@dataclass(frozen=True)
class MilvusSettings:
    host: str
    port: str
    collection_name: str
    uri: str
    timeout: float

    @classmethod
    def from_env(cls) -> MilvusSettings:
        host = os.getenv("MILVUS_HOST", "localhost")
        port = os.getenv("MILVUS_PORT", "19530")
        collection = os.getenv("MILVUS_COLLECTION", "embeddings_collection")
        timeout = float(os.getenv("MILVUS_TIMEOUT", "30"))
        return cls(
            host=host,
            port=port,
            collection_name=collection,
            uri=f"http://{host}:{port}",
            timeout=timeout,
        )


@dataclass(frozen=True)
class IndexVersionVerification:
    """候选索引版本与预期 manifest 的精确核验结果。"""

    expected_ids: tuple[str, ...]
    actual_ids: tuple[str, ...]
    missing_ids: tuple[str, ...]
    unexpected_ids: tuple[str, ...]
    duplicate_ids: tuple[str, ...]
    expected_count: int
    actual_count: int
    exact: bool


@contextmanager
def milvus_client_session(
    settings: MilvusSettings | None = None,
) -> Iterator[MilvusClient]:
    """一次 RPC 会话：创建连接，用完后关闭，不缓存 gRPC channel。"""
    cfg = settings or MilvusSettings.from_env()
    client = MilvusClient(uri=cfg.uri, timeout=cfg.timeout)
    try:
        yield client
    finally:
        client.close()


def _normalize_filter(filter_expr: str) -> str:
    return filter_expr.strip() if filter_expr.strip() else "id >= 0"


def _single_query_hits(results) -> list[Mapping]:
    """Validate the MilvusClient one-query response before empty-result semantics."""

    if isinstance(results, (str, bytes, Mapping)) or results is None:
        raise ValueError("invalid Milvus search response")
    try:
        outer = list(results)
    except TypeError as exc:
        raise ValueError("invalid Milvus search response") from exc
    if len(outer) != 1:
        raise ValueError("Milvus search response must contain one query result")
    raw_hits = outer[0]
    if isinstance(raw_hits, (str, bytes, Mapping)) or raw_hits is None:
        raise ValueError("invalid Milvus hits response")
    try:
        hits = list(raw_hits)
    except TypeError as exc:
        raise ValueError("invalid Milvus hits response") from exc
    if any(not isinstance(hit, Mapping) for hit in hits):
        raise ValueError("invalid Milvus hit")
    return hits


def _format_retrieval_hit(hit: Mapping) -> dict:
    entity_value = hit.get("entity")
    entity = entity_value if entity_value is not None else hit
    if not isinstance(entity, Mapping):
        raise ValueError("invalid Milvus hit entity")
    text = entity.get("text")
    chunk_id = entity.get("chunk_id")
    distance = hit.get("distance")
    if "id" not in hit or not isinstance(text, str) or not text.strip():
        raise ValueError("Milvus hit is missing required fields")
    if not isinstance(chunk_id, str) or not chunk_id.strip():
        raise ValueError("Milvus hit is missing chunk_id")
    if (
        isinstance(distance, bool)
        or not isinstance(distance, (int, float))
        or not math.isfinite(float(distance))
    ):
        raise ValueError("Milvus hit has an invalid distance")
    acl_tags = entity.get("acl_tags", [])
    if not isinstance(acl_tags, list):
        acl_tags = []
    document_version_id = entity.get("document_version_id", "")
    content_hash_value = entity.get("content_hash")
    content_hash = (
        content_hash_value.lower()
        if isinstance(content_hash_value, str)
        and _SHA256_RE.fullmatch(content_hash_value)
        else None
    )
    if document_version_id and content_hash is None:
        raise ValueError("versioned Milvus hit is missing a valid content_hash")
    return {
        "id": hit.get("id"),
        "text": text,
        "filename": entity.get("filename", ""),
        "file_type": entity.get("file_type", ""),
        "page_number": entity.get("page_number", 0),
        "chunk_id": chunk_id,
        "parent_chunk_id": entity.get("parent_chunk_id", ""),
        "root_chunk_id": entity.get("root_chunk_id", ""),
        "chunk_level": entity.get("chunk_level", 0),
        "chunk_idx": entity.get("chunk_idx", 0),
        "tenant_id": entity.get("tenant_id", "default"),
        "knowledge_base_id": entity.get("knowledge_base_id", ""),
        "document_id": entity.get("document_id", ""),
        "document_version_id": document_version_id,
        "section_id": entity.get("section_id", ""),
        "acl_tags": acl_tags,
        "index_version": entity.get("index_version", "v1"),
        "content_hash": content_hash,
        "score": float(distance),
    }


class MilvusStore:
    """Milvus 集合读写；本身不持有连接，所有 IO 经 milvus_client_session。"""

    def __init__(self, settings: MilvusSettings | None = None):
        self._settings = settings or MilvusSettings.from_env()

    @property
    def collection_name(self) -> str:
        return self._settings.collection_name

    def with_collection(self, name: str) -> MilvusStore:
        """返回绑定到另一 collection 的无状态 Adapter。"""

        collection_name = (name or "").strip()
        if not collection_name:
            raise ValueError("collection name must not be empty")
        return MilvusStore(replace(self._settings, collection_name=collection_name))

    def _run(self, operation: Callable[[MilvusClient], T]) -> T:
        with milvus_client_session(self._settings) as client:
            return operation(client)

    @contextmanager
    def session(self) -> Iterator[MilvusClient]:
        """同一业务流（如整次上传）内复用一条连接，用毕即关。"""
        with milvus_client_session(self._settings) as client:
            yield client

    @staticmethod
    def ensure_collection(
        client: MilvusClient,
        collection_name: str,
        dense_dim: int,
        *,
        include_catalog_fields: bool = False,
    ) -> None:
        if client.has_collection(collection_name):
            if include_catalog_fields:
                description = client.describe_collection(collection_name)
                if not isinstance(description, Mapping):
                    raise RuntimeError("invalid Milvus collection description")
                fields = description.get("fields")
                if not isinstance(fields, (list, tuple)):
                    raise RuntimeError("Milvus collection description has no fields")
                field_names = {
                    field.get("name") for field in fields if isinstance(field, Mapping)
                }
                missing = sorted(_CATALOG_SCHEMA_FIELDS - field_names)
                if missing:
                    raise RuntimeError(
                        "versioned Milvus collection is missing fields: "
                        + ", ".join(missing)
                    )
            return

        schema = client.create_schema(auto_id=True, enable_dynamic_field=True)
        schema.add_field("id", DataType.INT64, is_primary=True, auto_id=True)
        schema.add_field("dense_embedding", DataType.FLOAT_VECTOR, dim=dense_dim)
        schema.add_field("sparse_embedding", DataType.SPARSE_FLOAT_VECTOR)
        schema.add_field(
            "text",
            DataType.VARCHAR,
            max_length=65535,
            enable_analyzer=True,
            analyzer_params={"type": "chinese"},
            enable_match=True,
        )
        schema.add_field("filename", DataType.VARCHAR, max_length=255)
        schema.add_field("file_type", DataType.VARCHAR, max_length=50)
        schema.add_field("file_path", DataType.VARCHAR, max_length=1024)
        schema.add_field("page_number", DataType.INT64)
        schema.add_field("chunk_idx", DataType.INT64)
        schema.add_field("chunk_id", DataType.VARCHAR, max_length=512)
        schema.add_field("parent_chunk_id", DataType.VARCHAR, max_length=512)
        schema.add_field("root_chunk_id", DataType.VARCHAR, max_length=512)
        schema.add_field("chunk_level", DataType.INT64)
        if include_catalog_fields:
            schema.add_field("tenant_id", DataType.VARCHAR, max_length=64)
            schema.add_field("knowledge_base_id", DataType.VARCHAR, max_length=64)
            schema.add_field("document_id", DataType.VARCHAR, max_length=64)
            schema.add_field("document_version_id", DataType.VARCHAR, max_length=64)
            schema.add_field("section_id", DataType.VARCHAR, max_length=256)
            schema.add_field(
                "acl_tags",
                DataType.ARRAY,
                element_type=DataType.VARCHAR,
                max_capacity=128,
                max_length=256,
            )
            schema.add_field("index_version", DataType.VARCHAR, max_length=64)
            schema.add_field("content_hash", DataType.VARCHAR, max_length=64)

        bm25_function = Function(
            name="text_bm25_emb",
            function_type=FunctionType.BM25,
            input_field_names=["text"],
            output_field_names=["sparse_embedding"],
        )
        schema.add_function(bm25_function)

        index_params = client.prepare_index_params()
        index_params.add_index(
            field_name="dense_embedding",
            index_type="HNSW",
            metric_type="IP",
            params={"M": 16, "efConstruction": 256},
        )
        index_params.add_index(
            field_name="sparse_embedding",
            index_type="SPARSE_INVERTED_INDEX",
            metric_type="BM25",
            params={"drop_ratio_build": 0.2},
        )
        client.create_collection(
            collection_name=collection_name,
            schema=schema,
            index_params=index_params,
        )

    def init_collection(self, dense_dim: int | None = None) -> None:
        if dense_dim is None:
            dense_dim = int(os.getenv("DENSE_EMBEDDING_DIM", "1024"))

        def _init(client: MilvusClient) -> None:
            self.ensure_collection(client, self.collection_name, dense_dim)

        self._run(_init)

    def init_versioned_collection(self, dense_dim: int | None = None) -> None:
        """初始化与 legacy collection 隔离的显式 catalog schema。"""

        if dense_dim is None:
            dense_dim = int(os.getenv("DENSE_EMBEDDING_DIM", "1024"))

        def _init(client: MilvusClient) -> None:
            self.ensure_collection(
                client,
                self.collection_name,
                dense_dim,
                include_catalog_fields=True,
            )

        self._run(_init)

    def insert(self, data: list[dict]):
        return self._run(lambda client: client.insert(self.collection_name, data))

    def query(
        self,
        filter_expr: str = "",
        output_fields: list[str] | None = None,
        limit: int = 10000,
        offset: int = 0,
    ):
        expr = _normalize_filter(filter_expr)
        fields = output_fields or ["filename", "file_type"]

        def _query(client: MilvusClient):
            return client.query(
                collection_name=self.collection_name,
                filter=expr,
                output_fields=fields,
                limit=min(limit, QUERY_MAX_LIMIT),
                offset=offset,
            )

        return self._run(_query)

    def query_all(
        self, filter_expr: str = "", output_fields: list[str] | None = None
    ) -> list:
        """分页拉取；单次 session 内完成，避免每页新建连接。"""
        fields = output_fields or ["filename", "file_type"]
        expr = _normalize_filter(filter_expr)

        def _query_all(client: MilvusClient) -> list:
            out: list = []
            offset = 0
            while True:
                batch = client.query(
                    collection_name=self.collection_name,
                    filter=expr,
                    output_fields=fields,
                    limit=QUERY_MAX_LIMIT,
                    offset=offset,
                )
                if not batch:
                    break
                out.extend(batch)
                if len(batch) < QUERY_MAX_LIMIT:
                    break
                offset += len(batch)
            return out

        return self._run(_query_all)

    def get_chunks_by_ids(self, chunk_ids: list[str]) -> list[dict]:
        ids = [item for item in chunk_ids if item]
        if not ids:
            return []
        return self.query(
            filter_expr=in_filter("chunk_id", ids),
            output_fields=RETRIEVAL_OUTPUT_FIELDS,
            limit=len(ids),
        )

    def hybrid_retrieve(
        self,
        dense_embedding: list[float],
        query: str,
        top_k: int = 5,
        rrf_k: int = 60,
        filter_expr: str = "",
        timeout: float | None = None,
    ) -> list[dict]:
        output_fields = RETRIEVAL_OUTPUT_FIELDS
        try:
            dense_search = AnnSearchRequest(
                data=[dense_embedding],
                anns_field="dense_embedding",
                param={"metric_type": "IP", "params": {"ef": 64}},
                limit=top_k * 2,
                expr=filter_expr,
            )
            sparse_search = AnnSearchRequest(
                data=[query],
                anns_field="sparse_embedding",
                param={"metric_type": "BM25", "params": {"drop_ratio_search": 0.2}},
                limit=top_k * 2,
                expr=filter_expr,
            )
            reranker = RRFRanker(k=rrf_k)
        except Exception as exc:
            if _is_hybrid_capability_error(exc):
                raise HybridRetrievalUnsupported from exc
            raise

        def _search(client: MilvusClient):
            return client.hybrid_search(
                collection_name=self.collection_name,
                reqs=[dense_search, sparse_search],
                ranker=reranker,
                limit=top_k,
                output_fields=output_fields,
                timeout=timeout,
            )

        try:
            results = self._run(_search)
        except Exception as exc:
            if _is_hybrid_capability_error(exc):
                raise HybridRetrievalUnsupported from exc
            raise
        return [_format_retrieval_hit(hit) for hit in _single_query_hits(results)]

    def dense_retrieve(
        self,
        dense_embedding: list[float],
        top_k: int = 5,
        filter_expr: str = "",
        timeout: float | None = None,
    ) -> list[dict]:
        def _search(client: MilvusClient):
            return client.search(
                collection_name=self.collection_name,
                data=[dense_embedding],
                anns_field="dense_embedding",
                search_params={"metric_type": "IP", "params": {"ef": 64}},
                limit=top_k,
                output_fields=RETRIEVAL_OUTPUT_FIELDS,
                filter=filter_expr,
                timeout=timeout,
            )

        results = self._run(_search)
        return [_format_retrieval_hit(hit) for hit in _single_query_hits(results)]

    def query_version_chunk_ids(
        self,
        *,
        tenant_id: str,
        knowledge_base_id: str,
        document_id: str,
        document_version_id: str,
        index_version: str | None = None,
    ) -> list[str]:
        """读取一个精确版本 scope 中的全部 chunk ID（保留重复项）。"""

        expression = version_scope_filter(
            tenant_id=tenant_id,
            knowledge_base_id=knowledge_base_id,
            document_id=document_id,
            document_version_ids=[document_version_id],
            index_version=index_version,
        )
        rows = self.query_all(expression, output_fields=["chunk_id"])
        chunk_ids: list[str] = []
        for row in rows:
            if not isinstance(row, Mapping):
                raise ValueError("invalid Milvus version query response")
            chunk_id = row.get("chunk_id")
            if not isinstance(chunk_id, str) or not chunk_id.strip():
                raise ValueError("Milvus version row is missing chunk_id")
            chunk_ids.append(chunk_id)
        return chunk_ids

    def count_by_version(
        self,
        *,
        tenant_id: str,
        knowledge_base_id: str,
        document_id: str,
        document_version_id: str,
        index_version: str | None = None,
    ) -> int:
        """统计一个精确版本 scope 的物理记录数。"""

        return len(
            self.query_version_chunk_ids(
                tenant_id=tenant_id,
                knowledge_base_id=knowledge_base_id,
                document_id=document_id,
                document_version_id=document_version_id,
                index_version=index_version,
            )
        )

    def verify_version(
        self,
        *,
        tenant_id: str,
        knowledge_base_id: str,
        document_id: str,
        document_version_id: str,
        expected_chunk_ids: Iterable[str],
        index_version: str | None = None,
    ) -> IndexVersionVerification:
        """同时核验 ID 集合、物理 count 与重复 ID。"""

        expected_ids = tuple(expected_chunk_ids)
        if any(not isinstance(item, str) or not item.strip() for item in expected_ids):
            raise ValueError("expected_chunk_ids must contain non-empty strings")
        if len(set(expected_ids)) != len(expected_ids):
            raise ValueError("expected_chunk_ids must be unique")

        actual_ids = tuple(
            self.query_version_chunk_ids(
                tenant_id=tenant_id,
                knowledge_base_id=knowledge_base_id,
                document_id=document_id,
                document_version_id=document_version_id,
                index_version=index_version,
            )
        )
        expected_set = set(expected_ids)
        actual_set = set(actual_ids)
        duplicate_ids = tuple(
            sorted(
                chunk_id for chunk_id, count in Counter(actual_ids).items() if count > 1
            )
        )
        missing_ids = tuple(sorted(expected_set - actual_set))
        unexpected_ids = tuple(sorted(actual_set - expected_set))
        expected_count = len(expected_ids)
        actual_count = len(actual_ids)
        exact = (
            not missing_ids
            and not unexpected_ids
            and not duplicate_ids
            and expected_count == actual_count
        )
        return IndexVersionVerification(
            expected_ids=expected_ids,
            actual_ids=actual_ids,
            missing_ids=missing_ids,
            unexpected_ids=unexpected_ids,
            duplicate_ids=duplicate_ids,
            expected_count=expected_count,
            actual_count=actual_count,
            exact=exact,
        )

    def delete_by_version(
        self,
        *,
        tenant_id: str,
        knowledge_base_id: str,
        document_id: str,
        document_version_id: str,
        index_version: str | None = None,
    ) -> int:
        """只删除一个租户、知识库、文档和版本的候选向量。"""

        result = self.delete(
            version_scope_filter(
                tenant_id=tenant_id,
                knowledge_base_id=knowledge_base_id,
                document_id=document_id,
                document_version_ids=[document_version_id],
                index_version=index_version,
            )
        )
        if isinstance(result, Mapping) and "delete_count" in result:
            count = result["delete_count"]
            if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                raise ValueError("invalid Milvus delete_count")
            return count
        return 0

    def delete(self, filter_expr: str):
        return self._run(
            lambda client: client.delete(
                collection_name=self.collection_name, filter=filter_expr
            )
        )

    def has_collection(self) -> bool:
        return self._run(lambda client: client.has_collection(self.collection_name))

    def drop_collection(self) -> None:
        def _drop(client: MilvusClient) -> None:
            if client.has_collection(self.collection_name):
                client.drop_collection(self.collection_name)

        self._run(_drop)


_store: MilvusStore | None = None


def get_milvus_store() -> MilvusStore:
    global _store
    if _store is None:
        _store = MilvusStore()
    return _store
