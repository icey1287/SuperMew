from collections import defaultdict
from typing import List, Tuple, Dict, Any
import os
import json
import requests
from dotenv import load_dotenv

from milvus_client import MilvusManager
from embedding import EmbeddingService
from parent_chunk_store import ParentChunkStore
from graph_retriever import GraphRetriever
from langchain.chat_models import init_chat_model
from tools import emit_rag_step

load_dotenv()

ARK_API_KEY = os.getenv("ARK_API_KEY")
MODEL = os.getenv("MODEL")
BASE_URL = os.getenv("BASE_URL")
RERANK_MODEL = os.getenv("RERANK_MODEL")
RERANK_BINDING_HOST = os.getenv("RERANK_BINDING_HOST")
RERANK_API_KEY = os.getenv("RERANK_API_KEY")
AUTO_MERGE_ENABLED = os.getenv("AUTO_MERGE_ENABLED", "true").lower() != "false"
AUTO_MERGE_THRESHOLD = int(os.getenv("AUTO_MERGE_THRESHOLD", "2"))
LEAF_RETRIEVE_LEVEL = int(os.getenv("LEAF_RETRIEVE_LEVEL", "3"))

# 全局初始化检索依赖，避免反复构造
_embedding_service = EmbeddingService()
_milvus_manager = MilvusManager()
_parent_chunk_store = ParentChunkStore()

_stepback_model = None


def _get_rerank_endpoint() -> str:
    if not RERANK_BINDING_HOST:
        return ""
    host = RERANK_BINDING_HOST.strip().rstrip("/")
    return host if host.endswith("/v1/rerank") else f"{host}/v1/rerank"


def _merge_to_parent_level(docs: List[dict], threshold: int = 2) -> Tuple[List[dict], int]:
    groups: Dict[str, List[dict]] = defaultdict(list)
    for doc in docs:
        parent_id = (doc.get("parent_chunk_id") or "").strip()
        if parent_id:
            groups[parent_id].append(doc)

    merge_parent_ids = [parent_id for parent_id, children in groups.items() if len(children) >= threshold]
    if not merge_parent_ids:
        return docs, 0

    parent_docs = _parent_chunk_store.get_documents_by_ids(merge_parent_ids)
    parent_map = {item.get("chunk_id", ""): item for item in parent_docs if item.get("chunk_id")}

    merged_docs: List[dict] = []
    merged_count = 0
    for doc in docs:
        parent_id = (doc.get("parent_chunk_id") or "").strip()
        if not parent_id or parent_id not in parent_map:
            merged_docs.append(doc)
            continue
        parent_doc = dict(parent_map[parent_id])
        score = doc.get("score")
        if score is not None:
            parent_doc["score"] = max(float(parent_doc.get("score", score)), float(score))
        parent_doc["merged_from_children"] = True
        parent_doc["merged_child_count"] = len(groups[parent_id])
        merged_docs.append(parent_doc)
        merged_count += 1

    deduped: List[dict] = []
    seen = set()
    for item in merged_docs:
        key = item.get("chunk_id") or (item.get("filename"), item.get("page_number"), item.get("text"))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)

    return deduped, merged_count


def _auto_merge_documents(docs: List[dict], top_k: int) -> Tuple[List[dict], Dict[str, Any]]:
    if not AUTO_MERGE_ENABLED or not docs:
        return docs[:top_k], {
            "auto_merge_enabled": AUTO_MERGE_ENABLED,
            "auto_merge_applied": False,
            "auto_merge_threshold": AUTO_MERGE_THRESHOLD,
            "auto_merge_replaced_chunks": 0,
            "auto_merge_steps": 0,
        }

    # 两段自动合并：L3->L2，再 L2->L1。
    merged_docs, merged_count_l3_l2 = _merge_to_parent_level(docs, threshold=AUTO_MERGE_THRESHOLD)
    merged_docs, merged_count_l2_l1 = _merge_to_parent_level(merged_docs, threshold=AUTO_MERGE_THRESHOLD)

    merged_docs.sort(key=lambda item: item.get("score", 0.0), reverse=True)
    merged_docs = merged_docs[:top_k]

    replaced_count = merged_count_l3_l2 + merged_count_l2_l1
    return merged_docs, {
        "auto_merge_enabled": AUTO_MERGE_ENABLED,
        "auto_merge_applied": replaced_count > 0,
        "auto_merge_threshold": AUTO_MERGE_THRESHOLD,
        "auto_merge_replaced_chunks": replaced_count,
        "auto_merge_steps": int(merged_count_l3_l2 > 0) + int(merged_count_l2_l1 > 0),
    }


