"""Bounded Web Research runtime with injectable search and transport Adapters."""

from __future__ import annotations

import asyncio
import json
import math
import re
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from itertools import islice
from typing import Protocol
from urllib.parse import urlsplit

from bs4 import BeautifulSoup
from bs4.element import Comment

from backend.web_research.contracts import (
    DEFAULT_WEB_RESEARCH_LIMITS,
    WebCitation,
    WebEvidence,
    WebResearchContractCode,
    WebResearchContractError,
    WebResearchLimits,
    WebResearchResult,
)
from backend.web_research.http import (
    CancellationProbe,
    SafeWebHttpClient,
    WebHttpError,
    WebHttpFetch,
)
from backend.web_research.url_policy import (
    WebUrlPolicy,
    WebUrlPolicyCode,
    WebUrlPolicyError,
    SystemWebDnsResolver,
)


_HTML_CONTENT_TYPES = frozenset({"text/html", "application/xhtml+xml"})
_FETCH_CONTENT_TYPES = _HTML_CONTENT_TYPES | frozenset({"text/plain"})
_JSON_CONTENT_TYPES = frozenset({"application/json", "application/problem+json"})
_WHITESPACE = re.compile(r"[\t\x0b\x0c\r ]+")
_CHARSET = re.compile(r"(?:^|;)\s*charset\s*=\s*[\"']?([^;\"']+)", re.I)
_SAFE_TEXT_ENCODINGS = {
    "ascii": "ascii",
    "gb18030": "gb18030",
    "iso-8859-1": "latin-1",
    "latin-1": "latin-1",
    "us-ascii": "ascii",
    "utf-8": "utf-8",
    "utf8": "utf-8",
    "windows-1252": "cp1252",
}


class WebResearchErrorCode(StrEnum):
    DISABLED = "WEB_RESEARCH_DISABLED"
    SEARCH_NOT_CONFIGURED = "WEB_SEARCH_NOT_CONFIGURED"
    SEARCH_UNAVAILABLE = "WEB_SEARCH_UNAVAILABLE"
    INVALID_SEARCH_RESPONSE = "WEB_INVALID_SEARCH_RESPONSE"
    INVALID_CONTENT = "WEB_INVALID_CONTENT"
    DEADLINE_EXCEEDED = "WEB_DEADLINE_EXCEEDED"
    FETCH_UNAVAILABLE = "WEB_FETCH_UNAVAILABLE"
    CLOSED = "WEB_RESEARCH_CLOSED"
    NOT_STARTED = "WEB_RESEARCH_NOT_STARTED"
    RUNTIME_NOT_CONFIGURED = "WEB_RESEARCH_RUNTIME_NOT_CONFIGURED"


class WebResearchError(RuntimeError):
    """Stable runtime failure that never embeds query, URL, body, or secrets."""

    def __init__(
        self,
        code: WebResearchErrorCode | str,
        *,
        retryable: bool = False,
        safe_details: Mapping[str, str | int] | None = None,
    ) -> None:
        self.code = WebResearchErrorCode(code).value
        self.retryable = bool(retryable)
        self.safe_details = dict(safe_details or {})
        super().__init__(self.code)


@dataclass(frozen=True, slots=True)
class WebSearchHit:
    url: str = field(repr=False)
    title: str = field(default="", repr=False)
    snippet: str = field(default="", repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.url, str) or not self.url.strip():
            raise ValueError("search hit URL must be a non-empty string")
        if not isinstance(self.title, str) or not isinstance(self.snippet, str):
            raise TypeError("search hit title and snippet must be strings")
        object.__setattr__(self, "url", self.url.strip())
        object.__setattr__(self, "title", self.title.strip())
        object.__setattr__(self, "snippet", self.snippet.strip())


class WebSearchAdapter(Protocol):
    def search(
        self,
        query: str,
        *,
        limit: int,
        deadline_at: float | None,
        cancellation_probe: CancellationProbe | None,
    ) -> Sequence[WebSearchHit]: ...


