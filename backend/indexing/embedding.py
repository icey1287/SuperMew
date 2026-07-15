"""Compatibility Adapter for indexing callers using the async Provider Runtime."""

from backend.providers.embedding import EmbeddingScope, EmbeddingService
from backend.providers.runtime import provider_runtime


embedding_service = provider_runtime.embedding_service


__all__ = ["EmbeddingScope", "EmbeddingService", "embedding_service"]
