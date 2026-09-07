"""Run-local Source ID registry and final citation rendering."""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Final

from backend.web_research.contracts import (
    WebResearchResult,
    validate_source_id,
)


_SOURCE_TOKEN_RE = re.compile(r"\[(?P<source_id>S[1-9][0-9]{0,2})\](?!\s*\()")
_MAX_FINAL_CONTENT_BYTES: Final = 2 * 1024 * 1024
_MAX_SOURCES: Final = 128


class WebSourceLedgerCode(StrEnum):
    CONTEXT_CLOSED = "WEB_SOURCE_CONTEXT_CLOSED"
    SOURCE_LIMIT = "WEB_SOURCE_LIMIT"
    INVALID_CONTENT = "WEB_SOURCE_INVALID_CONTENT"
    UNKNOWN_SOURCE = "WEB_SOURCE_UNKNOWN"


_SAFE_ERROR_MESSAGES: Final = {
    WebSourceLedgerCode.CONTEXT_CLOSED: "Web source context is closed",
    WebSourceLedgerCode.SOURCE_LIMIT: "Web source limit exceeded",
    WebSourceLedgerCode.INVALID_CONTENT: "Web source content is invalid",
    WebSourceLedgerCode.UNKNOWN_SOURCE: "Web citation references an unknown source",
}


class WebSourceLedgerError(ValueError):
    """Stable source-registry failure without URL, query, or content details."""

    def __init__(
        self,
        code: WebSourceLedgerCode | str,
        *,
        safe_details: dict[str, int | bool] | None = None,
    ) -> None:
        self.code = WebSourceLedgerCode(code)
        self.safe_details = dict(safe_details or {})
        super().__init__(_SAFE_ERROR_MESSAGES[self.code])

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(code={self.code.value!r}, "
            f"safe_details={self.safe_details!r})"
        )


@dataclass(frozen=True, slots=True)
class WebSourceLedgerStatus:
    attempted: bool
    source_count: int


@dataclass(frozen=True, slots=True)
class WebSourceFinalization:
    content: str = field(repr=False)
    citation_count: int
    cited_source_count: int
    available_source_count: int
    rendering_applied: bool


@dataclass(frozen=True, slots=True)
class WebSourceReference:
    source_id: str
    url: str = field(repr=False)
    title: str = field(repr=False)
    default_query: str = field(repr=False)

    def __post_init__(self) -> None:
        validate_source_id(self.source_id)
        if not all(
            isinstance(value, str)
            for value in (self.url, self.title, self.default_query)
        ):
            raise TypeError("web source reference values must be strings")
        if not self.url or not self.default_query.strip():
            raise ValueError("web source reference is incomplete")


def _validated_content(content: str) -> str:
    if not isinstance(content, str):
        raise WebSourceLedgerError(WebSourceLedgerCode.INVALID_CONTENT)
    try:
        size = len(content.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise WebSourceLedgerError(WebSourceLedgerCode.INVALID_CONTENT) from exc
    if "\x00" in content or size > _MAX_FINAL_CONTENT_BYTES:
        raise WebSourceLedgerError(WebSourceLedgerCode.INVALID_CONTENT)
    return content


class WebSourceLedger:
    """Assign stable S1/S2 identifiers to sources in one Run."""

    __slots__ = ("_attempted", "_ids_by_url", "_sources")

    def __init__(self) -> None:
        self._attempted = False
        self._ids_by_url: dict[str, str] = {}
        self._sources: dict[str, WebSourceReference] = {}

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(attempted={self._attempted!r}, "
            f"source_count={len(self._sources)!r})"
        )

    def mark_attempted(self) -> None:
        self._attempted = True

    def register_search_result(
        self,
        result: WebResearchResult,
        *,
        query: str,
    ) -> tuple[str, ...]:
        if not isinstance(result, WebResearchResult):
            raise TypeError("result must be WebResearchResult")
        if not isinstance(query, str) or not query.strip():
            raise ValueError("query must be a non-empty string")
        self.mark_attempted()

        unseen_urls = {
            item.url for item in result.evidence if item.url not in self._ids_by_url
        }
        if len(self._sources) + len(unseen_urls) > _MAX_SOURCES:
            raise WebSourceLedgerError(WebSourceLedgerCode.SOURCE_LIMIT)

        source_ids: list[str] = []
        for item in result.evidence:
            source_id = self._ids_by_url.get(item.url)
            if source_id is None:
                source_id = f"S{len(self._sources) + 1}"
                reference = WebSourceReference(
                    source_id=source_id,
                    url=item.url,
                    title=item.title,
                    default_query=query.strip(),
                )
                self._sources[source_id] = reference
                self._ids_by_url[item.url] = source_id
            else:
                current = self._sources[source_id]
                if not current.title and item.title:
                    self._sources[source_id] = replace(current, title=item.title)
            source_ids.append(source_id)
        return tuple(source_ids)

    def resolve(self, source_id: str) -> WebSourceReference | None:
        try:
            validated = validate_source_id(source_id)
        except ValueError:
            return None
        return self._sources.get(validated)

    def status(self) -> WebSourceLedgerStatus:
        return WebSourceLedgerStatus(
            attempted=self._attempted,
            source_count=len(self._sources),
        )

    def finalize(self, content: str) -> WebSourceFinalization:
        content = _validated_content(content)
        matches = tuple(_SOURCE_TOKEN_RE.finditer(content))
        if not self._attempted and not matches:
            return WebSourceFinalization(
                content=content,
                citation_count=0,
                cited_source_count=0,
                available_source_count=0,
                rendering_applied=False,
            )

        cited_sources: set[str] = set()

        def render(match: re.Match[str]) -> str:
            source_id = match.group("source_id")
            source = self._sources.get(source_id)
            if source is None:
                raise WebSourceLedgerError(WebSourceLedgerCode.UNKNOWN_SOURCE)
            cited_sources.add(source_id)
            return f"[{source_id}](<{source.url}>)"

        rendered = _SOURCE_TOKEN_RE.sub(render, content)
        return WebSourceFinalization(
            content=rendered,
            citation_count=len(matches),
            cited_source_count=len(cited_sources),
            available_source_count=len(self._sources),
            rendering_applied=bool(matches),
        )

    def clear(self) -> None:
        self._attempted = False
        self._ids_by_url.clear()
        self._sources.clear()


__all__ = [
    "WebSourceFinalization",
    "WebSourceLedger",
    "WebSourceLedgerCode",
    "WebSourceLedgerError",
    "WebSourceLedgerStatus",
    "WebSourceReference",
]
