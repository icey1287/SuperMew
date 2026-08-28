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
            assert descriptor.version == "1.1.0"
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
    search_schema = registry.descriptor("web_search").input_schema
    assert set(search_schema["properties"]) == {
        "allowed_domains",
        "max_results",
        "query",
    }
    assert search_schema["properties"]["allowed_domains"]["maxItems"] == 8
    assert "official-domain filtering" in registry.descriptor("web_search").description
    assert "previously authorized" in registry.descriptor("web_fetch").description


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
        def search(
            self,
            query,
            *,
            limit,
            allowed_domains,
            deadline_at,
            cancellation_probe,
        ):
            calls.append(
                {
                    "query": query,
                    "limit": limit,
                    "allowed_domains": allowed_domains,
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

    payload = session.resolve("web_search").invoke(
        {
            "query": "current public research",
            "max_results": 3,
            "allowed_domains": ["Python.org", "docs.python.org", "python.org"],
        }
    )
    tool_result = ToolResultV1.model_validate_json(payload)
    evidence = result.evidence[0]

    assert tool_result.success is True
    public_evidence = tool_result.data["evidence"][0]
    assert public_evidence["citation_token"] == evidence.citation_token
    assert public_evidence["source_domain"] == "www.example.edu"
    assert public_evidence["content"] == evidence.content
    assert "canonical_url" not in public_evidence
    assert result.to_public_dict()["evidence"][0]["canonical_url"] == (
        evidence.canonical_url
    )
    assert result.tool_observability_metadata().items() <= (
        tool_result.observability_metadata.items()
    )
    assert ctx.resolve_web_evidence(evidence.evidence_id) == evidence.canonical_url
    assert ctx.resolve_web_fetch_authorization(evidence.evidence_id) == (
        evidence.canonical_url,
        ("docs.python.org", "python.org"),
    )
    assert calls == [
        {
            "query": "current public research",
            "limit": 3,
            "allowed_domains": ("docs.python.org", "python.org"),
            "deadline_at": 1234.5,
            "cancellation_probe": cancelled,
        }
    ]
    assert "current public research" not in str(tool_result.observability_metadata)
    assert evidence.canonical_url not in str(tool_result.observability_metadata)
    ctx.close()


def test_repeated_search_evidence_merges_compatible_domain_scopes():
    result = _result(url="https://docs.example.edu/research")

    class Runtime:
        def search(
            self,
            query,
            *,
            limit,
            allowed_domains,
            deadline_at,
            cancellation_probe,
        ):
            return result

    ctx = RunRequestContext.for_sync(user_id="alice", thread_id="web-scope-merge")
    registry = build_default_tool_registry(
        web_research_settings=_settings(),
        web_runtime=Runtime(),
    )
    session = registry.bind(ctx, _access())
    session.apply_skill({"web_search"})

    first = ToolResultV1.model_validate_json(
        session.resolve("web_search").invoke(
            {
                "query": "official release",
                "allowed_domains": ["example.edu"],
            }
        )
    )
    second = ToolResultV1.model_validate_json(
        session.resolve("web_search").invoke(
            {
                "query": "official documentation",
                "allowed_domains": ["docs.example.edu"],
            }
        )
    )

    assert first.success is True
    assert second.success is True
    assert ctx.resolve_web_fetch_authorization(result.evidence[0].evidence_id) == (
        result.evidence[0].canonical_url,
        ("docs.example.edu", "example.edu"),
    )
    ctx.close()


def test_repeated_web_search_stops_when_run_evidence_budget_is_exhausted():
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

    first_payload = session.resolve("web_search").invoke(
        {"query": "first public source"}
    )
    second_payload = session.resolve("web_search").invoke(
        {"query": "second public source"}
    )
    first = ToolResultV1.model_validate_json(first_payload)
    second = ToolResultV1.model_validate_json(second_payload)
    assert first.success is True
    assert second.success is False
    assert second.error_code == "WEB_EVIDENCE_BUDGET_EXHAUSTED"
    assert second.retryable is False
    assert (
        len(first_payload.encode("utf-8")) + len(second_payload.encode("utf-8"))
        <= settings.max_total_evidence_bytes
    )
    assert calls == 1
    ctx.close()


def test_web_search_skips_provider_when_remaining_budget_cannot_fit_evidence():
    calls = 0

    class Runtime:
        def search(self, query, *, limit, deadline_at, cancellation_probe):
            nonlocal calls
            calls += 1
            return _result(content="x" * 1_024)

    ctx = RunRequestContext.for_sync(user_id="alice", thread_id="web-fitted-empty")
    ctx.claim_web_tool_result_budget(2_500, limit_bytes=3_072)
    registry = build_default_tool_registry(
        web_research_settings=_settings(),
        web_runtime=Runtime(),
    )
    session = registry.bind(ctx, _access())
    session.apply_skill({"web_search"})

    payload = session.resolve("web_search").invoke({"query": "public source"})
    result = ToolResultV1.model_validate_json(payload)

    assert result.success is False
    assert result.error_code == "WEB_EVIDENCE_BUDGET_EXHAUSTED"
    assert result.retryable is False
    assert calls == 0
    ctx.close()


def test_web_fetch_accepts_only_run_local_search_evidence():
    calls: list[dict] = []
    search_result = _result()
    fetched_result = _result(
        url="https://www.example.edu/research",
        content="Full verified public page.",
    )

    class Runtime:
        def search(
            self,
            query,
            *,
            limit,
            allowed_domains,
            deadline_at,
            cancellation_probe,
        ):
            return search_result

        def fetch(
            self,
            url,
            *,
            allowed_domains,
            deadline_at,
            cancellation_probe,
        ):
            calls.append(
                {
                    "url": url,
                    "allowed_domains": allowed_domains,
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

    unknown_payload = session.resolve("web_fetch").invoke(
        {"evidence_id": f"web_ev_{'0' * 64}"}
    )
    unknown = ToolResultV1.model_validate_json(unknown_payload)
    assert unknown.success is False
    assert unknown.error_code == "WEB_EVIDENCE_NOT_AUTHORIZED"
    assert calls == []

    session.resolve("web_search").invoke(
        {
            "query": "public source",
            "allowed_domains": ["example.edu"],
        }
    )
    evidence = search_result.evidence[0]
    fetched_payload = session.resolve("web_fetch").invoke(
        {"evidence_id": evidence.evidence_id}
    )
    fetched = ToolResultV1.model_validate_json(fetched_payload)

    assert fetched.success is True
    assert calls[0]["url"] == evidence.canonical_url
    assert calls[0]["allowed_domains"] == ("example.edu",)
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
