"""文档向量化并写入 Milvus - 支持密集+稀疏向量"""
import os

from backend.indexing.embedding import EmbeddingService, embedding_service as _default_embedding_service
from backend.indexing.milvus_client import MilvusStore, get_milvus_store


class MilvusWriter:
    """文档向量化并写入 Milvus 服务 - 支持混合检索"""

    def __init__(self, embedding_service: EmbeddingService = None, milvus_manager: MilvusStore = None):
        self.embedding_service = embedding_service or _default_embedding_service
        self.milvus_manager = milvus_manager or get_milvus_store()

    def write_documents(self, documents: list[dict], batch_size: int = 50, progress_callback=None):
        if not documents:
            return

        dense_dim = int(os.getenv("DENSE_EMBEDDING_DIM", "1024"))

        total = len(documents)
        self.milvus_manager.init_collection(dense_dim)

        for i in range(0, total, batch_size):
            batch = documents[i : i + batch_size]
            texts = [doc["text"] for doc in batch]
            dense_embeddings = self.embedding_service.get_embeddings(texts)

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