class WebFetchClient(Protocol):
    def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        allowed_content_types: frozenset[str],
        max_compressed_bytes: int,
        max_response_bytes: int,
        max_redirects: int,
        timeout_seconds: float,
        deadline_at: float | None = None,
        cancellation_probe: CancellationProbe | None = None,
    ) -> WebHttpFetch: ...

    def post(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        body: bytes,
        allowed_content_types: frozenset[str],
        max_compressed_bytes: int,
        max_response_bytes: int,
        max_redirects: int,
        timeout_seconds: float,
        deadline_at: float | None = None,
        cancellation_probe: CancellationProbe | None = None,
    ) -> WebHttpFetch: ...


class WebResearchSettingsLike(Protocol):
    enabled: bool
    request_timeout_seconds: float
    dns_timeout_seconds: float
    dns_max_concurrency: int
    max_dns_addresses: int
    max_query_bytes: int
    max_url_bytes: int
    max_title_bytes: int
    max_snippet_bytes: int
    max_content_bytes: int
    max_total_evidence_bytes: int
    max_response_bytes: int
    max_compressed_bytes: int
    default_search_results: int
    max_search_results: int
    max_citations: int
    max_redirects: int
    max_concurrency: int
    user_agent: str


class AppWebResearchSettingsLike(Protocol):
    web_research: WebResearchSettingsLike


@dataclass(frozen=True, slots=True)
class WebResearchRuntimeConfig:
    """Internal config object; application settings map to it at composition."""

    enabled: bool = True
    tavily_endpoint: str = field(
        default="https://api.tavily.com/search",
        repr=False,
    )
    request_timeout_seconds: float = 10.0
    max_compressed_bytes: int = 1_000_000
    default_search_results: int = 5
    max_concurrency: int = 4
    user_agent: str = "SuperMew-WebResearch/1.0"
    limits: WebResearchLimits = DEFAULT_WEB_RESEARCH_LIMITS

    def __post_init__(self) -> None:
        if not isinstance(self.enabled, bool):
            raise TypeError("enabled must be a bool")
        if not isinstance(self.tavily_endpoint, str):
            raise TypeError("tavily_endpoint must be a string")
        endpoint = self.tavily_endpoint.strip()
        if not endpoint:
            raise ValueError("tavily_endpoint cannot be empty")
        try:
            endpoint_parts = urlsplit(endpoint)
            endpoint_port = endpoint_parts.port
        except ValueError:
            raise ValueError("tavily_endpoint must be a valid HTTPS URL") from None
        if (
            endpoint_parts.scheme.casefold() != "https"
            or endpoint_parts.hostname != "api.tavily.com"
            or endpoint_port not in {None, 443}
            or endpoint_parts.username is not None
            or endpoint_parts.password is not None
            or endpoint_parts.fragment
            or endpoint_parts.query
            or endpoint_parts.path != "/search"
        ):
            raise ValueError("tavily_endpoint must be the fixed Tavily HTTPS search URL")
        object.__setattr__(self, "tavily_endpoint", endpoint)
        if (
            isinstance(self.request_timeout_seconds, bool)
            or not isinstance(self.request_timeout_seconds, (int, float))
            or not math.isfinite(float(self.request_timeout_seconds))
            or self.request_timeout_seconds <= 0
        ):
            raise ValueError("request_timeout_seconds must be positive and finite")
        if (
            isinstance(self.max_compressed_bytes, bool)
            or not isinstance(self.max_compressed_bytes, int)
            or not 1 <= self.max_compressed_bytes <= 8 * 1024 * 1024
        ):
            raise ValueError("max_compressed_bytes must be between 1 and 8 MiB")
        if (
            isinstance(self.default_search_results, bool)
            or not isinstance(self.default_search_results, int)
            or self.default_search_results <= 0
        ):
            raise ValueError("default_search_results must be a positive integer")
        if (
            isinstance(self.max_concurrency, bool)
            or not isinstance(self.max_concurrency, int)
            or not 1 <= self.max_concurrency <= 64
        ):
            raise ValueError("max_concurrency must be between 1 and 64")
        if not isinstance(self.user_agent, str):
            raise TypeError("user_agent must be a string")
        agent = self.user_agent.strip()
        if not agent or "\r" in agent or "\n" in agent:
            raise ValueError("user_agent must be a safe non-empty value")
        object.__setattr__(self, "user_agent", agent)
        if not isinstance(self.limits, WebResearchLimits):
            raise TypeError("limits must be WebResearchLimits")
        if self.default_search_results > min(
            self.limits.max_evidence_items,
            self.limits.max_citations,
        ):
            raise ValueError("default_search_results exceeds the result limit")
        empty_result = WebResearchResult(evidence=(), citations=())
        if self.limits.max_total_evidence_bytes < empty_result.encoded_size:
            raise ValueError(
                "max_total_evidence_bytes cannot encode an empty research result"
            )


