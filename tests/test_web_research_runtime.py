from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

import httpx
import pytest

from backend.core.settings import WebResearchSettings
from backend.web_research.contracts import WebResearchLimits
from backend.web_research.runtime import (
    TavilyKeylessProvider,
    WebExtractResult,
    WebResearchError,
    WebResearchErrorCode,
    WebResearchRuntime,
    WebResearchRuntimeConfig,
    WebSearchHit,
    build_web_research_runtime,
)


NOW = datetime(2026, 8, 30, tzinfo=timezone.utc)


class FakeProvider:
    def __init__(
        self,
        *,
        hits: tuple[WebSearchHit, ...] = (),
        extracted: WebExtractResult | None = None,
    ) -> None:
        self.hits = hits
        self.extracted = extracted or WebExtractResult(
            url="https://example.org/source",
            title="Example",
            chunks=("Extracted content",),
        )
        self.search_calls: list[dict[str, object]] = []
        self.extract_calls: list[dict[str, object]] = []

    def search(
        self,
        query,
        *,
        limit,
        allowed_domains,
        timeout_seconds,
        deadline_at,
        cancellation_probe,
    ):
        self.search_calls.append(
            {
                "query": query,
                "limit": limit,
                "allowed_domains": allowed_domains,
                "timeout_seconds": timeout_seconds,
                "deadline_at": deadline_at,
                "cancellation_probe": cancellation_probe,
            }
        )
        return self.hits

    def extract(
        self,
        url,
        *,
        query,
        chunks_per_source,
        timeout_seconds,
        deadline_at,
        cancellation_probe,
    ):
        self.extract_calls.append(
            {
                "url": url,
                "query": query,
                "chunks_per_source": chunks_per_source,
                "timeout_seconds": timeout_seconds,
                "deadline_at": deadline_at,
                "cancellation_probe": cancellation_probe,
            }
        )
        return self.extracted


def _runtime(
    provider: FakeProvider,
    *,
    limits: WebResearchLimits | None = None,
) -> WebResearchRuntime:
    runtime = WebResearchRuntime(
        config=WebResearchRuntimeConfig(
            limits=limits or WebResearchLimits(),
        ),
        provider=provider,
        clock=lambda: NOW,
    )
    runtime.start()
    return runtime


def test_search_returns_provider_summaries_without_quarter_budget_pretruncate() -> None:
    limits = WebResearchLimits(
        max_evidence_items=2,
    )
    provider = FakeProvider(
        hits=(
            WebSearchHit("https://example.org/first", "First", "a" * 2_000),
            WebSearchHit("https://example.org/second", "Second", "b" * 2_000),
        )
    )
    runtime = _runtime(provider, limits=limits)
    try:
        result = runtime.search(
            "Python 3.15",
            limit=2,
            allowed_domains=("python.org",),
        )
    finally:
        runtime.close()

    assert len(result.evidence) == 2
    assert [len(item.content) for item in result.evidence] == [2_000, 2_000]
    assert provider.search_calls[0]["allowed_domains"] == ("python.org",)


def test_search_skips_duplicate_or_empty_hits_and_marks_result_truncated() -> None:
    provider = FakeProvider(
        hits=(
            WebSearchHit("https://example.org/first", "First", "summary"),
            WebSearchHit("https://example.org/first", "Duplicate", "other"),
            WebSearchHit("https://example.org/empty", "", ""),
        )
    )
    runtime = _runtime(provider)
    try:
        result = runtime.search("query", limit=3)
    finally:
        runtime.close()

    assert len(result.evidence) == 1
    assert result.truncated is True


def test_fetch_uses_query_ranked_extract_and_bounds_three_500_character_chunks() -> (
    None
):
    provider = FakeProvider(
        extracted=WebExtractResult(
            url="https://example.org/source",
            title="Official source",
            chunks=tuple(f"{index}:" + "x" * 700 for index in range(6)),
        )
    )
    runtime = _runtime(provider)
    try:
        result = runtime.fetch(
            "https://example.org/source",
            query="free-threading performance limitations",
        )
    finally:
        runtime.close()

    call = provider.extract_calls[0]
    assert call["url"] == "https://example.org/source"
    assert call["query"] == "free-threading performance limitations"
    assert call["chunks_per_source"] == 3
    chunks = result.evidence[0].content.split("\n\n")
    assert len(chunks) == 3
    assert all(len(chunk) <= 500 for chunk in chunks)


