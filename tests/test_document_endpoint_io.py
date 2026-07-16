from __future__ import annotations

import asyncio
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import backend.api.routes.documents as documents
from backend.core.errors import AppError, ErrorCode


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
    async def test_durable_upload_and_delete_routes_return_accepted(self):
        status_by_path = {
            route.path: route.status_code
            for route in documents.router.routes
            if hasattr(route, "status_code")
        }

        self.assertEqual(202, status_by_path["/documents/upload/async"])
        self.assertEqual(202, status_by_path["/documents/upload"])
        self.assertEqual(202, status_by_path["/documents/delete/async/{filename}"])
        self.assertEqual(202, status_by_path["/documents/{filename}"])

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
                retirement_job_id="retire-1",
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
        self.assertEqual("retire-1", response.job_id)
        self.assertGreater(ticks, 3)

    async def test_compat_delete_fails_closed_without_a_durable_job_id(self):
        outcome = SimpleNamespace(
            document_id="doc-1",
            retirement_job_id=None,
            chunks_deleted=0,
            cleanup_pending=False,
            cleanup_error_code=None,
        )
        with patch.object(
            documents,
            "delete_document_transactionally",
            return_value=outcome,
        ):
            with self.assertRaises(AppError) as caught:
                await documents.delete_document(
                    "manual.pdf",
                    SimpleNamespace(id=7),
                )

        self.assertEqual(ErrorCode.STORAGE_UNAVAILABLE, caught.exception.code)

    async def test_deprecated_direct_upload_only_submits_off_event_loop(self):
        stored = SimpleNamespace(
            original_name="manual.pdf",
            path=Path("/tmp/manual.pdf"),
        )

        def slow_submit(stored_upload, owner_id):
            self.assertIs(stored, stored_upload)
            self.assertEqual(7, owner_id)
            time.sleep(0.04)
            return SimpleNamespace(
                job=SimpleNamespace(id="job-1", status="pending"),
                document=SimpleNamespace(id="doc-1", canonical_name="manual.pdf"),
                version=SimpleNamespace(id="version-1", version_number=1),
                already_current=False,
            )

        with (
            patch.object(documents, "ensure_upload_dir"),
            patch.object(
                documents,
                "store_upload",
                new=AsyncMock(return_value=stored),
            ),
            patch.object(
                documents.document_publication,
                "submit",
                side_effect=slow_submit,
            ),
            patch.object(
                documents.document_publication,
                "run",
                side_effect=AssertionError("API must not execute indexing"),
            ) as run,
        ):
            response, ticks = await _ticks_while(
                documents.upload_document(SimpleNamespace(), SimpleNamespace(id=7))
            )

        self.assertEqual("job-1", response.job_id)
        self.assertGreater(ticks, 3)
        run.assert_not_called()

    async def test_async_upload_only_submits_the_persistent_job(self):
        stored = SimpleNamespace(
            original_name="manual.pdf",
            path=Path("/tmp/manual.pdf"),
        )
        reservation = SimpleNamespace(
            job=SimpleNamespace(id="job-1", status="pending"),
            document=SimpleNamespace(id="doc-1", canonical_name="manual.pdf"),
            version=SimpleNamespace(id="version-1", version_number=1),
            already_current=False,
        )

        with (
            patch.object(documents, "ensure_upload_dir"),
            patch.object(
                documents,
                "store_upload",
                new=AsyncMock(return_value=stored),
            ),
            patch.object(
                documents.document_publication,
                "submit",
                return_value=reservation,
            ) as submit,
            patch.object(
                documents.document_publication,
                "run",
                side_effect=AssertionError(
                    "the API process must not execute persistent indexing jobs"
                ),
            ) as run,
        ):
            response = await documents.upload_document_async(
                file=SimpleNamespace(),
                user=SimpleNamespace(id=7),
            )

        self.assertEqual("job-1", response.job_id)
        self.assertEqual("pending", response.status)
        submit.assert_called_once_with(stored, 7)
        run.assert_not_called()

    async def test_async_delete_only_persists_retirement_and_cleanup_queue(self):
        outcome = SimpleNamespace(
            document_id="doc-1",
            retirement_job_id="retire-1",
            chunks_deleted=0,
            cleanup_pending=True,
            cleanup_step="pending",
            cleanup_error_code="VECTOR_STORE_UNAVAILABLE",
        )

        with patch.object(
            documents,
            "delete_document_transactionally",
            return_value=outcome,
        ) as retire:
            response = await documents.delete_document_async(
                "manual.pdf",
                SimpleNamespace(id=7),
            )

        self.assertEqual("retire-1", response.job_id)
        retire.assert_called_once_with("manual.pdf", owner_id=7)


if __name__ == "__main__":
    unittest.main()
