#!/usr/bin/env python3
"""
将以下目录中的**支持格式**文件递归入库到 Milvus **brief** + parent_chunks + BM25：

  - <项目根>/data/CN_DXY
  - <项目根>/data/drive-download-20260418T072848Z-3-001

支持扩展名与 api / ingest_brief_corpus 一致，并包含 .md。

用法（在仓库根目录）：
  uv run python backend/scripts/ingest_cn_dxy_and_drive_download_brief.py

依赖：Milvus、.env；目录不存在时会跳过并提示。
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

KB_TIER = "brief"

INGEST_DIRS: list[Path] = [
    ROOT / "data" / "CN_DXY",
    ROOT / "data" / "drive-download-20260418T072848Z-3-001",
]

SUFFIXES = {
    ".pdf",
    ".doc",
    ".docx",
    ".xls",
    ".xlsx",
    ".html",
    ".htm",
    ".md",
}


def _virtual_filename(file_path: Path, data_root: Path) -> str:
    try:
        rel = file_path.resolve().relative_to(data_root.resolve())
    except ValueError:
        try:
            rel = file_path.resolve().relative_to(ROOT.resolve())
        except ValueError:
            rel = Path(file_path.name)
    return str(rel).replace("\\", "/")


def _collect_all_files() -> list[Path]:
    out: list[Path] = []
    for d in INGEST_DIRS:
        if not d.is_dir():
            print(f"[跳过] 目录不存在: {d}")
            continue
        for p in sorted(d.rglob("*")):
            if p.is_file() and p.suffix.lower() in SUFFIXES:
                out.append(p)
    return sorted(out, key=lambda x: str(x))


def _remove_old_vectors(filename: str, milvus_manager, embedding_service) -> None:
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


def main() -> int:
    data_root = (ROOT / "data").resolve()
    files = _collect_all_files()
    if not files:
        print("未找到可入库文件。请确认以下目录存在且含支持类型：")
        for d in INGEST_DIRS:
            print(f"  - {d}")
        print(f"支持后缀: {sorted(SUFFIXES)}")
        return 1

    from document_loader import DocumentLoader
    from embedding import embedding_service
    from milvus_client import MilvusManager
    from milvus_writer import MilvusWriter
    from parent_chunk_store import ParentChunkStore

    loader = DocumentLoader()
    parent_chunk_store = ParentChunkStore()
    milvus_manager = MilvusManager()
    writer = MilvusWriter(embedding_service=embedding_service, milvus_manager=milvus_manager)
    milvus_manager.init_collection(kb_tier=KB_TIER)

    print(f"共 {len(files)} 个文件 -> {KB_TIER}\n")

    total_leaf = 0
    for i, file_path in enumerate(files, 1):
        vname = _virtual_filename(file_path, data_root)
        print(f"[{i}/{len(files)}] {vname}")

        _remove_old_vectors(vname, milvus_manager, embedding_service)
        try:
            parent_chunk_store.delete_by_filename(vname, kb_tier=KB_TIER)
        except Exception as e:
            print(f"  [warn] parent_chunks 删除: {e}")

        try:
            new_docs = loader.load_document(str(file_path), vname)
        except Exception as e:
            print(f"  [错误] 跳过: {e}")
            continue
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