def _rerank_documents(query: str, docs: List[dict], top_k: int) -> Tuple[List[dict], Dict[str, Any]]:
    docs_with_rank = [{**doc, "rrf_rank": i} for i, doc in enumerate(docs, 1)]
    meta: Dict[str, Any] = {
        "rerank_enabled": bool(RERANK_MODEL and RERANK_API_KEY and RERANK_BINDING_HOST),
        "rerank_applied": False,
        "rerank_model": RERANK_MODEL,
        "rerank_endpoint": _get_rerank_endpoint(),
        "rerank_error": None,
        "candidate_count": len(docs_with_rank),
    }
    if not docs_with_rank or not meta["rerank_enabled"]:
        return docs_with_rank[:top_k], meta

    payload = {
        "model": RERANK_MODEL,
        "query": query,
        "documents": [doc.get("text", "") for doc in docs_with_rank],
        "top_n": min(top_k, len(docs_with_rank)),
        "return_documents": False,
    }

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {RERANK_API_KEY}",
    }
    try:
        meta["rerank_applied"] = True
        response = requests.post(
            meta["rerank_endpoint"],
            headers=headers,
            json=payload,
            timeout=15,
        )
        if response.status_code >= 400:
            meta["rerank_error"] = f"HTTP {response.status_code}: {response.text}"
            return docs_with_rank[:top_k], meta

        items = response.json().get("results", [])
        reranked = []
        for item in items:
            idx = item.get("index")
            if isinstance(idx, int) and 0 <= idx < len(docs_with_rank):
                doc = dict(docs_with_rank[idx])
                score = item.get("relevance_score")
                if score is not None:
                    doc["rerank_score"] = score
                reranked.append(doc)

        if reranked:
            return reranked[:top_k], meta

        meta["rerank_error"] = "empty_rerank_results"
        return docs_with_rank[:top_k], meta
    except (requests.RequestException, json.JSONDecodeError, KeyError, ValueError, TypeError) as e:
        meta["rerank_error"] = str(e)
        return docs_with_rank[:top_k], meta


def _get_stepback_model():
    global _stepback_model
    if not ARK_API_KEY or not MODEL:
        return None
    if _stepback_model is None:
        _stepback_model = init_chat_model(
            model=MODEL,
            model_provider="openai",
            api_key=ARK_API_KEY,
            base_url=BASE_URL,
            temperature=0.2,
        )
    return _stepback_model


def _generate_step_back_question(query: str) -> str:
    model = _get_stepback_model()
    if not model:
        return ""
    prompt = (
        "请将用户的具体问题抽象成更高层次、更概括的‘退步问题’，"
        "用于探寻背后的通用原理或核心概念。只输出退步问题一句话，不要解释。\n"
        f"用户问题：{query}"
    )
    try:
        return (model.invoke(prompt).content or "").strip()
    except Exception:
        return ""


def _answer_step_back_question(step_back_question: str) -> str:
    model = _get_stepback_model()
    if not model or not step_back_question:
        return ""
    prompt = (
        "请简要回答以下退步问题，提供通用原理/背景知识，"
        "控制在120字以内。只输出答案，不要列出推理过程。\n"
        f"退步问题：{step_back_question}"
    )
    try:
        return (model.invoke(prompt).content or "").strip()
    except Exception:
        return ""


