from __future__ import annotations

import asyncio
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import backend.api.routes.documents as documents


async def _ticks_while(awaitable) -> tuple[object, int]:
    stop = asyncio.Event()
    ticks = 0

    async def ticker() -> None:
        nonlocal ticks
        while not stop.is_set():
            ticks += 1
            await asyncio.sleep(0.001)

    ticker_task = asyncio.create_task(ticker())
    try:
        return await awaitable, ticks
    finally:
        stop.set()
        await ticker_task


class DocumentEndpointIoTests(unittest.IsolatedAsyncioTestCase):
    async def test_list_documents_runs_sync_milvus_work_off_event_loop(self):
        def slow_list():
            time.sleep(0.04)
            return []

        with patch.object(documents, "_list_documents_sync", side_effect=slow_list):
            response, ticks = await _ticks_while(documents.list_documents(None))

        self.assertEqual([], response.documents)
        self.assertGreater(ticks, 3)

    async def test_document_list_reads_catalog_without_scanning_milvus(self):
        now = SimpleNamespace(isoformat=lambda: "2026-07-15T12:00:00")
        version = SimpleNamespace(
            id="version-1",
            media_type="application/pdf",
            chunk_count=5,
            parent_chunk_count=2,
            version_number=1,
            size_bytes=123,
            published_at=now,
            created_at=now,
            build_fingerprint="a" * 64,
            parser_version="parser-v1",
            chunker_version="chunker-v1",
            embedding_model="embedding-v1",
            index_version="catalog-v1",
            vector_collection="embeddings_collection_catalog_v1",
            storage_layout="versioned",
            error_code=None,
        )
        record = SimpleNamespace(
            id="doc-1",
            canonical_name="manual.pdf",
            current_version=version,
            pending_version=None,
            status="ready",
        )

        with (
            patch.object(
                documents.document_catalog,
                "list_documents",
                side_effect=[[record], []],
            ) as list_catalog,
            patch(
                "backend.api.resources.milvus_manager.query",
                side_effect=AssertionError("document list must not query Milvus"),
            ) as query_milvus,
        ):
            response = await documents.list_documents(None)

        self.assertEqual("doc-1", response.documents[0].document_id)
        self.assertEqual(5, response.documents[0].chunk_count)
        list_catalog.assert_called_once()
        query_milvus.assert_not_called()

    async def test_delete_document_runs_storage_cleanup_off_event_loop(self):
        def slow_delete(filename, *, owner_id):
            self.assertEqual("manual.pdf", filename)
            self.assertEqual(7, owner_id)
            time.sleep(0.04)
            return SimpleNamespace(
                document_id="doc-1",
                chunks_deleted=3,
                cleanup_pending=False,
                cleanup_error_code=None,
            )

        with patch.object(
            documents,
            "delete_document_transactionally",
            side_effect=slow_delete,
        ):
            response, ticks = await _ticks_while(
                documents.delete_document("manual.pdf", SimpleNamespace(id=7))
            )

        self.assertEqual(3, response.chunks_deleted)
        self.assertGreater(ticks, 3)

    async def test_direct_upload_runs_parse_and_index_work_off_event_loop(self):
        stored = SimpleNamespace(
            original_name="manual.pdf",
            path=Path("/tmp/manual.pdf"),
        )

        def slow_store(stored_upload, owner_id):
            self.assertIs(stored, stored_upload)
            self.assertEqual(7, owner_id)
            time.sleep(0.04)
            return SimpleNamespace(
                document=SimpleNamespace(id="doc-1", canonical_name="manual.pdf"),
                version=SimpleNamespace(id="version-1", version_number=1),
                parent_chunk_count=2,
                vector_chunk_count=5,
                published=True,
                reused_current=False,
            )

        with (
            patch.object(documents, "ensure_upload_dir"),
            patch.object(
                documents,
                "store_upload",
                new=AsyncMock(return_value=stored),
            ),
            patch.object(
                documents,
                "_store_document_sync",
                side_effect=slow_store,
            ),
        ):
            response, ticks = await _ticks_while(
                documents.upload_document(SimpleNamespace(), SimpleNamespace(id=7))
            )

        self.assertEqual(5, response.chunks_processed)
        self.assertGreater(ticks, 3)

    async def test_delete_cleanup_failure_preserves_logical_retirement_status(self):
        job = documents.delete_job_manager.create_job(
            "manual.pdf",
            steps=documents.DELETE_STEPS,
            current_step="prepare",
            message="等待撤销",
            completion_step="parent_store",
        )
        outcome = SimpleNamespace(
            chunks_deleted=0,
            cleanup_pending=True,
            cleanup_step="milvus",
            cleanup_error_code="VECTOR_STORE_UNAVAILABLE",
        )

        with patch.object(
            documents,
            "delete_document_transactionally",
            return_value=outcome,
        ):
            await asyncio.to_thread(
                documents._process_delete_job,
                job["job_id"],
                "manual.pdf",
                7,
            )

        saved = documents.delete_job_manager.get_job(job["job_id"])
        self.assertEqual("cleanup_pending", saved["status"])
        self.assertEqual("completed", saved["steps"][0]["status"])
        self.assertEqual("CLEANUP_PENDING", saved["error"])


if __name__ == "__main__":
    unittest.main()
