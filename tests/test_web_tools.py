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
from backend.web_research.contracts import (
    WebEvidence,
    WebResearchLimits,
    WebResearchResult,
)
from backend.web_research.runtime import WebResearchError, WebResearchErrorCode


NOW = datetime(2026, 8, 30, tzinfo=timezone.utc)


def _settings(
    *,
    enabled: bool = True,
    search_provider_max_results: int = 3,
    search_model_visible_results: int = 3,
    search_per_source_max_bytes: int = 480,
    search_total_snippet_max_bytes: int = 1_440,
    fetch_response_max_bytes: int = 6_144,
    fetch_run_total_max_bytes: int = 12_288,
) -> WebResearchSettings:
    return WebResearchSettings(
        _env_file=None,
        WEB_RESEARCH_ENABLED=enabled,
        WEB_RESEARCH_SEARCH_PROVIDER_MAX_RESULTS=search_provider_max_results,
        WEB_RESEARCH_SEARCH_MODEL_VISIBLE_RESULTS=search_model_visible_results,
        WEB_RESEARCH_SEARCH_PER_SOURCE_MAX_BYTES=search_per_source_max_bytes,
        WEB_RESEARCH_SEARCH_TOTAL_SNIPPET_MAX_BYTES=(search_total_snippet_max_bytes),
        WEB_RESEARCH_FETCH_RESPONSE_MAX_BYTES=fetch_response_max_bytes,
        WEB_RESEARCH_FETCH_RUN_TOTAL_MAX_BYTES=fetch_run_total_max_bytes,
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
            assert descriptor.version == "2.2.0"
            assert descriptor.output_schema == TOOL_RESULT_V1_SCHEMA
            assert descriptor.required_secrets == frozenset({"WEB_RESEARCH_RUNTIME"})
            assert descriptor.network_policy == "restricted"
            assert descriptor.observability_metadata_keys == frozenset(
                {"source_count", "output_bytes", "truncated"}
            )
            assert registry.exposure(name) is ToolExposure.DEFERRED

    fetch_schema = registry.descriptor("web_fetch").input_schema
    search_schema = registry.descriptor("web_search").input_schema
    assert search_schema["properties"]["max_results"]["default"] == 3
    assert search_schema["properties"]["max_results"]["maximum"] == 3
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
                    "source": "www.example.edu",
                    "snippet": server_result.evidence[0].content,
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


def test_web_search_applies_visible_count_and_snippet_only_budgets() -> None:
    settings = _settings(
        search_provider_max_results=4,
        search_model_visible_results=3,
        search_total_snippet_max_bytes=1_000,
    )
    server_result = WebResearchResult.create(
        tuple(
            _result(
                url=f"https://www.example.edu/research/{index}",
                content=character * 2_000,
            ).evidence[0]
            for index, character in enumerate("abcd", start=1)
        ),
        limits=WebResearchLimits(max_evidence_items=4),
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
            return server_result

    context = RunRequestContext.for_sync(user_id="alice", thread_id="web-budget")
    registry = build_default_tool_registry(
        web_research_settings=settings,
        web_runtime=Runtime(),
    )
    session = registry.bind(context, _access())
    session.apply_skill({"web_search"})
    try:
        payload = session.resolve("web_search").invoke(
            {"query": "public evidence", "max_results": 4}
        )
        result = ToolResultV1.model_validate_json(payload)

        assert result.success is True
        assert result.data["truncated"] is True
        assert [item["source_id"] for item in result.data["sources"]] == [
            "S1",
            "S2",
            "S3",
        ]
        assert all(
            set(item) == {"source_id", "title", "source", "snippet"}
            for item in result.data["sources"]
        )
        assert all(
            item["source"] == "www.example.edu" for item in result.data["sources"]
        )
        snippet_sizes = [
            len(item["snippet"].encode("utf-8")) for item in result.data["sources"]
        ]
        assert snippet_sizes == [480, 480, 40]
        assert sum(snippet_sizes) == 1_000
        assert [item.content for item in server_result.evidence] == [
            character * 2_000 for character in "abcd"
        ]
        assert context.resolve_web_source("S4") is not None
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
    context.configure_provider_runtime(
        deadline_at=55.0, cancellation_probe=lambda: False
    )
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


def test_web_search_does_not_consume_the_run_local_web_fetch_budget() -> None:
    search_result = _result(content="s" * 2_000)
    fetched_result = _result(content="f" * 5_000)

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
            return fetched_result

    settings = _settings(
        fetch_response_max_bytes=1_400,
        fetch_run_total_max_bytes=2_800,
    )
    context = RunRequestContext.for_sync(user_id="alice", thread_id="web-fetch-budget")
    registry = build_default_tool_registry(
        web_research_settings=settings,
        web_runtime=Runtime(),
    )
    session = registry.bind(context, _access())
    session.apply_skill({"web_search", "web_fetch"})
    try:
        search = ToolResultV1.model_validate_json(
            session.resolve("web_search").invoke({"query": "broad question"})
        )
        first_payload = session.resolve("web_fetch").invoke({"source_id": "S1"})
        second_payload = session.resolve("web_fetch").invoke({"source_id": "S1"})
        exhausted = ToolResultV1.model_validate_json(
            session.resolve("web_fetch").invoke({"source_id": "S1"})
        )

        assert search.success is True
        assert len(search.data["sources"][0]["snippet"].encode("utf-8")) == 480
        for payload in (first_payload, second_payload):
            fetched = ToolResultV1.model_validate_json(payload)
            assert fetched.success is True
            assert fetched.data["truncated"] is True
            assert len(payload.encode("utf-8")) <= settings.fetch_response_max_bytes
        assert exhausted.success is False
        assert exhausted.error_code == "WEB_FETCH_BUDGET_EXHAUSTED"
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
        web_research_settings=_settings(),
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
