from collections import defaultdict
from collections.abc import Callable
import asyncio
import math
import os
import time
from typing import List, Tuple, Dict, Any, Literal, Optional

from backend.indexing.milvus_client import (
    HybridRetrievalUnsupported,
    get_milvus_store,
)
from backend.indexing.embedding import embedding_service as _embedding_service
from backend.indexing.parent_chunk_store import ParentChunkStore
from backend.providers import (
    EmbeddingScope,
    ProviderCallContext,
    ProviderError,
    ProviderExecutor,
    ProviderOperation,
    ProviderPolicy,
    classify_provider_exception,
)
from backend.providers.runtime import provider_runtime
from backend.rag.reranking import RerankStage
from langchain.chat_models import init_chat_model
from pydantic import BaseModel, Field

ARK_API_KEY = os.getenv("ARK_API_KEY")
FAST_MODEL = os.getenv("FAST_MODEL")
BASE_URL = os.getenv("BASE_URL")
try:
    RERANK_TIMEOUT_SECONDS = max(float(os.getenv("RERANK_TIMEOUT_SECONDS", "5")), 0.1)
except ValueError:
    RERANK_TIMEOUT_SECONDS = 5.0
AUTO_MERGE_ENABLED = os.getenv("AUTO_MERGE_ENABLED", "true").lower() != "false"
AUTO_MERGE_THRESHOLD = int(os.getenv("AUTO_MERGE_THRESHOLD", "2"))
LEAF_RETRIEVE_LEVEL = int(os.getenv("LEAF_RETRIEVE_LEVEL", "3"))


def _read_positive_int_env(name: str, default: int) -> int:
    try:
        return max(int(os.getenv(name, str(default))), 1)
    except ValueError:
        return default


RETRIEVAL_CANDIDATE_MULTIPLIER = _read_positive_int_env(
    "RETRIEVAL_CANDIDATE_MULTIPLIER", 3
)
_RETRIEVAL_CANDIDATE_K_RAW = os.getenv("RETRIEVAL_CANDIDATE_K", "").strip()
RETRIEVAL_TOP_K = _read_positive_int_env("RETRIEVAL_TOP_K", 8)


RERANK_MIN_SCORE = provider_runtime.settings.rerank.min_score

RETRIEVAL_TRACE_FIELDS = (
    "retrieval_pipeline",
    "retrieval_mode",
    "candidate_k",
    "candidate_k_source",
    "candidate_k_config_error",
    "retrieval_candidate_multiplier",
    "retrieval_top_k",
    "leaf_retrieve_level",
    "recall_count",
    "post_merge_candidate_count",
    "candidate_count",
    "auto_merge_enabled",
    "auto_merge_applied",
    "auto_merge_threshold",
    "auto_merge_replaced_chunks",
    "auto_merge_steps",
    "rerank_enabled",
    "rerank_applied",
    "rerank_model",
    "rerank_error_code",
    "rerank_retryable",
    "rerank_attempts",
    "rerank_fallback_applied",
    "rerank_timeout_seconds",
    "rerank_min_score",
    "rerank_threshold_applied",
    "rerank_skip_reason",
    "rerank_candidate_count",
    "rerank_candidate_limit",
    "rerank_candidate_limit_applied",
    "rerank_payload_characters",
    "rerank_document_character_limit",
    "rerank_total_character_limit",
    "rerank_truncated_document_count",
    "post_rerank_count",
    "post_threshold_count",
    "retrieval_empty",
    "retrieval_degraded_code",
)

# 全局初始化检索依赖（与 api 共用 embedding_service，保证 BM25 状态一致）
_milvus_manager = get_milvus_store()
_parent_chunk_store = ParentChunkStore()
_provider_executor = ProviderExecutor()
_rerank_stage: RerankStage | None = None
_embedding_scope = EmbeddingScope(
    namespace=os.getenv("EMBEDDING_CACHE_NAMESPACE", "default"),
    index_id=(
        os.getenv("INDEX_VERSION") or os.getenv("MILVUS_COLLECTION") or "default"
    ),
)

_rewrite_model = None


EMBEDDING_PROVIDER = os.getenv("EMBEDDING_MODEL", "BAAI/bge-m3")
EMBEDDING_PROVIDER_ID = EMBEDDING_PROVIDER.rsplit("/", 1)[-1] or "embedding-model"
try:
    EMBEDDING_TIMEOUT_SECONDS = max(
        float(os.getenv("EMBEDDING_TIMEOUT_SECONDS", "15")), 0.1
    )
