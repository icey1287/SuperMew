#!/usr/bin/env python3
"""
将目录 CN_ZLFZYJ_NPC_md 下所有 .md 分块后写入 Milvus **brief** 集合，并更新 parent_chunks + BM25。

默认扫描：<项目根>/data/abstract and mini golden paper_quick/CN_ZLFZYJ_NPC_md（可递归子目录）。

用法（在仓库根目录）：
  uv run python backend/scripts/ingest_cn_zlfzyj_npc_md_brief.py
  uv run python backend/scripts/ingest_cn_zlfzyj_npc_md_brief.py --dir "/path/to/CN_ZLFZYJ_NPC_md"

依赖：Milvus、.env；需已支持 .md（见 document_loader.DocumentLoader）。
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

KB_TIER = "brief"
DEFAULT_REL_DIR = Path("data") / "abstract and mini golden paper_quick" / "CN_ZLFZYJ_NPC_md"
SUFFIXES = {".md"}


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="将 CN_ZLFZYJ_NPC_md 下全部 .md 写入 brief 向量库")
    p.add_argument(
        "--dir",
        type=Path,
        default=None,
        help="Markdown 根目录（默认见脚本内 DEFAULT_REL_DIR）",
    )
    return p.parse_args()


def _ingest_root(args: argparse.Namespace) -> Path:
    if args.dir is not None:
        return args.dir.expanduser().resolve()
    return (ROOT / DEFAULT_REL_DIR).resolve()


def _virtual_filename(file_path: Path, data_root: Path) -> str:
    """与 api 一致：相对 data 的路径作 filename，避免重名。"""
    try:
        rel = file_path.resolve().relative_to(data_root.resolve())
    except ValueError:
        try:
            rel = file_path.resolve().relative_to(ROOT.resolve())
        except ValueError:
            rel = Path(file_path.name)
    return str(rel).replace("\\", "/")


def _collect_files(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    out: list[Path] = []
    for p in sorted(root.rglob("*")):
        if p.is_file() and p.suffix.lower() in SUFFIXES:
            out.append(p)
    return out


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
    args = _parse_args()
    ingest_root = _ingest_root(args)
    data_root = (ROOT / "data").resolve()

    files = _collect_files(ingest_root)
    if not files:
        print(f"未找到 .md 文件。请确认目录存在且非空：\n  {ingest_root}")
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

    print(f"根目录: {ingest_root}\n共 {len(files)} 个 .md，写入 {KB_TIER} …\n")

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
