from __future__ import annotations

from datetime import datetime, timezone

import pytest

from backend.runs.request_context import RunRequestContext
from backend.tools.contracts import ToolResultV1
from backend.tools.web import make_web_fetch, make_web_search
from backend.web_research.citations import (
    WebCitationLedger,
    WebCitationLedgerCode,
    WebCitationLedgerError,
    WebEvidenceKind,
)
from backend.web_research.contracts import WebEvidence, WebResearchResult
from backend.web_research.runtime import WebResearchError, WebResearchErrorCode


def _evidence(
    *,
    url: str = "https://public.example/research?edition=2026",
    title: str = "Trusted [source]",
    content: str = "Verified search evidence.",
) -> WebEvidence:
    return WebEvidence.create(
        canonical_url=url,
        title=title,
        snippet="Public search snippet.",
        content=content,
        retrieved_at=datetime(2026, 7, 16, tzinfo=timezone.utc),
    )


def _result(evidence: WebEvidence) -> WebResearchResult:
    return WebResearchResult.create([evidence])


def test_ledger_renders_only_run_local_tokens_with_authoritative_source_title():
    evidence = _evidence()
    ledger = WebCitationLedger()
    ledger.register_result(
        _result(evidence),
        kind=WebEvidenceKind.SEARCH_SNIPPET,
    )

    finalized = ledger.finalize(
        f"Verified claim [model-invented label](webcite:{evidence.evidence_id})."
    )

    assert finalized.content == (
        f"Verified claim [Trusted \\[source\\]](<{evidence.canonical_url}>)."
    )
    assert finalized.citation_count == 1
    assert finalized.cited_evidence_count == 1
    assert finalized.available_evidence_count == 1
    assert finalized.validation_applied is True
    assert evidence.evidence_id not in repr(finalized)
    assert evidence.canonical_url not in repr(finalized)


@pytest.mark.parametrize(
    "content",
    [
        "See https://public.example/research.",
        "See [source](HTTP://public.example/research).",
        "See <Https://public.example/research>.",
        "See https&#58;//public.example/research.",
        r"See https\://public.example/research.",
    ],
)
def test_ledger_rejects_raw_http_urls_without_leaking_them(content: str):
    ledger = WebCitationLedger()
    ledger.mark_attempted()

    with pytest.raises(WebCitationLedgerError) as captured:
        ledger.finalize(content)

    error = captured.value
    assert error.code is WebCitationLedgerCode.RAW_URL
    assert "public.example" not in str(error)
    assert "public.example" not in repr(error)


@pytest.mark.parametrize(
    "untrusted",
    [
        "[bad](//attacker.example/path)",
        "[bad](javascript:alert(1))",
        "[bad](data:text/html,<script>alert(1)</script>)",
        "[bad](mailto:steal@attacker.example)",
        "![bad](//attacker.example/pixel.png)",
        "<img src=x onerror=alert(1)>",
        "<svg/onload=alert(1)>",
        '<a href="//attacker.example">bad</a>',
        "<steal@attacker.example>",
        "www.attacker.example",
        "ftp://attacker.example/file",
        "steal@attacker.example",
        "[bad][target]\n[target]: //attacker.example/path",
    ],
)
def test_ledger_rejects_every_non_ledger_link_or_raw_html(untrusted: str):
    evidence = _evidence()
    ledger = WebCitationLedger()
    ledger.register_result(
        _result(evidence),
        kind=WebEvidenceKind.SEARCH_SNIPPET,
    )
    content = f"Verified [source](webcite:{evidence.evidence_id}).\n\n{untrusted}"

    with pytest.raises(WebCitationLedgerError) as captured:
        ledger.finalize(content)

    assert captured.value.code is WebCitationLedgerCode.UNAUTHORIZED_LINK
    assert "attacker.example" not in str(captured.value)
    assert "attacker.example" not in repr(captured.value)


def test_ledger_allows_link_shaped_text_inside_code_but_never_rewrites_it():
    evidence = _evidence()
    ledger = WebCitationLedger()
    ledger.register_result(
        _result(evidence),
        kind=WebEvidenceKind.SEARCH_SNIPPET,
    )
    code_token = f"[example](webcite:{evidence.evidence_id})"
    content = (
        f"Verified [source](webcite:{evidence.evidence_id}).\n\n"
        f"Inline `{code_token} https://code.example <img src=x>` remains code.\n\n"
        "```html\n"
        '<a href="//code.example">sample</a>\n'
        "```\n"
        "The comparison 2 < 3 and 5 > 4 remains ordinary text."
    )

    finalized = ledger.finalize(content)

    assert code_token in finalized.content
    assert "https://code.example" in finalized.content
    assert '<a href="//code.example">sample</a>' in finalized.content
    assert "2 < 3 and 5 > 4" in finalized.content
    assert finalized.citation_count == 1


