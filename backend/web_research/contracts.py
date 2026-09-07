"""Contracts for Run-local web sources and bounded provider results."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Final, Sequence
from urllib.parse import urlsplit


_SOURCE_ID_RE = re.compile(r"S[1-9][0-9]{0,2}")

_HARD_MAX_QUERY_BYTES: Final = 16 * 1024
_HARD_MAX_URL_BYTES: Final = 16 * 1024
_HARD_MAX_TITLE_BYTES: Final = 4 * 1024
_HARD_MAX_CONTENT_BYTES: Final = 2 * 1024 * 1024
_HARD_MAX_EVIDENCE_ITEMS: Final = 50


def _compact_json_size(payload: object) -> int:
    return len(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    )


def _integer(
    value: int,
    *,
    field_name: str,
    minimum: int,
    maximum: int,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{field_name} must be an integer")
    if not minimum <= value <= maximum:
        raise ValueError(f"{field_name} must be between {minimum} and {maximum}")
    return value


def _bounded_text(
    value: str,
    *,
    field_name: str,
    max_bytes: int,
    allow_empty: bool,
    strip: bool = False,
) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    normalized = value.strip() if strip else value
    if "\x00" in normalized:
        raise ValueError(f"{field_name} cannot contain NUL bytes")
    if not allow_empty and not normalized.strip():
        raise ValueError(f"{field_name} must be non-empty")
    try:
        size = len(normalized.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise ValueError(f"{field_name} contains invalid Unicode") from exc
    if size > max_bytes:
        raise ValueError(f"{field_name} exceeds its size limit")
    return normalized


def _web_url(value: str, *, max_bytes: int) -> str:
    url = _bounded_text(
        value,
        field_name="url",
        max_bytes=max_bytes,
        allow_empty=False,
        strip=True,
    )
    if any(character.isspace() for character in url) or any(
        marker in url for marker in ("<", ">", "\\")
    ):
        raise ValueError("url must be a normal HTTP(S) URL")
    try:
        parsed = urlsplit(url)
        parsed.port
    except ValueError as exc:
        raise ValueError("url must be a valid HTTP(S) URL") from exc
    if (
        parsed.scheme.casefold() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ValueError("url must be a normal HTTP(S) URL")
    return url


def validate_source_id(value: str) -> str:
    if not isinstance(value, str) or _SOURCE_ID_RE.fullmatch(value) is None:
        raise ValueError("source_id must look like S1")
    return value


def _validated_tool_source_id(source_id: str) -> str:
    try:
        return validate_source_id(source_id)
    except ValueError as exc:
        raise WebResearchContractError(
            WebResearchContractCode.INVALID_SOURCE_ID,
            "Web source ID is invalid",
        ) from exc


class WebResearchContractCode(StrEnum):
    INVALID_INPUT = "WEB_INVALID_INPUT"
    INPUT_TOO_LARGE = "WEB_INPUT_TOO_LARGE"
    OUTPUT_TOO_LARGE = "WEB_OUTPUT_TOO_LARGE"
    INVALID_EVIDENCE = "WEB_INVALID_EVIDENCE"
    INVALID_SOURCE_ID = "WEB_INVALID_SOURCE_ID"


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
    """Per-input and per-provider-result bounds."""

    max_query_bytes: int = 4 * 1024
    max_url_bytes: int = 4 * 1024
    max_title_bytes: int = 1024
    max_evidence_items: int = 3

    def __post_init__(self) -> None:
        ceilings = {
            "max_query_bytes": _HARD_MAX_QUERY_BYTES,
            "max_url_bytes": _HARD_MAX_URL_BYTES,
            "max_title_bytes": _HARD_MAX_TITLE_BYTES,
            "max_evidence_items": _HARD_MAX_EVIDENCE_ITEMS,
        }
        for field_name, maximum in ceilings.items():
            _integer(
                getattr(self, field_name),
                field_name=field_name,
                minimum=1,
                maximum=maximum,
            )


DEFAULT_WEB_RESEARCH_LIMITS: Final = WebResearchLimits()


@dataclass(frozen=True, slots=True)
class WebResearchQuery:
    query: str = field(repr=False)
    max_results: int = 3

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "query",
            _bounded_text(
                self.query,
                field_name="query",
                max_bytes=_HARD_MAX_QUERY_BYTES,
                allow_empty=False,
                strip=True,
            ),
        )
        _integer(
            self.max_results,
            field_name="max_results",
            minimum=1,
            maximum=_HARD_MAX_EVIDENCE_ITEMS,
        )

    @classmethod
    def create(
        cls,
        query: str,
        *,
        max_results: int = 3,
        limits: WebResearchLimits = DEFAULT_WEB_RESEARCH_LIMITS,
    ) -> WebResearchQuery:
        if not isinstance(limits, WebResearchLimits):
            raise TypeError("limits must be WebResearchLimits")
        try:
            normalized = _bounded_text(
                query,
                field_name="query",
                max_bytes=limits.max_query_bytes,
                allow_empty=False,
                strip=True,
            )
            _integer(
                max_results,
                field_name="max_results",
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
                code,
                "Web research input is invalid",
            ) from exc
        return cls(query=normalized, max_results=max_results)

    def observability_metadata(self) -> dict[str, int]:
        return {
            "max_results": self.max_results,
            "query_bytes": len(self.query.encode("utf-8")),
        }


@dataclass(frozen=True, slots=True)
class WebEvidence:
    """Server-side source content returned by the Web Research provider."""

    url: str = field(repr=False)
    title: str = field(repr=False)
    content: str = field(repr=False)
    retrieved_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "url",
            _web_url(self.url, max_bytes=_HARD_MAX_URL_BYTES),
        )
        object.__setattr__(
            self,
            "title",
            _bounded_text(
                self.title,
                field_name="WebEvidence title",
                max_bytes=_HARD_MAX_TITLE_BYTES,
                allow_empty=True,
            ),
        )
        object.__setattr__(
            self,
            "content",
            _bounded_text(
                self.content,
                field_name="WebEvidence content",
                max_bytes=_HARD_MAX_CONTENT_BYTES,
                allow_empty=False,
            ),
        )
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
        url: str,
        title: str,
        content: str,
        retrieved_at: datetime,
        limits: WebResearchLimits = DEFAULT_WEB_RESEARCH_LIMITS,
    ) -> WebEvidence:
        if not isinstance(limits, WebResearchLimits):
            raise TypeError("limits must be WebResearchLimits")
        try:
            source_url = _web_url(url, max_bytes=limits.max_url_bytes)
            safe_title = _bounded_text(
                title,
                field_name="WebEvidence title",
                max_bytes=limits.max_title_bytes,
                allow_empty=True,
            )
            safe_content = _bounded_text(
                content,
                field_name="WebEvidence content",
                max_bytes=_HARD_MAX_CONTENT_BYTES,
                allow_empty=False,
            )
        except (TypeError, ValueError) as exc:
            code = (
                WebResearchContractCode.OUTPUT_TOO_LARGE
                if "size limit" in str(exc)
                else WebResearchContractCode.INVALID_EVIDENCE
            )
            raise WebResearchContractError(
                code,
                "Web evidence is invalid",
            ) from exc
        return cls(
            url=source_url,
            title=safe_title,
            content=safe_content,
            retrieved_at=retrieved_at,
        )

    @property
    def encoded_size(self) -> int:
        return _compact_json_size(self.to_public_dict())

    def to_public_dict(self) -> dict[str, str]:
        return {
            "url": self.url,
            "title": self.title,
            "content": self.content,
            "retrieved_at": self.retrieved_at.isoformat().replace("+00:00", "Z"),
        }

    @property
    def source_domain(self) -> str:
        return urlsplit(self.url).hostname or ""

    def to_search_tool_dict(self, source_id: str) -> dict[str, str]:
        return {
            "source_id": _validated_tool_source_id(source_id),
            "title": self.title,
            "source": self.source_domain,
            "snippet": self.content,
        }

    def to_fetch_tool_dict(self, source_id: str) -> dict[str, str]:
        return {
            "source_id": _validated_tool_source_id(source_id),
            "title": self.title,
            "content": self.content,
        }

    def observability_metadata(self) -> dict[str, int]:
        return {
            "content_bytes": len(self.content.encode("utf-8")),
            "title_bytes": len(self.title.encode("utf-8")),
        }


@dataclass(frozen=True, slots=True)
class WebResearchResult:
    evidence: tuple[WebEvidence, ...]
    truncated: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.truncated, bool):
            raise TypeError("WebResearchResult truncated must be a bool")
        evidence = tuple(self.evidence)
        if any(not isinstance(item, WebEvidence) for item in evidence):
            raise TypeError("evidence must contain WebEvidence values")
        if len(evidence) > _HARD_MAX_EVIDENCE_ITEMS:
            raise ValueError("WebResearchResult has too many evidence items")
        urls = [item.url for item in evidence]
        if len(urls) != len(set(urls)):
            raise ValueError("WebResearchResult source URLs must be unique")
        object.__setattr__(self, "evidence", evidence)

    @classmethod
    def create(
        cls,
        evidence: Sequence[WebEvidence],
        *,
        truncated: bool = False,
        limits: WebResearchLimits = DEFAULT_WEB_RESEARCH_LIMITS,
    ) -> WebResearchResult:
        if not isinstance(limits, WebResearchLimits):
            raise TypeError("limits must be WebResearchLimits")
        items = tuple(evidence)
        if len(items) > limits.max_evidence_items:
            raise WebResearchContractError(
                WebResearchContractCode.OUTPUT_TOO_LARGE,
                "Web research result has too many sources",
                safe_details={"max_evidence_items": limits.max_evidence_items},
            )
        try:
            return cls(evidence=items, truncated=truncated)
        except (TypeError, ValueError) as exc:
            raise WebResearchContractError(
                WebResearchContractCode.INVALID_EVIDENCE,
                "Web research result is invalid",
            ) from exc

    @property
    def encoded_size(self) -> int:
        return _compact_json_size(self.to_public_dict())

    def to_public_dict(self) -> dict[str, object]:
        return {
            "evidence": [item.to_public_dict() for item in self.evidence],
            "truncated": self.truncated,
        }

    def _validated_source_ids(self, source_ids: Sequence[str]) -> tuple[str, ...]:
        ids = tuple(source_ids)
        if len(ids) != len(self.evidence):
            raise WebResearchContractError(
                WebResearchContractCode.INVALID_SOURCE_ID,
                "Web source IDs do not match the provider result",
            )
        return ids

    def to_search_tool_dict(self, source_ids: Sequence[str]) -> dict[str, object]:
        ids = self._validated_source_ids(source_ids)
        return {
            "sources": [
                item.to_search_tool_dict(source_id)
                for item, source_id in zip(self.evidence, ids, strict=True)
            ],
            "truncated": self.truncated,
        }

    def to_fetch_tool_dict(self, source_ids: Sequence[str]) -> dict[str, object]:
        ids = self._validated_source_ids(source_ids)
        return {
            "sources": [
                item.to_fetch_tool_dict(source_id)
                for item, source_id in zip(self.evidence, ids, strict=True)
            ],
            "truncated": self.truncated,
        }

    def observability_metadata(self) -> dict[str, int | bool]:
        return {
            "source_count": len(self.evidence),
            "output_bytes": self.encoded_size,
            "truncated": self.truncated,
        }

    def tool_observability_metadata(self) -> dict[str, int | bool]:
        return {
            "source_count": len(self.evidence),
            "truncated": self.truncated,
        }


__all__ = [
    "DEFAULT_WEB_RESEARCH_LIMITS",
    "WebEvidence",
    "WebResearchContractCode",
    "WebResearchContractError",
    "WebResearchLimits",
    "WebResearchQuery",
    "WebResearchResult",
    "validate_source_id",
]
