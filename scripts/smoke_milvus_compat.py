"""Exercise the production Milvus schema against a real standalone server."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from uuid import uuid4

import pymilvus

from backend.indexing.milvus_client import MilvusSettings, MilvusStore
from backend.security.milvus_filters import and_filter, eq_filter, version_scope_filter


def _row(
    chunk_id: str,
    text: str,
    vector: list[float],
    index: int,
    *,
    tenant_id: str = "default",
    document_version_id: str = "version-smoke",
) -> dict:
    return {
        "dense_embedding": vector,
        "text": text,
        "filename": "smoke.txt",
        "file_type": "txt",
        "file_path": "smoke/smoke.txt",
        "page_number": 1,
        "chunk_idx": index,
        "chunk_id": chunk_id,
        "parent_chunk_id": "parent-smoke",
        "root_chunk_id": "root-smoke",
        "chunk_level": 3,
        "tenant_id": tenant_id,
        "knowledge_base_id": "kb-smoke",
        "document_id": "doc-smoke",
        "document_version_id": document_version_id,
        "section_id": f"section-{index}",
        "acl_tags": ["public"],
        "index_version": "index-smoke",
        "content_hash": ("a" if index == 0 else "b") * 64,
    }


def main() -> int:
    host = os.getenv("MILVUS_HOST", "127.0.0.1")
    port = os.getenv("MILVUS_PORT", "19530")
    collection = os.getenv(
        "MILVUS_SMOKE_COLLECTION",
        f"supermew_compat_smoke_{uuid4().hex[:12]}",
    )
    store = MilvusStore(
        MilvusSettings(
            host=host,
            port=port,
            collection_name=collection,
            uri=f"http://{host}:{port}",
            timeout=30.0,
        )
    )
    store.drop_collection()
    try:
        store.init_versioned_collection(dense_dim=4)
        inserted = store.insert(
            [
                _row(
                    "chunk-alpha",
                    "alpha architecture contract",
                    [1.0, 0.0, 0.0, 0.0],
                    0,
                ),
                _row(
                    "chunk-beta",
                    "beta unrelated material",
                    [0.0, 1.0, 0.0, 0.0],
                    1,
                ),
                _row(
                    "chunk-cross-tenant",
                    "alpha architecture contract cross tenant",
                    [1.0, 0.0, 0.0, 0.0],
                    2,
                    tenant_id="other-tenant",
                    document_version_id="other-version",
                ),
            ]
        )
        if not isinstance(inserted, Mapping) or inserted.get("insert_count") != 3:
            raise AssertionError(f"unexpected insert result: {inserted!r}")
        with store.session() as client:
            client.flush(collection_name=collection)
            client.load_collection(collection_name=collection)

        verification = store.verify_version(
            tenant_id="default",
            knowledge_base_id="kb-smoke",
            document_id="doc-smoke",
            document_version_id="version-smoke",
            index_version="index-smoke",
            expected_chunk_ids=["chunk-alpha", "chunk-beta"],
        )
        if not verification.exact:
            raise AssertionError(verification)

        retrieval_filter = and_filter(
            version_scope_filter(
                tenant_id="default",
                knowledge_base_id="kb-smoke",
                document_id="doc-smoke",
                document_version_ids=["version-smoke"],
                index_version="index-smoke",
            ),
            eq_filter("chunk_level", 3),
        )
        dense = store.dense_retrieve(
            [1.0, 0.0, 0.0, 0.0],
            top_k=2,
            filter_expr=retrieval_filter,
        )
        hybrid = store.hybrid_retrieve(
            [1.0, 0.0, 0.0, 0.0],
            "alpha architecture",
            top_k=2,
            filter_expr=retrieval_filter,
        )
        if not dense or dense[0]["chunk_id"] != "chunk-alpha":
            raise AssertionError(f"unexpected dense result: {dense!r}")
        if not hybrid or hybrid[0]["chunk_id"] != "chunk-alpha":
            raise AssertionError(f"unexpected hybrid result: {hybrid!r}")

        deleted = store.delete_by_version(
            tenant_id="default",
            knowledge_base_id="kb-smoke",
            document_id="doc-smoke",
            document_version_id="version-smoke",
            index_version="index-smoke",
        )
        with store.session() as client:
            client.flush(collection_name=collection)
            client.release_collection(collection_name=collection)
            client.load_collection(collection_name=collection)
        remaining = store.query_version_chunk_ids(
            tenant_id="default",
            knowledge_base_id="kb-smoke",
            document_id="doc-smoke",
            document_version_id="version-smoke",
            index_version="index-smoke",
        )
        cross_tenant = store.query_version_chunk_ids(
            tenant_id="other-tenant",
            knowledge_base_id="kb-smoke",
            document_id="doc-smoke",
            document_version_id="other-version",
            index_version="index-smoke",
        )
        if deleted != 2 or remaining or cross_tenant != ["chunk-cross-tenant"]:
            raise AssertionError(
                {
                    "cross_tenant": cross_tenant,
                    "deleted": deleted,
                    "remaining": remaining,
                }
            )

        print(
            json.dumps(
                {
                    "client": f"pymilvus:{pymilvus.__version__}",
                    "deleted": deleted,
                    "dense_top": dense[0]["chunk_id"],
                    "hybrid_top": hybrid[0]["chunk_id"],
                    "insert_count": inserted["insert_count"],
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 0
    finally:
        store.drop_collection()


if __name__ == "__main__":
    raise SystemExit(main())
