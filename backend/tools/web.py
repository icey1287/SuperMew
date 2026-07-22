"""Request-owned Tool Adapters for the process-wide Web Research runtime."""

from __future__ import annotations

import json
from typing import Protocol

from langchain_core.tools import BaseTool, tool

from backend.runs.request_context import RunRequestContext
from backend.tools.contracts import ToolResultV1, new_tool_failure, new_tool_success
from backend.web_research.contracts import (
    WebCitation,
    WebEvidence,
    WebResearchLimits,
    WebResearchResult,
)


class WebResearchRuntime(Protocol):
    """Small Interface consumed by Tool Adapters at the runtime Seam."""

    def search(
        self,
        query: str,
        *,
        limit: int | None,
        deadline_at: float | None,
        cancellation_probe,
    ) -> WebResearchResult: ...

    def fetch(
        self,
        url: str,
        *,
        deadline_at: float | None,
        cancellation_probe,
    ) -> WebResearchResult: ...


WEB_RESEARCH_METADATA_KEYS = frozenset(
    {"citation_count", "evidence_count", "output_bytes", "truncated"}
)
_WEB_TOOL_VERSION = "1.0.0"
_MAX_WEB_TOOL_DURATION_MS = 999_999


def get_web_research_runtime() -> WebResearchRuntime:
    """Resolve the installed runtime without capturing it in a Run."""

    from backend.web_research.runtime import (
        get_web_research_runtime as resolve_runtime,
    )

    return resolve_runtime()


def _tool_result(result: WebResearchResult) -> ToolResultV1:
    if not isinstance(result, WebResearchResult):
        raise TypeError("Web runtime returned an invalid result contract")
    metadata = result.observability_metadata()
    return new_tool_success(
        data=result.to_public_dict(),
        observability_metadata={
            key: value
            for key, value in metadata.items()
            if key in WEB_RESEARCH_METADATA_KEYS
        },
    )


