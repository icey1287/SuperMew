#!/usr/bin/env python3
"""
按 file_type 批量删除 Milvus 中的向量，并同步扣减 BM25 统计、清理 parent_chunks.json 中对应 filename。

用法（在仓库根目录，backend 为 cwd）：
  uv run python backend/scripts/delete_milvus_by_file_type.py --file-type Excel
  uv run python backend/scripts/delete_milvus_by_file_type.py --file-type Excel --kb-tier brief
  uv run python backend/scripts/delete_milvus_by_file_type.py --file-type Excel --both-tiers

说明：
  - 普通上传的 Excel 文档在库中为 file_type == \"Excel\"（见 document_loader）。
  - ingest_excel_literature.py 写入的是 \"ExcelLiterature\"，删除 Excel 不会动到它。
"""

from __future__ import annotations

import argparse
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


def run_delete(file_type: str, kb_tier: str) -> int:
    from embedding import embedding_service
    from milvus_client import MilvusManager
    from parent_chunk_store import ParentChunkStore

    mm = MilvusManager()
    pcs = ParentChunkStore()

    # Milvus 过滤表达式：字符串用双引号
    safe = (file_type or "").replace("\\", "\\\\").replace('"', '\\"')
    filter_expr = f'file_type == "{safe}"'

    if not mm.has_collection(kb_tier=kb_tier):
        print(f"[{kb_tier}] 集合不存在，跳过")
        return 0

    rows = mm.query_all(
        filter_expr=filter_expr,
        output_fields=["text", "filename"],
        kb_tier=kb_tier,
    )
    if not rows:
        print(f"[{kb_tier}] 无 file_type == {file_type!r} 的记录")
        return 0

    texts = [r.get("text") or "" for r in rows]
    embedding_service.increment_remove_documents(texts)

    filenames = sorted({(r.get("filename") or "").strip() for r in rows if (r.get("filename") or "").strip()})

    result = mm.delete(filter_expr, kb_tier=kb_tier)
    deleted = 0
    if isinstance(result, dict):
        deleted = int(result.get("delete_count") or result.get("delete_cnt") or 0)
    else:
        deleted = len(rows)

    parent_removed = 0
    for fn in filenames:
        parent_removed += pcs.delete_by_filename(fn, kb_tier=kb_tier)

    print(
        f"[{kb_tier}] Milvus 删除约 {deleted} 条；"
        f"涉及文件 {len(filenames)} 个；parent_chunk 清理 {parent_removed} 条"
    )
    return deleted


def main():
    ap = argparse.ArgumentParser(description="按 file_type 删除 Milvus 向量并同步 BM25 / parent_chunk")
    ap.add_argument(
        "--file-type",
        default="Excel",
        help='要匹配的 file_type（默认 Excel，与 document_loader 中 xlsx 一致）',
    )
    ap.add_argument(
        "--kb-tier",
        default="brief",
        choices=("brief", "detailed"),
        help="只处理该集合（默认 brief）",
    )
    ap.add_argument(
        "--both-tiers",
        action="store_true",
        help="brief 与 detailed 各执行一遍",
    )
    args = ap.parse_args()
    ft = args.file_type.strip()
    if not ft:
        raise SystemExit("file-type 不能为空")

    if args.both_tiers:
        run_delete(ft, "brief")
        run_delete(ft, "detailed")
    else:
        run_delete(ft, args.kb_tier)


if __name__ == "__main__":
    main()
