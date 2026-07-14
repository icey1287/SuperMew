from __future__ import annotations

import hashlib
import os
import re
import stat
import unicodedata
import zipfile
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from fastapi import UploadFile
from pypdf import PdfReader

from backend.core.errors import AppError, ErrorCode
from backend.core.settings import StorageSettings, get_settings


_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_SUPPORTED_EXTENSIONS = {".pdf", ".docx", ".doc", ".xlsx", ".xls", ".html", ".htm"}
_EXPECTED_MIME_TYPES = {
    ".pdf": {"application/pdf"},
    ".docx": {
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/zip",
    },
    ".doc": {"application/msword", "application/x-ole-storage"},
    ".xlsx": {
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/zip",
    },
    ".xls": {"application/vnd.ms-excel", "application/x-ole-storage"},
    ".html": {"text/html", "application/xhtml+xml"},
    ".htm": {"text/html", "application/xhtml+xml"},
}
_GENERIC_MIME_TYPES = {"", "application/octet-stream", "binary/octet-stream"}
_OLE_MAGIC = bytes.fromhex("D0CF11E0A1B11AE1")


@dataclass(frozen=True)
class UploadPolicy:
    directory: Path
    max_bytes: int
    max_pages: int
    max_archive_entries: int
    max_uncompressed_bytes: int
    max_compression_ratio: float

    @classmethod
    def from_settings(cls, settings: StorageSettings | None = None) -> UploadPolicy:
        value = settings or get_settings().storage
        return cls(
            directory=value.upload_dir,
            max_bytes=value.max_upload_bytes,
            max_pages=value.max_document_pages,
            max_archive_entries=value.max_archive_entries,
            max_uncompressed_bytes=value.max_uncompressed_bytes,
            max_compression_ratio=value.max_compression_ratio,
        )


@dataclass(frozen=True)
class StoredUpload:
    original_name: str
    object_key: str
    path: Path
    extension: str
    media_type: str
    size_bytes: int
    content_sha256: str


def sanitize_original_filename(value: str) -> str:
    normalized = unicodedata.normalize("NFC", value or "").replace("\\", "/")
    basename = normalized.rsplit("/", 1)[-1]
    basename = _CONTROL_RE.sub("", basename).strip().strip(".")
    if not basename:
        raise AppError(ErrorCode.UPLOAD_INVALID, "文件名不能为空", status_code=400)
    if len(basename) > 255:
        stem = Path(basename).stem[:200]
        basename = f"{stem}{Path(basename).suffix[:20]}"
    return basename


def is_supported_document(filename: str) -> bool:
    return Path(filename).suffix.lower() in _SUPPORTED_EXTENSIONS


def _validate_claimed_mime(extension: str, content_type: str | None) -> None:
    claimed = (content_type or "").lower().split(";", 1)[0].strip()
    if claimed in _GENERIC_MIME_TYPES:
        return
    if claimed not in _EXPECTED_MIME_TYPES[extension]:
        raise AppError(
            ErrorCode.UPLOAD_INVALID,
            "文件 MIME 类型与扩展名不匹配",
            status_code=400,
        )


def _sniff_media_type(extension: str, head: bytes) -> str:
    stripped = head.lstrip(b"\xef\xbb\xbf\x00\t\r\n ").lower()
    if extension == ".pdf" and head.startswith(b"%PDF-"):
        return "application/pdf"
    if extension in {".docx", ".xlsx"} and head.startswith(b"PK\x03\x04"):
        if extension == ".docx":
            return "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        return "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    if extension in {".doc", ".xls"} and head.startswith(_OLE_MAGIC):
        return "application/x-ole-storage"
    if extension in {".html", ".htm"} and (
        stripped.startswith(b"<!doctype html")
        or stripped.startswith(b"<html")
        or stripped.startswith(b"<?xml")
    ):
        return "text/html"
    raise AppError(
        ErrorCode.UPLOAD_INVALID,
        "文件内容与扩展名不匹配",
        status_code=400,
    )


def _validate_pdf(path: Path, policy: UploadPolicy) -> None:
    try:
        reader = PdfReader(str(path), strict=False)
        if len(reader.pages) > policy.max_pages:
            raise AppError(
                ErrorCode.UPLOAD_INVALID,
                f"PDF 页数超过限制（最多 {policy.max_pages} 页）",
                status_code=400,
            )
    except AppError:
        raise
    except Exception as exc:
        raise AppError(
            ErrorCode.UPLOAD_INVALID, "PDF 文件结构无效", status_code=400
        ) from exc


