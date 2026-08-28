"""发布阶段真实 Embedding 模型离线烟测。"""

from __future__ import annotations

import math
import os
import re
import time
from collections.abc import Sequence
from importlib import metadata

from backend.providers.embedding import (
    EmbeddingRuntime,
    EmbeddingScope,
    EmbeddingService,
)
from backend.providers.loop_bridge import ProviderLoopBridge


def _require_offline_mode() -> None:
    required = ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE")
    missing = [name for name in required if os.getenv(name) != "1"]
    if missing:
        names = ", ".join(missing)
        raise RuntimeError(f"真实模型 smoke 必须强制离线；请设置 {names}=1")


def _require_revision() -> str:
    revision = os.getenv("EMBEDDING_MODEL_REVISION", "").strip()
    if re.fullmatch(r"[0-9a-f]{40}", revision) is None:
        raise RuntimeError(
            "EMBEDDING_MODEL_REVISION 必须是 40 位不可变 Hugging Face commit SHA"
        )
    return revision


def _validate_vector(
    vector: Sequence[float],
    *,
    expected_dimension: int,
    label: str,
) -> float:
    if len(vector) != expected_dimension:
        raise RuntimeError(
            f"{label} 向量维度错误：expected={expected_dimension}, actual={len(vector)}"
        )
    if not all(math.isfinite(value) for value in vector):
        raise RuntimeError(f"{label} 向量包含非有限值")
    norm = math.sqrt(sum(value * value for value in vector))
    if not math.isclose(norm, 1.0, rel_tol=1e-5, abs_tol=1e-5):
        raise RuntimeError(f"{label} 向量未归一化：norm={norm:.8f}")
    return norm


def main() -> None:
    _require_offline_mode()
    model = os.getenv("EMBEDDING_MODEL", "BAAI/bge-m3").strip()
    revision = _require_revision()
    device = os.getenv("EMBEDDING_DEVICE", "cpu").strip()
    dimension = int(os.getenv("DENSE_EMBEDDING_DIM", "1024"))
    timeout_seconds = float(os.getenv("EMBEDDING_TIMEOUT_SECONDS", "180"))
    deadline = time.monotonic() + timeout_seconds

    bridge = ProviderLoopBridge(thread_name="embedding-release-smoke")
    runtime = EmbeddingRuntime(
        model_name=model,
        model_revision=revision,
        device=device,
        provider_name="embedding-release-smoke",
        encoder_concurrency=1,
        executor_workers=1,
        max_batch_size=2,
        max_queue_size=4,
        cache_size=0,
        microbatch_window_seconds=0,
        expected_dimension=dimension,
        default_timeout_seconds=timeout_seconds,
    )
    service = EmbeddingService(runtime=runtime, bridge=bridge)
    scope = EmbeddingScope(
        namespace="release-smoke",
        tenant_id="release-smoke",
        index_id=f"{model}@{revision}",
    )

    bridge.start()
    try:
        query = service.embed_query(
            "如何验证知识库检索的查询向量？",
            scope=scope,
            deadline=deadline,
        )
        documents = service.embed_documents(
            [
                "查询向量用于在知识库中召回相关证据。",
                "文档向量必须与查询向量保持相同维度。",
            ],
            scope=scope,
            deadline=deadline,
        )
        query_norm = _validate_vector(
            query,
            expected_dimension=dimension,
            label="query",
        )
        document_norms = [
            _validate_vector(
                vector,
                expected_dimension=dimension,
                label=f"document[{index}]",
            )
            for index, vector in enumerate(documents)
        ]
    finally:
        service.close(close_bridge=True)

    norms = ", ".join(f"{value:.6f}" for value in document_norms)
    versions = {
        name: metadata.version(name)
        for name in ("sentence-transformers", "torch", "transformers")
    }
    print(
        "Embedding 离线 smoke 通过："
        f"model={model}, revision={revision}, device={device}, dimension={dimension}, "
        f"query_norm={query_norm:.6f}, document_norms=[{norms}], versions={versions}"
    )


if __name__ == "__main__":
    main()
