"""Request-owned Tool adapters for Web Search and Tavily Extract."""

from __future__ import annotations

import copy
import json
from typing import Protocol

from langchain_core.tools import BaseTool, tool

from backend.runs.request_context import RunRequestContext
from backend.tools.contracts import ToolResultV1, new_tool_failure, new_tool_success
from backend.web_research.contracts import (
    WebEvidence,
    WebResearchResult,
)


class WebResearchRuntime(Protocol):
    def search(
        self,
        query: str,
        *,
        limit: int | None,
        allowed_domains: tuple[str, ...] = (),
        deadline_at: float | None,
        cancellation_probe,
    ) -> WebResearchResult: ...

    def fetch(
        self,
        url: str,
        *,
        query: str,
        deadline_at: float | None,
        cancellation_probe,
    ) -> WebResearchResult: ...


WEB_RESEARCH_METADATA_KEYS = frozenset({"source_count", "output_bytes", "truncated"})
_WEB_TOOL_VERSION = "2.2.0"
_MAX_WEB_TOOL_DURATION_MS = 999_999
_WEB_FETCH_BUDGET_EXHAUSTED = "WEB_FETCH_BUDGET_EXHAUSTED"


def _web_fetch_budget_failure() -> ToolResultV1:
    return new_tool_failure(
        error_code=_WEB_FETCH_BUDGET_EXHAUSTED,
        retryable=False,
    )