def test_fetch_rejects_empty_extract_content() -> None:
    runtime = _runtime(
        FakeProvider(
            extracted=WebExtractResult(
                url="https://example.org/source",
                chunks=("  ",),
            )
        )
    )
    try:
        with pytest.raises(WebResearchError) as captured:
            runtime.fetch("https://example.org/source", query="detail")
    finally:
        runtime.close()

    assert captured.value.code == WebResearchErrorCode.INVALID_CONTENT.value


def test_runtime_requires_start_and_honors_cancellation() -> None:
    runtime = WebResearchRuntime(provider=FakeProvider())
    with pytest.raises(WebResearchError) as not_started:
        runtime.search("query")
    assert not_started.value.code == WebResearchErrorCode.NOT_STARTED.value

    runtime.start()
    try:
        with pytest.raises(asyncio.CancelledError):
            runtime.search("query", cancellation_probe=lambda: True)
    finally:
        runtime.close()


def test_tavily_extract_request_uses_fixed_endpoint_and_required_parameters() -> None:
    observed: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        observed["method"] = request.method
        observed["url"] = str(request.url)
        observed["payload"] = json.loads(request.content)
        observed["access_mode"] = request.headers["X-Tavily-Access-Mode"]
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "url": "https://example.org/source",
                        "raw_content": ["chunk one", "chunk two"],
                    }
                ]
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = TavilyKeylessProvider(
        user_agent="SuperMew-Test/1.0",
        provider_response_max_bytes=64_000,
        client=client,
    )
    try:
        result = provider.extract(
            "https://example.org/source",
            query="specific question",
            chunks_per_source=3,
            timeout_seconds=2.0,
            deadline_at=None,
            cancellation_probe=None,
        )
    finally:
        client.close()

    assert observed == {
        "method": "POST",
        "url": "https://api.tavily.com/extract",
        "payload": {
            "urls": "https://example.org/source",
            "query": "specific question",
            "chunks_per_source": 3,
            "extract_depth": "basic",
        },
        "access_mode": "keyless",
    }
    assert result.chunks == ("chunk one", "chunk two")


def test_tavily_search_uses_only_fixed_search_endpoint() -> None:
    observed: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        observed.append(str(request.url))
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "url": "https://example.org/result",
                        "title": "Result",
                        "content": "Summary",
                    }
                ]
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = TavilyKeylessProvider(
        user_agent="SuperMew-Test/1.0",
        provider_response_max_bytes=64_000,
        client=client,
    )
    try:
        hits = provider.search(
            "query",
            limit=1,
            allowed_domains=(),
            timeout_seconds=2.0,
            deadline_at=None,
            cancellation_probe=None,
        )
    finally:
        client.close()

    assert observed == ["https://api.tavily.com/search"]
    assert hits == (WebSearchHit("https://example.org/result", "Result", "Summary"),)


def test_build_runtime_maps_only_current_web_research_settings() -> None:
    settings = WebResearchSettings(
        _env_file=None,
        WEB_RESEARCH_ENABLED=True,
        WEB_RESEARCH_PROVIDER_RESPONSE_MAX_BYTES=131_072,
        WEB_RESEARCH_SEARCH_PROVIDER_MAX_RESULTS=3,
        WEB_RESEARCH_FETCH_CHUNKS_PER_SOURCE=2,
    )

    runtime = build_web_research_runtime(settings)
    try:
        assert runtime.config.provider_response_max_bytes == 131_072
        assert runtime.config.fetch_chunks_per_source == 2
        assert runtime.config.limits.max_evidence_items == 3
    finally:
        runtime.close()
