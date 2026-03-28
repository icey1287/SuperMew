"""Milvus 客户端 - 支持密集向量+稀疏向量混合检索"""
import os
from dotenv import load_dotenv
from pymilvus import MilvusClient, DataType, AnnSearchRequest, RRFRanker

load_dotenv()


class MilvusManager:
    """Milvus 连接和集合管理 - 支持混合检索"""

    def __init__(self):
        self.host = os.getenv("MILVUS_HOST", "localhost")
        self.port = os.getenv("MILVUS_PORT", "19530")
        self.collection_name = os.getenv("MILVUS_COLLECTION", "embeddings_collection")
        self.uri = f"http://{self.host}:{self.port}"
        self.client = None

    def _get_client(self) -> MilvusClient:
        # Lazy-create client to avoid blocking app import/startup when Milvus is temporarily unavailable.
        if self.client is None:
            self.client = MilvusClient(uri=self.uri)
        return self.client

    def init_collection(self, dense_dim: int = None):
        """
        初始化 Milvus 集合 - 同时支持密集向量和稀疏向量
        :param dense_dim: 密集向量维度
        """
        if dense_dim is None:
            dense_dim = int(os.getenv("EMBEDDING_DIM", "2560"))
        client = self._get_client()
        if not client.has_collection(self.collection_name):
            schema = client.create_schema(auto_id=True, enable_dynamic_field=True)
            
            # 主键
            schema.add_field("id", DataType.INT64, is_primary=True, auto_id=True)
            
            # 密集向量（来自 embedding 模型）
            schema.add_field("dense_embedding", DataType.FLOAT_VECTOR, dim=dense_dim)
            
            # 稀疏向量（来自 BM25）
            schema.add_field("sparse_embedding", DataType.SPARSE_FLOAT_VECTOR)
            
            # 文本和元数据字段
            schema.add_field("text", DataType.VARCHAR, max_length=2000)
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
            index_params = client.prepare_index_params()
            
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

            client.create_collection(
                collection_name=self.collection_name,
                schema=schema,
                index_params=index_params
            )

    def insert(self, data: list[dict]):
        """插入数据到 Milvus"""
        return self._get_client().insert(self.collection_name, data)

    def query(self, filter_expr: str = "", output_fields: list[str] = None, limit: int = 10000):
        """查询数据"""
        return self._get_client().query(
            collection_name=self.collection_name,
            filter=filter_expr,
            output_fields=output_fields or ["filename", "file_type"],
            limit=limit
        )

    def get_chunks_by_ids(self, chunk_ids: list[str]) -> list[dict]:
        """根据 chunk_id 批量查询分块（用于 Auto-merging 拉取父块）"""
        ids = [item for item in chunk_ids if item]
        if not ids:
            return []
        quoted_ids = ", ".join([f'"{item}"' for item in ids])
        filter_expr = f"chunk_id in [{quoted_ids}]"
        return self.query(
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
            ],
            limit=len(ids),
        )

    def hybrid_retrieve(
        self,
        dense_embedding: list[float],
        sparse_embedding: dict,
        top_k: int = 5,
        rrf_k: int = 60,     #可调节
        filter_expr: str = "",
    ) -> list[dict]:
        """
        混合检索 - 使用 RRF 融合密集向量和稀疏向量的检索结果
        
        :param dense_embedding: 密集向量
        :param sparse_embedding: 稀疏向量 {index: value, ...}
        :param top_k: 返回结果数量
        :param rrf_k: RRF 算法参数 k，默认60
        :return: 检索结果列表
        """
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
        
        results = self._get_client().hybrid_search(
            collection_name=self.collection_name,
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
                    "score": hit.get("distance", 0.0)
                })
        
        return formatted_results

    def dense_retrieve(self, dense_embedding: list[float], top_k: int = 5, filter_expr: str = "") -> list[dict]:
        """
        仅使用密集向量检索（降级模式，用于稀疏向量不可用时）
        """
        results = self._get_client().search(
            collection_name=self.collection_name,
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
            ],
            filter=filter_expr,
        )
        
        formatted_results = []
        for hits in results:
            for hit in hits:
                formatted_results.append({
                    "id": hit.get("id"),
                    "text": hit.get("entity", {}).get("text", ""),
                    "filename": hit.get("entity", {}).get("filename", ""),
                    "file_type": hit.get("entity", {}).get("file_type", ""),
                    "page_number": hit.get("entity", {}).get("page_number", 0),
                    "chunk_id": hit.get("entity", {}).get("chunk_id", ""),
                    "parent_chunk_id": hit.get("entity", {}).get("parent_chunk_id", ""),
                    "root_chunk_id": hit.get("entity", {}).get("root_chunk_id", ""),
                    "chunk_level": hit.get("entity", {}).get("chunk_level", 0),
                    "chunk_idx": hit.get("entity", {}).get("chunk_idx", 0),
                    "score": hit.get("distance", 0.0)
                })
        
        return formatted_results

    def delete(self, filter_expr: str):
        """删除数据"""
        return self._get_client().delete(
            collection_name=self.collection_name,
            filter=filter_expr
        )

    def has_collection(self) -> bool:
        """检查集合是否存在"""
        return self._get_client().has_collection(self.collection_name)

    def drop_collection(self):
        """删除集合（用于重建 schema）"""
        client = self._get_client()
        if client.has_collection(self.collection_name):
            client.drop_collection(self.collection_name)