except ValueError:
    EMBEDDING_TIMEOUT_SECONDS = 15.0
try:
    VECTOR_TIMEOUT_SECONDS = max(float(os.getenv("VECTOR_TIMEOUT_SECONDS", "10")), 0.1)
except ValueError:
    VECTOR_TIMEOUT_SECONDS = 10.0
_VECTOR_POLICY = ProviderPolicy(max_attempts=2)
_MODEL_POLICY = ProviderPolicy(max_attempts=2)
try:
    REWRITE_TIMEOUT_SECONDS = max(
        float(os.getenv("RAG_MODEL_TIMEOUT_SECONDS", "15")), 0.1
    )
except ValueError:
    REWRITE_TIMEOUT_SECONDS = 15.0


def _bounded_deadline(deadline: float | None, timeout_seconds: float) -> float:
    stage_deadline = time.monotonic() + timeout_seconds
    return min(deadline, stage_deadline) if deadline is not None else stage_deadline


def _remaining_timeout(deadline: float, configured_timeout: float) -> float:
    return max(min(deadline - time.monotonic(), configured_timeout), 0.001)


def _validate_retrieved_documents(value: Any) -> List[dict]:
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise ValueError("vector provider returned invalid documents")
    return value


def resolve_candidate_k(top_k: int) -> Tuple[int, Dict[str, Any]]:
    """解析 Milvus 候选池大小；RETRIEVAL_CANDIDATE_K 优先，否则 top_k × multiplier。"""
    if _RETRIEVAL_CANDIDATE_K_RAW:
        try:
            candidate_k = max(int(_RETRIEVAL_CANDIDATE_K_RAW), top_k)
        except ValueError:
            candidate_k = max(top_k * RETRIEVAL_CANDIDATE_MULTIPLIER, top_k)
            return candidate_k, {
                "candidate_k_source": "multiplier",
                "retrieval_candidate_multiplier": RETRIEVAL_CANDIDATE_MULTIPLIER,
                "candidate_k_config_error": "invalid RETRIEVAL_CANDIDATE_K",
            }
        return candidate_k, {
            "candidate_k_source": "env",
            "retrieval_candidate_multiplier": RETRIEVAL_CANDIDATE_MULTIPLIER,
        }
    candidate_k = max(top_k * RETRIEVAL_CANDIDATE_MULTIPLIER, top_k)
    return candidate_k, {
        "candidate_k_source": "multiplier",
        "retrieval_candidate_multiplier": RETRIEVAL_CANDIDATE_MULTIPLIER,
    }


def retrieval_trace_fields(meta: Dict[str, Any]) -> Dict[str, Any]:
    """从 retrieve meta 提取应写入 rag_trace 的检索字段。"""
    return {
        key: meta[key]
        for key in RETRIEVAL_TRACE_FIELDS
        if key in meta and meta[key] is not None
    }


def _effective_score(doc: dict) -> Optional[float]:
    """精排分优先，否则用召回分；用于合并聚合与合并后重排。"""
    rerank_score = doc.get("rerank_score")
    if rerank_score is not None:
        return float(rerank_score)
    score = doc.get("score")
    if score is not None:
        return float(score)
    return None


def _merge_rank_score_into(target: dict, source: dict) -> None:
    incoming = _effective_score(source)
    if incoming is None:
        return
    uses_rerank = (
        source.get("rerank_score") is not None or target.get("rerank_score") is not None
    )
    if uses_rerank:
        existing = target.get("rerank_score")
        if existing is None:
            target["rerank_score"] = incoming
        else:
            target["rerank_score"] = max(float(existing), incoming)
        return
    existing = target.get("score")
    if existing is None:
        target["score"] = incoming
    else:
        target["score"] = max(float(existing), incoming)


