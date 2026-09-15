import asyncio
import io
import tempfile
import time
import unittest
import zipfile
from pathlib import Path

from fastapi import UploadFile
from starlette.datastructures import Headers
from pypdf import PdfWriter

from backend.core.errors import AppError, ErrorCode
from backend.security.uploads import (
    UploadPolicy,
    sanitize_original_filename,
    store_upload,
)


class _SlowStream(io.BytesIO):
    def read(self, size: int = -1) -> bytes:
        time.sleep(0.01)
        return super().read(size)


def valid_pdf() -> bytes:
    output = io.BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    writer.write(output)
    return output.getvalue()


def valid_docx(payload: bytes = b"hello") -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", "<Types />")
        archive.writestr("word/document.xml", payload)
    return output.getvalue()


class UploadSecurityTests(unittest.IsolatedAsyncioTestCase):
    def policy(self, directory: Path, **overrides) -> UploadPolicy:
        values = {
            "directory": directory,
            "max_bytes": 1024 * 1024,
            "max_pages": 10,
            "max_archive_entries": 100,
            "max_uncompressed_bytes": 1024 * 1024,
            "max_compression_ratio": 100.0,
        }
        values.update(overrides)
        return UploadPolicy(**values)

    def test_filename_is_reduced_to_safe_metadata(self):
        self.assertEqual(
            "report.pdf", sanitize_original_filename("../../etc/report.pdf")
        )
        self.assertEqual("report.pdf", sanitize_original_filename("..\\..\\report.pdf"))

    async def test_server_generated_object_key_prevents_path_traversal(self):
        with tempfile.TemporaryDirectory() as directory:
            upload = UploadFile(
                io.BytesIO(valid_pdf()),
                filename="../../outside.pdf",
                headers=Headers({"content-type": "application/pdf"}),
            )
            stored = await store_upload(upload, self.policy(Path(directory)))

            self.assertEqual("outside.pdf", stored.original_name)
            self.assertNotEqual("outside.pdf", stored.object_key)
            self.assertEqual(Path(directory).resolve(), stored.path.parent)
            self.assertTrue(stored.path.exists())

    async def test_starlette_upload_storage_and_validation_do_not_block_event_loop(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            upload = UploadFile(
                _SlowStream(valid_pdf()),
                filename="manual.pdf",
                headers=Headers({"content-type": "application/pdf"}),
            )
            stop = False
            ticks = 0

            async def ticker():
                nonlocal ticks
                while not stop:
                    ticks += 1
                    await asyncio.sleep(0.001)

            ticker_task = asyncio.create_task(ticker())
            try:
                stored = await store_upload(
                    upload,
                    self.policy(Path(directory)),
                )
            finally:
                stop = True
                await ticker_task

            self.assertTrue(stored.path.exists())
            self.assertGreater(ticks, 3)

    async def test_forged_extension_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            upload = UploadFile(
                io.BytesIO(valid_docx()),
                filename="fake.pdf",
                headers=Headers({"content-type": "application/pdf"}),
            )
            with self.assertRaises(AppError) as raised:
                await store_upload(upload, self.policy(Path(directory)))
            self.assertEqual(ErrorCode.UPLOAD_INVALID, raised.exception.code)

    async def test_size_limit_is_enforced_while_streaming(self):
        with tempfile.TemporaryDirectory() as directory:
            upload = UploadFile(
                io.BytesIO(b"<html>" + b"x" * 100),
                filename="large.html",
                headers=Headers({"content-type": "text/html"}),
            )
            with self.assertRaises(AppError) as raised:
                await store_upload(upload, self.policy(Path(directory), max_bytes=32))
            self.assertEqual(ErrorCode.UPLOAD_TOO_LARGE, raised.exception.code)
            self.assertEqual([], list(Path(directory).iterdir()))

    async def test_archive_compression_ratio_is_limited(self):
        with tempfile.TemporaryDirectory() as directory:
            upload = UploadFile(
                io.BytesIO(valid_docx(b"0" * 10000)),
                filename="bomb.docx",
                headers=Headers(
                    {
                        "content-type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    }
                ),
            )
            with self.assertRaises(AppError) as raised:
                await store_upload(
                    upload,
                    self.policy(Path(directory), max_compression_ratio=2.0),
                )
            self.assertEqual(ErrorCode.UPLOAD_INVALID, raised.exception.code)


if __name__ == "__main__":
    unittest.main()
