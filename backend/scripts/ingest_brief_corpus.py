#!/usr/bin/env python3
"""
一键写入 brief 向量库：扫描下方硬编码目录中的文档，分块后写入 Milvus + parent_chunks + BM25。

用法（在仓库根目录）：
  uv run python backend/scripts/ingest_brief_corpus.py

依赖：Milvus 已启动、.env 已配置；目录不存在时会跳过并提示。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# backend/scripts/*.py -> 仓库根目录
SCRIPTS_DIR = Path(__file__).resolve().parent
BACKEND_DIR = SCRIPTS_DIR.parent
ROOT = BACKEND_DIR.parent
sys.path.insert(0, str(BACKEND_DIR))
os.chdir(BACKEND_DIR)

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

# =============================================================================
# 写死：要入库的目录（相对项目根目录 data/）
# =============================================================================
INGEST_DIRS: list[Path] = [
    ROOT / "data" / "abstract and mini golden paper_quick",
]

KB_TIER = "brief"

# 支持的扩展名（与 api 上传一致）
SUFFIXES = {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".html", ".htm"}

# =============================================================================


def _virtual_filename(file_path: Path) -> str:
    """用相对 data 的路径作为 filename，避免不同子目录同名冲突。"""
    try:
        rel = file_path.resolve().relative_to((ROOT / "data").resolve())
    except ValueError:
        rel = file_path.name
    return str(rel).replace("\\", "/")


def _remove_old_vectors(filename: str, milvus_manager, embedding_service) -> None:
    """与 api.upload 一致：先扣 BM25，再删 Milvus。"""
    try:
        rows = milvus_manager.query_all(
            filter_expr=f'filename == "{filename}"',
            output_fields=["text"],
            kb_tier=KB_TIER,
        )
        texts = [r.get("text") or "" for r in rows]
        if texts:
            embedding_service.increment_remove_documents(texts)
    except Exception as e:
        print(f"  [warn] BM25 扣减跳过: {e}")
    try:
        milvus_manager.delete(f'filename == "{filename}"', kb_tier=KB_TIER)
    except Exception as e:
        print(f"  [warn] Milvus 删除旧数据: {e}")


def _collect_files() -> list[Path]:
    out: list[Path] = []
    for d in INGEST_DIRS:
        if not d.is_dir():
            print(f"[跳过] 目录不存在: {d}")
            continue
        for p in sorted(d.rglob("*")):
            if p.is_file() and p.suffix.lower() in SUFFIXES:
                out.append(p)
    return out


def main() -> int:
    from document_loader import DocumentLoader
    from embedding import embedding_service
    from milvus_client import MilvusManager
    from milvus_writer import MilvusWriter
    from parent_chunk_store import ParentChunkStore

    files = _collect_files()
    if not files:
        print("未找到可入库文件。请确认目录存在且包含 PDF/Word/Excel/HTML：")
        for d in INGEST_DIRS:
            print(f"  - {d}")
        return 1

    loader = DocumentLoader()
    parent_chunk_store = ParentChunkStore()
    milvus_manager = MilvusManager()
    writer = MilvusWriter(embedding_service=embedding_service, milvus_manager=milvus_manager)

    milvus_manager.init_collection(kb_tier=KB_TIER)

    total_leaf = 0
    for i, file_path in enumerate(files, 1):
        vname = _virtual_filename(file_path)
        print(f"[{i}/{len(files)}] {vname}")

        _remove_old_vectors(vname, milvus_manager, embedding_service)
        try:
            parent_chunk_store.delete_by_filename(vname, kb_tier=KB_TIER)
        except Exception as e:
            print(f"  [warn] parent_chunks 删除: {e}")

        # 第二参数作为 Milvus filename / chunk_id 前缀，使用相对 data 的路径避免重名
        new_docs = loader.load_document(str(file_path), vname)
        if not new_docs:
            print("  -> 无分块，跳过")
            continue

        parent_docs = [d for d in new_docs if int(d.get("chunk_level", 0) or 0) in (1, 2)]
        leaf_docs = [d for d in new_docs if int(d.get("chunk_level", 0) or 0) == 3]
        if not leaf_docs:
            print("  -> 无叶子分块，跳过")
            continue

        parent_chunk_store.upsert_documents(parent_docs, kb_tier=KB_TIER)
        writer.write_documents(leaf_docs, kb_tier=KB_TIER)
        total_leaf += len(leaf_docs)
        print(f"  -> 叶子 {len(leaf_docs)} 条，父块 {len(parent_docs)} 条")

    print(f"\n完成。共写入叶子向量 {total_leaf} 条（{KB_TIER}）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
