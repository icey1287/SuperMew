"""文本向量化服务 - 只支持密集向量（由 Milvus 2.5+ 原生支持中文分词与 BM25 全文检索）"""

import os
from langchain_huggingface import HuggingFaceEmbeddings


def _create_dense_embedder() -> HuggingFaceEmbeddings:
    model_name = os.getenv("EMBEDDING_MODEL", "BAAI/bge-m3")
    device = os.getenv("EMBEDDING_DEVICE", "cpu")
    return HuggingFaceEmbeddings(
        model_name=model_name,
        model_kwargs={"device": device},
        encode_kwargs={"normalize_embeddings": True},
    )


class EmbeddingService:
    """文本向量化服务 - 密集向量本地模型"""

    def __init__(self, state_path=None):
        self._embedder = _create_dense_embedder()

    def get_embeddings(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        try:
            return self._embedder.embed_documents(texts)
        except Exception as exc:
            raise RuntimeError("dense embedding provider failed") from exc


# 全进程唯一实例
embedding_service = EmbeddingService()
