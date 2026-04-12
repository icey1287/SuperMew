#!/usr/bin/env python3
"""
Graph RAG 冒烟：Dense+Sparse RRF（Milvus）+ Neo4j 图谱合并。
从仓库根目录执行：uv run python backend/scripts/test_graph_rag_flow.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
BACKEND_DIR = SCRIPTS_DIR.parent
ROOT = BACKEND_DIR.parent
sys.path.insert(0, str(BACKEND_DIR))
os.chdir(BACKEND_DIR)

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")


def main() -> int:
    from tools import set_rag_config

    # fast：跳过 Rerank API，减少对外部服务的依赖
    set_rag_config({"think_mode": "fast"})

    from rag_utils import retrieve_documents

    # 尽量同时触发：向量库 + 实体（依赖 diseases.txt / drugs.txt / aliases.json）
    query = "糖尿病 二甲双胍 治疗与临床"
    print("Query:", query)
    print("NEO4J_URI:", os.getenv("NEO4J_URI", "(unset)"))
    print("MILVUS:", os.getenv("MILVUS_HOST"), os.getenv("MILVUS_PORT"))
    print("---")

    out = retrieve_documents(query, top_k=5, entity_query=query)
    docs = out.get("docs") or []
    meta = out.get("meta") or {}
    merged = (out.get("merged_context") or "").strip()

    print("retrieval_mode:", meta.get("retrieval_mode"))
    print("docs_count:", len(docs))
    print("dense_count:", meta.get("dense_count"), "sparse_count:", meta.get("sparse_count"))
    print("graph_kb_applied:", meta.get("graph_kb_applied"))
    print("graph_entities:", meta.get("graph_entities"))
    print("graph_error:", meta.get("graph_error"))
    print("--- merged_context (前 1200 字) ---")
    print(merged[:1200] if merged else "(empty)")

    ok_vector = len(docs) > 0 or meta.get("retrieval_mode") == "graph_only"
    ok_graph_meta = meta.get("graph_error") is None
    if not ok_vector:
        print("\n[WARN] 未召回向量片段；请确认 Milvus 有数据且集合可检索。")
    if meta.get("graph_kb_applied") is False and not meta.get("graph_error"):
        print("\n[INFO] 图谱未命中：可能 query 未匹配实体，或图中无相关边/文献。")
    if meta.get("graph_error"):
        print("\n[ERR] Neo4j:", meta.get("graph_error"))

    return 0 if (len(merged) > 0 or len(docs) > 0) else 1


if __name__ == "__main__":
    raise SystemExit(main())