class DisabledWebSearchAdapter:
    def search(
        self,
        query: str,
        *,
        limit: int,
        deadline_at: float | None,
        cancellation_probe: CancellationProbe | None,
    ) -> Sequence[WebSearchHit]:
        del query, limit, deadline_at, cancellation_probe
        raise WebResearchError(WebResearchErrorCode.SEARCH_NOT_CONFIGURED)


class TavilyKeylessWebSearchAdapter:
    """Tavily Keyless Adapter over the same pinned HTTP Seam as fetch."""

    def __init__(
        self,
        client: WebFetchClient,
        config: WebResearchRuntimeConfig,
    ) -> None:
        self._client = client
        self._config = config

    def search(
        self,
        query: str,
        *,
        limit: int,
        deadline_at: float | None,
        cancellation_probe: CancellationProbe | None,
    ) -> Sequence[WebSearchHit]:
        query = _bounded_input(
            query,
            field="query",
            max_bytes=self._config.limits.max_query_bytes,
        )
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            raise ValueError("limit must be a positive integer")
        limit = min(
            limit,
            self._config.limits.max_evidence_items,
            self._config.limits.max_citations,
        )
        body = json.dumps(
            {
                "query": query,
                "search_depth": "basic",
                "max_results": limit,
            },
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        fetched = self._client.post(
            self._config.tavily_endpoint,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "X-Tavily-Access-Mode": "keyless",
                "User-Agent": self._config.user_agent,
            },
            body=body,
            allowed_content_types=_JSON_CONTENT_TYPES,
            max_compressed_bytes=self._config.max_compressed_bytes,
            max_response_bytes=self._config.limits.max_response_bytes,
            max_redirects=0,
            timeout_seconds=self._config.request_timeout_seconds,
            deadline_at=deadline_at,
            cancellation_probe=cancellation_probe,
        )
        try:
            payload = json.loads(fetched.body)
            raw_results = payload.get("results") if isinstance(payload, dict) else None
            if not isinstance(raw_results, list):
                raise TypeError
        except (UnicodeError, ValueError, TypeError, json.JSONDecodeError):
            raise WebResearchError(
                WebResearchErrorCode.INVALID_SEARCH_RESPONSE,
                retryable=True,
            ) from None

        results: list[WebSearchHit] = []
        for raw in raw_results:
            if len(results) >= limit:
                break
            if not isinstance(raw, dict):
                continue
            url_value = raw.get("url")
            if not isinstance(url_value, str) or not url_value.strip():
                continue
            title = raw.get("title") if isinstance(raw.get("title"), str) else ""
            snippet = (
                raw.get("content")
                if isinstance(raw.get("content"), str)
                else ""
            )
            try:
                results.append(
                    WebSearchHit(
                        url=url_value,
                        title=title,
                        snippet=snippet,
                    )
                )
            except (TypeError, ValueError):
                continue
        return tuple(results)


