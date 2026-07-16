"""父级分块文档存储（用于 Auto-merging Retriever）。"""

import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import List

from backend.db.models import ParentChunk
from backend.infra.cache import cache
from backend.infra.database import SessionLocal


_ARTIFACT_DEFAULTS = {
    "tenant_id": "default",
    "knowledge_base_id": "",
    "document_id": "",
    "document_version_id": "",
    "section_id": "",
    "index_version": "v1",
    "content_hash": "",
}
_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")


@dataclass(frozen=True)
class ParentVersionVerification:
    """父块版本与预期 manifest 的精确核验结果。"""

    expected_ids: tuple[str, ...]
    actual_ids: tuple[str, ...]
    missing_ids: tuple[str, ...]
    unexpected_ids: tuple[str, ...]
    duplicate_ids: tuple[str, ...]
    metadata_mismatch_ids: tuple[str, ...]
    expected_count: int
    actual_count: int
    exact: bool


def _scope_value(field: str, value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value.strip()


def _acl_tags(value) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    normalized: list[str] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, str):
            continue
        tag = item.strip()
        if not tag or tag in seen:
            continue
        seen.add(tag)
        normalized.append(tag)
    return normalized


class ParentChunkStore:
    """基于 PostgreSQL + Redis 的父级分块存储。"""

    @staticmethod
    def _to_dict(item: ParentChunk) -> dict:
        return {
            "text": item.text,
            "filename": item.filename,
            "file_type": item.file_type,
            "file_path": item.file_path,
            "page_number": item.page_number,
            "chunk_id": item.chunk_id,
            "parent_chunk_id": item.parent_chunk_id,
            "root_chunk_id": item.root_chunk_id,
            "chunk_level": item.chunk_level,
            "chunk_idx": item.chunk_idx,
            "tenant_id": item.tenant_id,
            "knowledge_base_id": item.knowledge_base_id,
            "document_id": item.document_id,
            "document_version_id": item.document_version_id,
            "section_id": item.section_id,
            "index_version": item.index_version,
            "acl_tags": list(item.acl_tags or []),
            "content_hash": item.content_hash,
        }

    @staticmethod
    def _cache_key(chunk_id: str) -> str:
        return f"parent_chunk:{chunk_id}"

    @staticmethod
    def _version_query(
        db,
        *,
        tenant_id: str,
        knowledge_base_id: str,
        document_id: str,
        document_version_id: str,
        index_version: str | None,
    ):
        query = db.query(ParentChunk).filter(
            ParentChunk.tenant_id == _scope_value("tenant_id", tenant_id),
            ParentChunk.knowledge_base_id
            == _scope_value("knowledge_base_id", knowledge_base_id),
            ParentChunk.document_id == _scope_value("document_id", document_id),
            ParentChunk.document_version_id
            == _scope_value("document_version_id", document_version_id),
        )
        if index_version is not None:
            query = query.filter(
                ParentChunk.index_version
                == _scope_value("index_version", index_version)
            )
        return query

    def upsert_documents(self, docs: List[dict]) -> int:
        """写入/更新父级分块，返回写入条数。"""

        if not docs:
            return 0

        db = SessionLocal()
        upserted = 0
        cache_payloads: list[tuple[str, dict]] = []
        try:
            for doc in docs:
                chunk_id = (doc.get("chunk_id") or "").strip()
                if not chunk_id:
                    continue

                record = (
                    db.query(ParentChunk)
                    .filter(ParentChunk.chunk_id == chunk_id)
                    .first()
                )
                payload = {
                    "text": doc.get("text", ""),
                    "filename": doc.get("filename", ""),
                    "file_type": doc.get("file_type", ""),
                    "file_path": doc.get("file_path", ""),
                    "page_number": int(doc.get("page_number", 0) or 0),
                    "parent_chunk_id": doc.get("parent_chunk_id", ""),
                    "root_chunk_id": doc.get("root_chunk_id", ""),
                    "chunk_level": int(doc.get("chunk_level", 0) or 0),
                    "chunk_idx": int(doc.get("chunk_idx", 0) or 0),
                    "tenant_id": doc.get("tenant_id", _ARTIFACT_DEFAULTS["tenant_id"]),
                    "knowledge_base_id": doc.get(
                        "knowledge_base_id",
                        _ARTIFACT_DEFAULTS["knowledge_base_id"],
                    ),
                    "document_id": doc.get(
                        "document_id", _ARTIFACT_DEFAULTS["document_id"]
                    ),
                    "document_version_id": doc.get(
                        "document_version_id",
                        _ARTIFACT_DEFAULTS["document_version_id"],
                    ),
                    "section_id": doc.get(
                        "section_id", _ARTIFACT_DEFAULTS["section_id"]
                    ),
                    "index_version": doc.get(
                        "index_version", _ARTIFACT_DEFAULTS["index_version"]
                    ),
                    "acl_tags": _acl_tags(doc.get("acl_tags")),
                    "content_hash": doc.get(
                        "content_hash", _ARTIFACT_DEFAULTS["content_hash"]
                    ),
                    "updated_at": datetime.now(UTC).replace(tzinfo=None),
                }
                cache_payload = {"chunk_id": chunk_id, **payload}
                cache_payload.pop("updated_at")
                if record:
                    for key, value in payload.items():
                        setattr(record, key, value)
                else:
                    db.add(ParentChunk(chunk_id=chunk_id, **payload))

                cache_payloads.append((chunk_id, cache_payload))
                upserted += 1

            db.commit()
            for chunk_id, cache_payload in cache_payloads:
                cache.set_json(self._cache_key(chunk_id), cache_payload)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

        return upserted

    def get_documents_by_ids(self, chunk_ids: List[str]) -> List[dict]:
        if not chunk_ids:
            return []

        ordered_results = {}
        missing_ids = []
        ordered_keys = []
        for chunk_id in chunk_ids:
            key = (chunk_id or "").strip()
            if not key:
                continue
            ordered_keys.append(key)
            cached = cache.get_json(self._cache_key(key))
            if cached:
                ordered_results[key] = cached
            else:
                missing_ids.append(key)

        if missing_ids:
            db = SessionLocal()
            try:
                rows = (
                    db.query(ParentChunk)
                    .filter(ParentChunk.chunk_id.in_(missing_ids))
                    .all()
                )
                for row in rows:
                    payload = self._to_dict(row)
                    ordered_results[row.chunk_id] = payload
                    cache.set_json(self._cache_key(row.chunk_id), payload)
            finally:
                db.close()

        return [
            ordered_results[item] for item in ordered_keys if item in ordered_results
        ]

    def count_by_version(
        self,
        *,
        tenant_id: str,
        knowledge_base_id: str,
        document_id: str,
        document_version_id: str,
        index_version: str | None = None,
    ) -> int:
        db = SessionLocal()
        try:
            return self._version_query(
                db,
                tenant_id=tenant_id,
                knowledge_base_id=knowledge_base_id,
                document_id=document_id,
                document_version_id=document_version_id,
                index_version=index_version,
            ).count()
        finally:
            db.close()

    def verify_version(
        self,
        *,
        tenant_id: str,
        knowledge_base_id: str,
        document_id: str,
        document_version_id: str,
        expected_chunk_ids: Iterable[str],
        index_version: str | None = None,
    ) -> ParentVersionVerification:
        """核验父块 ID、count、重复项和完整 artifact metadata。"""

        expected_ids = tuple(expected_chunk_ids)
        if any(not isinstance(item, str) or not item.strip() for item in expected_ids):
            raise ValueError("expected_chunk_ids must contain non-empty strings")
        if len(set(expected_ids)) != len(expected_ids):
            raise ValueError("expected_chunk_ids must be unique")

        normalized_tenant = _scope_value("tenant_id", tenant_id)
        normalized_knowledge_base = _scope_value("knowledge_base_id", knowledge_base_id)
        normalized_document = _scope_value("document_id", document_id)
        normalized_version = _scope_value("document_version_id", document_version_id)
        normalized_index = (
            _scope_value("index_version", index_version)
            if index_version is not None
            else None
        )

        db = SessionLocal()
        try:
            rows = self._version_query(
                db,
                tenant_id=normalized_tenant,
                knowledge_base_id=normalized_knowledge_base,
                document_id=normalized_document,
                document_version_id=normalized_version,
                index_version=normalized_index,
            ).all()
        finally:
            db.close()

        actual_ids = tuple(row.chunk_id for row in rows)
        expected_set = set(expected_ids)
        actual_set = set(actual_ids)
        duplicate_ids = tuple(
            sorted(
                chunk_id for chunk_id, count in Counter(actual_ids).items() if count > 1
            )
        )
        metadata_mismatch_ids = tuple(
            sorted(
                row.chunk_id
                for row in rows
                if row.tenant_id != normalized_tenant
                or row.knowledge_base_id != normalized_knowledge_base
                or row.document_id != normalized_document
                or row.document_version_id != normalized_version
                or not isinstance(row.section_id, str)
                or not row.section_id.strip()
                or not isinstance(row.index_version, str)
                or not row.index_version.strip()
                or (
                    normalized_index is not None
                    and row.index_version != normalized_index
                )
                or not isinstance(row.acl_tags, list)
                or not isinstance(row.content_hash, str)
                or not _SHA256_RE.fullmatch(row.content_hash)
            )
        )
        missing_ids = tuple(sorted(expected_set - actual_set))
        unexpected_ids = tuple(sorted(actual_set - expected_set))
        expected_count = len(expected_ids)
        actual_count = len(actual_ids)
        exact = (
            not missing_ids
            and not unexpected_ids
            and not duplicate_ids
            and not metadata_mismatch_ids
            and expected_count == actual_count
        )
        return ParentVersionVerification(
            expected_ids=expected_ids,
            actual_ids=actual_ids,
            missing_ids=missing_ids,
            unexpected_ids=unexpected_ids,
            duplicate_ids=duplicate_ids,
            metadata_mismatch_ids=metadata_mismatch_ids,
            expected_count=expected_count,
            actual_count=actual_count,
            exact=exact,
        )

    def delete_by_version(
        self,
        *,
        tenant_id: str,
        knowledge_base_id: str,
        document_id: str,
        document_version_id: str,
        index_version: str | None = None,
    ) -> int:
        """精确删除一个版本；缓存先删，失败时保留 DB 真相供重试。"""

        db = SessionLocal()
        try:
            query = self._version_query(
                db,
                tenant_id=tenant_id,
                knowledge_base_id=knowledge_base_id,
                document_id=document_id,
                document_version_id=document_version_id,
                index_version=index_version,
            )
            rows = query.all()
            chunk_ids = [row.chunk_id for row in rows]
            if not chunk_ids:
                return 0
            for chunk_id in chunk_ids:
                cache.delete_strict(self._cache_key(chunk_id))
            query.delete(synchronize_session=False)
            db.commit()
            return len(chunk_ids)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def delete_legacy_by_filename(self, filename: str) -> int:
        """Delete only pre-Catalog rows for one legacy filename."""

        if not filename:
            return 0

        db = SessionLocal()
        try:
            query = db.query(ParentChunk).filter(
                ParentChunk.filename == filename,
                ParentChunk.knowledge_base_id == "",
                ParentChunk.document_id == "",
                ParentChunk.document_version_id == "",
            )
            rows = query.all()
            chunk_ids = [row.chunk_id for row in rows]
            deleted = len(chunk_ids)
            if deleted > 0:
                for chunk_id in chunk_ids:
                    cache.delete_strict(self._cache_key(chunk_id))
                query.delete(synchronize_session=False)
                db.commit()
            return deleted
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()