def test_unknown_and_cross_run_evidence_fail_closed_without_identity_leakage():
    evidence = _evidence()
    foreign = RunRequestContext.for_sync(user_id="alice", thread_id="foreign")
    current = RunRequestContext.for_sync(user_id="alice", thread_id="current")
    foreign.record_web_search_result(_result(evidence))
    current.mark_web_research_attempted()
    token = f"[source](webcite:{evidence.evidence_id})"

    try:
        with pytest.raises(WebCitationLedgerError) as captured:
            current.finalize_web_citations(token)
        error = captured.value
        assert error.code is WebCitationLedgerCode.UNKNOWN_EVIDENCE
        assert evidence.evidence_id not in str(error)
        assert evidence.evidence_id not in repr(error)
        assert evidence.canonical_url not in repr(error)
    finally:
        foreign.close()
        current.close()


@pytest.mark.parametrize(
    "content",
    [
        "webcite:web_ev_" + "a" * 64,
        "[source](webcite:web_ev_abc)",
        "![source](webcite:web_ev_" + "a" * 64 + ")",
        "[](webcite:web_ev_" + "a" * 64 + ")",
    ],
)
def test_malformed_or_non_link_webcite_tokens_are_rejected(content: str):
    ledger = WebCitationLedger()
    ledger.mark_attempted()

    with pytest.raises(WebCitationLedgerError) as captured:
        ledger.finalize(content)

    assert captured.value.code is WebCitationLedgerCode.INVALID_TOKEN
    assert "web_ev_" not in repr(captured.value)


def test_successful_evidence_requires_a_citation_but_failed_attempt_does_not():
    evidence = _evidence()
    with_evidence = WebCitationLedger()
    with_evidence.register_result(
        _result(evidence),
        kind=WebEvidenceKind.SEARCH_SNIPPET,
    )
    failed_attempt = WebCitationLedger()
    failed_attempt.mark_attempted()

    with pytest.raises(WebCitationLedgerError) as captured:
        with_evidence.finalize("No citation was emitted.")
    assert captured.value.code is WebCitationLedgerCode.REQUIRED

    finalized = failed_attempt.finalize("Search failed; no evidence is available.")
    assert finalized.content == "Search failed; no evidence is available."
    assert finalized.validation_applied is True
    assert finalized.citation_count == 0


def test_non_web_response_is_passthrough_until_web_research_is_attempted():
    ledger = WebCitationLedger()
    content = "A user-provided link may remain: https://user.example/input"

    finalized = ledger.finalize(content)

    assert finalized.content == content
    assert finalized.validation_applied is False


def test_fetched_page_provenance_promotes_authoritative_title_for_same_identity():
    search_evidence = _evidence(title="Search snippet title", content="Same body.")
    fetched_evidence = _evidence(title="Fetched page title", content="Same body.")
    ledger = WebCitationLedger()
    ledger.register_result(
        _result(search_evidence),
        kind=WebEvidenceKind.SEARCH_SNIPPET,
    )
    ledger.register_result(
        _result(fetched_evidence),
        kind=WebEvidenceKind.FETCHED_PAGE,
    )

    finalized = ledger.finalize(
        f"Claim [source](webcite:{search_evidence.evidence_id})."
    )

    assert "[Fetched page title]" in finalized.content
    assert "Search snippet title" not in finalized.content
    assert search_evidence.evidence_id not in repr(ledger)
    assert search_evidence.canonical_url not in repr(ledger)


def test_untrusted_source_title_cannot_render_a_second_raw_url():
    evidence = _evidence(title="Visit https://attacker.example now")
    ledger = WebCitationLedger()
    ledger.register_result(
        _result(evidence),
        kind=WebEvidenceKind.SEARCH_SNIPPET,
    )

    finalized = ledger.finalize(f"Claim [source](webcite:{evidence.evidence_id}).")

    assert "attacker.example" not in finalized.content
    assert f"[来源](<{evidence.canonical_url}>)" in finalized.content