class WebResearchRuntime:
    """Deep Module owning search projection, safe fetch, and evidence bounds."""

    def __init__(
        self,
        *,
        url_policy: WebUrlPolicy,
        config: WebResearchRuntimeConfig | None = None,
        http_client: WebFetchClient | None = None,
        search_adapter: WebSearchAdapter | None = None,
        clock: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if not isinstance(url_policy, WebUrlPolicy):
            raise TypeError("url_policy must be WebUrlPolicy")
        if config is not None and not isinstance(config, WebResearchRuntimeConfig):
            raise TypeError("config must be WebResearchRuntimeConfig")
        if clock is not None and not callable(clock):
            raise TypeError("clock must be callable")
        if not callable(monotonic):
            raise TypeError("monotonic must be callable")
        self.config = config or WebResearchRuntimeConfig()
        self.url_policy = url_policy
        self.http_client = http_client or SafeWebHttpClient(
            url_policy,
            monotonic=monotonic,
        )
        if search_adapter is not None:
            self.search_adapter = search_adapter
        elif self.config.enabled:
            self.search_adapter = TavilyKeylessWebSearchAdapter(
                self.http_client,
                self.config,
            )
        else:
            self.search_adapter = DisabledWebSearchAdapter()
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._monotonic = monotonic
        self._lifecycle_lock = threading.RLock()
        self._slots = threading.BoundedSemaphore(self.config.max_concurrency)
        self._started = False
        self._closed = False

    def start(self) -> None:
        with self._lifecycle_lock:
            if self._closed:
                raise WebResearchError(WebResearchErrorCode.CLOSED)
            if self._started:
                return
            if not self.config.enabled:
                raise WebResearchError(WebResearchErrorCode.DISABLED)
            if isinstance(self.search_adapter, DisabledWebSearchAdapter):
                raise WebResearchError(WebResearchErrorCode.SEARCH_NOT_CONFIGURED)
            self._started = True

    def close(self) -> None:
        with self._lifecycle_lock:
            if self._closed:
                return
            self._started = False
            self._closed = True
        self.url_policy.close()

    def readiness(self) -> dict[str, bool]:
        with self._lifecycle_lock:
            started = self._started
            closed = self._closed
        search_ready = not isinstance(
            self.search_adapter,
            DisabledWebSearchAdapter,
        )
        return {
            "enabled": self.config.enabled,
            "started": started,
            "closed": closed,
            "ready": self.config.enabled and started and not closed and search_ready,
            "search_ready": search_ready,
        }

    def search(
        self,
        query: str,
        *,
        limit: int | None = None,
        deadline_at: float | None = None,
        cancellation_probe: CancellationProbe | None = None,
    ) -> WebResearchResult:
        deadline_at = self._stage_deadline(deadline_at)
        with self._permit(
            deadline_at=deadline_at,
            cancellation_probe=cancellation_probe,
        ):
            return self._search(
                query,
                limit=limit,
                deadline_at=deadline_at,
                cancellation_probe=cancellation_probe,
            )

    def _search(
        self,
        query: str,
        *,
        limit: int | None,
        deadline_at: float,
        cancellation_probe: CancellationProbe | None,
    ) -> WebResearchResult:
        self._guard(
            deadline_at=deadline_at,
            cancellation_probe=cancellation_probe,
        )
        normalized_query = _bounded_input(
            query,
            field="query",
            max_bytes=self.config.limits.max_query_bytes,
        )
        result_limit = self._result_limit(limit)
        try:
            raw_hits = self.search_adapter.search(
                normalized_query,
                limit=result_limit,
                deadline_at=deadline_at,
                cancellation_probe=cancellation_probe,
            )
            if isinstance(raw_hits, (str, bytes)):
                raise TypeError("search adapter returned an invalid sequence")
            hits = tuple(islice(iter(raw_hits), result_limit + 1))
        except asyncio.CancelledError:
            raise
        except WebUrlPolicyError as exc:
            if exc.code is WebUrlPolicyCode.DNS_RESOLUTION_FAILED:
                raise WebResearchError(
                    WebResearchErrorCode.SEARCH_UNAVAILABLE,
                    retryable=True,
                ) from None
            raise
        except (WebHttpError, WebResearchError):
            raise
        except Exception:
            raise WebResearchError(
                WebResearchErrorCode.SEARCH_UNAVAILABLE,
                retryable=True,
            ) from None

        self._guard(
            deadline_at=deadline_at,
            cancellation_probe=cancellation_probe,
        )
        retrieved_at = self._now()
        evidence: list[WebEvidence] = []
        evidence_ids: set[str] = set()
        canonical_urls: set[str] = set()
        truncated = len(hits) > result_limit
        for hit in hits[:result_limit]:
            self._guard(
                deadline_at=deadline_at,
                cancellation_probe=cancellation_probe,
            )
            if not isinstance(hit, WebSearchHit):
                truncated = True
                continue
            try:
                resolved = self.url_policy.resolve(
                    hit.url,
                    deadline_at=deadline_at,
                    cancellation_probe=cancellation_probe,
                )
            except WebUrlPolicyError as exc:
                if exc.code is WebUrlPolicyCode.DNS_RESOLUTION_FAILED:
                    raise WebResearchError(
                        WebResearchErrorCode.SEARCH_UNAVAILABLE,
                        retryable=True,
                    ) from None
                # Search providers are untrusted.  A denied result is omitted,
                # never fetched and never allowed to fail open.
                truncated = True
                continue
            if resolved.canonical_url in canonical_urls:
                truncated = True
                continue
            try:
                title = _truncate_utf8(
                    _normalize_inline(hit.title),
                    self.config.limits.max_title_bytes,
                )
                snippet = _truncate_utf8(
                    _normalize_inline(hit.snippet),
                    self.config.limits.max_snippet_bytes,
                )
                content = _truncate_utf8(
                    snippet or title,
                    self.config.limits.max_content_bytes,
                )
            except UnicodeError:
                truncated = True
                continue
            if not content:
                truncated = True
                continue
            try:
                item = _create_single_result(
                    canonical_url=resolved.canonical_url,
                    title=title,
                    snippet=snippet,
                    content=content,
                    retrieved_at=retrieved_at,
                    limits=self.config.limits,
                ).evidence[0]
            except WebResearchError:
                truncated = True
                continue
            if item.evidence_id in evidence_ids:
                truncated = True
                continue
            try:
                WebResearchResult.create(
                    [*evidence, item],
                    truncated=truncated,
                    limits=self.config.limits,
                )
            except WebResearchContractError as exc:
                if exc.code is not WebResearchContractCode.OUTPUT_TOO_LARGE:
                    raise
                truncated = True
                break
            evidence.append(item)
            evidence_ids.add(item.evidence_id)
            canonical_urls.add(item.canonical_url)
        self._guard(
            deadline_at=deadline_at,
            cancellation_probe=cancellation_probe,
        )
        return WebResearchResult.create(
            evidence,
            truncated=truncated,
            limits=self.config.limits,
        )

    def fetch(
        self,
        url: str,
        *,
        deadline_at: float | None = None,
        cancellation_probe: CancellationProbe | None = None,
    ) -> WebResearchResult:
        deadline_at = self._stage_deadline(deadline_at)
        with self._permit(
            deadline_at=deadline_at,
            cancellation_probe=cancellation_probe,
        ):
            return self._fetch(
                url,
                deadline_at=deadline_at,
                cancellation_probe=cancellation_probe,
            )

    def _fetch(
        self,
        url: str,
        *,
        deadline_at: float,
        cancellation_probe: CancellationProbe | None,
    ) -> WebResearchResult:
        self._guard(
            deadline_at=deadline_at,
            cancellation_probe=cancellation_probe,
        )
        try:
            fetched = self.http_client.get(
                url,
                headers={
                    "Accept": "text/html, application/xhtml+xml, text/plain;q=0.8",
                    "User-Agent": self.config.user_agent,
                },
                allowed_content_types=_FETCH_CONTENT_TYPES,
                max_compressed_bytes=self.config.max_compressed_bytes,
                max_response_bytes=self.config.limits.max_response_bytes,
                max_redirects=self.config.limits.max_redirects,
                timeout_seconds=self.config.request_timeout_seconds,
                deadline_at=deadline_at,
                cancellation_probe=cancellation_probe,
            )
        except WebUrlPolicyError as exc:
            if exc.code is WebUrlPolicyCode.DNS_RESOLUTION_FAILED:
                raise WebResearchError(
                    WebResearchErrorCode.FETCH_UNAVAILABLE,
                    retryable=True,
                ) from None
            raise
        self._guard(
            deadline_at=deadline_at,
            cancellation_probe=cancellation_probe,
        )
        title, content = _extract_content(fetched)
        self._guard(
            deadline_at=deadline_at,
            cancellation_probe=cancellation_probe,
        )
        content_limit = min(
            self.config.limits.max_content_bytes,
            self.config.limits.max_total_evidence_bytes,
        )
        content = _truncate_utf8(content, content_limit)
        if not content:
            raise WebResearchError(WebResearchErrorCode.INVALID_CONTENT)
        title = _truncate_utf8(title, self.config.limits.max_title_bytes)
        snippet = _truncate_utf8(
            _normalize_inline(content),
            self.config.limits.max_snippet_bytes,
        )
        return _create_single_result(
            canonical_url=fetched.resolved.canonical_url,
            title=title,
            snippet=snippet,
            content=content,
            retrieved_at=self._now(),
            limits=self.config.limits,
        )

    def _guard(
        self,
        *,
        deadline_at: float | None,
        cancellation_probe: CancellationProbe | None,
    ) -> None:
        with self._lifecycle_lock:
            closed = self._closed
            started = self._started
        if closed:
            raise WebResearchError(WebResearchErrorCode.CLOSED)
        if not self.config.enabled:
            raise WebResearchError(WebResearchErrorCode.DISABLED)
        if not started:
            raise WebResearchError(WebResearchErrorCode.NOT_STARTED)
        _raise_if_cancelled(cancellation_probe)
        if deadline_at is not None and self._monotonic() >= deadline_at:
            raise WebResearchError(
                WebResearchErrorCode.DEADLINE_EXCEEDED,
                retryable=True,
            )

    @contextmanager
    def _permit(
        self,
        *,
        deadline_at: float,
        cancellation_probe: CancellationProbe | None,
    ):
        acquired = False
        while not acquired:
            self._guard(
                deadline_at=deadline_at,
                cancellation_probe=cancellation_probe,
            )
            remaining = max(deadline_at - self._monotonic(), 0.0)
            acquired = self._slots.acquire(timeout=min(remaining, 0.05))
        try:
            yield
        finally:
            self._slots.release()

    def _result_limit(self, value: int | None) -> int:
        maximum = min(
            self.config.limits.max_evidence_items,
            self.config.limits.max_citations,
        )
        if value is None:
            return min(self.config.default_search_results, maximum)
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError("limit must be a positive integer")
        return min(value, maximum)

    def _stage_deadline(self, deadline_at: float | None) -> float:
        local_deadline = self._monotonic() + self.config.request_timeout_seconds
        if deadline_at is None:
            return local_deadline
        if (
            isinstance(deadline_at, bool)
            or not isinstance(deadline_at, (int, float))
            or not math.isfinite(float(deadline_at))
        ):
            raise ValueError("deadline_at must be finite")
        return min(local_deadline, float(deadline_at))

    def _now(self) -> datetime:
        value = self._clock()
        if (
            not isinstance(value, datetime)
            or value.tzinfo is None
            or value.utcoffset() is None
        ):
            raise TypeError("clock must return a timezone-aware datetime")
        return value.astimezone(timezone.utc)


def build_web_research_runtime(
    settings: WebResearchSettingsLike | AppWebResearchSettingsLike,
) -> WebResearchRuntime:
    """Compose the deep runtime from validated application settings.

    The function accepts either the Web Research settings object itself or an
    application settings object exposing ``.web_research``.  This keeps all
    field mapping local without making the Module import the settings layer.
    """

    source = getattr(settings, "web_research", settings)
    limits = WebResearchLimits(
        max_query_bytes=getattr(source, "max_query_bytes"),
        max_url_bytes=getattr(source, "max_url_bytes"),
        max_title_bytes=getattr(source, "max_title_bytes"),
        max_snippet_bytes=getattr(source, "max_snippet_bytes"),
        max_content_bytes=getattr(source, "max_content_bytes"),
        max_total_evidence_bytes=getattr(source, "max_total_evidence_bytes"),
        max_response_bytes=getattr(source, "max_response_bytes"),
        max_redirects=getattr(source, "max_redirects"),
        max_evidence_items=getattr(source, "max_search_results"),
        max_citations=getattr(source, "max_citations"),
    )
    resolver = SystemWebDnsResolver(
        timeout_seconds=getattr(source, "dns_timeout_seconds"),
        max_concurrency=getattr(source, "dns_max_concurrency"),
    )
    try:
        policy = WebUrlPolicy(
            resolver,
            allowed_scheme_ports={
                "http": frozenset({80}),
                "https": frozenset({443}),
            },
            max_url_bytes=limits.max_url_bytes,
            max_resolved_addresses=getattr(source, "max_dns_addresses"),
        )
        config = WebResearchRuntimeConfig(
            enabled=getattr(source, "enabled"),
            request_timeout_seconds=getattr(source, "request_timeout_seconds"),
            max_compressed_bytes=getattr(source, "max_compressed_bytes"),
            default_search_results=getattr(source, "default_search_results"),
            max_concurrency=getattr(source, "max_concurrency"),
            user_agent=getattr(source, "user_agent"),
            limits=limits,
        )
        return WebResearchRuntime(url_policy=policy, config=config)
    except BaseException:
        resolver.close()
        raise
_runtime_lock = threading.RLock()
_installed_runtime: WebResearchRuntime | None = None


def install_web_research_runtime(runtime: WebResearchRuntime) -> None:
    """Install the process runtime from the application composition root."""

    if not isinstance(runtime, WebResearchRuntime):
        raise TypeError("runtime must be WebResearchRuntime")
    global _installed_runtime
    with _runtime_lock:
        _installed_runtime = runtime


def clear_web_research_runtime(runtime: WebResearchRuntime | None = None) -> None:
    """Clear one installed runtime without closing a replacement instance."""

    global _installed_runtime
    with _runtime_lock:
        if runtime is None or _installed_runtime is runtime:
            _installed_runtime = None


def get_web_research_runtime() -> WebResearchRuntime:
    """Resolve the request-independent runtime used by Tool Adapters."""

    with _runtime_lock:
        runtime = _installed_runtime
    if runtime is None:
        raise WebResearchError(WebResearchErrorCode.RUNTIME_NOT_CONFIGURED)
    return runtime


def _raise_if_cancelled(cancellation_probe: CancellationProbe | None) -> None:
    if cancellation_probe is None:
        return
    try:
        cancelled = bool(cancellation_probe())
    except asyncio.CancelledError:
        raise
    except Exception:
        raise asyncio.CancelledError("web cancellation probe failed") from None
    if cancelled:
        raise asyncio.CancelledError("web research cancelled")


def _bounded_input(value: str, *, field: str, max_bytes: int) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field} must be a string")
    normalized = value.strip()
    if not normalized or "\x00" in normalized:
        raise ValueError(f"{field} must be a non-empty safe string")
    try:
        encoded_size = len(normalized.encode("utf-8"))
    except UnicodeEncodeError:
        raise ValueError(f"{field} contains invalid Unicode") from None
    if encoded_size > max_bytes:
        raise ValueError(f"{field} exceeds its byte limit")
    return normalized


