#!/usr/bin/env python3
"""
将 Milvus `embeddings_collection_detailed`（或 .env 中 MILVUS_COLLECTION_DETAILED）
中的全部向量行复制到 `embeddings_collection_brief`（或 MILVUS_COLLECTION_BRIEF）。

去重规则：与 brief 中已有行按「逻辑主键」比对，已存在则跳过。
  - 优先使用 chunk_id（非空）
  - 否则使用 filename + page_number + chunk_idx

注意：
  - 不修改 auto_id：brief 中新行会分配新 id。
  - 不调用 embedding_service.increment_add_documents；若你依赖 BM25 持久化统计与 Milvus 完全一致，
    需自行评估（detailed 入库时已计入的文本不应再重复 increment）。
  - 两集合 schema / dense 维度需一致（与现有 init_collection 一致）。

用法（仓库根目录）：
  uv run python backend/scripts/copy_milvus_detailed_to_brief.py
  uv run python backend/scripts/copy_milvus_detailed_to_brief.py --dry-run
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

SCRIPTS_DIR = Path(__file__).resolve().parent
BACKEND_DIR = SCRIPTS_DIR.parent
ROOT = BACKEND_DIR.parent
sys.path.insert(0, str(BACKEND_DIR))
os.chdir(BACKEND_DIR)

from dotenv import load_dotenv
from pymilvus.orm.constants import UNLIMITED

load_dotenv(ROOT / ".env")

OUTPUT_FIELDS = [
    "dense_embedding",
    "sparse_embedding",
    "text",
    "filename",
    "file_type",
    "file_path",
    "page_number",
    "chunk_idx",
    "chunk_id",
    "parent_chunk_id",
    "root_chunk_id",
    "chunk_level",
    "meta",
]


def _dedupe_key(row: dict) -> str:
    cid = (row.get("chunk_id") or "").strip()
    if cid:
        return f"cid:{cid}"
    fn = str(row.get("filename") or "")
    pn = int(row.get("page_number") or 0)
    ci = int(row.get("chunk_idx") or 0)
    return f"legacy:{fn}|{pn}|{ci}"


def _normalize_sparse(sp: Any) -> dict:
    if sp is None:
        return {}
    if isinstance(sp, dict):
        out: dict[int, float] = {}
        for k, v in sp.items():
            try:
                ik = int(k) if not isinstance(k, int) else k
                out[ik] = float(v)
            except (TypeError, ValueError):
                continue
        return out
    if hasattr(sp, "data") and isinstance(getattr(sp, "data"), dict):
        return _normalize_sparse(getattr(sp, "data"))
    try:
        return dict(sp)  # type: ignore[arg-type]
    except Exception:
        return {}


def _normalize_dense(d: Any) -> list[float]:
    if d is None:
        return []
    if isinstance(d, list):
        return [float(x) for x in d]
    try:
        return [float(x) for x in list(d)]
    except Exception:
        return []


def _normalize_meta(meta: Any) -> str | None:
    if meta is None or meta == "":
        return None
    if isinstance(meta, str):
        return meta
    if isinstance(meta, dict):
        return json.dumps(meta, ensure_ascii=False) if meta else None
    return str(meta)


def _row_to_insert(row: dict) -> dict[str, Any]:
    out: dict[str, Any] = {
        "dense_embedding": _normalize_dense(row.get("dense_embedding")),
        "sparse_embedding": _normalize_sparse(row.get("sparse_embedding")),
        "text": str(row.get("text") or "")[:2400],
        "filename": str(row.get("filename") or "")[:255],
        "file_type": str(row.get("file_type") or "")[:50],
        "file_path": str(row.get("file_path") or "")[:1024],
        "page_number": int(row.get("page_number") or 0),
        "chunk_idx": int(row.get("chunk_idx") or 0),
        "chunk_id": str(row.get("chunk_id") or "")[:512],
        "parent_chunk_id": str(row.get("parent_chunk_id") or "")[:512],
        "root_chunk_id": str(row.get("root_chunk_id") or "")[:512],
        "chunk_level": int(row.get("chunk_level") or 0),
    }
    meta = _normalize_meta(row.get("meta"))
    if meta is not None:
        out["meta"] = meta
    return out


def _load_existing_keys(mm) -> set[str]:
    if not mm.has_collection(kb_tier="brief"):
        return set()
    rows = mm.query_all(
        filter_expr="id > 0",
        output_fields=["chunk_id", "filename", "page_number", "chunk_idx"],
        kb_tier="brief",
    )
    keys: set[str] = set()
    for r in rows:
        if not isinstance(r, dict):
            continue
        keys.add(_dedupe_key(r))
    return keys


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="只统计不写 brief")
    parser.add_argument("--batch-read", type=int, default=2000, help="query_iterator 每批条数")
    parser.add_argument("--batch-insert", type=int, default=200, help="insert 每批条数")
    args = parser.parse_args()

    from milvus_client import MilvusManager, QUERY_MAX_LIMIT

    mm = MilvusManager()
    if not mm.has_collection(kb_tier="detailed"):
        print(f"源集合不存在: {mm.collection_detailed}")
        return 1

    mm.init_collection(kb_tier="brief")

    existing = _load_existing_keys(mm)
    print(f"brief 已有逻辑键数量: {len(existing)}")
    print(f"源: {mm.collection_detailed} -> 目标: {mm.collection_brief}")

    mm._ensure_connection(kb_tier="detailed")
    iterator = mm.client.query_iterator(
        collection_name=mm.collection_detailed,
        filter="id > 0",
        output_fields=OUTPUT_FIELDS,
        batch_size=min(args.batch_read, QUERY_MAX_LIMIT),
        limit=UNLIMITED,
    )

    total_src = 0
    skipped = 0
    inserted = 0
    pending: list[dict[str, Any]] = []

    def flush_batch() -> None:
        nonlocal inserted, pending
        if not pending:
            return
        if args.dry_run:
            inserted += len(pending)
        else:
            mm.insert(pending, kb_tier="brief")
            inserted += len(pending)
        pending = []

    try:
        while True:
            batch = iterator.next()
            if not batch:
                break
            for row in batch:
                if not isinstance(row, dict):
                    continue
                total_src += 1
                k = _dedupe_key(row)
                if k in existing:
                    skipped += 1
                    continue
                existing.add(k)
                pending.append(_row_to_insert(row))
                if len(pending) >= args.batch_insert:
                    flush_batch()
    finally:
        iterator.close()

    flush_batch()

    print(
        f"\n完成。源扫描行数(含重复键内多条): {total_src}；"
        f"跳过(已在 brief): {skipped}；"
        f"新增 brief 条数: {inserted}" + ("（dry-run，未实际 insert）" if args.dry_run else "")
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