def _tool_data_size(data: dict[str, object]) -> int:
    return len(
        json.dumps(
            data,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    )


def _tool_result(
    result: WebResearchResult,
    *,
    data: dict[str, object],
) -> ToolResultV1:
    if not isinstance(result, WebResearchResult):
        raise TypeError("Web runtime returned an invalid result contract")
    projection = data
    metadata = result.tool_observability_metadata()
    metadata["output_bytes"] = _tool_data_size(projection)
    projected_truncated = projection.get("truncated")
    if isinstance(projected_truncated, bool):
        metadata["truncated"] = projected_truncated
    sources = projection.get("sources")
    if isinstance(sources, list):
        metadata["source_count"] = len(sources)
    return new_tool_success(
        data=projection,
        observability_metadata={
            key: value
            for key, value in metadata.items()
            if key in WEB_RESEARCH_METADATA_KEYS
        },
    )


def _registered_fetch_tool_result_size(result: ToolResultV1) -> int:
    """Estimate the complete Registry-wrapped web_fetch payload."""

    encoded = json.dumps(
        result.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    metadata = {
        **result.observability_metadata,
        "tool_name": "web_fetch",
        "tool_version": _WEB_TOOL_VERSION,
        "result_size": len(encoded),
    }
    wrapped = result.model_copy(
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


def _bounded_search_projection(
    result: WebResearchResult,
    source_ids: tuple[str, ...],
    *,
    model_visible_results: int,
    per_source_max_bytes: int,
    total_snippet_max_bytes: int,
) -> dict[str, object]:
    projection = result.to_search_tool_dict(source_ids)
    sources = projection["sources"]
    if not isinstance(sources, list):
        raise TypeError("Web search projection has invalid sources")

    visible_sources = sources[:model_visible_results]
    truncated = result.truncated or len(visible_sources) < len(sources)
    remaining = total_snippet_max_bytes
    for item in visible_sources:
        if not isinstance(item, dict) or not isinstance(item.get("snippet"), str):
            raise TypeError("Web search projection has invalid snippets")
        snippet = item["snippet"]
        bounded = _truncate_utf8(snippet, min(per_source_max_bytes, remaining))
        if bounded != snippet:
            truncated = True
        item["snippet"] = bounded
        remaining -= len(bounded.encode("utf-8"))

    projection["sources"] = visible_sources
    projection["truncated"] = truncated
    return projection


def _fit_fetch_tool_result(
    result: WebResearchResult,
    projection: dict[str, object],
    max_bytes: int,
) -> ToolResultV1 | None:
    """Build the ToolResult first, then trim only model-visible fetch content."""

    data = copy.deepcopy(projection)
    fitted = _tool_result(result, data=data)
    if _registered_fetch_tool_result_size(fitted) <= max_bytes:
        return fitted

    sources = data.get("sources")
    if not isinstance(sources, list):
        return None
    data["truncated"] = True
    fitted = _tool_result(result, data=data)

    for item in reversed(sources):
        if not isinstance(item, dict):
            return None
        content = item.get("content")
        if not isinstance(content, str):
            return None
        excess = _registered_fetch_tool_result_size(fitted) - max_bytes
        if excess <= 0:
            break
        content_bytes = len(content.encode("utf-8"))
        item["content"] = _truncate_utf8(
            content,
            max(content_bytes - excess, 0),
        )
        fitted = _tool_result(result, data=data)

    if _registered_fetch_tool_result_size(fitted) > max_bytes:
        return None
    return fitted


def _bounded_fetch_tool_result(
    ctx: RunRequestContext,
    result: WebResearchResult,
    projection: dict[str, object],
    *,
    response_max_bytes: int,
    run_total_max_bytes: int,
) -> ToolResultV1 | None:
    remaining = ctx.remaining_web_fetch_result_budget(run_total_max_bytes)
    fitted = _fit_fetch_tool_result(
        result,
        projection,
        min(response_max_bytes, remaining),
    )
    if fitted is None:
        return None
    actual_size = _registered_fetch_tool_result_size(fitted)
    claimed = ctx.claim_web_fetch_result_budget(
        actual_size,
        limit_bytes=run_total_max_bytes,
    )
    if claimed == actual_size:
        return fitted
    if claimed <= 0:
        return None
    return _fit_fetch_tool_result(
        result,
        projection,
        claimed,
    )


def _web_failure(error: Exception) -> ToolResultV1 | None:
    from backend.web_research.citations import WebSourceLedgerError
    from backend.web_research.contracts import WebResearchContractError
    from backend.web_research.runtime import WebResearchError

    if not isinstance(
        error,
        (WebResearchContractError, WebSourceLedgerError, WebResearchError),
    ):
        return None
    raw_code = error.code
    error_code = raw_code.value if hasattr(raw_code, "value") else str(raw_code)
    return new_tool_failure(
        error_code=error_code,
        retryable=bool(getattr(error, "retryable", False)),
    )


def _with_fallback_title(result: WebResearchResult, title: str) -> WebResearchResult:
    if len(result.evidence) != 1 or result.evidence[0].title or not title:
        return result
    item = result.evidence[0]
    return WebResearchResult(
        evidence=(
            WebEvidence(
                url=item.url,
                title=title,
                content=item.content,
                retrieved_at=item.retrieved_at,
            ),
        ),
        truncated=result.truncated,
    )


def make_web_search(
    ctx: RunRequestContext,
    *,
    runtime: WebResearchRuntime | None = None,
    provider_max_results: int = 3,
    model_visible_results: int = 3,
    per_source_max_bytes: int = 480,
    total_snippet_max_bytes: int = 1_440,
) -> BaseTool:
    """Build a request-owned search tool that assigns Run-local Source IDs."""

    if runtime is None:
        raise RuntimeError("Web Research runtime is not configured")

    @tool("web_search")
    def web_search(
        query: str,
        max_results: int = provider_max_results,
        allowed_domains: tuple[str, ...] = (),
    ) -> ToolResultV1:
        """Search the public web and return compact Run-local sources."""

        deadline_at, cancellation_probe = ctx.provider_runtime()
        ctx.mark_web_research_attempted()
        try:
            result = runtime.search(
                query,
                limit=max_results,
                allowed_domains=tuple(
                    sorted({domain.casefold() for domain in allowed_domains})
                ),
                deadline_at=deadline_at,
                cancellation_probe=cancellation_probe,
            )
            source_ids = ctx.record_web_search_result(result, query=query)
            projection = _bounded_search_projection(
                result,
                source_ids,
                model_visible_results=model_visible_results,
                per_source_max_bytes=per_source_max_bytes,
                total_snippet_max_bytes=total_snippet_max_bytes,
            )
            tool_result = _tool_result(result, data=projection)
        except Exception as exc:
            failure = _web_failure(exc)
            if failure is None:
                raise
            return failure
        return tool_result

    return web_search


def make_web_fetch(
    ctx: RunRequestContext,
    *,
    runtime: WebResearchRuntime | None = None,
    response_max_bytes: int = 6_144,
    run_total_max_bytes: int = 12_288,
) -> BaseTool:
    """Build a request-owned Tavily Extract tool over Run-local Source IDs."""

    if runtime is None:
        raise RuntimeError("Web Research runtime is not configured")

    @tool("web_fetch")
    def web_fetch(source_id: str, query: str | None = None) -> ToolResultV1:
        """Extract query-ranked chunks from one source returned by web_search."""

        ctx.mark_web_research_attempted()
        source = ctx.resolve_web_source(source_id)
        if source is None:
            return new_tool_failure(
                error_code="WEB_SOURCE_NOT_FOUND",
                retryable=False,
            )
        effective_query = query.strip() if isinstance(query, str) else ""
        effective_query = effective_query or source.default_query
        deadline_at, cancellation_probe = ctx.provider_runtime()
        try:
            result = runtime.fetch(
                source.url,
                query=effective_query,
                deadline_at=deadline_at,
                cancellation_probe=cancellation_probe,
            )
            result = _with_fallback_title(result, source.title)
            projection = result.to_fetch_tool_dict((source.source_id,))
            bounded_result = _bounded_fetch_tool_result(
                ctx,
                result,
                projection,
                response_max_bytes=response_max_bytes,
                run_total_max_bytes=run_total_max_bytes,
            )
            if bounded_result is None:
                return _web_fetch_budget_failure()
        except Exception as exc:
            failure = _web_failure(exc)
            if failure is None:
                raise
            return failure
        return bounded_result

    return web_fetch


__all__ = [
    "WEB_RESEARCH_METADATA_KEYS",
    "WebResearchRuntime",
    "make_web_fetch",
    "make_web_search",
]
