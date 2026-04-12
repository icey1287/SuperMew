"""本地检索 / 图谱冒烟（需 Milvus、Neo4j、embedding 可用）。"""
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

from rag_utils import _embedding_service, _get_medical_graph, _milvus_manager

query = "系统性轻链淀粉样变性的临床表现"
sparse_emb = _embedding_service.get_sparse_embedding(query)
print("Sparse embedding size:", len(sparse_emb))

try:
    res = _milvus_manager.sparse_retrieve(sparse_emb, top_k=5, filter_expr="chunk_level == 3")
    print("Sparse results count:", len(res))
except Exception as e:
    print("Sparse Error:", e)

gr = _get_medical_graph()
if gr:
    try:
        ctx = gr.build_context_for_llm(query, top_k=5)
        print("Neo4j graph context length:", len(ctx or ""))
    except Exception as e:
        print("Neo4j Error:", e)
else:
    print("Neo4j graph retriever unavailable (check NEO4J_PASSWORD / driver)")

long_query = "2023版中国系统性轻链淀粉样变性诊疗规范摘录：多数患者起病隐匿，早期缺乏特异性表现，超过60%的患者初诊时已累及2个及以上器官。肾脏受累最为常见，占比约70%~80%，主要表现为不同程度蛋白尿，近半数可达肾病综合征水平，仅不到20%的患者初诊时即出现血清肌酐升高，随疾病进展可逐渐出现肾功能不全、肾病综合征相关水肿。"
if gr:
    try:
        ctx3 = gr.build_context_for_llm(long_query, top_k=5)
        print("Long query Neo4j context length:", len(ctx3 or ""))
    except Exception as e:
        print("Long Neo4j Error:", e)
