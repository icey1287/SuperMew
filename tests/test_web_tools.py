from __future__ import annotations

from datetime import datetime, timezone

from backend.runs.request_context import RunRequestContext
from backend.core.settings import WebResearchSettings
from backend.tools.catalog import (
    build_default_tool_registry,
    configured_secret_names,
)
from backend.tools.contracts import TOOL_RESULT_V1_SCHEMA, ToolResultV1
from backend.tools.registry import ToolAccess, ToolExposure
from backend.web_research.contracts import WebEvidence, WebResearchResult
from backend.web_research.runtime import WebResearchError, WebResearchErrorCode


def _settings(
    *,
    enabled: bool = True,
) -> WebResearchSettings:
    return WebResearchSettings(
        _env_file=None,
        WEB_RESEARCH_ENABLED=enabled,
    )


def _access(
    *,
    role: str = "user",
    secrets: frozenset[str] = frozenset({"WEB_RESEARCH_RUNTIME"}),
) -> ToolAccess:
    return ToolAccess(
        roles=frozenset({role}),
        available_secrets=secrets,
        caller_allowed_tools=frozenset({"web_search", "web_fetch"}),
        approved_tools=frozenset(),
        allowed_network_policies=frozenset({"restricted"}),
    )


def _result(
    *,
    url: str = "https://www.example.edu/research",
    content: str = "Verified public evidence.",
) -> WebResearchResult:
    evidence = WebEvidence.create(
        canonical_url=url,
        title="Research source",
        snippet="Verified evidence",
        content=content,
        retrieved_at=datetime(2026, 7, 16, tzinfo=timezone.utc),
    )
    return WebResearchResult.create([evidence])


def test_catalog_registers_web_tools_as_runtime_gated_deferred_adapters():
    settings = _settings()
    registry = build_default_tool_registry(web_research_settings=settings)

    for role in ("user", "admin"):
        for name in ("web_search", "web_fetch"):
            descriptor = registry.describe(name, _access(role=role))
            assert descriptor is not None
            assert descriptor.output_schema == TOOL_RESULT_V1_SCHEMA
            assert descriptor.required_roles == frozenset()
            assert descriptor.required_secrets == frozenset({"WEB_RESEARCH_RUNTIME"})
            assert descriptor.network_policy == "restricted"
            assert descriptor.observability_metadata_keys == frozenset(
                {
                    "citation_count",
                    "evidence_count",
                    "output_bytes",
                    "truncated",
                }
            )
            assert registry.exposure(name) is ToolExposure.DEFERRED

    fetch_schema = registry.descriptor("web_fetch").input_schema
    assert set(fetch_schema["properties"]) == {"evidence_id"}
    assert "url" not in str(fetch_schema).casefold()


def test_feature_flag_and_runtime_capability_intersection_fail_closed():
    disabled = _settings(enabled=False)
    registry = build_default_tool_registry(web_research_settings=disabled)

    assert "WEB_RESEARCH_RUNTIME" not in configured_secret_names(
        registry,
        web_research_settings=disabled,
    )
    assert registry.describe("web_search", _access(secrets=frozenset())) is None

    enabled = _settings()
    assert "WEB_RESEARCH_RUNTIME" in configured_secret_names(
        registry,
        web_research_settings=enabled,
    )


def test_tool_envelope_budget_does_not_reuse_http_response_budget():
    settings = WebResearchSettings(
        _env_file=None,
        WEB_RESEARCH_ENABLED=True,
        WEB_RESEARCH_MAX_CONTENT_BYTES=75_000,
        WEB_RESEARCH_MAX_TOTAL_EVIDENCE_BYTES=80_000,
        WEB_RESEARCH_MAX_RESPONSE_BYTES=1_024,
        WEB_RESEARCH_MAX_COMPRESSED_BYTES=2_048,
    )
    result = _result(content="x" * 74_000)

    class Runtime:
        def search(self, query, *, limit, deadline_at, cancellation_probe):
            return result

    registry = build_default_tool_registry(
        web_research_settings=settings,
        web_runtime=Runtime(),
    )
    assert registry.descriptor("web_search").result_size_limit == 145_536

    ctx = RunRequestContext.for_sync(user_id="alice", thread_id="web-envelope")
    session = registry.bind(ctx, _access())
    session.apply_skill({"web_search"})
    session.search("public web evidence")

    payload = session.resolve("web_search").invoke({"query": "public research"})
    tool_result = ToolResultV1.model_validate_json(payload)

    assert tool_result.success is True
    assert len(payload.encode("utf-8")) > settings.max_response_bytes
    assert len(payload.encode("utf-8")) < (settings.max_total_evidence_bytes + 65_536)
    ctx.close()


