"""Request-owned Tool Adapters for the process-wide Web Research runtime."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Protocol

from langchain_core.tools import BaseTool, tool

from backend.runs.request_context import RunRequestContext
from backend.tools.contracts import ToolResultV1, new_tool_failure, new_tool_success
from backend.web_research.contracts import (
    WebCitation,
    WebEvidence,
    WebResearchResult,
)


class WebResearchRuntime(Protocol):
    """Small Interface consumed by Tool Adapters at the runtime Seam."""

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
        allowed_domains: tuple[str, ...] = (),
        deadline_at: float | None,
        cancellation_probe,
    ) -> WebResearchResult: ...


WEB_RESEARCH_METADATA_KEYS = frozenset(
    {"citation_count", "evidence_count", "output_bytes", "truncated"}
)
_WEB_TOOL_VERSION = "1.1.0"
_MAX_WEB_TOOL_DURATION_MS = 999_999
_WEB_EVIDENCE_BUDGET_EXHAUSTED = "WEB_EVIDENCE_BUDGET_EXHAUSTED"


def _web_evidence_budget_failure() -> ToolResultV1:
    return new_tool_failure(
        error_code=_WEB_EVIDENCE_BUDGET_EXHAUSTED,
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
    data: dict[str, object] | None = None,
) -> ToolResultV1:
    if not isinstance(result, WebResearchResult):
        raise TypeError("Web runtime returned an invalid result contract")
    projection = result.to_tool_dict() if data is None else data
    metadata = result.tool_observability_metadata()
    metadata["output_bytes"] = _tool_data_size(projection)
    projected_truncated = projection.get("truncated")
    if isinstance(projected_truncated, bool):
        metadata["truncated"] = projected_truncated
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


_MINIMUM_WEB_RESEARCH_RESULT = _research_result(
    [
        WebEvidence.create(
            canonical_url="https://example.com/",
            title="",
            snippet="",
            content="x",
            retrieved_at=datetime(1970, 1, 1, tzinfo=timezone.utc),
        )
    ],
    truncated=True,
)


def _fit_web_tool_result(
    result: WebResearchResult,
    max_bytes: int,
    *,
    tool_name: str,
) -> ToolResultV1 | None:
    """Trim only model-visible evidence bodies after building the ToolResult."""

    fitted = _tool_result(result)
    if _registered_tool_result_size(fitted, tool_name=tool_name) <= max_bytes:
        return fitted

    data = result.to_tool_dict()
    evidence = data.get("evidence")
    if not isinstance(evidence, list):
        return None
    data["truncated"] = True
    fitted = _tool_result(result, data=data)

    for item in evidence:
        if not isinstance(item, dict):
            return None
        content = item.get("content")
        if not isinstance(content, str):
            return None
        while (
            content
            and (
                excess := _registered_tool_result_size(fitted, tool_name=tool_name)
                - max_bytes
            )
            > 0
        ):
            content_bytes = len(content.encode("utf-8"))
            reduced = _truncate_utf8(content, max(content_bytes - excess, 0))
            if reduced == content:
                reduced = _truncate_utf8(content, max(content_bytes - 1, 0))
            item["content"] = reduced
            content = reduced
            fitted = _tool_result(result, data=data)

    if _registered_tool_result_size(fitted, tool_name=tool_name) > max_bytes:
        return None
    return fitted


def _bounded_web_tool_result(
    ctx: RunRequestContext,
    result: WebResearchResult,
    *,
    max_total_evidence_bytes: int,
    tool_name: str,
) -> ToolResultV1 | None:
    remaining = ctx.remaining_web_tool_result_budget(max_total_evidence_bytes)
    full = _tool_result(result)
    empty_size = _registered_tool_result_size(
        _tool_result(_research_result([], truncated=True)),
        tool_name=tool_name,
    )
    claimable = (
        max(remaining - empty_size, 1)
        if remaining == max_total_evidence_bytes
        else remaining
    )
    claimed = ctx.claim_web_tool_result_budget(
        min(
            _registered_tool_result_size(full, tool_name=tool_name),
            claimable,
        ),
        limit_bytes=max_total_evidence_bytes,
    )
    return _fit_web_tool_result(result, claimed, tool_name=tool_name)


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
    runtime: WebResearchRuntime | None = None,
    default_results: int = 5,
    max_total_evidence_bytes: int = 3_072,
) -> BaseTool:
    """Build a request-owned search Adapter and mint Run-local fetch capabilities."""

    if runtime is None:
        raise RuntimeError("Web Research runtime is not configured")

    @tool("web_search")
    def web_search(
        query: str,
        max_results: int = default_results,
        allowed_domains: tuple[str, ...] = (),
    ) -> ToolResultV1:
        """Search the public web for bounded, citable evidence."""

        deadline_at, cancellation_probe = ctx.provider_runtime()
        ctx.mark_web_research_attempted()
        if ctx.remaining_web_tool_result_budget(
            max_total_evidence_bytes
        ) < _registered_tool_result_size(
            _tool_result(_MINIMUM_WEB_RESEARCH_RESULT),
            tool_name="web_search",
        ):
            return _web_evidence_budget_failure()
        try:
            normalized_domains = tuple(
                sorted({domain.casefold() for domain in allowed_domains})
            )
            if normalized_domains:
                result = runtime.search(
                    query,
                    limit=max_results,
                    allowed_domains=normalized_domains,
                    deadline_at=deadline_at,
                    cancellation_probe=cancellation_probe,
                )
            else:
                result = runtime.search(
                    query,
                    limit=max_results,
                    deadline_at=deadline_at,
                    cancellation_probe=cancellation_probe,
                )
            bounded_result = _bounded_web_tool_result(
                ctx,
                result,
                max_total_evidence_bytes=max_total_evidence_bytes,
                tool_name="web_search",
            )
            if bounded_result is None:
                return _web_evidence_budget_failure()
            ctx.record_web_search_result(
                result,
                allowed_domains=normalized_domains,
            )
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
    max_total_evidence_bytes: int = 3_072,
) -> BaseTool:
    """Build a request-owned fetch Adapter over search-minted capabilities."""

    if runtime is None:
        raise RuntimeError("Web Research runtime is not configured")

    @tool("web_fetch")
    def web_fetch(evidence_id: str) -> ToolResultV1:
        """Fetch one page previously authorized by web_search in this Run."""

        ctx.mark_web_research_attempted()
        if ctx.remaining_web_tool_result_budget(
            max_total_evidence_bytes
        ) < _registered_tool_result_size(
            _tool_result(_MINIMUM_WEB_RESEARCH_RESULT),
            tool_name="web_fetch",
        ):
            return _web_evidence_budget_failure()
        authorization = ctx.resolve_web_fetch_authorization(evidence_id)
        if authorization is None:
            return new_tool_failure(
                error_code="WEB_EVIDENCE_NOT_AUTHORIZED",
                retryable=False,
            )
        url, allowed_domains = authorization
        deadline_at, cancellation_probe = ctx.provider_runtime()
        try:
            if allowed_domains:
                result = runtime.fetch(
                    url,
                    allowed_domains=allowed_domains,
                    deadline_at=deadline_at,
                    cancellation_probe=cancellation_probe,
                )
            else:
                result = runtime.fetch(
                    url,
                    deadline_at=deadline_at,
                    cancellation_probe=cancellation_probe,
                )
            bounded_result = _bounded_web_tool_result(
                ctx,
                result,
                max_total_evidence_bytes=max_total_evidence_bytes,
                tool_name="web_fetch",
            )
            if bounded_result is None:
                return _web_evidence_budget_failure()
            ctx.record_web_fetch_result(result)
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