def generate_hypothetical_document(query: str) -> str:
    model = _get_stepback_model()
    if not model:
        return ""
    prompt = (
        "请基于用户问题生成一段‘假设性文档’，内容应像真实资料片段，"
        "用于帮助检索相关信息。文档可以包含合理推测，但需与问题语义相关。"
        "只输出文档正文，不要标题或解释。\n"
        f"用户问题：{query}"
    )
    try:
        return (model.invoke(prompt).content or "").strip()
    except Exception:
        return ""


def step_back_expand(query: str) -> dict:
    step_back_question = _generate_step_back_question(query)
    step_back_answer = _answer_step_back_question(step_back_question)
    if step_back_question or step_back_answer:
        expanded_query = (
            f"{query}\n\n"
            f"退步问题：{step_back_question}\n"
            f"退步问题答案：{step_back_answer}"
        )
    else:
        expanded_query = query
    return {
        "step_back_question": step_back_question,
        "step_back_answer": step_back_answer,
        "expanded_query": expanded_query,
    }


def _compute_rrf(*result_lists: List[List[dict]], k: int = 60, top_k: int = 5) -> List[dict]:
    """
    Python 层的 RRF 算法，将多个检索列表按名次融合。
    返回融合后的字典列表。
    """
    rrf_scores = defaultdict(float)
    doc_map = {}

    for doc_list in result_lists:
        if not doc_list:
            continue
        for rank, doc in enumerate(doc_list, 1):
            key = doc.get("chunk_id") or doc.get("text", "")
            if not key:
                continue
            rrf_scores[key] += 1.0 / (k + rank)
            if key not in doc_map:
                doc_map[key] = dict(doc)
    
    # 按照 RRF 分数排序
    sorted_keys = sorted(rrf_scores.keys(), key=lambda x: rrf_scores[x], reverse=True)
    
    fused_docs = []
    for rank, key in enumerate(sorted_keys[:top_k], 1):
        fused_doc = doc_map[key]
        fused_doc["rrf_rank"] = rank
        fused_doc["rrf_score"] = rrf_scores[key]
        fused_docs.append(fused_doc)
        
    return fused_docs


from tools import get_rag_config

