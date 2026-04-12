"""Milvus 客户端 - 支持密集向量+稀疏向量混合检索"""
import json
import os
from typing import Any

from dotenv import load_dotenv
from pymilvus import MilvusClient, DataType, AnnSearchRequest, RRFRanker
from pymilvus.orm.constants import UNLIMITED

load_dotenv()

# Milvus 单次 query 的 limit 上限（超出会报 invalid max query result window）
QUERY_MAX_LIMIT = 16384


def _normalize_meta_field(raw: Any) -> dict | None:
    """动态字段 meta（JSON 字符串或 dict），与 ingest_excel_literature 写入格式一致。"""
    if raw is None or raw == "":
        return None
    if isinstance(raw, dict):
        return raw if raw else None
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else None
        except (json.JSONDecodeError, TypeError):
            return None
    return None


def _meta_from_search_hit(hit: dict) -> dict | None:
    """MilvusClient.search 返回的 hit 中 meta 可能在 entity 内。"""
    if not isinstance(hit, dict):
        return None
    ent = hit.get("entity")
    if isinstance(ent, dict) and ent.get("meta") is not None:
        return _normalize_meta_field(ent.get("meta"))
    return _normalize_meta_field(hit.get("meta"))


def _meta_from_hybrid_hit(hit: dict) -> dict | None:
    """hybrid_search 的 hit 多为扁平字段。"""
    if not isinstance(hit, dict):
        return None
    raw = hit.get("meta")
    if raw is None and isinstance(hit.get("entity"), dict):
        raw = hit["entity"].get("meta")
    return _normalize_meta_field(raw)


