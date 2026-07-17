"""Request-owned Tool Adapters for the process-wide Web Research runtime."""

from __future__ import annotations

from typing import Protocol

from langchain_core.tools import BaseTool, tool

from backend.runs.request_context import RunRequestContext
from backend.tools.contracts import ToolResultV1, new_tool_failure, new_tool_success
from backend.web_research.contracts import WebResearchResult


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
) -> BaseTool:
    """Build a request-owned search Adapter and mint Run-local fetch capabilities."""

    @tool("web_search")
    def web_search(query: str, max_results: int = default_results) -> ToolResultV1:
        """Search the public web and return bounded evidence with stable citations."""

        deadline_at, cancellation_probe = ctx.provider_runtime()
        ctx.mark_web_research_attempted()
        try:
            result = get_web_research_runtime().search(
                query,
                limit=max_results,
                deadline_at=deadline_at,
                cancellation_probe=cancellation_probe,
            )
            ctx.record_web_search_result(result)
        except Exception as exc:
            failure = _web_failure(exc)
            if failure is None:
                raise
            return failure

        return _tool_result(result)

    return web_search


def make_web_fetch(ctx: RunRequestContext) -> BaseTool:
    """Build a request-owned fetch Adapter over search-minted capabilities."""

    @tool("web_fetch")
    def web_fetch(evidence_id: str) -> ToolResultV1:
        """Fetch one URL previously returned by web_search in this Run."""

        ctx.mark_web_research_attempted()
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
