from __future__ import annotations

import asyncio
import threading
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from backend.web_research.contracts import WebResearchLimits
from backend.web_research.http import WebHttpFetch
from backend.web_research.runtime import (
    TavilyKeylessWebSearchAdapter,
    WebResearchError,
    WebResearchErrorCode,
    WebResearchRuntime,
    WebResearchRuntimeConfig,
    WebSearchHit,
    build_web_research_runtime,
    clear_web_research_runtime,
    get_web_research_runtime,
    install_web_research_runtime,
)
from backend.web_research.url_policy import WebUrlPolicy


_NOW = datetime(2026, 7, 16, 8, 0, tzinfo=timezone.utc)


class _Resolver:
    def __init__(self, addresses: dict[str, tuple[str, ...]]) -> None:
        self.addresses = addresses

    def resolve(
        self,
        host: str,
        port: int,
        *,
        deadline_at: float | None = None,
        cancellation_probe=None,
    ) -> tuple[str, ...]:
        del port, deadline_at, cancellation_probe
        return self.addresses[host]


class _Search:
    def __init__(self, hits) -> None:
        self.hits = hits
        self.calls = []

    def search(self, query: str, **kwargs):
        self.calls.append((query, kwargs))
        return self.hits


class _Fetch:
    def __init__(self, result: WebHttpFetch) -> None:
        self.result = result
        self.calls = []

    def get(self, url: str, **kwargs) -> WebHttpFetch:
        self.calls.append(("GET", url, kwargs))
        return self.result

    def post(self, url: str, **kwargs) -> WebHttpFetch:
        self.calls.append(("POST", url, kwargs))
        return self.result


def _policy() -> WebUrlPolicy:
    return WebUrlPolicy(
        _Resolver(
            {
                "api.tavily.com": ("1.1.1.1",),
                "news.openai.com": ("93.184.216.34",),
                "research.cloudflare.com": ("8.8.8.8",),
                "private.openai.com": ("127.0.0.1",),
            }
        )
    )


def test_search_returns_deterministic_bounded_evidence_and_omits_private_hits() -> None:
    policy = _policy()
    search = _Search(
        (
            WebSearchHit(
                "https://news.openai.com/story#section",
                "  Stable   title  ",
                "A stable search snippet.",
            ),
            WebSearchHit(
                "http://private.openai.com/admin",
                "Denied",
                "Must never become evidence",
            ),
            WebSearchHit(
                "https://news.openai.com/story",
                "Duplicate",
                "Different content at the same canonical URL.",
            ),
            WebSearchHit(
                "https://research.cloudflare.com/second",
                "Second",
                "Second result",
            ),
        )
    )
    runtime = WebResearchRuntime(
        url_policy=policy,
        search_adapter=search,
        clock=lambda: _NOW,
    )
    runtime.start()

    first = runtime.search("  architecture research  ", limit=4)
    second = runtime.search("architecture research", limit=4)

    assert first == second
    assert [item.canonical_url for item in first.evidence] == [
        "https://news.openai.com/story",
        "https://research.cloudflare.com/second",
    ]
    assert first.evidence[0].title == "Stable title"
    assert first.evidence[0].content == "A stable search snippet."
    assert all(
        item.encoded_size <= runtime.config.limits.max_total_evidence_bytes
        for item in first.evidence
    )
    assert first.encoded_size <= runtime.config.limits.max_total_evidence_bytes
    assert search.calls[0][0] == "architecture research"