def _validate_zip_document(path: Path, extension: str, policy: UploadPolicy) -> None:
    try:
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            if len(infos) > policy.max_archive_entries:
                raise AppError(
                    ErrorCode.UPLOAD_INVALID, "压缩包条目数量超过限制", status_code=400
                )

            total_compressed = 0
            total_uncompressed = 0
            names: set[str] = set()
            for info in infos:
                name = info.filename.replace("\\", "/")
                names.add(name)
                pure_parts = Path(name).parts
                if name.startswith("/") or ".." in pure_parts:
                    raise AppError(
                        ErrorCode.UPLOAD_INVALID,
                        "文档压缩包包含不安全路径",
                        status_code=400,
                    )
                mode = (info.external_attr >> 16) & 0o170000
                if mode == stat.S_IFLNK:
                    raise AppError(
                        ErrorCode.UPLOAD_INVALID,
                        "文档压缩包包含符号链接",
                        status_code=400,
                    )
                total_compressed += max(info.compress_size, 0)
                total_uncompressed += max(info.file_size, 0)

            if total_uncompressed > policy.max_uncompressed_bytes:
                raise AppError(
                    ErrorCode.UPLOAD_INVALID, "文档解压后大小超过限制", status_code=400
                )
            ratio = total_uncompressed / max(total_compressed, 1)
            if ratio > policy.max_compression_ratio:
                raise AppError(
                    ErrorCode.UPLOAD_INVALID, "文档压缩比超过安全限制", status_code=400
                )

            required_prefix = "word/" if extension == ".docx" else "xl/"
            if "[Content_Types].xml" not in names or not any(
                name.startswith(required_prefix) for name in names
            ):
                raise AppError(
                    ErrorCode.UPLOAD_INVALID,
                    "Office 文档结构与扩展名不匹配",
                    status_code=400,
                )
    except AppError:
        raise
    except (OSError, zipfile.BadZipFile) as exc:
        raise AppError(
            ErrorCode.UPLOAD_INVALID, "Office 文档压缩结构无效", status_code=400
        ) from exc


def _validate_saved_document(path: Path, extension: str, policy: UploadPolicy) -> None:
    if extension == ".pdf":
        _validate_pdf(path, policy)
    elif extension in {".docx", ".xlsx"}:
        _validate_zip_document(path, extension, policy)


async def store_upload(
    file: UploadFile, policy: UploadPolicy | None = None
) -> StoredUpload:
    policy = policy or UploadPolicy.from_settings()
    original_name = sanitize_original_filename(file.filename or "")
    extension = Path(original_name).suffix.lower()
    if extension not in _SUPPORTED_EXTENSIONS:
        raise AppError(
            ErrorCode.UPLOAD_INVALID,
            "仅支持 PDF、Word、Excel 和 HTML 文档",
            status_code=400,
        )
    _validate_claimed_mime(extension, file.content_type)

    policy.directory.mkdir(parents=True, exist_ok=True)
    directory = policy.directory.resolve()
    object_key = f"{uuid4().hex}{extension}"
    final_path = (directory / object_key).resolve()
    if final_path.parent != directory:
        raise AppError(ErrorCode.UPLOAD_INVALID, "上传路径无效", status_code=400)
    temporary_path = final_path.with_suffix(f"{final_path.suffix}.uploading")

    total = 0
    digest = hashlib.sha256()
    head = bytearray()
    try:
        with temporary_path.open("xb") as output:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > policy.max_bytes:
                    raise AppError(
                        ErrorCode.UPLOAD_TOO_LARGE,
                        f"文件超过大小限制（最多 {policy.max_bytes} 字节）",
                        status_code=413,
                    )
                if len(head) < 8192:
                    head.extend(chunk[: 8192 - len(head)])
                digest.update(chunk)
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())

        if total == 0:
            raise AppError(
                ErrorCode.UPLOAD_INVALID, "上传文件不能为空", status_code=400
            )
        media_type = _sniff_media_type(extension, bytes(head))
        _validate_saved_document(temporary_path, extension, policy)
        os.replace(temporary_path, final_path)
        return StoredUpload(
            original_name=original_name,
            object_key=object_key,
            path=final_path,
            extension=extension,
            media_type=media_type,
            size_bytes=total,
            content_sha256=digest.hexdigest(),
        )
    except Exception:
        temporary_path.unlink(missing_ok=True)
        final_path.unlink(missing_ok=True)
        raise