def _extract_content(fetched: WebHttpFetch) -> tuple[str, str]:
    content_type = fetched.content_type
    if content_type in _HTML_CONTENT_TYPES:
        soup = BeautifulSoup(fetched.body, "html.parser")
        title = (
            _normalize_inline(soup.title.get_text(" ", strip=True))
            if soup.title
            else ""
        )
        for node in soup.find_all(
            ["script", "style", "noscript", "template", "svg", "head"]
        ):
            node.decompose()
        for node in tuple(soup.find_all(True)):
            if node.parent is None:
                continue
            aria_hidden = str(node.attrs.get("aria-hidden", "")).casefold()
            style = re.sub(r"\s+", "", str(node.attrs.get("style", "")).casefold())
            if (
                node.has_attr("hidden")
                or node.has_attr("inert")
                or aria_hidden == "true"
                or "display:none" in style
                or "visibility:hidden" in style
            ):
                node.decompose()
        for comment in soup.find_all(string=lambda value: isinstance(value, Comment)):
            comment.extract()
        content = _normalize_document(soup.get_text("\n"))
        return title, content
    if content_type == "text/plain":
        text = _decode_plain_text(fetched.body, fetched.headers.get("content-type", ""))
        return "", _normalize_document(text)
    raise WebResearchError(WebResearchErrorCode.INVALID_CONTENT)


