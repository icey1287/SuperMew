from __future__ import annotations

from datetime import datetime, timezone

import pytest

from backend.runs.request_context import RunRequestContext
from backend.web_research.citations import (
    WebSourceLedger,
    WebSourceLedgerCode,
    WebSourceLedgerError,
)
from backend.web_research.contracts import WebEvidence, WebResearchResult


NOW = datetime(2026, 8, 30, tzinfo=timezone.utc)


def _result(
    *,
    url: str = "https://docs.python.org/3.15/whatsnew/3.15.html",
    title: str = "Python 3.15 release notes",
    content: str = "Search summary",
) -> WebResearchResult:
    return WebResearchResult.create(
        (
            WebEvidence.create(
                url=url,
                title=title,
                content=content,
                retrieved_at=NOW,
            ),
        )
    )


def test_ledger_assigns_stable_run_local_source_ids_and_default_queries() -> None:
    ledger = WebSourceLedger()
    first = _result()
    second = _result(
        url="https://peps.python.org/pep-0779/",
        title="PEP 779",
    )

    assert ledger.register_search_result(first, query="Python 3.15") == ("S1",)
    assert ledger.register_search_result(second, query="free-threading PEP") == (
        "S2",
    )
    assert ledger.register_search_result(first, query="newer query") == ("S1",)

    source = ledger.resolve("S1")
    assert source is not None
    assert source.url == first.evidence[0].url
    assert source.title == first.evidence[0].title
    assert source.default_query == "Python 3.15"
    assert ledger.status().source_count == 2


def test_finalize_renders_short_source_tokens_and_leaves_other_links_alone() -> None:
    ledger = WebSourceLedger()
    result = _result()
    ledger.register_search_result(result, query="Python 3.15")

    finalized = ledger.finalize(
        "Python changed free-threading [S1]. "
        "Existing [project link](https://example.org/) remains ordinary Markdown."
    )

    assert f"[S1](<{result.evidence[0].url}>)" in finalized.content
    assert "[project link](https://example.org/)" in finalized.content
    assert finalized.citation_count == 1
    assert finalized.cited_source_count == 1
    assert finalized.available_source_count == 1
    assert finalized.rendering_applied is True


def test_finalize_rejects_only_unknown_source_tokens() -> None:
    ledger = WebSourceLedger()
    ledger.register_search_result(_result(), query="Python 3.15")

    with pytest.raises(WebSourceLedgerError) as captured:
        ledger.finalize("Unsupported claim [S2].")

    assert captured.value.code is WebSourceLedgerCode.UNKNOWN_SOURCE
    assert "S2" not in repr(captured.value)


def test_finalize_without_web_sources_is_unchanged() -> None:
    finalized = WebSourceLedger().finalize("Ordinary answer.")

    assert finalized.content == "Ordinary answer."
    assert finalized.rendering_applied is False


def test_request_context_keeps_source_ids_isolated_per_run() -> None:
    first = RunRequestContext.for_sync(user_id="alice", thread_id="thread-a")
    second = RunRequestContext.for_sync(user_id="alice", thread_id="thread-b")
    result = _result()
    try:
        assert first.record_web_search_result(result, query="Python 3.15") == ("S1",)
        assert first.resolve_web_source("S1") is not None
        assert second.resolve_web_source("S1") is None
        assert first.web_source_count() == 1
        assert second.web_source_count() == 0
        assert first.web_research_requires_source_rendering() is True
        rendered = first.render_web_source_citations("Claim [S1].")
        assert result.evidence[0].url in rendered
    finally:
        first.close()
        second.close()


def test_closed_request_context_rejects_source_rendering_and_resolution() -> None:
    context = RunRequestContext.for_sync(user_id="alice", thread_id="thread-a")
    context.record_web_search_result(_result(), query="Python 3.15")
    context.close()

    assert context.resolve_web_source("S1") is None
    assert context.web_source_count() == 0
    with pytest.raises(WebSourceLedgerError) as captured:
        context.render_web_source_citations("Claim [S1].")
    assert captured.value.code is WebSourceLedgerCode.CONTEXT_CLOSED