def _registered_tool_result_size(result: WebResearchResult, *, tool_name: str) -> int:
    """Estimate the complete Registry-wrapped payload seen by the model."""

    base = _tool_result(result)
    encoded = json.dumps(
        base.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    metadata = {
        **base.observability_metadata,
        "tool_name": tool_name,
        "tool_version": _WEB_TOOL_VERSION,
        "result_size": len(encoded),
    }
    wrapped = base.model_copy(
        update={
            "duration_ms": _MAX_WEB_TOOL_DURATION_MS,
            "observability_metadata": metadata,
        }
    )
    return len(wrapped.model_dump_json().encode("utf-8"))


def _truncate_utf8(value: str, max_bytes: int) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value
    return encoded[: max(max_bytes, 0)].decode("utf-8", errors="ignore").rstrip()


def _rebuild_evidence(
    source: WebEvidence,
    *,
    title: str,
    snippet: str,
    content: str,
) -> WebEvidence:
    title_bytes = len(title.encode("utf-8"))
    snippet_bytes = len(snippet.encode("utf-8"))
    content_bytes = len(content.encode("utf-8"))
    limits = WebResearchLimits(
        max_title_bytes=max(title_bytes, 1),
        max_snippet_bytes=max(snippet_bytes, 1),
        max_content_bytes=max(content_bytes, 1),
        max_total_evidence_bytes=max(content_bytes, 1),
    )
    return WebEvidence.create(
        canonical_url=source.canonical_url,
        title=title,
        snippet=snippet,
        content=content,
        retrieved_at=source.retrieved_at,
        limits=limits,
    )


def _research_result(
    evidence: list[WebEvidence],
    *,
    truncated: bool,
) -> WebResearchResult:
    return WebResearchResult(
        evidence=tuple(evidence),
        citations=tuple(WebCitation.from_evidence(item) for item in evidence),
        truncated=truncated,
    )


def _fit_web_result(
    result: WebResearchResult,
    max_bytes: int,
    *,
    tool_name: str,
) -> WebResearchResult:
    """Keep a valid structured result while fitting the remaining Run budget."""

    if _registered_tool_result_size(result, tool_name=tool_name) <= max_bytes:
        return result
    empty = _research_result([], truncated=True)
    if _registered_tool_result_size(empty, tool_name=tool_name) > max_bytes:
        return empty

    fitted: list[WebEvidence] = []
    for source in result.evidence:
        title = source.title
        snippet = source.snippet
        content = source.content
        while content:
            item = _rebuild_evidence(
                source,
                title=title,
                snippet=snippet,
                content=content,
            )
            candidate = _research_result([*fitted, item], truncated=True)
            excess = (
                _registered_tool_result_size(candidate, tool_name=tool_name)
                - max_bytes
            )
            if excess <= 0:
                fitted.append(item)
                break
            if snippet:
                snippet = _truncate_utf8(
                    snippet,
                    max(len(snippet.encode("utf-8")) - excess, 0),
                )
                continue
            if title:
                title = _truncate_utf8(
                    title,
                    max(len(title.encode("utf-8")) - excess, 0),
                )
                continue
            content_bytes = len(content.encode("utf-8"))
            minimum_content_bytes = len(content[0].encode("utf-8"))
            reduced = _truncate_utf8(
                content,
                max(content_bytes - excess, minimum_content_bytes),
            )
            if not reduced or reduced == content:
                content = ""
                break
            content = reduced
        if not content:
            break
    return _research_result(fitted, truncated=True)


def _bounded_web_result(
    ctx: RunRequestContext,
    result: WebResearchResult,
    *,
    max_total_evidence_bytes: int,
    tool_name: str,
) -> WebResearchResult:
    remaining = ctx.remaining_web_tool_result_budget(max_total_evidence_bytes)
    empty_size = _registered_tool_result_size(
        _research_result([], truncated=True),
        tool_name=tool_name,
    )
    claimable = (
        max(remaining - empty_size, 1)
        if remaining == max_total_evidence_bytes
        else remaining
    )
    claimed = ctx.claim_web_tool_result_budget(
        min(
            _registered_tool_result_size(result, tool_name=tool_name),
            claimable,
        ),
        limit_bytes=max_total_evidence_bytes,
    )
    return _fit_web_result(result, claimed, tool_name=tool_name)


def _web_failure(error: Exception) -> ToolResultV1 | None:
    from backend.web_research.citations import WebCitationLedgerError
    from backend.web_research.contracts import WebResearchContractError
    from backend.web_research.http import WebHttpError
    from backend.web_research.runtime import WebResearchError
    from backend.web_research.url_policy import WebUrlPolicyError

    if not isinstance(
        error,
        (
            WebResearchContractError,
            WebCitationLedgerError,
            WebHttpError,
            WebResearchError,
            WebUrlPolicyError,
        ),
    ):
        return None
    raw_code = error.code
    error_code = raw_code.value if hasattr(raw_code, "value") else str(raw_code)
    return new_tool_failure(
        error_code=error_code,
        retryable=bool(getattr(error, "retryable", False)),
    )


def make_web_search(
    ctx: RunRequestContext,
    *,
    default_results: int = 5,
    max_total_evidence_bytes: int = 3_072,
) -> BaseTool:
    """Build a request-owned search Adapter and mint Run-local fetch capabilities."""

    @tool("web_search")
    def web_search(query: str, max_results: int = default_results) -> ToolResultV1:
        """Search the public web and return bounded evidence with stable citations."""

        deadline_at, cancellation_probe = ctx.provider_runtime()
        ctx.mark_web_research_attempted()
        empty = _research_result([], truncated=True)
        if ctx.remaining_web_tool_result_budget(
            max_total_evidence_bytes
        ) < _registered_tool_result_size(empty, tool_name="web_search"):
            return _tool_result(empty)
        try:
            result = get_web_research_runtime().search(
                query,
                limit=max_results,
                deadline_at=deadline_at,
                cancellation_probe=cancellation_probe,
            )
            result = _bounded_web_result(
                ctx,
                result,
                max_total_evidence_bytes=max_total_evidence_bytes,
                tool_name="web_search",
            )
            ctx.record_web_search_result(result)
        except Exception as exc:
            failure = _web_failure(exc)
            if failure is None:
                raise
            return failure

        return _tool_result(result)

    return web_search


def make_web_fetch(
    ctx: RunRequestContext,
    *,
    max_total_evidence_bytes: int = 3_072,
) -> BaseTool:
    """Build a request-owned fetch Adapter over search-minted capabilities."""

    @tool("web_fetch")
    def web_fetch(evidence_id: str) -> ToolResultV1:
        """Fetch one URL previously returned by web_search in this Run."""

        ctx.mark_web_research_attempted()
        empty = _research_result([], truncated=True)
        if ctx.remaining_web_tool_result_budget(
            max_total_evidence_bytes
        ) < _registered_tool_result_size(empty, tool_name="web_fetch"):
            return _tool_result(empty)
        url = ctx.resolve_web_evidence(evidence_id)
        if url is None:
            return new_tool_failure(
                error_code="WEB_EVIDENCE_NOT_AUTHORIZED",
                retryable=False,
            )
        deadline_at, cancellation_probe = ctx.provider_runtime()
        try:
            result = get_web_research_runtime().fetch(
                url,
                deadline_at=deadline_at,
                cancellation_probe=cancellation_probe,
            )
            result = _bounded_web_result(
                ctx,
                result,
                max_total_evidence_bytes=max_total_evidence_bytes,
                tool_name="web_fetch",
            )
            ctx.record_web_fetch_result(result)
        except Exception as exc:
            failure = _web_failure(exc)
            if failure is None:
                raise
            return failure
        return _tool_result(result)

    return web_fetch


__all__ = [
    "WEB_RESEARCH_METADATA_KEYS",
    "WebResearchRuntime",
    "get_web_research_runtime",
    "make_web_fetch",
    "make_web_search",
]