def test_search_enforces_result_and_aggregate_byte_limits() -> None:
    limits = WebResearchLimits(
        max_title_bytes=16,
        max_snippet_bytes=24,
        max_content_bytes=32,
        max_total_evidence_bytes=900,
        max_evidence_items=2,
    )
    config = WebResearchRuntimeConfig(
        limits=limits,
        default_search_results=2,
    )
    runtime = WebResearchRuntime(
        url_policy=_policy(),
        config=config,
        search_adapter=_Search(
            (
                WebSearchHit(
                    "https://news.openai.com/one",
                    "T" * 100,
                    "a" * 100,
                ),
                WebSearchHit(
                    "https://research.cloudflare.com/two",
                    "second",
                    "b" * 100,
                ),
                WebSearchHit(
                    "https://news.openai.com/three",
                    "third",
                    "c" * 100,
                ),
            )
        ),
        clock=lambda: _NOW,
    )
    runtime.start()

    evidence = runtime.search("query", limit=20)

    assert len(evidence.evidence) <= 2
    assert evidence.encoded_size <= 900
    assert evidence.truncated is True
    assert all(len(item.content.encode("utf-8")) <= 32 for item in evidence.evidence)


def test_fetch_extracts_html_text_and_removes_active_or_hidden_content() -> None:
    policy = _policy()
    resolved = policy.resolve("https://news.openai.com/final")
    client = _Fetch(
        WebHttpFetch(
            resolved=resolved,
            status_code=200,
            headers={"content-type": "text/html; charset=utf-8"},
            body=b"""
                <html><head><title>  Research  Page </title>
                <style>.secret{display:none}</style></head>
                <body><main><h1>Finding</h1><p>Useful evidence.</p>
                <p hidden>hidden attribute</p><p aria-hidden="true">aria hidden</p>
                <p style="display: none">style hidden</p></main>
                <script>steal('token')</script><!-- hidden comment --></body></html>
            """,
            redirects=1,
        )
    )
    runtime = WebResearchRuntime(
        url_policy=policy,
        http_client=client,
        search_adapter=_Search(()),
        clock=lambda: _NOW,
    )
    runtime.start()

    result = runtime.fetch("https://news.openai.com/start?secret=value")
    evidence = result.evidence[0]

    assert evidence.canonical_url == "https://news.openai.com/final"
    assert evidence.title == "Research Page"
    assert evidence.content == "Finding\nUseful evidence."
    assert "token" not in evidence.content
    assert "comment" not in evidence.content
    assert result.encoded_size <= runtime.config.limits.max_total_evidence_bytes
    assert client.calls[0][2]["max_redirects"] == runtime.config.limits.max_redirects
    assert client.calls[0][2]["allowed_content_types"] == frozenset(
        {"text/html", "application/xhtml+xml", "text/plain"}
    )


def test_fetch_truncates_utf8_content_without_splitting_characters() -> None:
    limits = WebResearchLimits(
        max_title_bytes=32,
        max_snippet_bytes=32,
        max_content_bytes=33,
        max_total_evidence_bytes=700,
    )
    policy = _policy()
    client = _Fetch(
        WebHttpFetch(
            resolved=policy.resolve("https://news.openai.com/text"),
            status_code=200,
            headers={"content-type": "text/plain; charset=utf-8"},
            body=("证据" * 100).encode(),
            redirects=0,
        )
    )
    runtime = WebResearchRuntime(
        url_policy=policy,
        config=WebResearchRuntimeConfig(limits=limits),
        http_client=client,
        search_adapter=_Search(()),
        clock=lambda: _NOW,
    )
    runtime.start()

    evidence = runtime.fetch("https://news.openai.com/text").evidence[0]

    assert len(evidence.content.encode("utf-8")) <= 33
    assert evidence.content.encode("utf-8").decode("utf-8") == evidence.content


def test_fetch_rejects_binary_content_even_from_an_untrusted_fake_adapter() -> None:
    policy = _policy()
    client = _Fetch(
        WebHttpFetch(
            resolved=policy.resolve("https://news.openai.com/file"),
            status_code=200,
            headers={"content-type": "application/octet-stream"},
            body=b"\x00\x01secret binary",
            redirects=0,
        )
    )
    runtime = WebResearchRuntime(
        url_policy=policy,
        http_client=client,
        search_adapter=_Search(()),
    )
    runtime.start()

    with pytest.raises(WebResearchError) as raised:
        runtime.fetch("https://news.openai.com/file")

    assert raised.value.code == WebResearchErrorCode.INVALID_CONTENT.value
    assert "secret" not in str(raised.value)