def _merge_to_parent_level(
    docs: List[dict], threshold: int = 2
) -> Tuple[List[dict], int]:
    groups: Dict[str, List[dict]] = defaultdict(list)
    for doc in docs:
        parent_id = (doc.get("parent_chunk_id") or "").strip()
        if parent_id:
            groups[parent_id].append(doc)

    merge_parent_ids = [
        parent_id
        for parent_id, children in groups.items()
        if len(children) >= threshold
    ]
    if not merge_parent_ids:
        return docs, 0

    parent_docs = _parent_chunk_store.get_documents_by_ids(merge_parent_ids)
    parent_map = {
        item.get("chunk_id", ""): item for item in parent_docs if item.get("chunk_id")
    }

    merged_docs: List[dict] = []
    parent_slot: Dict[str, int] = {}
    merged_count = 0
    for doc in docs:
        parent_id = (doc.get("parent_chunk_id") or "").strip()
        if not parent_id or parent_id not in parent_map:
            merged_docs.append(doc)
            continue

        if parent_id in parent_slot:
            existing = merged_docs[parent_slot[parent_id]]
            _merge_rank_score_into(existing, doc)
            merged_count += 1
            continue

        parent_doc = dict(parent_map[parent_id])
        _merge_rank_score_into(parent_doc, doc)
        parent_doc["merged_from_children"] = True
        parent_doc["merged_child_count"] = len(groups[parent_id])
        parent_slot[parent_id] = len(merged_docs)
        merged_docs.append(parent_doc)
        merged_count += 1

    return merged_docs, merged_count


def _empty_merge_meta() -> Dict[str, Any]:
    return {
        "auto_merge_enabled": AUTO_MERGE_ENABLED,
        "auto_merge_applied": False,
        "auto_merge_threshold": AUTO_MERGE_THRESHOLD,
        "auto_merge_replaced_chunks": 0,
        "auto_merge_steps": 0,
        "post_merge_candidate_count": 0,
    }


def _auto_merge_candidates(docs: List[dict]) -> Tuple[List[dict], Dict[str, Any]]:
    """在完整召回候选上执行 L3→L2→L1 合并；不改变顺序，精排由后续步骤负责。"""
    meta = _empty_merge_meta()
    meta["post_merge_candidate_count"] = len(docs)
    if not AUTO_MERGE_ENABLED or not docs:
        return docs, meta

    merged_docs, merged_count_l3_l2 = _merge_to_parent_level(
        docs, threshold=AUTO_MERGE_THRESHOLD
    )
    merged_docs, merged_count_l2_l1 = _merge_to_parent_level(
        merged_docs, threshold=AUTO_MERGE_THRESHOLD
    )

    replaced_count = merged_count_l3_l2 + merged_count_l2_l1
    meta.update(
        {
            "auto_merge_applied": replaced_count > 0,
            "auto_merge_replaced_chunks": replaced_count,
            "auto_merge_steps": int(merged_count_l3_l2 > 0)
            + int(merged_count_l2_l1 > 0),
            "post_merge_candidate_count": len(merged_docs),
        }
    )
    return merged_docs, meta


def dedupe_documents(docs: List[dict]) -> List[dict]:
    """按 chunk_id 去重；重复项保留更高 rank 分（rerank_score 优先）。"""
    by_key: Dict[str, dict] = {}
    order: List[str] = []
    for item in docs:
        chunk_id = (item.get("chunk_id") or "").strip()
        key = (
            chunk_id
            or f"{item.get('filename')}|{item.get('page_number')}|{item.get('text')}"
        )
        if key not in by_key:
            by_key[key] = item
            order.append(key)
            continue
        _merge_rank_score_into(by_key[key], item)
    return [by_key[key] for key in order]


def _get_rerank_stage() -> RerankStage:
    global _rerank_stage
    if _rerank_stage is None:
        rerank = provider_runtime.settings.rerank
        provider = provider_runtime.get_reranker_sync()
        _rerank_stage = RerankStage(
            provider,
            loop_bridge=provider_runtime.bridge,
            candidate_limit=rerank.candidate_limit,
            max_document_characters=rerank.max_document_characters,
            max_total_characters=rerank.max_total_characters,
            min_score=RERANK_MIN_SCORE,
        )
    return _rerank_stage


def _rerank_documents(
    query: str,
    docs: List[dict],
    top_k: int,
    *,
    deadline: float | None = None,
    cancellation: Callable[[], bool] | None = None,
) -> Tuple[List[dict], Dict[str, Any]]:
    stage_deadline = _bounded_deadline(deadline, RERANK_TIMEOUT_SECONDS)
    return _get_rerank_stage().run(
        query,
        docs,
        top_k,
        deadline=stage_deadline,
        cancellation=cancellation,
    )