def test_web_search_passes_run_controls_and_mints_fetch_capability():
    calls: list[dict] = []
    result = _result()

    class Runtime:
        def search(self, query, *, limit, deadline_at, cancellation_probe):
            calls.append(
                {
                    "query": query,
                    "limit": limit,
                    "deadline_at": deadline_at,
                    "cancellation_probe": cancellation_probe,
                }
            )
            return result

    def cancelled() -> bool:
        return False

    ctx = RunRequestContext.for_sync(user_id="alice", thread_id="web-search")
    ctx.configure_provider_runtime(
        deadline_at=1234.5,
        cancellation_probe=cancelled,
    )
    registry = build_default_tool_registry(
        web_research_settings=_settings(),
        web_runtime=Runtime(),
    )
    session = registry.bind(ctx, _access())
    session.apply_skill({"web_search", "web_fetch"})
    session.search("public web evidence")

    payload = session.resolve("web_search").invoke(
        {"query": "current public research", "max_results": 3}
    )
    tool_result = ToolResultV1.model_validate_json(payload)
    evidence = result.evidence[0]

    assert tool_result.success is True
    assert tool_result.data == result.to_public_dict()
    assert result.observability_metadata().items() <= (
        tool_result.observability_metadata.items()
    )
    assert ctx.resolve_web_evidence(evidence.evidence_id) == evidence.canonical_url
    assert calls == [
        {
            "query": "current public research",
            "limit": 3,
            "deadline_at": 1234.5,
            "cancellation_probe": cancelled,
        }
    ]
    assert "current public research" not in str(tool_result.observability_metadata)
    assert evidence.canonical_url not in str(tool_result.observability_metadata)
    ctx.close()


def test_repeated_web_searches_share_one_run_evidence_budget():
    settings = WebResearchSettings(
        _env_file=None,
        WEB_RESEARCH_ENABLED=True,
        WEB_RESEARCH_MAX_CONTENT_BYTES=3_072,
        WEB_RESEARCH_MAX_TOTAL_EVIDENCE_BYTES=3_072,
    )
    calls = 0

    class Runtime:
        def search(self, query, *, limit, deadline_at, cancellation_probe):
            nonlocal calls
            calls += 1
            return _result(
                url=f"https://www.example.edu/research/{calls}",
                content=(f"evidence-{calls}-" + ("x" * 2_600)),
            )

    ctx = RunRequestContext.for_sync(user_id="alice", thread_id="web-cumulative")
    registry = build_default_tool_registry(
        web_research_settings=settings,
        web_runtime=Runtime(),
    )
    session = registry.bind(ctx, _access())
    session.apply_skill({"web_search"})
    session.search("public web evidence")

    first_payload = session.resolve("web_search").invoke(
        {"query": "first public source"}
    )
    second_payload = session.resolve("web_search").invoke(
        {"query": "second public source"}
    )
    first = ToolResultV1.model_validate_json(first_payload)
    second = ToolResultV1.model_validate_json(second_payload)
    assert first.success is True
    assert second.success is True
    assert second.data["truncated"] is True
    assert (
        len(first_payload.encode("utf-8")) + len(second_payload.encode("utf-8"))
        <= settings.max_total_evidence_bytes
    )
    assert calls == 2
    ctx.close()


def test_web_fetch_accepts_only_run_local_search_evidence():
    calls: list[dict] = []
    search_result = _result()
    fetched_result = _result(
        url="https://www.example.edu/research",
        content="Full verified public page.",
    )

    class Runtime:
        def search(self, query, *, limit, deadline_at, cancellation_probe):
            return search_result

        def fetch(self, url, *, deadline_at, cancellation_probe):
            calls.append(
                {
                    "url": url,
                    "deadline_at": deadline_at,
                    "cancellation_probe": cancellation_probe,
                }
            )
            return fetched_result

    ctx = RunRequestContext.for_sync(user_id="alice", thread_id="web-fetch")
    ctx.configure_provider_runtime(deadline_at=55.0, cancellation_probe=lambda: False)
    registry = build_default_tool_registry(
        web_research_settings=_settings(),
        web_runtime=Runtime(),
    )
    session = registry.bind(ctx, _access())
    session.apply_skill({"web_search", "web_fetch"})
    session.search("public web evidence fetch")

    unknown_payload = session.resolve("web_fetch").invoke(
        {"evidence_id": f"web_ev_{'0' * 64}"}
    )
    unknown = ToolResultV1.model_validate_json(unknown_payload)
    assert unknown.success is False
    assert unknown.error_code == "WEB_EVIDENCE_NOT_AUTHORIZED"
    assert calls == []

    session.resolve("web_search").invoke({"query": "public source"})
    evidence = search_result.evidence[0]
    fetched_payload = session.resolve("web_fetch").invoke(
        {"evidence_id": evidence.evidence_id}
    )
    fetched = ToolResultV1.model_validate_json(fetched_payload)

    assert fetched.success is True
    assert calls[0]["url"] == evidence.canonical_url
    assert calls[0]["deadline_at"] == 55.0
    ctx.close()


def test_web_runtime_stable_error_is_preserved_without_sensitive_details():
    class Runtime:
        def search(self, query, *, limit, deadline_at, cancellation_probe):
            raise WebResearchError(
                WebResearchErrorCode.SEARCH_UNAVAILABLE,
                retryable=True,
                safe_details={"source_count": 0},
            )

    ctx = RunRequestContext.for_sync(user_id="alice", thread_id="web-failure")
    registry = build_default_tool_registry(
        web_research_settings=_settings(),
        web_runtime=Runtime(),
    )
    session = registry.bind(ctx, _access())
    session.apply_skill({"web_search"})
    session.search("public web evidence")

    payload = session.resolve("web_search").invoke(
        {"query": "secret-shaped query must not enter the failure"}
    )
    result = ToolResultV1.model_validate_json(payload)

    assert result.success is False
    assert result.error_code == "WEB_SEARCH_UNAVAILABLE"
    assert result.retryable is True
    assert "secret-shaped" not in payload
    assert "source_count" not in payload
    ctx.close()
