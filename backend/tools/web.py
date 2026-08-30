"""Request-owned Tool adapters for Web Search and Tavily Extract."""

from __future__ import annotations

import json
from typing import Protocol, Sequence

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


WEB_RESEARCH_METADATA_KEYS = frozenset(
    {"source_count", "output_bytes", "truncated"}
)
_WEB_TOOL_VERSION = "2.0.0"
_MAX_WEB_TOOL_DURATION_MS = 999_999
_WEB_SOURCE_BUDGET_EXHAUSTED = "WEB_SOURCE_BUDGET_EXHAUSTED"


def _web_source_budget_failure() -> ToolResultV1:
    return new_tool_failure(
        error_code=_WEB_SOURCE_BUDGET_EXHAUSTED,
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
    source_ids: Sequence[str],
    *,
    data: dict[str, object] | None = None,
) -> ToolResultV1:
    if not isinstance(result, WebResearchResult):
        raise TypeError("Web runtime returned an invalid result contract")
    projection = result.to_tool_dict(source_ids) if data is None else data
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


def _registered_tool_result_size(
    result: ToolResultV1,
    *,
    tool_name: str,
) -> int:
    """Estimate the complete Registry-wrapped payload seen by the model."""

    encoded = json.dumps(
        result.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    metadata = {
        **result.observability_metadata,
        "tool_name": tool_name,
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


def _fit_web_tool_result(
    result: WebResearchResult,
    source_ids: Sequence[str],
    max_bytes: int,
    *,
    tool_name: str,
) -> ToolResultV1 | None:
    """Build the ToolResult first, then trim only model-visible content."""

    fitted = _tool_result(result, source_ids)
    if _registered_tool_result_size(fitted, tool_name=tool_name) <= max_bytes:
        return fitted

    data = result.to_tool_dict(source_ids)
    sources = data.get("sources")
    if not isinstance(sources, list):
        return None
    data["truncated"] = True
    fitted = _tool_result(result, source_ids, data=data)

    for item in reversed(sources):
        if not isinstance(item, dict):
            return None
        content = item.get("content")
        if not isinstance(content, str):
            return None
        excess = _registered_tool_result_size(fitted, tool_name=tool_name) - max_bytes
        if excess <= 0:
            break
        content_bytes = len(content.encode("utf-8"))
        item["content"] = _truncate_utf8(
            content,
            max(content_bytes - excess, 0),
        )
        fitted = _tool_result(result, source_ids, data=data)

    if _registered_tool_result_size(fitted, tool_name=tool_name) > max_bytes:
        return None
    return fitted


def _bounded_web_tool_result(
    ctx: RunRequestContext,
    result: WebResearchResult,
    source_ids: Sequence[str],
    *,
    max_total_source_bytes: int,
    tool_name: str,
) -> ToolResultV1 | None:
    remaining = ctx.remaining_web_tool_result_budget(max_total_source_bytes)
    fitted = _fit_web_tool_result(
        result,
        source_ids,
        remaining,
        tool_name=tool_name,
    )
    if fitted is None:
        return None
    actual_size = _registered_tool_result_size(fitted, tool_name=tool_name)
    claimed = ctx.claim_web_tool_result_budget(
        actual_size,
        limit_bytes=max_total_source_bytes,
    )
    if claimed == actual_size:
        return fitted
    if claimed <= 0:
        return None
    return _fit_web_tool_result(
        result,
        source_ids,
        claimed,
        tool_name=tool_name,
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
    default_results: int = 5,
    max_total_source_bytes: int = 3_072,
) -> BaseTool:
    """Build a request-owned search tool that assigns Run-local Source IDs."""

    if runtime is None:
        raise RuntimeError("Web Research runtime is not configured")

    @tool("web_search")
    def web_search(
        query: str,
        max_results: int = default_results,
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
            bounded_result = _bounded_web_tool_result(
                ctx,
                result,
                source_ids,
                max_total_source_bytes=max_total_source_bytes,
                tool_name="web_search",
            )
            if bounded_result is None:
                return _web_source_budget_failure()
        except Exception as exc:
            failure = _web_failure(exc)
            if failure is None:
                raise
            return failure
        return bounded_result

    return web_search


def make_web_fetch(
    ctx: RunRequestContext,
    *,
    runtime: WebResearchRuntime | None = None,
    max_total_source_bytes: int = 3_072,
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
            bounded_result = _bounded_web_tool_result(
                ctx,
                result,
                (source.source_id,),
                max_total_source_bytes=max_total_source_bytes,
                tool_name="web_fetch",
            )
            if bounded_result is None:
                return _web_source_budget_failure()
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
