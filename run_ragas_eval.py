"""
使用 RAGAS 对 CSV 评测集跑 Faithfulness / Answer relevancy / Context precision / Context recall。

答案与 `test_langsmith_eval.py` 一致：调用完整 Agent 流程 `chat_with_agent`；
检索上下文取自返回的 `rag_trace`（优先 expanded_retrieved_chunks，否则 retrieved_chunks）。

依赖：ragas、datasets、pandas、openai（已由 llm_factory 使用）。
用法：
  uv run python run_ragas_eval.py --csv "RAG DATA_INTRO - Copy of test.csv" --limit 5

需要：.env 中 ARK_API_KEY、MODEL、BASE_URL；与 `backend/embedding.py` 相同的 EMBEDDING_MODEL / EMBEDDING_DEVICE（默认 BAAI/bge-m3 + cpu）；本地 Milvus 与已入库知识（与线上一致）。
"""

from __future__ import annotations

import argparse
import importlib
import os
import sys
from typing import Any
from uuid import uuid4

# backend 模块路径
_BACKEND = os.path.join(os.path.dirname(__file__), "backend")
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import pandas as pd
from dotenv import load_dotenv

from ragas import evaluate
from ragas.dataset_schema import EvaluationDataset, SingleTurnSample
from ragas.embeddings import HuggingFaceEmbeddings
# ragas.evaluate 要求 ragas.metrics.base.Metric 子类；metrics.collections 为新 BaseMetric，会 TypeError
from ragas.metrics._answer_relevance import AnswerRelevancy
from ragas.metrics._context_precision import ContextPrecision
from ragas.metrics._context_recall import ContextRecall
from ragas.metrics._faithfulness import Faithfulness

from ragas_llm_compat import build_ragas_instructor_llm

load_dotenv()

_agent = importlib.import_module("agent")
chat_with_agent = _agent.chat_with_agent


def _contexts_from_rag_trace(rag_trace: dict | None) -> list[str]:
    """与线上一致：使用 RAG 管线最终写入 trace 的片段列表。"""
    if not rag_trace:
        return ["(无 rag_trace)"]
    chunks = rag_trace.get("expanded_retrieved_chunks") or rag_trace.get("retrieved_chunks") or []
    if not isinstance(chunks, list):
        return ["(rag_trace 中无片段列表)"]
    texts: list[str] = []
    for c in chunks:
        if isinstance(c, dict):
            t = str(c.get("text") or "").strip()
            if t:
                texts.append(t)
    return texts if texts else ["(检索片段为空)"]


def _build_ragas_llm():
    try:
        return build_ragas_instructor_llm()
    except ValueError as e:
        raise SystemExit(str(e))


def _build_embeddings() -> HuggingFaceEmbeddings:
    # Answer Relevancy 需要向量；与线上检索共用同一套模型，避免再拉别的 HF 权重
    model = os.getenv("EMBEDDING_MODEL", "BAAI/bge-m3")
    device = os.getenv("EMBEDDING_DEVICE", "cpu")
    return HuggingFaceEmbeddings(
        model=model,
        device=device,
        normalize_embeddings=True,
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--csv",
        default="RAG DATA_INTRO - Copy of test.csv",
        help="评测 CSV 路径（首列为「问题」）",
    )
    parser.add_argument("--limit", type=int, default=0, help="只跑前 N 条；0 表示全部")
    parser.add_argument(
        "--skip-empty-ref",
        action="store_true",
        help="跳过「回答要点总结」为空的行",
    )
    args = parser.parse_args()

    csv_path = os.path.join(os.path.dirname(__file__), args.csv)
    if not os.path.isfile(csv_path):
        raise SystemExit(f"找不到 CSV: {csv_path}")

    df = pd.read_csv(csv_path, encoding="utf-8")
    col_q = "问题"
    col_ref = "回答要点总结"
    if col_q not in df.columns:
        raise SystemExit(f"CSV 缺少列「{col_q}」，实际列: {list(df.columns)}")

    if args.limit and args.limit > 0:
        df = df.head(args.limit)

    ragas_llm = _build_ragas_llm()
    embeddings = _build_embeddings()

    samples: list[SingleTurnSample] = []
    for _, row in df.iterrows():
        q = str(row.get(col_q, "") or "").strip()
        if not q:
            continue
        ref = str(row.get(col_ref, "") or "").strip()
        if not ref:
            ref = str(row.get("回答", "") or "").strip()
        if args.skip_empty_ref and not ref:
            continue

        session_id = f"ragas_eval_{uuid4().hex}"
        result = chat_with_agent(
            user_text=q,
            user_id="ragas_eval_user",
            session_id=session_id,
        )
        answer = ""
        rag_trace: dict[str, Any] = {}
        if isinstance(result, dict):
            answer = str(result.get("response", "") or "").strip()
            rag_trace = result.get("rag_trace") or {}
        else:
            answer = str(result).strip()

        # 与 test_langsmith_eval 一致：工具原始串不应作为最终答案
        if not answer or "Retrieved Chunks:" in answer:
            print(f"[跳过] 问题未得到有效 Agent 回复: {q[:48]}...")
            continue

        contexts = _contexts_from_rag_trace(rag_trace if isinstance(rag_trace, dict) else None)

        samples.append(
            SingleTurnSample(
                user_input=q,
                retrieved_contexts=contexts,
                response=answer,
                reference=ref,
            )
        )

    if not samples:
        raise SystemExit("没有可评测的样本（检查 CSV 与 --skip-empty-ref）")

    dataset = EvaluationDataset(samples=samples)

    metrics = [
        Faithfulness(llm=ragas_llm),
        AnswerRelevancy(llm=ragas_llm, embeddings=embeddings),
        ContextPrecision(llm=ragas_llm),
        ContextRecall(llm=ragas_llm),
    ]

    result = evaluate(
        dataset,
        metrics=metrics,
        llm=ragas_llm,
        embeddings=embeddings,
        raise_exceptions=False,
    )
    print("\n=== RAGAS 指标均值 ===\n", result, sep="")
    try:
        import json

        print("\n=== 逐条分数 ===\n", json.dumps(result.scores, ensure_ascii=False, indent=2))
    except Exception:
        pass


if __name__ == "__main__":
    main()