def test_runtime_checks_cancel_and_deadline_before_calling_adapters() -> None:
    search = _Search(())
    runtime = WebResearchRuntime(
        url_policy=_policy(),
        search_adapter=search,
        monotonic=lambda: 10.0,
    )
    runtime.start()

    with pytest.raises(asyncio.CancelledError):
        runtime.search("query", cancellation_probe=lambda: True)

    def broken_probe() -> bool:
        raise RuntimeError("secret cancellation detail")

    with pytest.raises(asyncio.CancelledError) as cancelled:
        runtime.search("query", cancellation_probe=broken_probe)
    assert "secret" not in str(cancelled.value)
    with pytest.raises(WebResearchError) as raised:
        runtime.search("query", deadline_at=9.0)

    assert raised.value.code == WebResearchErrorCode.DEADLINE_EXCEEDED.value
    assert search.calls == []


def test_tavily_keyless_adapter_posts_bounded_json_without_an_api_key() -> None:
    policy = _policy()
    resolved = policy.resolve("https://api.tavily.com/search")
    client = _Fetch(
        WebHttpFetch(
            resolved=resolved,
            status_code=200,
            headers={"content-type": "application/json"},
            body=b'{"results":[{"url":"https://news.openai.com/a",'
            b'"title":"A","content":"Evidence","score":0.9}]}',
            redirects=0,
        )
    )
    config = WebResearchRuntimeConfig()
    adapter = TavilyKeylessWebSearchAdapter(client, config)

    hits = adapter.search(
        "safe query",
        limit=1,
        deadline_at=None,
        cancellation_probe=None,
    )

    assert hits == (WebSearchHit("https://news.openai.com/a", "A", "Evidence"),)
    assert client.calls[0][0] == "POST"
    assert client.calls[0][1] == "https://api.tavily.com/search"
    assert client.calls[0][2]["headers"]["X-Tavily-Access-Mode"] == "keyless"
    assert "Authorization" not in client.calls[0][2]["headers"]
    assert client.calls[0][2]["max_response_bytes"] == config.limits.max_response_bytes
    assert client.calls[0][2]["max_redirects"] == 0
    assert client.calls[0][2]["body"] == (
        b'{"query":"safe query","search_depth":"basic","max_results":1}'
    )


def test_process_runtime_is_installed_only_by_the_composition_root() -> None:
    runtime = WebResearchRuntime(url_policy=_policy(), search_adapter=_Search(()))
    runtime.start()
    clear_web_research_runtime()
    try:
        with pytest.raises(WebResearchError) as raised:
            get_web_research_runtime()
        assert raised.value.code == WebResearchErrorCode.RUNTIME_NOT_CONFIGURED.value

        install_web_research_runtime(runtime)
        assert get_web_research_runtime() is runtime
    finally:
        clear_web_research_runtime(runtime)


def test_lifecycle_readiness_matches_start_and_close_state() -> None:
    runtime = WebResearchRuntime(url_policy=_policy(), search_adapter=_Search(()))

    assert runtime.readiness() == {
        "enabled": True,
        "started": False,
        "closed": False,
        "ready": False,
        "search_ready": True,
    }
    with pytest.raises(WebResearchError) as not_started:
        runtime.search("query")
    assert not_started.value.code == WebResearchErrorCode.NOT_STARTED.value

    runtime.start()
    runtime.start()
    assert runtime.readiness()["ready"] is True
    runtime.close()
    assert runtime.readiness() == {
        "enabled": True,
        "started": False,
        "closed": True,
        "ready": False,
        "search_ready": True,
    }


