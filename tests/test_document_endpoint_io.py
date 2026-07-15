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

    async def test_delete_document_runs_storage_cleanup_off_event_loop(self):
        def slow_delete(filename):
            self.assertEqual("manual.pdf", filename)
            time.sleep(0.04)
            return 3

        with patch.object(
            documents,
            "delete_document_transactionally",
            side_effect=slow_delete,
        ):
            response, ticks = await _ticks_while(
                documents.delete_document("manual.pdf", None)
            )

        self.assertEqual(3, response.chunks_deleted)
        self.assertGreater(ticks, 3)

    async def test_direct_upload_runs_parse_and_index_work_off_event_loop(self):
        stored = SimpleNamespace(
            original_name="manual.pdf",
            path=Path("/tmp/manual.pdf"),
        )

        def slow_store(file_path, filename):
            self.assertEqual("/tmp/manual.pdf", file_path)
            self.assertEqual("manual.pdf", filename)
            time.sleep(0.04)
            return 2, 5

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
                documents.upload_document(SimpleNamespace(), None)
            )

        self.assertEqual(5, response.chunks_processed)
        self.assertGreater(ticks, 3)


if __name__ == "__main__":
    unittest.main()