class RewritePlan(BaseModel):
    method: Literal["step_back", "hyde"] = Field(
        description="本轮唯一使用的查询重写方式"
    )
    step_back_question: str = Field(
        default="",
        max_length=300,
        description="仅在 method=step_back 时填写的抽象退步问题",
    )
    hyde_document: str = Field(
        default="",
        max_length=1200,
        description="仅在 method=hyde 时填写的假设性答案文档",
    )


REWRITE_PROMPT = (
    "你是 RAG 查询重写规划器。初次检索已经找到相关信号，但证据不足。"
    "请在 step_back 和 hyde 中只选择一种重写方式，并同时生成该方式需要的内容。\n\n"
    "选择规则：\n"
    "- step_back：原问题过于具体，包含实体名、型号、时间、条件或细节，"
    "需要提升到更概括的概念、机制或原理后再检索。\n"
    "- hyde：原问题模糊、概念性强、缺少知识库常用术语，"
    "适合先生成一段可能的答案式文档，再用这段文档检索真实证据。\n\n"
    "约束：\n"
    "- method=step_back 时，只填写 step_back_question，hyde_document 必须留空。\n"
    "- method=hyde 时，只填写 hyde_document，step_back_question 必须留空。\n"
    "- HyDE 文档只能用于检索，不代表真实证据，不要编造引用或来源。\n\n"
    "用户问题：{query}"
)


def _get_rewrite_model():
    global _rewrite_model
    if not ARK_API_KEY or not FAST_MODEL:
        return None
    if _rewrite_model is None:
        _rewrite_model = init_chat_model(
            model=FAST_MODEL,
            model_provider="openai",
            api_key=ARK_API_KEY,
            base_url=BASE_URL,
            temperature=0,
            stream_usage=True,
            max_retries=0,
            timeout=REWRITE_TIMEOUT_SECONDS,
        )
    return _rewrite_model


def rewrite_query_once(
    query: str,
    *,
    deadline: float | None = None,
    cancellation: Callable[[], bool] | None = None,
) -> dict:
    model = _get_rewrite_model()
    if not model:
        raise RuntimeError("FAST_MODEL is required for query rewriting")

    result = _provider_executor.call(
        lambda: model.with_structured_output(RewritePlan).invoke(
            [{"role": "user", "content": REWRITE_PROMPT.format(query=query)}]
        ),
        context=ProviderCallContext(
            provider=FAST_MODEL or "fast-model",
            operation=ProviderOperation.MODEL,
            deadline=_bounded_deadline(deadline, REWRITE_TIMEOUT_SECONDS),
            cancellation=cancellation,
        ),
        policy=_MODEL_POLICY,
    )
    method = result.method
    step_back_question = (result.step_back_question or "").strip()
    hyde_document = (result.hyde_document or "").strip()

    if method == "step_back":
        if not step_back_question or hyde_document:
            raise ValueError(
                "Step-back rewrite plan must contain only step_back_question"
            )
        rewritten_query = f"{query}\n\n退步问题：{step_back_question}"
    elif method == "hyde":
        if not hyde_document or step_back_question:
            raise ValueError("HyDE rewrite plan must contain only hyde_document")
        rewritten_query = f"{query}\n\n假设性答案文档：{hyde_document}"
    else:
        raise ValueError(f"Unsupported rewrite method: {method}")

    return {
        "rewrite_method": method,
        "rewritten_query": rewritten_query,
        "step_back_question": step_back_question,
        "hyde_document": hyde_document,
    }


def _finalize_retrieval(
    query: str,
    retrieved: List[dict],
    top_k: int,
    retrieval_mode: str,
    candidate_k: int,
    candidate_config: Dict[str, Any],
    *,
    deadline: float | None = None,
    cancellation: Callable[[], bool] | None = None,
    retrieval_degraded_code: str | None = None,
) -> Dict[str, Any]:
    """生产流水线：召回候选 → Auto-merge → Rerank（top_k）→ 阈值过滤。"""
    candidates, merge_meta = _auto_merge_candidates(retrieved)
    reranked_docs, rerank_meta = _rerank_documents(
        query=query,
        docs=candidates,
        top_k=top_k,
        deadline=deadline,
        cancellation=cancellation,
    )
    post_rerank_count = int(rerank_meta.get("post_rerank_count", len(reranked_docs)))
    threshold_applied = bool(rerank_meta.get("rerank_threshold_applied"))
    final_docs = reranked_docs
    meta = {
        **rerank_meta,
        **merge_meta,
        **candidate_config,
        "retrieval_mode": retrieval_mode,
        "retrieval_pipeline": "recall_merge_rerank",
        "candidate_k": candidate_k,
        "retrieval_top_k": top_k,
        "leaf_retrieve_level": LEAF_RETRIEVE_LEVEL,
        "recall_count": len(retrieved),
        "rerank_min_score": RERANK_MIN_SCORE,
        "rerank_threshold_applied": threshold_applied,
        "post_rerank_count": post_rerank_count,
        "post_threshold_count": len(final_docs),
        "retrieval_empty": len(final_docs) == 0,
        "retrieval_degraded_code": retrieval_degraded_code,
    }
    return {"docs": final_docs, "meta": meta}