def retrieve_documents(query: str, top_k: int = 5) -> Dict[str, Any]:
    config = get_rag_config()
    think_mode = config.get("think_mode", "normal")
    
    if think_mode == "fast":
        candidate_k = 10
        final_top_k = 10
        skip_rerank = True
    elif think_mode == "deep":
        candidate_k = 30
        final_top_k = 15
        skip_rerank = False
    else: # normal
        candidate_k = 15
        final_top_k = 10
        skip_rerank = False
        
    filter_expr = f"chunk_level == {LEAF_RETRIEVE_LEVEL}"
    
    dense_embedding = []
    sparse_embedding = {}
    try:
        dense_embeddings = _embedding_service.get_embeddings([query])
        if dense_embeddings:
            dense_embedding = dense_embeddings[0]
        sparse_embedding = _embedding_service.get_sparse_embedding(query)
    except Exception as e:
        print(f"Embedding generation error: {e}")

    # 1. 密集向量检索 (Dense)
    dense_results = []
    if dense_embedding:
        try:
            dense_results = _milvus_manager.dense_retrieve(
                dense_embedding=dense_embedding,
                top_k=candidate_k,
                filter_expr=filter_expr,
            )
            emit_rag_step("🎯", f"密集向量(Dense)召回", f"获取 {len(dense_results)} 个候选片段")
        except Exception as e:
            print(f"Dense retrieve error: {e}")

    # 2. 稀疏向量检索 (Sparse / BM25)
    sparse_results = []
    if sparse_embedding:
        try:
            sparse_results = _milvus_manager.sparse_retrieve(
                sparse_embedding=sparse_embedding,
                top_k=candidate_k,
                filter_expr=filter_expr,
            )
            emit_rag_step("🔤", f"关键字(Sparse)召回", f"获取 {len(sparse_results)} 个候选片段")
        except Exception as e:
            print(f"Sparse retrieve error: {e}")

    # 3. 图检索 (Graph)
    graph_results = []
    try:
        graph_results = _graph_retriever.retrieve(query, top_k=candidate_k)
        if graph_results:
            emit_rag_step("🕸️", f"知识图谱(Graph)召回", f"匹配到 {len(graph_results)} 条关联关系")
    except Exception as e:
        print(f"Graph retrieve error: {e}")

    # 本地 RRF 融合三路结果
    retrieved = _compute_rrf(dense_results, sparse_results, graph_results, k=60, top_k=candidate_k)
    emit_rag_step("🔀", "多路 RRF 混合重排", f"三路融合得出 Top {len(retrieved)} 候选")

    if not retrieved:
        # Fallback to failed status if absolutely nothing
        return {
            "docs": [],
            "meta": {
                "rerank_enabled": bool(RERANK_MODEL and RERANK_API_KEY and RERANK_BINDING_HOST),
                "rerank_applied": False,
                "rerank_model": RERANK_MODEL,
                "rerank_endpoint": _get_rerank_endpoint(),
                "rerank_error": "all_retrieve_failed",
                "retrieval_mode": "failed",
                "candidate_k": candidate_k,
                "leaf_retrieve_level": LEAF_RETRIEVE_LEVEL,
                "auto_merge_enabled": AUTO_MERGE_ENABLED,
                "auto_merge_applied": False,
                "auto_merge_threshold": AUTO_MERGE_THRESHOLD,
                "auto_merge_replaced_chunks": 0,
                "auto_merge_steps": 0,
                "candidate_count": 0,
            },
        }

    # 重新排序并使用自动合并 (Auto-merging) 与 Reranker
    try:
        if skip_rerank:
            for i, doc in enumerate(retrieved[:final_top_k], 1):
                doc["rerank_score"] = doc.get("rrf_score", 0.0)
            reranked = retrieved[:final_top_k]
            rerank_meta = {
                "rerank_enabled": False,
                "rerank_applied": False,
                "rerank_model": None,
                "rerank_endpoint": None,
                "rerank_error": "skipped_by_think_mode",
                "candidate_count": len(retrieved),
            }
        else:
            reranked, rerank_meta = _rerank_documents(query=query, docs=retrieved, top_k=final_top_k)
            
        merged_docs, merge_meta = _auto_merge_documents(docs=reranked, top_k=final_top_k)
        rerank_meta["retrieval_mode"] = f"hybrid_3way_rrf_{think_mode}"
        rerank_meta["candidate_k"] = candidate_k
        rerank_meta["leaf_retrieve_level"] = LEAF_RETRIEVE_LEVEL
        rerank_meta["dense_count"] = len(dense_results)
        rerank_meta["sparse_count"] = len(sparse_results)
        rerank_meta["graph_count"] = len(graph_results)
        rerank_meta.update(merge_meta)
        return {"docs": merged_docs, "meta": rerank_meta}
    except Exception as e:
        return {
            "docs": retrieved[:final_top_k],
            "meta": {
                "rerank_enabled": bool(RERANK_MODEL and RERANK_API_KEY and RERANK_BINDING_HOST),
                "rerank_applied": False,
                "rerank_model": RERANK_MODEL,
                "rerank_endpoint": _get_rerank_endpoint(),
                "rerank_error": f"process_error: {e}",
                "retrieval_mode": f"hybrid_3way_rrf_{think_mode}",
                "candidate_k": candidate_k,
                "leaf_retrieve_level": LEAF_RETRIEVE_LEVEL,
                "dense_count": len(dense_results),
                "sparse_count": len(sparse_results),
                "graph_count": len(graph_results),
                "auto_merge_enabled": AUTO_MERGE_ENABLED,
                "auto_merge_applied": False,
                "auto_merge_threshold": AUTO_MERGE_THRESHOLD,
                "auto_merge_replaced_chunks": 0,
                "auto_merge_steps": 0,
                "candidate_count": len(retrieved),
            },
        }
