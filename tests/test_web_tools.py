from __future__ import annotations

from datetime import datetime, timezone

from backend.core.settings import WebResearchSettings
from backend.runs.request_context import RunRequestContext
from backend.tools.catalog import (
    build_default_tool_registry,
    configured_secret_names,
)
from backend.tools.contracts import TOOL_RESULT_V1_SCHEMA, ToolResultV1
from backend.tools.registry import ToolAccess, ToolExposure
from backend.web_research.contracts import WebEvidence, WebResearchResult
from backend.web_research.runtime import WebResearchError, WebResearchErrorCode


NOW = datetime(2026, 8, 30, tzinfo=timezone.utc)


def _settings(*, enabled: bool = True, budget: int = 3_072) -> WebResearchSettings:
    return WebResearchSettings(
        _env_file=None,
        WEB_RESEARCH_ENABLED=enabled,
        WEB_RESEARCH_MAX_CONTENT_BYTES=budget,
        WEB_RESEARCH_MAX_TOTAL_SOURCE_BYTES=budget,
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
    title: str = "Research source",
    content: str = "Verified public evidence.",
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


def test_catalog_exposes_source_id_and_optional_query_schema() -> None:
    registry = build_default_tool_registry(web_research_settings=_settings())

    for role in ("user", "admin"):
        for name in ("web_search", "web_fetch"):
            descriptor = registry.describe(name, _access(role=role))
            assert descriptor is not None
            assert descriptor.version == "2.0.0"
            assert descriptor.output_schema == TOOL_RESULT_V1_SCHEMA
            assert descriptor.required_secrets == frozenset({"WEB_RESEARCH_RUNTIME"})
            assert descriptor.network_policy == "restricted"
            assert descriptor.observability_metadata_keys == frozenset(
                {"source_count", "output_bytes", "truncated"}
            )
            assert registry.exposure(name) is ToolExposure.DEFERRED

    fetch_schema = registry.descriptor("web_fetch").input_schema
    assert set(fetch_schema["properties"]) == {"source_id", "query"}
    assert fetch_schema["properties"]["source_id"]["pattern"].startswith("^S")
    assert "evidence_id" not in str(fetch_schema)
    assert "url" not in str(fetch_schema).casefold()
    assert "Tavily Extract" in registry.descriptor("web_fetch").description


def test_feature_flag_and_runtime_capability_intersection_fail_closed() -> None:
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


def test_web_search_registers_source_id_and_hides_server_only_fields() -> None:
    calls: list[dict[str, object]] = []
    server_result = _result()

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
            return server_result

    def cancelled() -> bool:
        return False

    context = RunRequestContext.for_sync(user_id="alice", thread_id="web-search")
    context.configure_provider_runtime(
        deadline_at=1234.5,
        cancellation_probe=cancelled,
    )
    registry = build_default_tool_registry(
        web_research_settings=_settings(),
        web_runtime=Runtime(),
    )
    session = registry.bind(context, _access())
    session.apply_skill({"web_search", "web_fetch"})
    try:
        payload = session.resolve("web_search").invoke(
            {
                "query": "current public research",
                "max_results": 3,
                "allowed_domains": ["Python.org", "docs.python.org"],
            }
        )
        tool_result = ToolResultV1.model_validate_json(payload)

        assert tool_result.success is True
        assert tool_result.data == {
            "sources": [
                {
                    "source_id": "S1",
                    "title": server_result.evidence[0].title,
                    "content": server_result.evidence[0].content,
                }
            ],
            "truncated": False,
        }
        source = context.resolve_web_source("S1")
        assert source is not None
        assert source.url == server_result.evidence[0].url
        assert source.default_query == "current public research"
        assert server_result.evidence[0].url not in payload
        assert "retrieved_at" not in payload
        assert calls == [
            {
                "query": "current public research",
                "limit": 3,
                "allowed_domains": ("docs.python.org", "python.org"),
                "deadline_at": 1234.5,
                "cancellation_probe": cancelled,
            }
        ]
    finally:
        context.close()


def test_web_search_crops_content_only_after_complete_tool_result_wrapping() -> None:
    settings = _settings(budget=3_072)
    first = _result(
        url="https://www.example.edu/research/first",
        content="a" * 2_000,
    ).evidence[0]
    second = _result(
        url="https://www.example.edu/research/second",
        content="b" * 2_000,
    ).evidence[0]
    server_result = WebResearchResult.create((first, second))

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
            return server_result

    context = RunRequestContext.for_sync(user_id="alice", thread_id="web-budget")
    registry = build_default_tool_registry(
        web_research_settings=settings,
        web_runtime=Runtime(),
    )
    session = registry.bind(context, _access())
    session.apply_skill({"web_search"})
    try:
        payload = session.resolve("web_search").invoke({"query": "public evidence"})
        result = ToolResultV1.model_validate_json(payload)

        assert result.success is True
        assert result.data["truncated"] is True
        assert len(payload.encode("utf-8")) <= settings.max_total_source_bytes
        assert [item["source_id"] for item in result.data["sources"]] == ["S1", "S2"]
        assert all(
            set(item) == {"source_id", "title", "content"}
            for item in result.data["sources"]
        )
        assert [item.content for item in server_result.evidence] == [
            "a" * 2_000,
            "b" * 2_000,
        ]
    finally:
        context.close()


def test_web_fetch_resolves_source_and_uses_original_query_when_omitted() -> None:
    search_result = _result()
    fetched_result = _result(title="", content="Relevant extracted chunk.")
    fetch_calls: list[dict[str, object]] = []

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
            query,
            deadline_at,
            cancellation_probe,
        ):
            fetch_calls.append(
                {
                    "url": url,
                    "query": query,
                    "deadline_at": deadline_at,
                    "cancellation_probe": cancellation_probe,
                }
            )
            return fetched_result

    context = RunRequestContext.for_sync(user_id="alice", thread_id="web-fetch")
    context.configure_provider_runtime(deadline_at=55.0, cancellation_probe=lambda: False)
    registry = build_default_tool_registry(
        web_research_settings=_settings(),
        web_runtime=Runtime(),
    )
    session = registry.bind(context, _access())
    session.apply_skill({"web_search", "web_fetch"})
    try:
        unknown = ToolResultV1.model_validate_json(
            session.resolve("web_fetch").invoke({"source_id": "S1"})
        )
        assert unknown.success is False
        assert unknown.error_code == "WEB_SOURCE_NOT_FOUND"
        assert fetch_calls == []

        session.resolve("web_search").invoke({"query": "original user question"})
        fetched = ToolResultV1.model_validate_json(
            session.resolve("web_fetch").invoke({"source_id": "S1"})
        )

        assert fetched.success is True
        assert fetched.data["sources"] == [
            {
                "source_id": "S1",
                "title": search_result.evidence[0].title,
                "content": "Relevant extracted chunk.",
            }
        ]
        assert fetch_calls[0]["url"] == search_result.evidence[0].url
        assert fetch_calls[0]["query"] == "original user question"
        assert fetch_calls[0]["deadline_at"] == 55.0
    finally:
        context.close()


