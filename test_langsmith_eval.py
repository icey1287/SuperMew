from typing import Any, Optional
import csv
import importlib
import os
import sys
import threading
from uuid import uuid4

from dotenv import load_dotenv
from langsmith import evaluate

# 消融实验：关闭 Dense+Sparse 的 Python 层 RRF，改为 Dense 优先 + Sparse 去重拼接；
# 保持三级自动合并开启（须在导入 backend/agent → rag_utils 之前设置）
os.environ["HYBRID_RRF_ENABLED"] = "true"
os.environ["AUTO_MERGE_ENABLED"] = "true"

# 将 backend 路径添加到 sys.path，以便导入你的 Agent 模块
backend_path = os.path.join(os.path.dirname(__file__), "backend")
if backend_path not in sys.path:
    sys.path.append(backend_path)

chat_with_agent = importlib.import_module("agent").chat_with_agent
set_rag_config = importlib.import_module("tools").set_rag_config

load_dotenv()

# 每条样本召回 chunk 追加写入的 CSV（UTF-8 BOM，Excel 友好）
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__)))
CHUNKS_CSV_PATH = os.getenv(
    "LANGSMITH_EVAL_CHUNKS_CSV",
    os.path.join(_REPO_ROOT, "langsmith_eval_retrieved_chunks.csv"),
)
_CHUNKS_CSV_LOCK = threading.Lock()
_CHUNKS_CSV_FIELDS = [
    "session_id",
    "question",
    "chunk_rank",
    "chunk_id",
    "filename",
    "page_number",
    "chunk_level",
    "chunk_idx",
    "score",
    "text",
]


def _chunks_from_rag_trace(rag_trace: dict) -> list[dict]:
    if not rag_trace:
        return []
    chunks = rag_trace.get("expanded_retrieved_chunks") or rag_trace.get("retrieved_chunks") or []
    return chunks if isinstance(chunks, list) else []


def _score_from_chunk(ch: dict) -> str:
    for k in ("rerank_score", "score", "rrf_score"):
        v = ch.get(k)
        if v is not None and v != "":
            return str(v)
    return ""


def _append_chunks_csv(session_id: str, question: str, rag_trace: dict) -> None:
    """把本次问题对应的召回块追加到 CSV（与 LangSmith 评估并行时加锁）。"""
    chunks = _chunks_from_rag_trace(rag_trace if isinstance(rag_trace, dict) else {})
    parent = os.path.dirname(os.path.abspath(CHUNKS_CSV_PATH))
    if parent:
        os.makedirs(parent, exist_ok=True)

    rows: list[dict[str, Any]] = []
    if not chunks:
        rows.append(
            {
                "session_id": session_id,
                "question": question,
                "chunk_rank": 0,
                "chunk_id": "",
                "filename": "",
                "page_number": "",
                "chunk_level": "",
                "chunk_idx": "",
                "score": "",
                "text": "(无召回记录)",
            }
        )
    else:
        for rank, ch in enumerate(chunks, 1):
            if not isinstance(ch, dict):
                continue
            text = str(ch.get("text") or "")
            if len(text) > 32000:
                text = text[:32000] + "…(截断)"
            rows.append(
                {
                    "session_id": session_id,
                    "question": question,
                    "chunk_rank": rank,
                    "chunk_id": str(ch.get("chunk_id", "") or ""),
                    "filename": str(ch.get("filename", "") or ""),
                    "page_number": ch.get("page_number", ""),
                    "chunk_level": ch.get("chunk_level", ""),
                    "chunk_idx": ch.get("chunk_idx", ""),
                    "score": _score_from_chunk(ch),
                    "text": text,
                }
            )

    with _CHUNKS_CSV_LOCK:
        is_new = (not os.path.isfile(CHUNKS_CSV_PATH)) or (
            os.path.getsize(CHUNKS_CSV_PATH) == 0
        )
        with open(CHUNKS_CSV_PATH, "a", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=_CHUNKS_CSV_FIELDS, extrasaction="ignore")
            if is_new:
                w.writeheader()
            w.writerows(rows)


def _extract_answer(outputs: Any) -> str:
    if isinstance(outputs, dict):
        # 优先取真实最终回复字段
        answer = outputs.get("response") or outputs.get("answer") or outputs.get("output")
        return str(answer or "").strip()
    if hasattr(outputs, "outputs") and isinstance(outputs.outputs, dict):
        answer = (
            outputs.outputs.get("response")
            or outputs.outputs.get("answer")
            or outputs.outputs.get("output")
        )
        return str(answer or "").strip()
    return ""


def _extract_reference(reference_outputs: Optional[dict]) -> str:
    if not isinstance(reference_outputs, dict):
        return ""
    for key in ("response", "answer", "output", "expected_answer"):
        value = reference_outputs.get(key)
        if value:
            return str(value).strip()
    return ""

# 1. Select your dataset
dataset_name = "med"

# 2. Define an evaluator (评估最终答案，不评估检索块)
def custom_evaluator(run_outputs: dict, reference_outputs: dict) -> bool:
    answer = _extract_answer(run_outputs)
    if not answer:
        return False
    if "Retrieved Chunks:" in answer:
        return False

    reference = _extract_reference(reference_outputs)
    if not reference:
        return True

    # 有参考答案时，至少保证存在一定语义重合（使用字符集合重合率做轻量检查）
    answer_chars = {ch for ch in answer if not ch.isspace()}
    ref_chars = {ch for ch in reference if not ch.isspace()}
    if not answer_chars or not ref_chars:
        return False

    overlap = len(answer_chars & ref_chars) / max(1, len(ref_chars))
    return overlap >= 0.2

# 直接调用你现有的完整 Agent 流程作为评估对象
def target_function(inputs: dict) -> dict:
    question = inputs["question"]
    # 每条评估样本使用独立会话，避免上下文串扰
    session_id = f"langsmith_eval_{uuid4().hex}"
    # 消融实验：跳过文档相关性打分与查询重写，直接基于初次检索生成答案
    set_rag_config({"skip_grade_and_rewrite": True})
    result = chat_with_agent(
        user_text=question,
        user_id="langsmith_eval_user",
        session_id=session_id,
    )

    response_text = ""
    rag_trace = {}
    if isinstance(result, dict):
        response_text = str(result.get("response", "") or "")
        rag_trace = result.get("rag_trace", {}) or {}
    else:
        response_text = str(result)

    try:
        _append_chunks_csv(session_id, question, rag_trace)
    except Exception as e:
        print(f"[langsmith_eval] 写入召回 CSV 失败: {e}", file=sys.stderr)

    return {
        "response": response_text,
        "rag_trace": rag_trace,
    }

# 3. Run an evaluation
# For more info on evaluators, see: https://docs.langchain.com/langsmith/evaluation-concepts
evaluate(
    target_function,
    data=dataset_name,
    evaluators=[custom_evaluator],
    experiment_prefix="med experiment3"
)
