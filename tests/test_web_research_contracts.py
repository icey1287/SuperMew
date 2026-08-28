from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from backend.web_research.contracts import (
    WebCitation,
    WebEvidence,
    WebResearchContractCode,
    WebResearchContractError,
    WebResearchLimits,
    WebResearchQuery,
    WebResearchResult,
)


NOW = datetime(2026, 7, 16, 8, 30, tzinfo=timezone.utc)


def evidence(
    *,
    url: str = "https://news.research.dev/article",
    title: str = "Architecture report",
    snippet: str = "A short summary",
    content: str = "Evidence body",
    retrieved_at: datetime = NOW,
    limits: WebResearchLimits = WebResearchLimits(),
) -> WebEvidence:
    return WebEvidence.create(
        canonical_url=url,
        title=title,
        snippet=snippet,
        content=content,
        retrieved_at=retrieved_at,
        limits=limits,
    )


def test_limits_have_security_ceilings_and_consistent_total_budget() -> None:
    with pytest.raises(ValueError, match="max_redirects"):
        WebResearchLimits(max_redirects=11)
    with pytest.raises(TypeError, match="max_query_bytes"):
        WebResearchLimits(max_query_bytes=True)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="cannot be smaller"):
        WebResearchLimits(
            max_content_bytes=1024,
            max_total_evidence_bytes=512,
        )


def test_query_is_canonical_bounded_and_never_exposes_a_hash_or_raw_repr() -> None:
    limits = WebResearchLimits(max_query_bytes=16, max_evidence_items=3)
    query = WebResearchQuery.create(
        "  source quality  ",
        max_results=3,
        limits=limits,
    )

    assert query.query == "source quality"
    assert query.observability_metadata() == {
        "max_results": 3,
        "query_bytes": len("source quality"),
    }
    assert "source quality" not in repr(query)
    assert not hasattr(query, "fingerprint")

    with pytest.raises(WebResearchContractError) as too_large:
        WebResearchQuery.create("四" * 6, limits=limits)
    assert too_large.value.code is WebResearchContractCode.INPUT_TOO_LARGE
    assert "四" not in str(too_large.value)

    with pytest.raises(WebResearchContractError) as too_many:
        WebResearchQuery.create("valid", max_results=4, limits=limits)
    assert too_many.value.code is WebResearchContractCode.INVALID_INPUT


def test_evidence_identity_is_stable_across_presentation_and_retrieval_time() -> None:
    first = evidence()
    second = evidence(
        title="Changed title",
        snippet="Changed snippet",
        retrieved_at=NOW + timedelta(hours=1),
    )

    assert first.evidence_id == second.evidence_id
    assert first.content_sha256 == second.content_sha256
    assert first.evidence_id.startswith("web_ev_")
    assert len(first.evidence_id) == len("web_ev_") + 64
    assert first.retrieved_at.tzinfo is timezone.utc

    assert evidence(content="Different body").evidence_id != first.evidence_id
    assert (
        evidence(
            url="https://news.research.dev/another",
        ).evidence_id
        != first.evidence_id
    )
    for sensitive in (
        first.evidence_id,
        first.canonical_url,
        first.title,
        first.snippet,
        first.content,
        first.content_sha256,
    ):
        assert sensitive not in repr(first)


def test_evidence_normalizes_retrieval_time_to_utc() -> None:
    china_time = datetime(
        2026,
        7,
        16,
        16,
        30,
        tzinfo=timezone(timedelta(hours=8)),
    )
    item = evidence(retrieved_at=china_time)
    assert item.retrieved_at == NOW
    assert item.retrieved_at.tzinfo is timezone.utc


def test_evidence_rejects_tampered_hash_identity_and_noncanonical_url() -> None:
    item = evidence()
    with pytest.raises(ValueError, match="content_sha256"):
        replace(item, content_sha256="0" * 64)
    with pytest.raises(ValueError, match="evidence_id"):
        replace(item, evidence_id=f"web_ev_{'0' * 64}")
    with pytest.raises(WebResearchContractError) as noncanonical:
        evidence(url="HTTPS://NEWS.RESEARCH.DEV:443/article")
    assert noncanonical.value.code is WebResearchContractCode.INVALID_EVIDENCE
    with pytest.raises(WebResearchContractError):
        evidence(url="https://news.research.dev/%7Earticle")
    with pytest.raises(WebResearchContractError):
        evidence(url="https://news.research.dev/two words")
    with pytest.raises(WebResearchContractError):
        evidence(url="https://news.research.dev/a/../article")
    with pytest.raises(ValueError, match="timezone-aware"):
        evidence(retrieved_at=datetime(2026, 7, 16, 8, 30))