class MilvusManager:
    """Milvus 连接和集合管理 - 支持混合检索"""

    def __init__(self):
        self.host = os.getenv("MILVUS_HOST", "localhost")
        self.port = os.getenv("MILVUS_PORT", "19530")
        base_collection = os.getenv("MILVUS_COLLECTION", "embeddings_collection")
        self.collection_brief = os.getenv("MILVUS_COLLECTION_BRIEF", f"{base_collection}_brief")
        self.collection_detailed = os.getenv("MILVUS_COLLECTION_DETAILED", f"{base_collection}_detailed")
        self._client = None

    @staticmethod
    def normalize_kb_tier(kb_tier: str | None) -> str:
        tier = (kb_tier or "brief").strip().lower()
        if tier in ("brief", "summary", "simple", "fast", "normal"):
            return "brief"
        if tier in ("detailed", "detail", "deep"):
            return "detailed"
        return "brief"

    def _collection_name_for_tier(self, kb_tier: str | None) -> str:
        tier = self.normalize_kb_tier(kb_tier)
        return self.collection_detailed if tier == "detailed" else self.collection_brief

    @property
    def client(self):
        """延迟初始化并重连 Milvus Client 解决 closed channel 报错"""
        if self._client is None:
            self._client = MilvusClient(uri=f"http://{self.host}:{self.port}")
        return self._client

    def _ensure_connection(self, kb_tier: str | None = None):
        collection_name = self._collection_name_for_tier(kb_tier)
        try:
            # 轻量调用探测连接是否存活
            self.client.has_collection(collection_name)
        except Exception as e:
            if "closed channel" in str(e).lower() or "connection" in str(e).lower():
                print("Milvus RPC channel closed, attempting to reconnect...")
                if self._client:
                    try:
                        self._client.close()
                    except Exception:
                        pass
                self._client = MilvusClient(uri=f"http://{self.host}:{self.port}")

    def init_collection(self, dense_dim: int | None = None, kb_tier: str | None = None):
        """
        初始化 Milvus 集合 - 同时支持密集向量和稀疏向量
        :param dense_dim: 密集向量维度；默认读环境变量 DENSE_EMBEDDING_DIM（本地 BAAI/bge-m3 为 1024）
        """
        if dense_dim is None:
            dense_dim = int(os.getenv("DENSE_EMBEDDING_DIM", "1024"))
        collection_name = self._collection_name_for_tier(kb_tier)
        self._ensure_connection(kb_tier=kb_tier)
        if not self.client.has_collection(collection_name):
            schema = self.client.create_schema(auto_id=True, enable_dynamic_field=True)
            
            # 主键
            schema.add_field("id", DataType.INT64, is_primary=True, auto_id=True)
            
            # 密集向量（来自 embedding 模型）
            schema.add_field("dense_embedding", DataType.FLOAT_VECTOR, dim=dense_dim)
            
            # 稀疏向量（来自 BM25）
            schema.add_field("sparse_embedding", DataType.SPARSE_FLOAT_VECTOR)
            
            # 文本和元数据字段
            schema.add_field("text", DataType.VARCHAR, max_length=2400)
            schema.add_field("filename", DataType.VARCHAR, max_length=255)
            schema.add_field("file_type", DataType.VARCHAR, max_length=50)
            schema.add_field("file_path", DataType.VARCHAR, max_length=1024)
            schema.add_field("page_number", DataType.INT64)
            schema.add_field("chunk_idx", DataType.INT64)

            # Auto-merging 所需层级字段
            schema.add_field("chunk_id", DataType.VARCHAR, max_length=512)
            schema.add_field("parent_chunk_id", DataType.VARCHAR, max_length=512)
            schema.add_field("root_chunk_id", DataType.VARCHAR, max_length=512)
            schema.add_field("chunk_level", DataType.INT64)

            # 为两种向量分别创建索引
            index_params = self.client.prepare_index_params()
            
            # 密集向量索引 - 使用 HNSW（更适合混合检索）
            index_params.add_index(
                field_name="dense_embedding",
                index_type="HNSW",
                metric_type="IP",
                params={"M": 16, "efConstruction": 256}
            )
            
            # 稀疏向量索引
            index_params.add_index(
                field_name="sparse_embedding",
                index_type="SPARSE_INVERTED_INDEX",
                metric_type="IP",
                params={"drop_ratio_build": 0.2}
            )

            self.client.create_collection(
                collection_name=collection_name,
                schema=schema,
                index_params=index_params
            )

    def insert(self, data: list[dict], kb_tier: str | None = None):
        """插入数据到 Milvus"""
        collection_name = self._collection_name_for_tier(kb_tier)
        self._ensure_connection(kb_tier=kb_tier)
        return self.client.insert(collection_name, data)

    def query(
        self,
        filter_expr: str = "",
        output_fields: list[str] = None,
        limit: int = 10000,
        offset: int = 0,
        kb_tier: str | None = None,
    ):
        """查询数据。limit 不宜超过 QUERY_MAX_LIMIT。"""
        collection_name = self._collection_name_for_tier(kb_tier)
        self._ensure_connection(kb_tier=kb_tier)
        if not filter_expr:
            filter_expr = "id > 0"
        return self.client.query(
            collection_name=collection_name,
            filter=filter_expr,
            output_fields=output_fields or ["filename", "file_type"],
            limit=min(limit, QUERY_MAX_LIMIT),
            offset=offset,
        )

    def query_all(
        self,
        filter_expr: str = "",
        output_fields: list[str] | None = None,
        kb_tier: str | None = None,
    ) -> list:
        """拉取匹配 filter 的全部行。Milvus 单次 query 要求 offset+limit≤16384，故用 query_iterator 全量遍历。"""
        collection_name = self._collection_name_for_tier(kb_tier)
        self._ensure_connection(kb_tier=kb_tier)
        if not filter_expr:
            filter_expr = "id > 0"
        fields = output_fields or ["filename", "file_type"]
        out: list = []
        iterator = self.client.query_iterator(
            collection_name=collection_name,
            filter=filter_expr,
            output_fields=fields,
            batch_size=QUERY_MAX_LIMIT,
            limit=UNLIMITED,
        )
        try:
            while True:
                batch = iterator.next()
                if not batch:
                    break
                out.extend(batch)
        finally:
            iterator.close()
        return out

    def get_chunks_by_ids(self, chunk_ids: list[str], kb_tier: str | None = None) -> list[dict]:
        """根据 chunk_id 批量查询分块（用于 Auto-merging 拉取父块）"""
        ids = [item for item in chunk_ids if item]
        if not ids:
            return []
        quoted_ids = ", ".join([f'"{item}"' for item in ids])
        filter_expr = f"chunk_id in [{quoted_ids}]"
        rows = self.query(
            filter_expr=filter_expr,
            output_fields=[
                "text",
                "filename",
                "file_type",
                "page_number",
                "chunk_id",
                "parent_chunk_id",
                "root_chunk_id",
                "chunk_level",
                "chunk_idx",
                "meta",
            ],
            limit=len(ids),
            kb_tier=kb_tier,
        )
        for row in rows:
            if isinstance(row, dict) and "meta" in row:
                row["meta"] = _normalize_meta_field(row.get("meta"))
        return rows

    def hybrid_retrieve(
        self,
        dense_embedding: list[float],
        sparse_embedding: dict,
        top_k: int = 5,
        rrf_k: int = 60,     #可调节
        filter_expr: str = "",
        kb_tier: str | None = None,
    ) -> list[dict]:
        """
        混合检索 - 使用 RRF 融合密集向量和稀疏向量的检索结果
        
        :param dense_embedding: 密集向量
        :param sparse_embedding: 稀疏向量 {index: value, ...}
        :param top_k: 返回结果数量
        :param rrf_k: RRF 算法参数 k，默认60
        :return: 检索结果列表
        """
        collection_name = self._collection_name_for_tier(kb_tier)
        self._ensure_connection(kb_tier=kb_tier)
        output_fields = [
            "text",
            "filename",
            "file_type",
            "page_number",
            "chunk_id",
            "parent_chunk_id",
            "root_chunk_id",
            "chunk_level",
            "chunk_idx",
            "meta",
        ]
        
        # 密集向量搜索请求
        dense_search = AnnSearchRequest(
            data=[dense_embedding],
            anns_field="dense_embedding",
            param={"metric_type": "IP", "params": {"ef": 64}},
            limit=top_k * 2,  # 多取一些用于融合
            expr=filter_expr,
        )
        
        # 稀疏向量搜索请求
        sparse_search = AnnSearchRequest(
            data=[sparse_embedding],
            anns_field="sparse_embedding",
            param={"metric_type": "IP", "params": {"drop_ratio_search": 0.2}},
            limit=top_k * 2,
            expr=filter_expr,
        )
        
        # 使用 RRF 排序算法融合结果
        reranker = RRFRanker(k=rrf_k)
        
        results = self.client.hybrid_search(
            collection_name=collection_name,
            reqs=[dense_search, sparse_search],
            ranker=reranker,
            limit=top_k,
            output_fields=output_fields
        )
        
        # 格式化返回结果
        formatted_results = []
        for hits in results:
            for hit in hits:
                formatted_results.append({
                    "id": hit.get("id"),
                    "text": hit.get("text", ""),
                    "filename": hit.get("filename", ""),
                    "file_type": hit.get("file_type", ""),
                    "page_number": hit.get("page_number", 0),
                    "chunk_id": hit.get("chunk_id", ""),
                    "parent_chunk_id": hit.get("parent_chunk_id", ""),
                    "root_chunk_id": hit.get("root_chunk_id", ""),
                    "chunk_level": hit.get("chunk_level", 0),
                    "chunk_idx": hit.get("chunk_idx", 0),
                    "meta": _meta_from_hybrid_hit(hit),
                    "score": hit.get("distance", 0.0)
                })
        
        return formatted_results

    def dense_retrieve(
        self,
        dense_embedding: list[float],
        top_k: int = 5,
        filter_expr: str = "",
        kb_tier: str | None = None,
    ) -> list[dict]:
        """
        仅使用密集向量检索（降级模式，用于稀疏向量不可用时）
        """
        collection_name = self._collection_name_for_tier(kb_tier)
        self._ensure_connection(kb_tier=kb_tier)
        results = self.client.search(
            collection_name=collection_name,
            data=[dense_embedding],
            anns_field="dense_embedding",
            search_params={"metric_type": "IP", "params": {"ef": 64}},
            limit=top_k,
            output_fields=[
                "text",
                "filename",
                "file_type",
                "page_number",
                "chunk_id",
                "parent_chunk_id",
                "root_chunk_id",
                "chunk_level",
                "chunk_idx",
                "meta",
            ],
            filter=filter_expr,
        )
        
        formatted_results = []
        for hits in results:
            for hit in hits:
                ent = hit.get("entity") or {}
                formatted_results.append({
                    "id": hit.get("id"),
                    "text": ent.get("text", ""),
                    "filename": ent.get("filename", ""),
                    "file_type": ent.get("file_type", ""),
                    "page_number": ent.get("page_number", 0),
                    "chunk_id": ent.get("chunk_id", ""),
                    "parent_chunk_id": ent.get("parent_chunk_id", ""),
                    "root_chunk_id": ent.get("root_chunk_id", ""),
                    "chunk_level": ent.get("chunk_level", 0),
                    "chunk_idx": ent.get("chunk_idx", 0),
                    "meta": _meta_from_search_hit(hit),
                    "score": hit.get("distance", 0.0)
                })
        
        return formatted_results

    def sparse_retrieve(
        self,
        sparse_embedding: dict,
        top_k: int = 5,
        filter_expr: str = "",
        kb_tier: str | None = None,
    ) -> list[dict]:
        """仅使用稀疏向量检索（BM25）"""
        collection_name = self._collection_name_for_tier(kb_tier)
        self._ensure_connection(kb_tier=kb_tier)
        results = self.client.search(
            collection_name=collection_name,
            data=[sparse_embedding],
            anns_field="sparse_embedding",
            search_params={"metric_type": "IP", "params": {"drop_ratio_search": 0.2}},
            limit=top_k,
            output_fields=[
                "text",
                "filename",
                "file_type",
                "page_number",
                "chunk_id",
                "parent_chunk_id",
                "root_chunk_id",
                "chunk_level",
                "chunk_idx",
                "meta",
            ],
            filter=filter_expr,
        )

        formatted_results = []
        for hits in results:
            for hit in hits:
                ent = hit.get("entity") or {}
                formatted_results.append({
                    "id": hit.get("id"),
                    "text": ent.get("text", ""),
                    "filename": ent.get("filename", ""),
                    "file_type": ent.get("file_type", ""),
                    "page_number": ent.get("page_number", 0),
                    "chunk_id": ent.get("chunk_id", ""),
                    "parent_chunk_id": ent.get("parent_chunk_id", ""),
                    "root_chunk_id": ent.get("root_chunk_id", ""),
                    "chunk_level": ent.get("chunk_level", 0),
                    "chunk_idx": ent.get("chunk_idx", 0),
                    "meta": _meta_from_search_hit(hit),
                    "score": hit.get("distance", 0.0)
                })
        return formatted_results

    def delete(self, filter_expr: str, kb_tier: str | None = None):
        """删除数据"""
        collection_name = self._collection_name_for_tier(kb_tier)
        self._ensure_connection(kb_tier=kb_tier)
        return self.client.delete(
            collection_name=collection_name,
            filter=filter_expr
        )

    def has_collection(self, kb_tier: str | None = None) -> bool:
        """检查集合是否存在"""
        collection_name = self._collection_name_for_tier(kb_tier)
        self._ensure_connection(kb_tier=kb_tier)
        return self.client.has_collection(collection_name)

    def drop_collection(self, kb_tier: str | None = None):
        """删除集合（用于重建 schema）"""
        collection_name = self._collection_name_for_tier(kb_tier)
        self._ensure_connection(kb_tier=kb_tier)
        if self.client.has_collection(collection_name):
            self.client.drop_collection(collection_name)
