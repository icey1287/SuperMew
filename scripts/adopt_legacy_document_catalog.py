from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import unicodedata
from collections import Counter, defaultdict
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy.orm import Session  # noqa: E402

from backend.core.errors import AppError, ErrorCode  # noqa: E402
from backend.db.models import ParentChunk, User  # noqa: E402
from backend.documents.catalog import (  # noqa: E402
    DocumentCatalog,
    DocumentVersionStatus,
    StorageLayout,
    legacy_source_identity,
)
from backend.indexing.milvus_client import (  # noqa: E402
    MilvusStore,
    get_milvus_store,
)
from backend.infra.database import SessionLocal  # noqa: E402
from backend.security.milvus_filters import eq_filter  # noqa: E402
from backend.security.uploads import sanitize_original_filename  # noqa: E402


SessionFactory = Callable[[], Session]
_COLLECTION_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,159}$")
_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_INVISIBLE_RE = re.compile(r"[\u200b-\u200f\u202a-\u202e\u2060\ufeff]")
_LEAF_CHUNK_LEVEL = 3
_SUMMARY_SCHEMA_VERSION = 1


class LegacyAdoptionError(RuntimeError):
    """A stable, redacted CLI failure with no provider or database details."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class LegacyDocumentCandidate:
    canonical_name: str
    file_type: str
    chunk_count: int
    parent_chunk_count: int
    content_sha256: str
    legacy_identity: str
    leaf_fingerprints: tuple[str, ...]
    leaf_parent_ids: tuple[str, ...]

    @property
    def document_ref(self) -> str:
        digest = hashlib.sha256(self.canonical_name.encode("utf-8")).hexdigest()
        return f"document_{digest[:16]}"


@dataclass(frozen=True, slots=True)
class LegacyScan:
    candidates: tuple[LegacyDocumentCandidate, ...]
    leaf_chunks_scanned: int
    invalid_rows_skipped: int
    unsafe_documents_skipped: int


def _scan_fingerprint(scan: LegacyScan, *, collection_name: str) -> str:
    return _stable_digest(
        {
            "schema_version": 1,
            "collection_name": collection_name,
            "leaf_chunks_scanned": scan.leaf_chunks_scanned,
            "invalid_rows_skipped": scan.invalid_rows_skipped,
            "unsafe_documents_skipped": scan.unsafe_documents_skipped,
            "documents": [
                {
                    "legacy_identity": candidate.legacy_identity,
                    "content_sha256": candidate.content_sha256,
                    "chunk_count": candidate.chunk_count,
                    "parent_chunk_count": candidate.parent_chunk_count,
                }
                for candidate in scan.candidates
            ],
        }
    )


def _stable_digest(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _safe_canonical_name(value: Any) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    normalized = unicodedata.normalize("NFC", value)
    if normalized != value or _INVISIBLE_RE.search(value):
        return None
    if "/" in value or "\\" in value or value in {".", ".."}:
        return None
    try:
        sanitized = sanitize_original_filename(value)
    except AppError:
        return None
    # Adoption must preserve the exact filename stored in Milvus. Otherwise later
    # cleanup by canonical_name could leave the malicious legacy row visible.
    return value if sanitized == value else None


def _safe_file_type(canonical_name: str, values: list[Any]) -> str:
    suffix = Path(canonical_name).suffix.lower()
    by_suffix = {
        ".pdf": "PDF",
        ".doc": "Word",
        ".docx": "Word",
        ".xls": "Excel",
        ".xlsx": "Excel",
        ".html": "HTML",
        ".htm": "HTML",
    }
    if suffix in by_suffix:
        return by_suffix[suffix]
    allowed = {
        "pdf": "PDF",
        "word": "Word",
        "excel": "Excel",
        "html": "HTML",
        "document": "Document",
    }
    normalized = [
        allowed[item.strip().casefold()]
        for item in values
        if isinstance(item, str) and item.strip().casefold() in allowed
    ]
    if not normalized:
        return "Document"
    counts = Counter(normalized)
    return sorted(counts, key=lambda item: (-counts[item], item))[0]


def _row_content_hash(row: Mapping[str, Any]) -> str | None:
    text = row.get("text")
    if not isinstance(text, str) or not text.strip():
        return None
    actual = hashlib.sha256(text.encode("utf-8")).hexdigest()
    supplied = row.get("content_hash")
    if supplied in {None, ""}:
        return actual
    if not isinstance(supplied, str) or not _SHA256_RE.fullmatch(supplied.strip()):
        return None
    return actual if supplied.strip().lower() == actual else None


def _leaf_parent_id(row: Mapping[str, Any]) -> str | None:
    value = row.get("parent_chunk_id")
    if value is None:
        return ""
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    if len(normalized) > 512:
        return None
    return normalized


def _leaf_chunk_id(row: Mapping[str, Any]) -> str | None:
    value = row.get("chunk_id")
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    if not normalized or len(normalized) > 512:
        return None
    return normalized


def _row_fingerprint(row: Mapping[str, Any]) -> str | None:
    content_hash = _row_content_hash(row)
    parent_chunk_id = _leaf_parent_id(row)
    chunk_id = _leaf_chunk_id(row)
    if content_hash is None or parent_chunk_id is None or chunk_id is None:
        return None
    return _stable_digest(
        {
            "schema_version": 1,
            "chunk_id": chunk_id,
            "content_hash": content_hash,
            "parent_chunk_id": parent_chunk_id,
        }
    )


def scan_legacy_documents(store: MilvusStore) -> LegacyScan:
    collection = str(store.collection_name or "").strip()
    if not _COLLECTION_RE.fullmatch(collection):
        raise LegacyAdoptionError("INVALID_COLLECTION_CONFIGURATION")
    try:
        has_collection = getattr(store, "has_collection", None)
        if callable(has_collection) and not has_collection():
            return LegacyScan(
                candidates=(),
                leaf_chunks_scanned=0,
                invalid_rows_skipped=0,
                unsafe_documents_skipped=0,
            )
        rows = store.query_all(
            eq_filter("chunk_level", _LEAF_CHUNK_LEVEL),
            output_fields=[
                "filename",
                "file_type",
                "chunk_id",
                "chunk_level",
                "parent_chunk_id",
                "text",
            ],
        )
    except Exception as exc:
        raise LegacyAdoptionError(ErrorCode.VECTOR_STORE_UNAVAILABLE.value) from exc
    if not isinstance(rows, list):
        raise LegacyAdoptionError(ErrorCode.VECTOR_STORE_UNAVAILABLE.value)

    fingerprints: dict[str, list[str]] = defaultdict(list)
    file_types: dict[str, list[Any]] = defaultdict(list)
    parent_ids: dict[str, list[str]] = defaultdict(list)
    chunk_id_owners: dict[str, str] = {}
    tainted_names: set[str] = set()
    unsafe_name_refs: set[str] = set()
    invalid_rows = 0
    for raw_row in rows:
        if not isinstance(raw_row, Mapping):
            invalid_rows += 1
            continue
        try:
            level = int(raw_row.get("chunk_level", -1))
        except (TypeError, ValueError):
            invalid_rows += 1
            continue
        if level != _LEAF_CHUNK_LEVEL:
            invalid_rows += 1
            continue
        canonical_name = _safe_canonical_name(raw_row.get("filename"))
        if canonical_name is None:
            raw_name = raw_row.get("filename")
            if isinstance(raw_name, str):
                unsafe_name_refs.add(
                    hashlib.sha256(raw_name.encode("utf-8")).hexdigest()
                )
            else:
                invalid_rows += 1
            continue
        fingerprint = _row_fingerprint(raw_row)
        if fingerprint is None:
            tainted_names.add(canonical_name)
            invalid_rows += 1
            continue
        chunk_id = _leaf_chunk_id(raw_row)
        previous_owner = chunk_id_owners.get(chunk_id)
        if previous_owner is not None:
            tainted_names.update({previous_owner, canonical_name})
            invalid_rows += 1
            continue
        chunk_id_owners[chunk_id] = canonical_name
        fingerprints[canonical_name].append(fingerprint)
        file_types[canonical_name].append(raw_row.get("file_type"))
        parent_chunk_id = _leaf_parent_id(raw_row)
        if parent_chunk_id:
            parent_ids[canonical_name].append(parent_chunk_id)

    unsafe_documents = len(tainted_names)
    candidates: list[LegacyDocumentCandidate] = []
    for canonical_name in sorted(fingerprints):
        if canonical_name in tainted_names:
            continue
        chunk_fingerprints = sorted(fingerprints[canonical_name])
        content_sha256 = _stable_digest(
            {
                "schema_version": 1,
                "leaf_chunks": chunk_fingerprints,
            }
        )
        legacy_identity = legacy_source_identity(
            vector_collection=collection,
            canonical_name=canonical_name,
        )
        candidates.append(
            LegacyDocumentCandidate(
                canonical_name=canonical_name,
                file_type=_safe_file_type(
                    canonical_name,
                    file_types[canonical_name],
                ),
                chunk_count=len(chunk_fingerprints),
                parent_chunk_count=0,
                content_sha256=content_sha256,
                legacy_identity=legacy_identity,
                leaf_fingerprints=tuple(chunk_fingerprints),
                leaf_parent_ids=tuple(sorted(set(parent_ids[canonical_name]))),
            )
        )
    return LegacyScan(
        candidates=tuple(candidates),
        leaf_chunks_scanned=len(rows),
        invalid_rows_skipped=invalid_rows,
        unsafe_documents_skipped=unsafe_documents + len(unsafe_name_refs),
    )


def _parent_content_hash(row: ParentChunk) -> str | None:
    if not isinstance(row.text, str) or not row.text.strip():
        return None
    actual = hashlib.sha256(row.text.encode("utf-8")).hexdigest()
    supplied = row.content_hash
    if supplied in {None, ""}:
        return actual
    if not isinstance(supplied, str) or not _SHA256_RE.fullmatch(supplied.strip()):
        return None
    return actual if supplied.strip().lower() == actual else None


def _attach_parent_graph(
    session_factory: SessionFactory,
    scan: LegacyScan,
) -> LegacyScan:
    if not scan.candidates:
        return scan
    canonical_names = tuple(item.canonical_name for item in scan.candidates)
    db = session_factory()
    try:
        rows = (
            db.query(ParentChunk)
            .filter(ParentChunk.filename.in_(canonical_names))
            .all()
        )
    except Exception as exc:
        raise LegacyAdoptionError(ErrorCode.STORAGE_UNAVAILABLE.value) from exc
    finally:
        db.close()

    rows_by_id = {row.chunk_id: row for row in rows}
    validated: list[LegacyDocumentCandidate] = []
    unsafe_documents = 0
    for candidate in scan.candidates:
        pending = [
            (chunk_id, _LEAF_CHUNK_LEVEL) for chunk_id in candidate.leaf_parent_ids
        ]
        visited: set[str] = set()
        parent_fingerprints: list[str] = []
        unsafe = False
        while pending:
            chunk_id, child_level = pending.pop()
            if chunk_id in visited:
                continue
            visited.add(chunk_id)
            row = rows_by_id.get(chunk_id)
            if row is None:
                unsafe = True
                break
            parent_chunk_id = (
                row.parent_chunk_id.strip()
                if isinstance(row.parent_chunk_id, str)
                else ""
            )
            content_hash = _parent_content_hash(row)
            if (
                row.filename != candidate.canonical_name
                or bool(row.document_version_id)
                or bool(row.document_id)
                or bool(row.knowledge_base_id)
                or row.chunk_level not in {1, 2}
                or row.chunk_level >= child_level
                or content_hash is None
                or len(parent_chunk_id) > 512
                or parent_chunk_id == chunk_id
            ):
                unsafe = True
                break
            parent_fingerprints.append(
                _stable_digest(
                    {
                        "schema_version": 1,
                        "chunk_id": row.chunk_id,
                        "content_hash": content_hash,
                        "filename": row.filename,
                        "parent_chunk_id": parent_chunk_id,
                        "root_chunk_id": row.root_chunk_id,
                        "chunk_level": row.chunk_level,
                        "chunk_idx": row.chunk_idx,
                        "document_version_id": row.document_version_id,
                    }
                )
            )
            if parent_chunk_id:
                pending.append((parent_chunk_id, row.chunk_level))
        if unsafe:
            unsafe_documents += 1
            continue
        parent_fingerprints.sort()
        content_sha256 = _stable_digest(
            {
                "schema_version": 2,
                "leaf_chunks": list(candidate.leaf_fingerprints),
                "parent_chunks": parent_fingerprints,
            }
        )
        validated.append(
            replace(
                candidate,
                parent_chunk_count=len(visited),
                content_sha256=content_sha256,
            )
        )
    return LegacyScan(
        candidates=tuple(validated),
        leaf_chunks_scanned=scan.leaf_chunks_scanned,
        invalid_rows_skipped=scan.invalid_rows_skipped,
        unsafe_documents_skipped=(scan.unsafe_documents_skipped + unsafe_documents),
    )


def _owner_id(session_factory: SessionFactory, username: str) -> int | None:
    db = session_factory()
    try:
        row = db.query(User.id).filter(User.username == username).first()
        return int(row[0]) if row else None
    except Exception as exc:
        raise LegacyAdoptionError(ErrorCode.STORAGE_UNAVAILABLE.value) from exc
    finally:
        db.close()


def _candidate_summary(
    candidate: LegacyDocumentCandidate,
    *,
    parent_count: int,
    status: str,
    reason: str,
) -> dict[str, Any]:
    return {
        "document_ref": candidate.document_ref,
        "status": status,
        "reason": reason,
        "file_type": candidate.file_type,
        "chunk_count": candidate.chunk_count,
        "parent_chunk_count": parent_count,
        "content_identity": f"sha256:{candidate.content_sha256}",
    }


class LegacyDocumentCatalogAdopter:
    """Legacy scan and Catalog adoption behind one testable CLI Module."""

    def __init__(
        self,
        *,
        store: MilvusStore,
        catalog: DocumentCatalog,
        session_factory: SessionFactory,
    ) -> None:
        self._store = store
        self._catalog = catalog
        self._session_factory = session_factory

    def run(
        self,
        *,
        owner_username: str,
        tenant_id: str,
        knowledge_base_name: str,
        dry_run: bool,
        create_target_knowledge_base: bool = False,
    ) -> dict[str, Any]:
        username = str(owner_username or "").strip()
        tenant = str(tenant_id or "").strip()
        knowledge_base = str(knowledge_base_name or "").strip()
        if not username or len(username) > 100:
            raise LegacyAdoptionError(ErrorCode.INVALID_REQUEST.value)
        if not tenant or len(tenant) > 64:
            raise LegacyAdoptionError(ErrorCode.INVALID_REQUEST.value)
        if not knowledge_base or len(knowledge_base) > 160:
            raise LegacyAdoptionError(ErrorCode.INVALID_REQUEST.value)

        scan = _attach_parent_graph(
            self._session_factory,
            scan_legacy_documents(self._store),
        )
        collection = str(self._store.collection_name).strip()
        corpus_fingerprint = _scan_fingerprint(
            scan,
            collection_name=collection,
        )
        summary: dict[str, Any] = {
            "schema_version": _SUMMARY_SCHEMA_VERSION,
            "operation": "legacy_document_catalog_adoption",
            "mode": "dry_run" if dry_run else "apply",
            "status": "ok",
            "leaf_chunks_scanned": scan.leaf_chunks_scanned,
            "documents_eligible": len(scan.candidates),
            "documents_adopted": 0,
            "documents_skipped": 0,
            "invalid_rows_skipped": scan.invalid_rows_skipped,
            "unsafe_documents_skipped": scan.unsafe_documents_skipped,
            "legacy_corpus_fingerprint": corpus_fingerprint,
            "adoption_complete": False,
            "target_knowledge_base_created": False,
            "target_knowledge_base_creation_planned": False,
            "target_knowledge_base_exists": False,
            "target_knowledge_base_creation_required": False,
            "results": [],
        }
        adoption_state = None
        if not dry_run:
            try:
                adoption_state = self._catalog.begin_legacy_adoption(
                    tenant_id=tenant,
                    legacy_collection=collection,
                    knowledge_base_name=knowledge_base,
                    corpus_fingerprint=corpus_fingerprint,
                )
            except AppError as exc:
                raise LegacyAdoptionError(str(exc.public_error.code)) from exc
            except Exception as exc:
                raise LegacyAdoptionError(ErrorCode.STORAGE_UNAVAILABLE.value) from exc
        owner_id = _owner_id(self._session_factory, username)
        if owner_id is None:
            summary.update(
                {
                    "status": "error",
                    "error_code": "OWNER_NOT_FOUND",
                    "documents_skipped": len(scan.candidates),
                }
            )
            return summary

        try:
            kb = self._catalog.find_knowledge_base(
                tenant_id=tenant,
                name=knowledge_base,
            )
        except AppError as exc:
            raise LegacyAdoptionError(str(exc.public_error.code)) from exc
        except Exception as exc:
            raise LegacyAdoptionError(ErrorCode.STORAGE_UNAVAILABLE.value) from exc
        if scan.candidates and kb is None:
            summary["target_knowledge_base_creation_required"] = True
            if dry_run:
                summary["target_knowledge_base_creation_planned"] = bool(
                    create_target_knowledge_base
                )
            elif not create_target_knowledge_base:
                summary.update(
                    {
                        "status": "error",
                        "error_code": "KNOWLEDGE_BASE_NOT_FOUND",
                        "documents_skipped": len(scan.candidates),
                    }
                )
                return summary
            else:
                try:
                    kb = self._catalog.ensure_knowledge_base(
                        tenant_id=tenant,
                        owner_id=owner_id,
                        name=knowledge_base,
                    )
                    summary["target_knowledge_base_created"] = True
                    summary["target_knowledge_base_exists"] = True
                except AppError as exc:
                    raise LegacyAdoptionError(str(exc.public_error.code)) from exc
                except Exception as exc:
                    raise LegacyAdoptionError(
                        ErrorCode.STORAGE_UNAVAILABLE.value
                    ) from exc
        if kb is not None and kb.owner_id != owner_id:
            summary.update(
                {
                    "status": "error",
                    "error_code": ErrorCode.PERMISSION_DENIED.value,
                    "documents_skipped": len(scan.candidates),
                }
            )
            return summary
        if kb is not None:
            summary["target_knowledge_base_exists"] = True

        if dry_run:
            if scan.invalid_rows_skipped or scan.unsafe_documents_skipped:
                summary.update(
                    {
                        "status": "error",
                        "error_code": "LEGACY_ADOPTION_REQUIRES_CLEANUP",
                        "documents_skipped": len(scan.candidates),
                    }
                )
                return summary
            summary["results"] = [
                _candidate_summary(
                    candidate,
                    parent_count=candidate.parent_chunk_count,
                    status="planned",
                    reason="eligible",
                )
                for candidate in scan.candidates
            ]
            summary["adoption_ready"] = True
            return summary

        if scan.invalid_rows_skipped or scan.unsafe_documents_skipped:
            summary.update(
                {
                    "status": "error",
                    "error_code": "LEGACY_ADOPTION_REQUIRES_CLEANUP",
                    "documents_skipped": len(scan.candidates),
                }
            )
            return summary

        results: list[dict[str, Any]] = []
        adopted_count = 0
        skipped_count = 0
        for candidate in scan.candidates:
            if kb is None:
                raise AssertionError("eligible legacy corpus requires a knowledge base")
            if adoption_state is None:
                raise AssertionError("apply mode requires an adoption fence")
            parent_count = candidate.parent_chunk_count
            try:
                outcome = self._catalog.adopt_legacy(
                    tenant_id=tenant,
                    knowledge_base_id=kb.id,
                    canonical_name=candidate.canonical_name,
                    owner_id=owner_id,
                    legacy_identity=candidate.legacy_identity,
                    corpus_fingerprint=corpus_fingerprint,
                    adoption_fence=adoption_state.fence,
                    source_object_key=candidate.canonical_name,
                    vector_collection=collection,
                    chunk_count=candidate.chunk_count,
                    parent_chunk_count=parent_count,
                    content_sha256=candidate.content_sha256,
                    media_type=candidate.file_type,
                    index_version="legacy",
                )
            except AppError as exc:
                public_code = str(exc.public_error.code)
                results.append(
                    _candidate_summary(
                        candidate,
                        parent_count=parent_count,
                        status="skipped",
                        reason=public_code.lower(),
                    )
                )
                skipped_count += 1
                continue
            except Exception as exc:
                raise LegacyAdoptionError(ErrorCode.STORAGE_UNAVAILABLE.value) from exc
            current = outcome.document.current_version
            satisfied_without_adoption = outcome.reason in {
                "already_adopted",
                "legacy_tombstoned",
                "catalog_current_suppresses_legacy",
            } or bool(
                outcome.reason == "catalog_not_empty"
                and current is not None
                and current.status == DocumentVersionStatus.READY
                and current.storage_layout == StorageLayout.VERSIONED
            )
            if outcome.adopted:
                adopted_count += 1
                status = "adopted"
            elif satisfied_without_adoption:
                status = "satisfied"
            else:
                skipped_count += 1
                status = "skipped"
            results.append(
                _candidate_summary(
                    candidate,
                    parent_count=parent_count,
                    status=status,
                    reason=outcome.reason,
                )
            )
        if skipped_count == 0:
            try:
                if adoption_state is None:
                    raise AssertionError("apply mode requires an adoption fence")
                self._catalog.mark_legacy_adoption_complete(
                    tenant_id=tenant,
                    legacy_collection=collection,
                    knowledge_base_name=knowledge_base,
                    knowledge_base_id=(kb.id if kb is not None else None),
                    corpus_fingerprint=corpus_fingerprint,
                    adoption_fence=adoption_state.fence,
                )
            except AppError as exc:
                raise LegacyAdoptionError(str(exc.public_error.code)) from exc
            except Exception as exc:
                raise LegacyAdoptionError(ErrorCode.STORAGE_UNAVAILABLE.value) from exc
        summary.update(
            {
                "status": (
                    "error"
                    if skipped_count
                    else ("ok" if adopted_count else "no_changes")
                ),
                "error_code": ("LEGACY_ADOPTION_INCOMPLETE" if skipped_count else None),
                "documents_adopted": adopted_count,
                "documents_skipped": skipped_count,
                "adoption_complete": skipped_count == 0,
                "results": results,
            }
        )
        if summary["error_code"] is None:
            summary.pop("error_code")
        return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Adopt legacy filename-scoped Milvus documents into the Catalog."
    )
    parser.add_argument("--owner-username", required=True)
    parser.add_argument("--tenant-id", default="default")
    parser.add_argument("--knowledge-base-name", default="默认知识库")
    parser.add_argument(
        "--create-target-knowledge-base",
        action="store_true",
        help="Explicitly create the named target before adopting a non-empty corpus.",
    )
    parser.add_argument("--dry-run", action="store_true")
    return parser


def _default_adopter() -> LegacyDocumentCatalogAdopter:
    return LegacyDocumentCatalogAdopter(
        store=get_milvus_store(),
        catalog=DocumentCatalog(),
        session_factory=SessionLocal,
    )


def _error_summary(code: str) -> dict[str, Any]:
    return {
        "schema_version": _SUMMARY_SCHEMA_VERSION,
        "operation": "legacy_document_catalog_adoption",
        "status": "error",
        "error_code": code,
    }


def main(
    argv: list[str] | None = None,
    *,
    adopter: LegacyDocumentCatalogAdopter | None = None,
) -> int:
    args = build_parser().parse_args(argv)
    try:
        summary = (adopter or _default_adopter()).run(
            owner_username=args.owner_username,
            tenant_id=args.tenant_id,
            knowledge_base_name=args.knowledge_base_name,
            dry_run=args.dry_run,
            create_target_knowledge_base=args.create_target_knowledge_base,
        )
    except LegacyAdoptionError as exc:
        print(
            json.dumps(_error_summary(exc.code), ensure_ascii=False, sort_keys=True),
            file=sys.stderr,
        )
        return 2
    except Exception:
        print(
            json.dumps(
                _error_summary(ErrorCode.INTERNAL_ERROR.value),
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0 if summary.get("status") != "error" else 2


if __name__ == "__main__":
    raise SystemExit(main())