def _decode_plain_text(body: bytes, content_type: str) -> str:
    match = _CHARSET.search(content_type)
    requested = match.group(1).strip().casefold() if match else "utf-8"
    encoding = _SAFE_TEXT_ENCODINGS.get(requested)
    if encoding is None:
        raise WebResearchError(WebResearchErrorCode.INVALID_CONTENT)
    try:
        return body.decode(encoding, errors="replace")
    except (LookupError, UnicodeError):
        raise WebResearchError(WebResearchErrorCode.INVALID_CONTENT) from None


def _normalize_inline(value: str) -> str:
    return " ".join(value.split())


def _normalize_document(value: str) -> str:
    lines: list[str] = []
    for raw_line in value.splitlines():
        line = _WHITESPACE.sub(" ", raw_line).strip()
        if line:
            lines.append(line)
    return "\n".join(lines)


def _truncate_utf8(value: str, max_bytes: int) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value
    return encoded[:max_bytes].decode("utf-8", errors="ignore").rstrip()


def _create_single_result(
    *,
    canonical_url: str,
    title: str,
    snippet: str,
    content: str,
    retrieved_at: datetime,
    limits: WebResearchLimits,
) -> WebResearchResult:
    """Fit one evidence plus citation and wrapper to the exact output budget."""

    candidate_title = title
    candidate_snippet = snippet
    candidate_content = content
    while candidate_content:
        evidence = WebEvidence.create(
            canonical_url=canonical_url,
            title=candidate_title,
            snippet=candidate_snippet,
            content=candidate_content,
            retrieved_at=retrieved_at,
            limits=limits,
        )
        raw_result = WebResearchResult(
            evidence=(evidence,),
            citations=(WebCitation.from_evidence(evidence),),
        )
        excess = raw_result.encoded_size - limits.max_total_evidence_bytes
        if excess <= 0:
            return WebResearchResult.create((evidence,), limits=limits)
        snippet_bytes = len(candidate_snippet.encode("utf-8"))
        if snippet_bytes:
            candidate_snippet = _truncate_utf8(
                candidate_snippet,
                max(snippet_bytes - excess, 0),
            )
            continue
        title_bytes = len(candidate_title.encode("utf-8"))
        if title_bytes:
            candidate_title = _truncate_utf8(
                candidate_title,
                max(title_bytes - excess, 0),
            )
            continue
        content_bytes = len(candidate_content.encode("utf-8"))
        candidate_content = _truncate_utf8(
            candidate_content,
            max(content_bytes - excess, 0),
        )
    raise WebResearchError(WebResearchErrorCode.INVALID_CONTENT)


__all__ = [
    "AppWebResearchSettingsLike",
    "TavilyKeylessWebSearchAdapter",
    "build_web_research_runtime",
    "clear_web_research_runtime",
    "DisabledWebSearchAdapter",
    "get_web_research_runtime",
    "install_web_research_runtime",
    "WebResearchError",
    "WebResearchErrorCode",
    "WebResearchRuntime",
    "WebResearchRuntimeConfig",
    "WebResearchSettingsLike",
    "WebSearchAdapter",
    "WebSearchHit",
]