def test_builder_maps_settings_without_importing_the_settings_layer() -> None:
    web = SimpleNamespace(
        enabled=True,
        request_timeout_seconds=3.0,
        dns_timeout_seconds=1.0,
        dns_max_concurrency=2,
        max_dns_addresses=4,
        max_query_bytes=256,
        max_url_bytes=512,
        max_title_bytes=128,
        max_snippet_bytes=512,
        max_content_bytes=1_024,
        max_total_evidence_bytes=4_096,
        max_response_bytes=8_192,
        max_compressed_bytes=4_096,
        default_search_results=2,
        max_search_results=3,
        max_citations=3,
        max_redirects=2,
        max_concurrency=2,
        user_agent="Builder-Agent/1.0",
    )

    runtime = build_web_research_runtime(SimpleNamespace(web_research=web))
    try:
        runtime.start()
        assert runtime.config.request_timeout_seconds == 3.0
        assert runtime.config.default_search_results == 2
        assert runtime.config.max_concurrency == 2
        assert runtime.config.limits.max_evidence_items == 3
        assert runtime.url_policy.max_url_bytes == 512
        assert runtime.url_policy.max_resolved_addresses == 4
        assert runtime.url_policy.allowed_scheme_ports == {
            "http": frozenset({80}),
            "https": frozenset({443}),
        }
        resolver = runtime.url_policy._resolver  # noqa: SLF001
        assert resolver.timeout_seconds == 1.0
        assert resolver.max_concurrency == 2
    finally:
        runtime.close()


def test_runtime_concurrency_wait_is_cancellable_before_adapter_entry() -> None:
    entered = threading.Event()
    release = threading.Event()
    cancelled = threading.Event()
    adapter_calls: list[str] = []

    class _BlockingSearch:
        def search(self, query: str, **kwargs):
            del kwargs
            adapter_calls.append(query)
            entered.set()
            assert release.wait(timeout=1)
            return ()

    runtime = WebResearchRuntime(
        url_policy=_policy(),
        config=WebResearchRuntimeConfig(max_concurrency=1),
        search_adapter=_BlockingSearch(),
    )
    runtime.start()
    failures: list[BaseException] = []

    def first() -> None:
        try:
            runtime.search("first")
        except BaseException as exc:  # pragma: no cover - assertion reports it
            failures.append(exc)

    def second() -> None:
        try:
            runtime.search("second", cancellation_probe=cancelled.is_set)
        except BaseException as exc:
            failures.append(exc)

    first_thread = threading.Thread(target=first)
    second_thread = threading.Thread(target=second)
    first_thread.start()
    assert entered.wait(timeout=1)
    second_thread.start()
    cancelled.set()
    second_thread.join(timeout=1)
    release.set()
    first_thread.join(timeout=1)
    runtime.close()

    assert not first_thread.is_alive()
    assert not second_thread.is_alive()
    assert adapter_calls == ["first"]
    assert len(failures) == 1
    assert isinstance(failures[0], asyncio.CancelledError)


def test_runtime_close_is_idempotent_and_closes_its_url_policy() -> None:
    policy = _policy()
    runtime = WebResearchRuntime(url_policy=policy, search_adapter=_Search(()))
    runtime.start()

    runtime.close()
    runtime.close()

    with pytest.raises(WebResearchError) as raised:
        runtime.search("query")
    assert raised.value.code == WebResearchErrorCode.CLOSED.value


@pytest.mark.parametrize(
    "values",
    [
        {"tavily_endpoint": "http://api.tavily.com/search"},
        {"tavily_endpoint": "https://user@api.tavily.com/search"},
        {"tavily_endpoint": "https://api.tavily.com.evil.test/search"},
        {"tavily_endpoint": "https://api.tavily.com/other"},
    ],
)
def test_tavily_configuration_rejects_endpoint_transport_risks(values) -> None:
    with pytest.raises(ValueError):
        WebResearchRuntimeConfig(**values)


def test_runtime_value_repr_omits_search_content_and_credentials() -> None:
    hit = WebSearchHit(
        "https://news.openai.com/?token=secret",
        "secret title",
        "secret snippet",
    )
    config = WebResearchRuntimeConfig()

    assert "secret" not in repr(hit)
    assert "secret" not in repr(config)
    assert "openai.com" not in repr(hit)