@pytest.mark.parametrize(
    ("field", "kwargs"),
    [
        (
            "title",
            {
                "title": "abcd",
                "limits": WebResearchLimits(max_title_bytes=3),
            },
        ),
        (
            "snippet",
            {
                "snippet": "abcd",
                "limits": WebResearchLimits(max_snippet_bytes=3),
            },
        ),
        (
            "content",
            {
                "content": "abcd",
                "limits": WebResearchLimits(
                    max_content_bytes=3,
                    max_total_evidence_bytes=3,
                ),
            },
        ),
    ],
)
def test_evidence_fields_enforce_configured_utf8_size_limits(
    field: str,
    kwargs: dict[str, object],
) -> None:
    with pytest.raises(WebResearchContractError) as captured:
        evidence(**kwargs)  # type: ignore[arg-type]
    assert captured.value.code is WebResearchContractCode.OUTPUT_TOO_LARGE
    assert field not in str(captured.value).lower()


def test_citation_identity_is_derived_only_from_evidence_identity() -> None:
    item = evidence()
    citation = WebCitation.from_evidence(item)

    assert item.citation_token == f"[source](webcite:{item.evidence_id})"
    assert item.source_domain == "news.research.dev"
    assert item.to_public_dict()["canonical_url"] == item.canonical_url
    assert "citation_token" not in item.to_public_dict()
    assert item.to_tool_dict()["citation_token"] == item.citation_token
    assert item.to_tool_dict()["source_domain"] == item.source_domain
    assert "canonical_url" not in item.to_tool_dict()
    assert citation.evidence_id == item.evidence_id
    assert citation.citation_id.startswith("web_cit_")
    assert citation == WebCitation.from_evidence(item)

    with pytest.raises(ValueError, match="does not match"):
        replace(citation, citation_id=f"web_cit_{'0' * 64}")


def test_result_generates_citations_and_exposes_aggregate_observability_only() -> None:
    secret_body = "private body token"
    secret_url = "https://news.research.dev/private-path-token"
    item = evidence(url=secret_url, content=secret_body)
    result = WebResearchResult.create([item], truncated=True)

    assert result.citations == (WebCitation.from_evidence(item),)
    assert result.evidence == (item,)
    metadata = result.observability_metadata()
    assert metadata == {
        "citation_count": 1,
        "evidence_count": 1,
        "output_bytes": result.encoded_size,
        "truncated": True,
    }
    serialized = repr(metadata)
    for sensitive in (
        secret_body,
        secret_url,
        item.title,
        item.snippet,
        item.content_sha256,
        item.evidence_id,
    ):
        assert sensitive not in serialized
    assert set(item.observability_metadata()) == {
        "content_bytes",
        "snippet_bytes",
        "title_bytes",
    }
    assert result.tool_observability_metadata()["output_bytes"] == (
        result.tool_encoded_size
    )


def test_result_rejects_duplicate_or_unknown_identities() -> None:
    first = evidence()
    second = evidence(url="https://news.research.dev/second", content="Second")

    with pytest.raises(WebResearchContractError):
        WebResearchResult.create([first, first])

    with pytest.raises(WebResearchContractError) as unknown:
        WebResearchResult.create(
            [first],
            citations=[WebCitation.from_evidence(second)],
        )
    assert unknown.value.code is WebResearchContractCode.INVALID_CITATION


def test_result_enforces_item_citation_and_total_output_limits() -> None:
    first = evidence(content="a" * 700)
    second = evidence(
        url="https://news.research.dev/second",
        content="b" * 700,
    )

    with pytest.raises(WebResearchContractError) as item_count:
        WebResearchResult.create(
            [first, second],
            limits=WebResearchLimits(max_evidence_items=1),
        )
    assert item_count.value.code is WebResearchContractCode.OUTPUT_TOO_LARGE

    with pytest.raises(WebResearchContractError) as citation_count:
        WebResearchResult.create(
            [first, second],
            limits=WebResearchLimits(max_citations=1),
        )
    assert citation_count.value.code is WebResearchContractCode.OUTPUT_TOO_LARGE

    with pytest.raises(WebResearchContractError) as total_size:
        WebResearchResult.create(
            [first, second],
            limits=WebResearchLimits(
                max_content_bytes=800,
                max_total_evidence_bytes=1000,
            ),
        )
    assert total_size.value.code is WebResearchContractCode.OUTPUT_TOO_LARGE


def test_empty_result_is_valid_and_contains_no_citations() -> None:
    result = WebResearchResult.create([])
    assert result.evidence == ()
    assert result.citations == ()
    assert result.observability_metadata()["output_bytes"] == result.encoded_size
