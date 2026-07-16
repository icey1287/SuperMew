"""Deterministic, Run-local rendering for Web Research citations.

The model may name evidence only through ``webcite:`` tokens.  This deep Module
keeps URL resolution, identity authorization and Markdown rendering behind one
small Interface so raw or cross-Run links never cross the terminal response
Seam.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final, Iterable

from backend.web_research.contracts import WebEvidence, WebResearchResult


_EVIDENCE_ID_RE = re.compile(r"web_ev_[0-9a-f]{64}")
_RAW_HTTP_URL_RE = re.compile(r"(?i)https?://")
_WEB_CITATION_MARKER_RE = re.compile(r"(?i)webcite:")
_WEB_CITATION_TOKEN_RE = re.compile(
    r"(?<!!)\[(?P<label>(?:\\.|[^\[\]\\\r\n])+)\]"
    r"\(webcite:(?P<evidence_id>web_ev_[0-9a-f]{64})\)"
)
_RAW_HTML_RE = re.compile(
    r"<(?:!--.*?--|![^<>]*|\?[^<>]*|//[^<>]*|/?[A-Za-z][^<>]*)>",
    re.DOTALL,
)
_REFERENCE_DEFINITION_RE = re.compile(r"(?m)^ {0,3}\[[^\]\r\n]+\]:")
_GFM_WWW_RE = re.compile(r"(?i)(?<![\w@])www\.[A-Za-z0-9]")
_GFM_FTP_RE = re.compile(r"(?i)(?<![A-Za-z0-9+.-])ftp://")
_GFM_EMAIL_RE = re.compile(
    r"(?i)(?<![\w@])[A-Z0-9._%+\-]+@"
    r"[A-Z0-9](?:[A-Z0-9.\-]*[A-Z0-9])?\.[A-Z]{2,}(?![\w@])"
)
_FENCE_OPEN_RE = re.compile(r" {0,3}(?P<fence>`{3,}|~{3,})(?P<info>[^\r\n]*)")
_FENCE_CLOSE_RE = re.compile(r" {0,3}(?P<fence>`{3,}|~{3,})[ \t]*")

_MAX_FINAL_CONTENT_BYTES: Final = 2 * 1024 * 1024
_MAX_LABEL_BYTES: Final = 4 * 1024
_MAX_LEDGER_EVIDENCE: Final = 128
_MAX_RENDERED_CITATIONS: Final = 256


class WebCitationLedgerCode(StrEnum):
    """Stable terminal-validation failures safe to expose across the Seam."""

    CONTEXT_CLOSED = "WEB_CITATION_CONTEXT_CLOSED"
    EVIDENCE_LIMIT = "WEB_CITATION_EVIDENCE_LIMIT"
    INVALID_CONTENT = "WEB_CITATION_INVALID_CONTENT"
    INVALID_TOKEN = "WEB_CITATION_INVALID_TOKEN"
    RAW_URL = "WEB_CITATION_RAW_URL"
    REQUIRED = "WEB_CITATION_REQUIRED"
    UNAUTHORIZED_LINK = "WEB_CITATION_UNAUTHORIZED_LINK"
    UNKNOWN_EVIDENCE = "WEB_CITATION_UNKNOWN_EVIDENCE"


class WebEvidenceKind(StrEnum):
    """How evidence crossed the Web Research Seam into this Run."""

    SEARCH_SNIPPET = "search_snippet"
    FETCHED_PAGE = "fetched_page"


_SAFE_ERROR_MESSAGES: Final = {
    WebCitationLedgerCode.CONTEXT_CLOSED: "Web citation context is closed",
    WebCitationLedgerCode.EVIDENCE_LIMIT: "Web citation evidence limit exceeded",
    WebCitationLedgerCode.INVALID_CONTENT: "Web citation content is invalid",
    WebCitationLedgerCode.INVALID_TOKEN: "Web citation token is invalid",
    WebCitationLedgerCode.RAW_URL: "Raw web URLs are not allowed in this response",
    WebCitationLedgerCode.REQUIRED: "A validated web citation is required",
    WebCitationLedgerCode.UNAUTHORIZED_LINK: (
        "Only validated Run-local web citation links are allowed"
    ),
    WebCitationLedgerCode.UNKNOWN_EVIDENCE: (
        "Web citation references unavailable evidence"
    ),
}


class WebCitationLedgerError(ValueError):
    """Stable failure whose text and repr never include content or identities."""

    def __init__(
        self,
        code: WebCitationLedgerCode | str,
        *,
        safe_details: dict[str, int | bool] | None = None,
    ) -> None:
        self.code = WebCitationLedgerCode(code)
        self.safe_details = dict(safe_details or {})
        super().__init__(_SAFE_ERROR_MESSAGES[self.code])

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(code={self.code.value!r}, "
            f"safe_details={self.safe_details!r})"
        )


@dataclass(frozen=True, slots=True)
class WebCitationLedgerStatus:
    """Aggregate-only state safe for terminal policy and diagnostics."""

    attempted: bool
    evidence_count: int
    citation_required: bool


@dataclass(frozen=True, slots=True)
class WebCitationFinalization:
    """Rendered output with a repr-safe aggregate validation summary."""

    content: str = field(repr=False)
    citation_count: int
    cited_evidence_count: int
    available_evidence_count: int
    validation_applied: bool


@dataclass(frozen=True, slots=True)
class _LedgerEvidence:
    evidence_id: str = field(repr=False)
    canonical_url: str = field(repr=False)
    title: str = field(repr=False)
    kind: WebEvidenceKind = field(repr=False)

    @classmethod
    def from_evidence(
        cls,
        evidence: WebEvidence,
        *,
        kind: WebEvidenceKind,
    ) -> _LedgerEvidence:
        return cls(
            evidence_id=evidence.evidence_id,
            canonical_url=evidence.canonical_url,
            title=evidence.title,
            kind=kind,
        )


def _validated_content(content: str) -> str:
    if not isinstance(content, str):
        raise WebCitationLedgerError(WebCitationLedgerCode.INVALID_CONTENT)
    try:
        size = len(content.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise WebCitationLedgerError(WebCitationLedgerCode.INVALID_CONTENT) from exc
    if "\x00" in content or size > _MAX_FINAL_CONTENT_BYTES:
        raise WebCitationLedgerError(WebCitationLedgerCode.INVALID_CONTENT)
    return content


def _validated_label(label: str) -> None:
    try:
        size = len(label.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise WebCitationLedgerError(WebCitationLedgerCode.INVALID_TOKEN) from exc
    if not label.strip() or size > _MAX_LABEL_BYTES:
        raise WebCitationLedgerError(WebCitationLedgerCode.INVALID_TOKEN)


def _contains_raw_http_url(value: str) -> bool:
    decoded = html.unescape(value)
    markdown_deescaped = re.sub(r"\\([:/])", r"\1", decoded)
    return bool(_RAW_HTTP_URL_RE.search(markdown_deescaped))


def _is_escaped(value: str, index: int) -> bool:
    backslashes = 0
    cursor = index - 1
    while cursor >= 0 and value[cursor] == "\\":
        backslashes += 1
        cursor -= 1
    return backslashes % 2 == 1


def _inline_code_segments(value: str) -> tuple[tuple[bool, str], ...]:
    segments: list[tuple[bool, str]] = []
    cursor = 0
    prose_start = 0
    while cursor < len(value):
        if value[cursor] != "`" or _is_escaped(value, cursor):
            cursor += 1
            continue
        run_end = cursor + 1
        while run_end < len(value) and value[run_end] == "`":
            run_end += 1
        width = run_end - cursor
        closing_start = run_end
        closing_end: int | None = None
        while closing_start < len(value):
            candidate = value.find("`", closing_start)
            if candidate < 0:
                break
            candidate_end = candidate + 1
            while candidate_end < len(value) and value[candidate_end] == "`":
                candidate_end += 1
            if candidate_end - candidate == width:
                closing_end = candidate_end
                break
            closing_start = candidate_end
        if closing_end is None:
            cursor = run_end
            continue
        if cursor > prose_start:
            segments.append((False, value[prose_start:cursor]))
        segments.append((True, value[cursor:closing_end]))
        cursor = closing_end
        prose_start = cursor
    if prose_start < len(value):
        segments.append((False, value[prose_start:]))
    return tuple(segments) or ((False, value),)


def _markdown_segments(value: str) -> tuple[tuple[bool, str], ...]:
    fenced_ranges: list[tuple[int, int]] = []
    active: tuple[str, int, int] | None = None
    offset = 0
    for line in value.splitlines(keepends=True):
        body = line.rstrip("\r\n")
        if active is None:
            opened = _FENCE_OPEN_RE.fullmatch(body)
            if opened is not None:
                fence = opened.group("fence")
                info = opened.group("info")
                if fence[0] != "`" or "`" not in info:
                    active = (fence[0], len(fence), offset)
        else:
            closed = _FENCE_CLOSE_RE.fullmatch(body)
            if (
                closed is not None
                and closed.group("fence")[0] == active[0]
                and len(closed.group("fence")) >= active[1]
            ):
                fenced_ranges.append((active[2], offset + len(line)))
                active = None
        offset += len(line)
    if active is not None:
        fenced_ranges.append((active[2], len(value)))

    segments: list[tuple[bool, str]] = []
    cursor = 0
    for start, end in fenced_ranges:
        if cursor < start:
            segments.extend(_inline_code_segments(value[cursor:start]))
        segments.append((True, value[start:end]))
        cursor = end
    if cursor < len(value):
        segments.extend(_inline_code_segments(value[cursor:]))
    return tuple(segments) or ((False, value),)


def _mask_citation_tokens(value: str) -> str:
    masked = list(value)
    for match in _WEB_CITATION_TOKEN_RE.finditer(value):
        _validated_label(match.group("label"))
        for index in range(match.start(), match.end()):
            masked[index] = " "
    return "".join(masked)


def _contains_markdown_link(value: str) -> bool:
    for index, character in enumerate(value):
        if (
            character == "!"
            and index + 1 < len(value)
            and value[index + 1] == "["
            and not _is_escaped(value, index)
        ):
            return True
        if character != "]" or _is_escaped(value, index):
            continue
        cursor = index + 1
        while cursor < len(value) and value[cursor].isspace():
            cursor += 1
        if cursor < len(value) and value[cursor] in {"(", "["}:
            return True
    return bool(_REFERENCE_DEFINITION_RE.search(value))


def _validate_prose_links(value: str) -> None:
    masked = _mask_citation_tokens(value)
    if _WEB_CITATION_MARKER_RE.search(masked):
        raise WebCitationLedgerError(WebCitationLedgerCode.INVALID_TOKEN)
    if _contains_raw_http_url(value):
        raise WebCitationLedgerError(WebCitationLedgerCode.RAW_URL)
    if (
        _RAW_HTML_RE.search(masked)
        or _GFM_WWW_RE.search(masked)
        or _GFM_FTP_RE.search(masked)
        or _GFM_EMAIL_RE.search(masked)
        or _contains_markdown_link(masked)
    ):
        raise WebCitationLedgerError(WebCitationLedgerCode.UNAUTHORIZED_LINK)


def _markdown_label(title: str) -> str:
    normalized = " ".join(title.split()) or "来源"
    if (
        _contains_raw_http_url(normalized)
        or _GFM_WWW_RE.search(normalized)
        or _GFM_FTP_RE.search(normalized)
        or _GFM_EMAIL_RE.search(normalized)
        or _WEB_CITATION_MARKER_RE.search(normalized)
    ):
        normalized = "来源"
    markdown_escaped = re.sub(r"([\\`*{}\[\]()_+.!|~\-])", r"\\\1", normalized)
    return html.escape(markdown_escaped, quote=True)


class WebCitationLedger:
    """Request-owned evidence ledger and deterministic rendering Module."""

    __slots__ = ("_attempted", "_evidence")

    def __init__(self) -> None:
        self._attempted = False
        self._evidence: dict[str, _LedgerEvidence] = {}

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(attempted={self._attempted!r}, "
            f"evidence_count={len(self._evidence)!r})"
        )

    def mark_attempted(self) -> None:
        self._attempted = True

    def register_result(
        self,
        result: WebResearchResult,
        *,
        kind: WebEvidenceKind | str,
    ) -> None:
        if not isinstance(result, WebResearchResult):
            raise TypeError("result must be WebResearchResult")
        self.mark_attempted()
        self.register_evidence(result.evidence, kind=kind)

    def register_evidence(
        self,
        evidence: Iterable[WebEvidence],
        *,
        kind: WebEvidenceKind | str,
    ) -> None:
        evidence_kind = WebEvidenceKind(kind)
        items = tuple(evidence)
        if any(not isinstance(item, WebEvidence) for item in items):
            raise TypeError("evidence must contain WebEvidence values")
        incoming = {
            item.evidence_id: _LedgerEvidence.from_evidence(
                item,
                kind=evidence_kind,
            )
            for item in items
        }
        if len(set(self._evidence).union(incoming)) > _MAX_LEDGER_EVIDENCE:
            raise WebCitationLedgerError(WebCitationLedgerCode.EVIDENCE_LIMIT)

        for evidence_id, item in incoming.items():
            existing = self._evidence.get(evidence_id)
            if existing is not None and existing.canonical_url != item.canonical_url:
                raise WebCitationLedgerError(WebCitationLedgerCode.UNKNOWN_EVIDENCE)
        for evidence_id, item in incoming.items():
            existing = self._evidence.get(evidence_id)
            if (
                existing is None
                or existing.kind is WebEvidenceKind.SEARCH_SNIPPET
                and item.kind is WebEvidenceKind.FETCHED_PAGE
            ):
                self._evidence[evidence_id] = item
        if items:
            self._attempted = True

    def status(self) -> WebCitationLedgerStatus:
        evidence_count = len(self._evidence)
        return WebCitationLedgerStatus(
            attempted=self._attempted,
            evidence_count=evidence_count,
            citation_required=evidence_count > 0,
        )

    def finalize(self, content: str) -> WebCitationFinalization:
        content = _validated_content(content)
        segments = _markdown_segments(content)
        validation_applied = self._attempted or any(
            not is_code and _WEB_CITATION_MARKER_RE.search(value)
            for is_code, value in segments
        )
        if not validation_applied:
            return WebCitationFinalization(
                content=content,
                citation_count=0,
                cited_evidence_count=0,
                available_evidence_count=0,
                validation_applied=False,
            )

        citation_count = 0
        cited_evidence: set[str] = set()

        def render(match: re.Match[str]) -> str:
            nonlocal citation_count
            _validated_label(match.group("label"))
            evidence_id = match.group("evidence_id")
            if not _EVIDENCE_ID_RE.fullmatch(evidence_id):
                raise WebCitationLedgerError(WebCitationLedgerCode.INVALID_TOKEN)
            source = self._evidence.get(evidence_id)
            if source is None:
                raise WebCitationLedgerError(WebCitationLedgerCode.UNKNOWN_EVIDENCE)
            citation_count += 1
            if citation_count > _MAX_RENDERED_CITATIONS:
                raise WebCitationLedgerError(WebCitationLedgerCode.INVALID_CONTENT)
            cited_evidence.add(evidence_id)
            return f"[{_markdown_label(source.title)}](<{source.canonical_url}>)"

        rendered_segments: list[str] = []
        for is_code, value in segments:
            if is_code:
                rendered_segments.append(value)
                continue
            _validate_prose_links(value)
            rendered_prose = _WEB_CITATION_TOKEN_RE.sub(render, value)
            if _WEB_CITATION_MARKER_RE.search(rendered_prose):
                raise WebCitationLedgerError(WebCitationLedgerCode.INVALID_TOKEN)
            rendered_segments.append(rendered_prose)
        rendered = "".join(rendered_segments)
        if self._evidence and citation_count == 0:
            raise WebCitationLedgerError(WebCitationLedgerCode.REQUIRED)
        return WebCitationFinalization(
            content=rendered,
            citation_count=citation_count,
            cited_evidence_count=len(cited_evidence),
            available_evidence_count=len(self._evidence),
            validation_applied=True,
        )

    def clear(self) -> None:
        self._attempted = False
        self._evidence.clear()


__all__ = [
    "WebCitationFinalization",
    "WebCitationLedger",
    "WebCitationLedgerCode",
    "WebCitationLedgerError",
    "WebCitationLedgerStatus",
    "WebEvidenceKind",
]