def retrieve_documents(
    query: str,
    top_k: int = RETRIEVAL_TOP_K,
    *,
    deadline: float | None = None,
    cancellation: Callable[[], bool] | None = None,
) -> Dict[str, Any]:
    candidate_k, candidate_config = resolve_candidate_k(top_k)
    filter_expr = f"chunk_level == {LEAF_RETRIEVE_LEVEL}"
    embedding_deadline = _bounded_deadline(deadline, EMBEDDING_TIMEOUT_SECONDS)
    embedding_context = ProviderCallContext(
        provider=EMBEDDING_PROVIDER_ID,
        operation=ProviderOperation.EMBEDDING,
        deadline=embedding_deadline,
        cancellation=cancellation,
    )

    def _embed_query() -> list[float]:
        query_method = getattr(_embedding_service, "embed_query", None)
        if callable(query_method):
            vector = query_method(
                query,
                scope=_embedding_scope,
                deadline=embedding_deadline,
                cancellation=cancellation,
            )
        else:
            dense_embeddings = _embedding_service.get_embeddings([query])
            if not dense_embeddings:
                raise ValueError("embedding provider returned no vector")
            vector = dense_embeddings[0]
        if not vector:
            raise ValueError("embedding provider returned no vector")
        if not isinstance(vector, list) or any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            for value in vector
        ):
            raise ValueError("embedding provider returned an invalid vector")
        return [float(value) for value in vector]

    try:
        dense_embedding = _embed_query()
    except asyncio.CancelledError:
        raise
    except ProviderError:
        raise
    except Exception as exc:
        raise classify_provider_exception(
            exc,
            context=embedding_context,
            attempts=1,
            max_attempts=1,
        ) from exc

    vector_deadline = _bounded_deadline(deadline, VECTOR_TIMEOUT_SECONDS)

    def _hybrid_retrieve() -> tuple[bool, List[dict]]:
        try:
            return False, _validate_retrieved_documents(
                _milvus_manager.hybrid_retrieve(
                    dense_embedding=dense_embedding,
                    query=query,
                    top_k=candidate_k,
                    filter_expr=filter_expr,
                    timeout=_remaining_timeout(vector_deadline, VECTOR_TIMEOUT_SECONDS),
                )
            )
        except HybridRetrievalUnsupported:
            return True, []

    fallback_required, retrieved = _provider_executor.call(
        _hybrid_retrieve,
        context=ProviderCallContext(
            provider="milvus",
            operation=ProviderOperation.VECTOR_SEARCH,
            deadline=vector_deadline,
            cancellation=cancellation,
        ),
        policy=_VECTOR_POLICY,
    )
    retrieval_mode = "hybrid"
    degraded_code = None
    if fallback_required:
        retrieved = _provider_executor.call(
            lambda: _validate_retrieved_documents(
                _milvus_manager.dense_retrieve(
                    dense_embedding=dense_embedding,
                    top_k=candidate_k,
                    filter_expr=filter_expr,
                    timeout=_remaining_timeout(
                        vector_deadline,
                        VECTOR_TIMEOUT_SECONDS,
                    ),
                )
            ),
            context=ProviderCallContext(
                provider="milvus",
                operation=ProviderOperation.VECTOR_SEARCH,
                deadline=vector_deadline,
                cancellation=cancellation,
            ),
            policy=_VECTOR_POLICY,
        )
        retrieval_mode = "dense_fallback"
        degraded_code = "HYBRID_RETRIEVAL_DEGRADED"

    return _finalize_retrieval(
        query=query,
        retrieved=retrieved,
        top_k=top_k,
        retrieval_mode=retrieval_mode,
        candidate_k=candidate_k,
        candidate_config=candidate_config,
        deadline=deadline,
        cancellation=cancellation,
        retrieval_degraded_code=degraded_code,
    )
