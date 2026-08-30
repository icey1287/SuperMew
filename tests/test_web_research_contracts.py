from __future__ import annotations

from datetime import datetime, timezone

import pytest

from backend.web_research.contracts import (
    WebEvidence,
    WebResearchContractCode,
    WebResearchContractError,
    WebResearchLimits,
    WebResearchQuery,
    WebResearchResult,
    validate_source_id,
)


NOW = datetime(2026, 8, 30, tzinfo=timezone.utc)


def evidence(
    *,
    url: str = "https://docs.python.org/3.15/whatsnew/3.15.html",
    title: str = "What is new in Python 3.15",
    content: str = "Python 3.15 improves free-threading.",
    limits: WebResearchLimits = WebResearchLimits(),
) -> WebEvidence:
    return WebEvidence.create(
        url=url,
        title=title,
        content=content,
        retrieved_at=NOW,
        limits=limits,
    )


def test_limits_reject_invalid_values() -> None:
    with pytest.raises(ValueError, match="max_query_bytes"):
        WebResearchLimits(max_query_bytes=0)
    with pytest.raises(TypeError, match="max_evidence_items"):
        WebResearchLimits(max_evidence_items=True)  # type: ignore[arg-type]


def test_query_normalizes_whitespace_and_reports_only_sizes() -> None:
    query = WebResearchQuery.create(
        "  Python 3.15 free-threading  ",
        max_results=3,
        limits=WebResearchLimits(max_query_bytes=64, max_evidence_items=3),
    )

    assert query.query == "Python 3.15 free-threading"
    assert query.observability_metadata() == {
        "max_results": 3,
        "query_bytes": len(query.query.encode("utf-8")),
    }
    assert query.query not in repr(query)


def test_query_maps_size_failure_to_stable_contract_code() -> None:
    with pytest.raises(WebResearchContractError) as captured:
        WebResearchQuery.create(
            "too long",
            limits=WebResearchLimits(max_query_bytes=3),
        )

    assert captured.value.code is WebResearchContractCode.INPUT_TOO_LARGE


@pytest.mark.parametrize(
    "url",
    (
        "",
        "ftp://example.com/file",
        "https://user:secret@example.com/",
        "https://example.com/has space",
    ),
)
def test_evidence_rejects_non_web_provider_urls(url: str) -> None:
    with pytest.raises(WebResearchContractError) as captured:
        evidence(url=url)

    assert captured.value.code is WebResearchContractCode.INVALID_EVIDENCE


def test_server_evidence_keeps_url_and_time_out_of_model_projection() -> None:
    item = evidence()

    assert item.to_public_dict() == {
        "url": item.url,
        "title": item.title,
        "content": item.content,
        "retrieved_at": "2026-08-30T00:00:00Z",
    }
    assert item.to_tool_dict("S1") == {
        "source_id": "S1",
        "title": item.title,
        "content": item.content,
    }
    assert item.url not in repr(item)
    assert item.content not in repr(item)


def test_tool_projection_contains_only_source_id_title_content_and_truncated() -> None:
    first = evidence()
    second = evidence(
        url="https://peps.python.org/pep-0779/",
        title="PEP 779",
        content="Free-threaded Python is officially supported.",
    )
    result = WebResearchResult.create((first, second), truncated=True)

    projection = result.to_tool_dict(("S1", "S2"))

    assert projection == {
        "sources": [
            {"source_id": "S1", "title": first.title, "content": first.content},
            {
                "source_id": "S2",
                "title": second.title,
                "content": second.content,
            },
        ],
        "truncated": True,
    }
    serialized = str(projection)
    for removed in (
        "citations",
        "citation_id",
        "citation_token",
        "content_sha256",
        "evidence_id",
        "retrieved_at",
        "schema_version",
        "snippet",
        "source_domain",
        "url",
    ):
        assert removed not in serialized


def test_result_does_not_pretruncate_to_an_aggregate_quarter_budget() -> None:
    limits = WebResearchLimits(max_content_bytes=3_072, max_evidence_items=2)
    first = evidence(content="a" * 2_000, limits=limits)
    second = evidence(
        url="https://example.org/second",
        content="b" * 2_000,
        limits=limits,
    )

    result = WebResearchResult.create((first, second), limits=limits)

    assert [len(item.content) for item in result.evidence] == [2_000, 2_000]


def test_result_rejects_duplicate_urls_and_mismatched_source_ids() -> None:
    item = evidence()
    with pytest.raises(WebResearchContractError):
        WebResearchResult.create((item, item))

    result = WebResearchResult.create((item,))
    with pytest.raises(WebResearchContractError) as captured:
        result.to_tool_dict(())
    assert captured.value.code is WebResearchContractCode.INVALID_SOURCE_ID


@pytest.mark.parametrize("source_id", ("S1", "S12", "S999"))
def test_source_id_validation_accepts_short_run_local_ids(source_id: str) -> None:
    assert validate_source_id(source_id) == source_id


@pytest.mark.parametrize("source_id", ("", "S0", "s1", "S1000", "web_ev_dead"))
def test_source_id_validation_rejects_old_or_invalid_identities(source_id: str) -> None:
    with pytest.raises(ValueError):
        validate_source_id(source_id)


def test_observability_contains_counts_and_sizes_only() -> None:
    item = evidence(content="private body token")
    result = WebResearchResult.create((item,), truncated=True)

    assert result.tool_observability_metadata() == {
        "source_count": 1,
        "truncated": True,
    }
    metadata = result.observability_metadata()
    assert metadata["source_count"] == 1
    assert metadata["output_bytes"] == result.encoded_size
    assert item.content not in str(metadata)
    assert item.url not in str(metadata)