def test_web_fetch_prefers_explicit_query() -> None:
    search_result = _result()
    observed_queries: list[str] = []

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
            query,
            deadline_at,
            cancellation_probe,
        ):
            observed_queries.append(query)
            return _result(content="specific chunk")

    context = RunRequestContext.for_sync(user_id="alice", thread_id="web-query")
    registry = build_default_tool_registry(
        web_research_settings=_settings(),
        web_runtime=Runtime(),
    )
    session = registry.bind(context, _access())
    session.apply_skill({"web_search", "web_fetch"})
    try:
        session.resolve("web_search").invoke({"query": "broad question"})
        result = ToolResultV1.model_validate_json(
            session.resolve("web_fetch").invoke(
                {"source_id": "S1", "query": "specific performance limitations"}
            )
        )
        assert result.success is True
        assert observed_queries == ["specific performance limitations"]
    finally:
        context.close()


def test_repeated_search_reuses_source_id_for_same_url() -> None:
    server_result = _result()

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
            return server_result

    context = RunRequestContext.for_sync(user_id="alice", thread_id="web-reuse")
    registry = build_default_tool_registry(
        web_research_settings=WebResearchSettings(
            _env_file=None,
            WEB_RESEARCH_ENABLED=True,
            WEB_RESEARCH_MAX_CONTENT_BYTES=8_192,
            WEB_RESEARCH_MAX_TOTAL_SOURCE_BYTES=8_192,
        ),
        web_runtime=Runtime(),
    )
    session = registry.bind(context, _access())
    session.apply_skill({"web_search"})
    try:
        first = ToolResultV1.model_validate_json(
            session.resolve("web_search").invoke({"query": "first query"})
        )
        second = ToolResultV1.model_validate_json(
            session.resolve("web_search").invoke({"query": "second query"})
        )
        assert first.data["sources"][0]["source_id"] == "S1"
        assert second.data["sources"][0]["source_id"] == "S1"
    finally:
        context.close()


def test_web_runtime_stable_error_is_preserved_without_sensitive_details() -> None:
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
            raise WebResearchError(
                WebResearchErrorCode.SEARCH_UNAVAILABLE,
                retryable=True,
                safe_details={"source_count": 0},
            )

    context = RunRequestContext.for_sync(user_id="alice", thread_id="web-failure")
    registry = build_default_tool_registry(
        web_research_settings=_settings(),
        web_runtime=Runtime(),
    )
    session = registry.bind(context, _access())
    session.apply_skill({"web_search"})
    try:
        payload = session.resolve("web_search").invoke(
            {"query": "secret-shaped query must not enter the failure"}
        )
        result = ToolResultV1.model_validate_json(payload)

        assert result.success is False
        assert result.error_code == "WEB_SEARCH_UNAVAILABLE"
        assert result.retryable is True
        assert "secret-shaped" not in payload
        assert "source_count" not in payload
    finally:
        context.close()