def test_untrusted_source_title_is_html_and_markdown_escaped():
    evidence = _evidence(
        title='<img src=x onerror="alert(1)"> & [click] \\ quote\'"',
    )
    ledger = WebCitationLedger()
    ledger.register_result(
        _result(evidence),
        kind=WebEvidenceKind.SEARCH_SNIPPET,
    )

    finalized = ledger.finalize(f"Claim [source](webcite:{evidence.evidence_id}).")

    assert "<img" not in finalized.content
    assert "&lt;img" in finalized.content
    assert "&amp;" in finalized.content
    assert "\\[click\\]" in finalized.content
    assert 'onerror="' not in finalized.content


def test_context_keeps_fetch_capability_separate_from_citation_evidence():
    search_evidence = _evidence(content="Search result content.")
    fetched_evidence = _evidence(content="Fetched full page content.")
    ctx = RunRequestContext.for_sync(user_id="alice", thread_id="separate-seams")

    try:
        ctx.record_web_search_result(_result(search_evidence))
        ctx.record_web_fetch_result(_result(fetched_evidence))

        assert ctx.web_research_requires_terminal_validation() is True
        assert ctx.web_evidence_count() == 2
        assert (
            ctx.resolve_web_evidence(search_evidence.evidence_id)
            == search_evidence.canonical_url
        )
        assert ctx.resolve_web_evidence(fetched_evidence.evidence_id) is None
        rendered = ctx.finalize_web_citations(
            f"Fetched claim [source](webcite:{fetched_evidence.evidence_id})."
        )
        assert fetched_evidence.canonical_url in rendered
        assert "webcite:" not in rendered
    finally:
        ctx.close()


def test_context_close_clears_ledger_and_returns_only_stable_failure():
    evidence = _evidence()
    ctx = RunRequestContext.for_sync(user_id="alice", thread_id="closed")
    ctx.record_web_search_result(_result(evidence))
    ctx.close()

    assert ctx.web_research_requires_terminal_validation() is False
    assert ctx.web_evidence_count() == 0
    with pytest.raises(WebCitationLedgerError) as captured:
        ctx.finalize_web_citations(f"[source](webcite:{evidence.evidence_id})")
    assert captured.value.code is WebCitationLedgerCode.CONTEXT_CLOSED
    assert evidence.evidence_id not in repr(captured.value)


def test_web_tools_register_search_and_fetch_results_without_minting_fetch_ids(
    monkeypatch,
):
    search_evidence = _evidence(content="Search result content.")
    fetched_evidence = _evidence(content="Fetched full page content.")

    class Runtime:
        def search(self, query, *, limit, deadline_at, cancellation_probe):
            return _result(search_evidence)

        def fetch(self, url, *, deadline_at, cancellation_probe):
            assert url == search_evidence.canonical_url
            return _result(fetched_evidence)

    monkeypatch.setattr(
        "backend.tools.web.get_web_research_runtime",
        lambda: Runtime(),
    )
    ctx = RunRequestContext.for_sync(user_id="alice", thread_id="tool-ledger")

    try:
        search = make_web_search(ctx)
        fetch = make_web_fetch(ctx)
        search_result = search.invoke({"query": "public evidence"})
        assert ToolResultV1.model_validate(search_result).success is True
        assert ctx.web_evidence_count() == 1

        fetch_result = fetch.invoke({"evidence_id": search_evidence.evidence_id})
        assert ToolResultV1.model_validate(fetch_result).success is True
        assert ctx.web_evidence_count() == 2
        assert ctx.resolve_web_evidence(fetched_evidence.evidence_id) is None
        assert fetched_evidence.canonical_url in ctx.finalize_web_citations(
            f"Page claim [source](webcite:{fetched_evidence.evidence_id})."
        )
    finally:
        ctx.close()


def test_failed_web_tool_attempt_still_enables_terminal_validation(monkeypatch):
    class Runtime:
        def search(self, query, *, limit, deadline_at, cancellation_probe):
            raise WebResearchError(
                WebResearchErrorCode.SEARCH_UNAVAILABLE,
                retryable=True,
            )

    monkeypatch.setattr(
        "backend.tools.web.get_web_research_runtime",
        lambda: Runtime(),
    )
    ctx = RunRequestContext.for_sync(user_id="alice", thread_id="failed-tool")

    try:
        result = make_web_search(ctx).invoke({"query": "public evidence"})
        assert ToolResultV1.model_validate(result).success is False
        assert ctx.web_research_requires_terminal_validation() is True
        assert ctx.web_evidence_count() == 0
        assert (
            ctx.finalize_web_citations("No web evidence is available.")
            == "No web evidence is available."
        )
    finally:
        ctx.close()
