"""Pure Interface for the deep Web Research Module.

Stable evidence, citation and budget contracts keep output knowledge local to
one Seam.  Search, HTTP and Tool Adapters gain the same validation Leverage
without duplicating policy.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Final
from urllib.parse import quote, urlsplit, urlunsplit


_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_EVIDENCE_ID_RE = re.compile(r"web_ev_[0-9a-f]{64}")
_CITATION_ID_RE = re.compile(r"web_cit_[0-9a-f]{64}")
_UPPER_PERCENT_ESCAPE_RE = re.compile(r"%(?:[0-9A-F]{2})")
_URL_UNRESERVED = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"
)

_HARD_MAX_QUERY_BYTES: Final = 16 * 1024
_HARD_MAX_URL_BYTES: Final = 16 * 1024
_HARD_MAX_TITLE_BYTES: Final = 4 * 1024
_HARD_MAX_SNIPPET_BYTES: Final = 32 * 1024
_HARD_MAX_CONTENT_BYTES: Final = 2 * 1024 * 1024
_HARD_MAX_TOTAL_EVIDENCE_BYTES: Final = 8 * 1024 * 1024
_HARD_MAX_RESPONSE_BYTES: Final = 8 * 1024 * 1024
_HARD_MAX_REDIRECTS: Final = 10
_HARD_MAX_EVIDENCE_ITEMS: Final = 50
_HARD_MAX_CITATIONS: Final = 100


def _canonical_fingerprint(payload: Any) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def _compact_json_size(payload: Any) -> int:
    return len(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )


def _integer(
    value: int,
    *,
    field: str,
    minimum: int,
    maximum: int,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{field} must be an integer")
    if not minimum <= value <= maximum:
        raise ValueError(f"{field} must be between {minimum} and {maximum}")
    return value


def _utf8_size(value: str, *, field: str) -> int:
    if not isinstance(value, str):
        raise TypeError(f"{field} must be a string")
    try:
        return len(value.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise ValueError(f"{field} contains invalid Unicode") from exc


def _bounded_text(
    value: str,
    *,
    field: str,
    max_bytes: int,
    allow_empty: bool,
    canonical_whitespace: bool = False,
) -> str:
    size = _utf8_size(value, field=field)
    if "\x00" in value:
        raise ValueError(f"{field} cannot contain NUL bytes")
    normalized = value.strip() if canonical_whitespace else value
    if not allow_empty and not normalized.strip():
        raise ValueError(f"{field} must be non-empty")
    if canonical_whitespace and normalized != value:
        value = normalized
        size = _utf8_size(value, field=field)
    if size > max_bytes:
        raise ValueError(f"{field} exceeds its size limit")
    return value


def _validate_percent_escapes(value: str, *, field: str) -> None:
    index = 0
    while index < len(value):
        if value[index] != "%":
            index += 1
            continue
        escape = value[index : index + 3]
        if not _UPPER_PERCENT_ESCAPE_RE.fullmatch(escape):
            raise ValueError(f"{field} must use canonical percent escapes")
        if chr(int(escape[1:], 16)) in _URL_UNRESERVED:
            raise ValueError(f"{field} contains a non-canonical percent escape")
        index += 3


def _canonical_url(value: str, *, max_bytes: int) -> str:
    _bounded_text(
        value,
        field="canonical_url",
        max_bytes=max_bytes,
        allow_empty=False,
    )
    if not value.isascii() or value != value.strip() or "\\" in value:
        raise ValueError("canonical_url must be a canonical ASCII URL")
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        raise ValueError("canonical_url cannot contain control characters")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("canonical_url is invalid") from exc
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("canonical_url must use http or https")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("canonical_url cannot contain user information")
    host = parsed.hostname
    if not host or host != host.lower() or host.endswith("."):
        raise ValueError("canonical_url host is not canonical")
    if parsed.fragment:
        raise ValueError("canonical_url cannot contain a fragment")
    default_port = 80 if parsed.scheme == "http" else 443
    effective_port = port or default_port
    bracketed_host = f"[{host}]" if ":" in host else host
    authority = (
        bracketed_host
        if effective_port == default_port
        else f"{bracketed_host}:{effective_port}"
    )
    path = parsed.path or "/"
    if parsed.netloc != authority or parsed.path != path:
        raise ValueError("canonical_url authority or path is not canonical")
    _validate_percent_escapes(path, field="canonical_url path")
    _validate_percent_escapes(parsed.query, field="canonical_url query")
    if any(segment in {".", ".."} for segment in path.split("/")):
        raise ValueError("canonical_url path contains a dot segment")
    if (
        quote(path, safe="/!$&'()*+,;=:@-._~%") != path
        or quote(
            parsed.query,
            safe="/?!$&'()*+,;=:@-._~%",
        )
        != parsed.query
    ):
        raise ValueError("canonical_url contains a non-canonical URL character")
    if urlunsplit((parsed.scheme, authority, path, parsed.query, "")) != value:
        raise ValueError("canonical_url is not canonical")
    return value


def _content_digest(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _evidence_id(canonical_url: str, content_sha256: str) -> str:
    digest = _canonical_fingerprint(
        {
            "canonical_url": canonical_url,
            "content_sha256": content_sha256,
            "schema_version": 1,
        }
    )
    return f"web_ev_{digest}"


def _citation_id(evidence_id: str) -> str:
    digest = _canonical_fingerprint(
        {
            "evidence_id": evidence_id,
            "schema_version": 1,
        }
    )
    return f"web_cit_{digest}"


class WebResearchContractCode(StrEnum):
    INVALID_INPUT = "WEB_INVALID_INPUT"
    INPUT_TOO_LARGE = "WEB_INPUT_TOO_LARGE"
    OUTPUT_TOO_LARGE = "WEB_OUTPUT_TOO_LARGE"
    INVALID_EVIDENCE = "WEB_INVALID_EVIDENCE"
    INVALID_CITATION = "WEB_INVALID_CITATION"


class WebResearchContractError(ValueError):
    """Stable contract failure that never embeds research content."""

    def __init__(
        self,
        code: WebResearchContractCode | str,
        message: str,
        *,
        safe_details: dict[str, int | bool] | None = None,
    ) -> None:
        self.code = WebResearchContractCode(code)
        self.safe_details = dict(safe_details or {})
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class WebResearchLimits:
    """Hard-capped limits shared by search, fetch and result Adapters."""

    max_query_bytes: int = 4 * 1024
    max_url_bytes: int = 4 * 1024
    max_title_bytes: int = 1024
    max_snippet_bytes: int = 8 * 1024
    max_content_bytes: int = 256 * 1024
    max_total_evidence_bytes: int = 512 * 1024
    max_response_bytes: int = 2 * 1024 * 1024
    max_redirects: int = 5
    max_evidence_items: int = 12
    max_citations: int = 32

    def __post_init__(self) -> None:
        ceilings = {
            "max_query_bytes": _HARD_MAX_QUERY_BYTES,
            "max_url_bytes": _HARD_MAX_URL_BYTES,
            "max_title_bytes": _HARD_MAX_TITLE_BYTES,
            "max_snippet_bytes": _HARD_MAX_SNIPPET_BYTES,
            "max_content_bytes": _HARD_MAX_CONTENT_BYTES,
            "max_total_evidence_bytes": _HARD_MAX_TOTAL_EVIDENCE_BYTES,
            "max_response_bytes": _HARD_MAX_RESPONSE_BYTES,
            "max_evidence_items": _HARD_MAX_EVIDENCE_ITEMS,
            "max_citations": _HARD_MAX_CITATIONS,
        }
        for field_name, maximum in ceilings.items():
            _integer(
                getattr(self, field_name),
                field=field_name,
                minimum=1,
                maximum=maximum,
            )
        _integer(
            self.max_redirects,
            field="max_redirects",
            minimum=0,
            maximum=_HARD_MAX_REDIRECTS,
        )
        if self.max_total_evidence_bytes < self.max_content_bytes:
            raise ValueError(
                "max_total_evidence_bytes cannot be smaller than max_content_bytes"
            )


DEFAULT_WEB_RESEARCH_LIMITS: Final = WebResearchLimits()


@dataclass(frozen=True, slots=True)
class WebResearchQuery:
    query: str = field(repr=False)
    max_results: int = 5

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "query",
            _bounded_text(
                self.query,
                field="query",
                max_bytes=_HARD_MAX_QUERY_BYTES,
                allow_empty=False,
                canonical_whitespace=True,
            ),
        )
        _integer(
            self.max_results,
            field="max_results",
            minimum=1,
            maximum=_HARD_MAX_EVIDENCE_ITEMS,
        )

    @classmethod
    def create(
        cls,
        query: str,
        *,
        max_results: int = 5,
        limits: WebResearchLimits = DEFAULT_WEB_RESEARCH_LIMITS,
    ) -> WebResearchQuery:
        if not isinstance(limits, WebResearchLimits):
            raise TypeError("limits must be WebResearchLimits")
        try:
            normalized = _bounded_text(
                query,
                field="query",
                max_bytes=limits.max_query_bytes,
                allow_empty=False,
                canonical_whitespace=True,
            )
            _integer(
                max_results,
                field="max_results",
                minimum=1,
                maximum=limits.max_evidence_items,
            )
        except (TypeError, ValueError) as exc:
            code = (
                WebResearchContractCode.INPUT_TOO_LARGE
                if "size limit" in str(exc)
                else WebResearchContractCode.INVALID_INPUT
            )
            raise WebResearchContractError(
                code, "Web research input is invalid"
            ) from exc
        return cls(query=normalized, max_results=max_results)

    def observability_metadata(self) -> dict[str, int]:
        """Return input sizes only; raw or hashed queries cross no audit Seam."""

        return {
            "max_results": self.max_results,
            "query_bytes": len(self.query.encode("utf-8")),
        }


@dataclass(frozen=True, slots=True)
class WebEvidence:
    evidence_id: str = field(repr=False)
    canonical_url: str = field(repr=False)
    title: str = field(repr=False)
    snippet: str = field(repr=False)
    content: str = field(repr=False)
    content_sha256: str = field(repr=False)
    retrieved_at: datetime
    schema_version: int = 1

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ValueError("WebEvidence schema_version must be 1")
        if not isinstance(self.evidence_id, str) or not _EVIDENCE_ID_RE.fullmatch(
            self.evidence_id
        ):
            raise ValueError("WebEvidence evidence_id is invalid")
        canonical_url = _canonical_url(
            self.canonical_url,
            max_bytes=_HARD_MAX_URL_BYTES,
        )
        object.__setattr__(self, "canonical_url", canonical_url)
        object.__setattr__(
            self,
            "title",
            _bounded_text(
                self.title,
                field="WebEvidence title",
                max_bytes=_HARD_MAX_TITLE_BYTES,
                allow_empty=True,
            ),
        )
        object.__setattr__(
            self,
            "snippet",
            _bounded_text(
                self.snippet,
                field="WebEvidence snippet",
                max_bytes=_HARD_MAX_SNIPPET_BYTES,
                allow_empty=True,
            ),
        )
        object.__setattr__(
            self,
            "content",
            _bounded_text(
                self.content,
                field="WebEvidence content",
                max_bytes=_HARD_MAX_CONTENT_BYTES,
                allow_empty=False,
            ),
        )
        if (
            not isinstance(self.content_sha256, str)
            or not _SHA256_RE.fullmatch(self.content_sha256)
            or self.content_sha256 != _content_digest(self.content)
        ):
            raise ValueError("WebEvidence content_sha256 is invalid")
        expected_id = _evidence_id(self.canonical_url, self.content_sha256)
        if self.evidence_id != expected_id:
            raise ValueError("WebEvidence evidence_id does not match its source")
        if not isinstance(self.retrieved_at, datetime):
            raise TypeError("WebEvidence retrieved_at must be a datetime")
        if self.retrieved_at.tzinfo is None or self.retrieved_at.utcoffset() is None:
            raise ValueError("WebEvidence retrieved_at must be timezone-aware")
        object.__setattr__(
            self,
            "retrieved_at",
            self.retrieved_at.astimezone(timezone.utc),
        )

    @classmethod
    def create(
        cls,
        *,
        canonical_url: str,
        title: str,
        snippet: str,
        content: str,
        retrieved_at: datetime,
        limits: WebResearchLimits = DEFAULT_WEB_RESEARCH_LIMITS,
    ) -> WebEvidence:
        if not isinstance(limits, WebResearchLimits):
            raise TypeError("limits must be WebResearchLimits")
        try:
            source = _canonical_url(canonical_url, max_bytes=limits.max_url_bytes)
            safe_title = _bounded_text(
                title,
                field="WebEvidence title",
                max_bytes=limits.max_title_bytes,
                allow_empty=True,
            )
            safe_snippet = _bounded_text(
                snippet,
                field="WebEvidence snippet",
                max_bytes=limits.max_snippet_bytes,
                allow_empty=True,
            )
            safe_content = _bounded_text(
                content,
                field="WebEvidence content",
                max_bytes=limits.max_content_bytes,
                allow_empty=False,
            )
        except (TypeError, ValueError) as exc:
            raise WebResearchContractError(
                WebResearchContractCode.OUTPUT_TOO_LARGE
                if "size limit" in str(exc)
                else WebResearchContractCode.INVALID_EVIDENCE,
                "Web evidence is invalid",
            ) from exc
        content_sha256 = _content_digest(safe_content)
        return cls(
            evidence_id=_evidence_id(source, content_sha256),
            canonical_url=source,
            title=safe_title,
            snippet=safe_snippet,
            content=safe_content,
            content_sha256=content_sha256,
            retrieved_at=retrieved_at,
        )

    @property
    def encoded_size(self) -> int:
        return _compact_json_size(self.to_public_dict())

    @property
    def citation_token(self) -> str:
        return f"[source](webcite:{self.evidence_id})"

    @property
    def source_domain(self) -> str:
        return urlsplit(self.canonical_url).hostname or ""

    def to_public_dict(self) -> dict[str, str | int]:
        return {
            "canonical_url": self.canonical_url,
            "content": self.content,
            "content_sha256": self.content_sha256,
            "evidence_id": self.evidence_id,
            "retrieved_at": self.retrieved_at.isoformat().replace("+00:00", "Z"),
            "schema_version": self.schema_version,
            "snippet": self.snippet,
            "title": self.title,
        }

    def to_tool_dict(self) -> dict[str, str | int]:
        data = self.to_public_dict()
        data.pop("canonical_url")
        data["citation_token"] = self.citation_token
        data["source_domain"] = self.source_domain
        return data

    def observability_metadata(self) -> dict[str, int]:
        """Return aggregate-safe sizes; never return URL, title, hash or body."""

        return {
            "content_bytes": len(self.content.encode("utf-8")),
            "snippet_bytes": len(self.snippet.encode("utf-8")),
            "title_bytes": len(self.title.encode("utf-8")),
        }


@dataclass(frozen=True, slots=True)
class WebCitation:
    citation_id: str = field(repr=False)
    evidence_id: str = field(repr=False)
    schema_version: int = 1

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ValueError("WebCitation schema_version must be 1")
        if not isinstance(self.evidence_id, str) or not _EVIDENCE_ID_RE.fullmatch(
            self.evidence_id
        ):
            raise ValueError("WebCitation evidence_id is invalid")
        if not isinstance(self.citation_id, str) or not _CITATION_ID_RE.fullmatch(
            self.citation_id
        ):
            raise ValueError("WebCitation citation_id is invalid")
        if self.citation_id != _citation_id(self.evidence_id):
            raise ValueError("WebCitation citation_id does not match evidence_id")

    @classmethod
    def from_evidence(cls, evidence: WebEvidence) -> WebCitation:
        if not isinstance(evidence, WebEvidence):
            raise TypeError("evidence must be WebEvidence")
        return cls(
            citation_id=_citation_id(evidence.evidence_id),
            evidence_id=evidence.evidence_id,
        )

    @property
    def encoded_size(self) -> int:
        return _compact_json_size(self.to_public_dict())

    def to_public_dict(self) -> dict[str, str | int]:
        return {
            "citation_id": self.citation_id,
            "evidence_id": self.evidence_id,
            "schema_version": self.schema_version,
        }


@dataclass(frozen=True, slots=True)
class WebResearchResult:
    evidence: tuple[WebEvidence, ...]
    citations: tuple[WebCitation, ...]
    truncated: bool = False
    schema_version: int = 1

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ValueError("WebResearchResult schema_version must be 1")
        if not isinstance(self.truncated, bool):
            raise TypeError("WebResearchResult truncated must be a bool")
        evidence = tuple(self.evidence)
        citations = tuple(self.citations)
        if any(not isinstance(item, WebEvidence) for item in evidence):
            raise TypeError("evidence must contain WebEvidence values")
        if any(not isinstance(item, WebCitation) for item in citations):
            raise TypeError("citations must contain WebCitation values")
        if len(evidence) > _HARD_MAX_EVIDENCE_ITEMS:
            raise ValueError("WebResearchResult has too many evidence items")
        if len(citations) > _HARD_MAX_CITATIONS:
            raise ValueError("WebResearchResult has too many citations")
        evidence_ids = [item.evidence_id for item in evidence]
        citation_ids = [item.citation_id for item in citations]
        if len(evidence_ids) != len(set(evidence_ids)):
            raise ValueError("WebResearchResult evidence identities must be unique")
        if len(citation_ids) != len(set(citation_ids)):
            raise ValueError("WebResearchResult citation identities must be unique")
        unknown = {citation.evidence_id for citation in citations} - set(evidence_ids)
        if unknown:
            raise ValueError("WebResearchResult citation references unknown evidence")
        object.__setattr__(self, "evidence", evidence)
        object.__setattr__(self, "citations", citations)
        if self.encoded_size > _HARD_MAX_TOTAL_EVIDENCE_BYTES:
            raise ValueError("WebResearchResult exceeds the hard output size limit")

    @classmethod
    def create(
        cls,
        evidence: tuple[WebEvidence, ...] | list[WebEvidence],
        *,
        citations: tuple[WebCitation, ...] | list[WebCitation] | None = None,
        truncated: bool = False,
        limits: WebResearchLimits = DEFAULT_WEB_RESEARCH_LIMITS,
    ) -> WebResearchResult:
        if not isinstance(limits, WebResearchLimits):
            raise TypeError("limits must be WebResearchLimits")
        evidence_items = tuple(evidence)
        citation_items = (
            tuple(citations)
            if citations is not None
            else tuple(WebCitation.from_evidence(item) for item in evidence_items)
        )
        if len(evidence_items) > limits.max_evidence_items:
            raise WebResearchContractError(
                WebResearchContractCode.OUTPUT_TOO_LARGE,
                "Web research result has too many evidence items",
                safe_details={"max_evidence_items": limits.max_evidence_items},
            )
        if len(citation_items) > limits.max_citations:
            raise WebResearchContractError(
                WebResearchContractCode.OUTPUT_TOO_LARGE,
                "Web research result has too many citations",
                safe_details={"max_citations": limits.max_citations},
            )
        try:
            result = cls(
                evidence=evidence_items,
                citations=citation_items,
                truncated=truncated,
            )
        except (TypeError, ValueError) as exc:
            message = str(exc)
            code = (
                WebResearchContractCode.OUTPUT_TOO_LARGE
                if "output size limit" in message
                else WebResearchContractCode.INVALID_CITATION
                if "citation" in message
                else WebResearchContractCode.INVALID_EVIDENCE
            )
            raise WebResearchContractError(
                code,
                "Web research result is invalid",
            ) from exc
        if result.tool_encoded_size > limits.max_total_evidence_bytes:
            raise WebResearchContractError(
                WebResearchContractCode.OUTPUT_TOO_LARGE,
                "Web research result exceeds its output size limit",
                safe_details={
                    "max_total_evidence_bytes": limits.max_total_evidence_bytes
                },
            )
        return result

    @property
    def encoded_size(self) -> int:
        return _compact_json_size(self.to_public_dict())

    @property
    def tool_encoded_size(self) -> int:
        return _compact_json_size(self.to_tool_dict())

    def to_public_dict(self) -> dict[str, object]:
        return {
            "citations": [item.to_public_dict() for item in self.citations],
            "evidence": [item.to_public_dict() for item in self.evidence],
            "schema_version": self.schema_version,
            "truncated": self.truncated,
        }

    def to_tool_dict(self) -> dict[str, object]:
        return {
            "citations": [item.to_public_dict() for item in self.citations],
            "evidence": [item.to_tool_dict() for item in self.evidence],
            "schema_version": self.schema_version,
            "truncated": self.truncated,
        }

    def observability_metadata(self) -> dict[str, int | bool]:
        """Return aggregate-only metadata safe for ToolAudit and tracing."""

        return {
            "citation_count": len(self.citations),
            "evidence_count": len(self.evidence),
            "output_bytes": self.encoded_size,
            "truncated": self.truncated,
        }

    def tool_observability_metadata(self) -> dict[str, int | bool]:
        metadata = self.observability_metadata()
        metadata["output_bytes"] = self.tool_encoded_size
        return metadata


__all__ = [
    "DEFAULT_WEB_RESEARCH_LIMITS",
    "WebCitation",
    "WebEvidence",
    "WebResearchContractCode",
    "WebResearchContractError",
    "WebResearchLimits",
    "WebResearchQuery",
    "WebResearchResult",
]
