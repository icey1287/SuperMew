"""Web Research runtime backed only by Tavily Search and Extract APIs."""

from __future__ import annotations

import asyncio
import math
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from itertools import islice
from typing import Protocol

import httpx

from backend.web_research.contracts import (
    DEFAULT_WEB_RESEARCH_LIMITS,
    WebEvidence,
    WebResearchContractError,
    WebResearchLimits,
    WebResearchResult,
)


CancellationProbe = Callable[[], bool]

_TAVILY_SEARCH_ENDPOINT = "https://api.tavily.com/search"
_TAVILY_EXTRACT_ENDPOINT = "https://api.tavily.com/extract"
_EXTRACT_CHUNKS_PER_SOURCE = 5
_EXTRACT_CHUNK_CHARACTERS = 500


class WebResearchErrorCode(StrEnum):
    DISABLED = "WEB_RESEARCH_DISABLED"
    SEARCH_UNAVAILABLE = "WEB_SEARCH_UNAVAILABLE"
    INVALID_SEARCH_RESPONSE = "WEB_INVALID_SEARCH_RESPONSE"
    FETCH_UNAVAILABLE = "WEB_FETCH_UNAVAILABLE"
    INVALID_EXTRACT_RESPONSE = "WEB_INVALID_EXTRACT_RESPONSE"
    INVALID_CONTENT = "WEB_INVALID_CONTENT"
    DEADLINE_EXCEEDED = "WEB_DEADLINE_EXCEEDED"
    CLOSED = "WEB_RESEARCH_CLOSED"
    NOT_STARTED = "WEB_RESEARCH_NOT_STARTED"
    RUNTIME_NOT_CONFIGURED = "WEB_RESEARCH_RUNTIME_NOT_CONFIGURED"


class WebResearchError(RuntimeError):
    """Stable provider failure without query, URL, response, or secret details."""

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
    content: str = field(default="", repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.url, str) or not self.url.strip():
            raise ValueError("search hit URL must be a non-empty string")
        if not isinstance(self.title, str) or not isinstance(self.content, str):
            raise TypeError("search hit title and content must be strings")
        object.__setattr__(self, "url", self.url.strip())
        object.__setattr__(self, "title", self.title.strip())
        object.__setattr__(self, "content", self.content.strip())


@dataclass(frozen=True, slots=True)
class WebExtractResult:
    url: str = field(repr=False)
    chunks: tuple[str, ...] = field(repr=False)
    title: str = field(default="", repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.url, str) or not self.url.strip():
            raise ValueError("extract URL must be a non-empty string")
        chunks = tuple(self.chunks)
        if any(not isinstance(chunk, str) for chunk in chunks):
            raise TypeError("extract chunks must contain strings")
        if not isinstance(self.title, str):
            raise TypeError("extract title must be a string")
        object.__setattr__(self, "url", self.url.strip())
        object.__setattr__(self, "chunks", chunks)
        object.__setattr__(self, "title", self.title.strip())


class WebResearchProvider(Protocol):
    def search(
        self,
        query: str,
        *,
        limit: int,
        allowed_domains: tuple[str, ...],
        timeout_seconds: float,
        deadline_at: float | None,
        cancellation_probe: CancellationProbe | None,
    ) -> Sequence[WebSearchHit]: ...

    def extract(
        self,
        url: str,
        *,
        query: str,
        chunks_per_source: int,
        timeout_seconds: float,
        deadline_at: float | None,
        cancellation_probe: CancellationProbe | None,
    ) -> WebExtractResult: ...


class WebResearchSettingsLike(Protocol):
    enabled: bool
    request_timeout_seconds: float
    max_query_bytes: int
    max_url_bytes: int
    max_title_bytes: int
    max_content_bytes: int
    max_response_bytes: int
    default_search_results: int
    max_search_results: int
    max_concurrency: int
    user_agent: str


class AppWebResearchSettingsLike(Protocol):
    web_research: WebResearchSettingsLike


@dataclass(frozen=True, slots=True)
class WebResearchRuntimeConfig:
    enabled: bool = True
    request_timeout_seconds: float = 10.0
    default_search_results: int = 5
    max_concurrency: int = 4
    user_agent: str = "SuperMew-WebResearch/2.0"
    limits: WebResearchLimits = DEFAULT_WEB_RESEARCH_LIMITS

    def __post_init__(self) -> None:
        if not isinstance(self.enabled, bool):
            raise TypeError("enabled must be a bool")
        if (
            isinstance(self.request_timeout_seconds, bool)
            or not isinstance(self.request_timeout_seconds, (int, float))
            or not math.isfinite(float(self.request_timeout_seconds))
            or self.request_timeout_seconds <= 0
        ):
            raise ValueError("request_timeout_seconds must be positive and finite")
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
        user_agent = self.user_agent.strip()
        if not user_agent or any(marker in user_agent for marker in ("\r", "\n", "\x00")):
            raise ValueError("user_agent must be a safe non-empty value")
        object.__setattr__(self, "user_agent", user_agent)
        if not isinstance(self.limits, WebResearchLimits):
            raise TypeError("limits must be WebResearchLimits")
        if self.default_search_results > self.limits.max_evidence_items:
            raise ValueError("default_search_results exceeds the result limit")


class TavilyKeylessProvider:
    """Fixed-origin Tavily provider for search and query-ranked extraction."""

    def __init__(
        self,
        *,
        user_agent: str,
        max_response_bytes: int,
        client: httpx.Client | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._user_agent = user_agent
        self._max_response_bytes = max_response_bytes
        self._client = client or httpx.Client(
            follow_redirects=False,
            trust_env=False,
        )
        self._owns_client = client is None
        self._monotonic = monotonic

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def search(
        self,
        query: str,
        *,
        limit: int,
        allowed_domains: tuple[str, ...],
        timeout_seconds: float,
        deadline_at: float | None,
        cancellation_probe: CancellationProbe | None,
    ) -> Sequence[WebSearchHit]:
        request: dict[str, object] = {
            "query": query,
            "search_depth": "basic",
            "max_results": limit,
        }
        if allowed_domains:
            request["include_domains"] = list(allowed_domains)
        payload = self._post(
            _TAVILY_SEARCH_ENDPOINT,
            request,
            timeout_seconds=timeout_seconds,
            deadline_at=deadline_at,
            cancellation_probe=cancellation_probe,
            unavailable_code=WebResearchErrorCode.SEARCH_UNAVAILABLE,
            invalid_code=WebResearchErrorCode.INVALID_SEARCH_RESPONSE,
        )
        raw_results = payload.get("results") if isinstance(payload, dict) else None
        if not isinstance(raw_results, list):
            raise WebResearchError(
                WebResearchErrorCode.INVALID_SEARCH_RESPONSE,
                retryable=True,
            )

        results: list[WebSearchHit] = []
        for raw in raw_results:
            if len(results) >= limit:
                break
            if not isinstance(raw, dict):
                continue
            url = raw.get("url")
            if not isinstance(url, str) or not url.strip():
                continue
            title = raw.get("title") if isinstance(raw.get("title"), str) else ""
            content = (
                raw.get("content") if isinstance(raw.get("content"), str) else ""
            )
            try:
                results.append(WebSearchHit(url=url, title=title, content=content))
            except (TypeError, ValueError):
                continue
        return tuple(results)

    def extract(
        self,
        url: str,
        *,
        query: str,
        chunks_per_source: int,
        timeout_seconds: float,
        deadline_at: float | None,
        cancellation_probe: CancellationProbe | None,
    ) -> WebExtractResult:
        payload = self._post(
            _TAVILY_EXTRACT_ENDPOINT,
            {
                "urls": url,
                "query": query,
                "chunks_per_source": chunks_per_source,
                "extract_depth": "basic",
            },
            timeout_seconds=timeout_seconds,
            deadline_at=deadline_at,
            cancellation_probe=cancellation_probe,
            unavailable_code=WebResearchErrorCode.FETCH_UNAVAILABLE,
            invalid_code=WebResearchErrorCode.INVALID_EXTRACT_RESPONSE,
        )
        raw_results = payload.get("results") if isinstance(payload, dict) else None
        if not isinstance(raw_results, list) or not raw_results:
            raise WebResearchError(
                WebResearchErrorCode.INVALID_EXTRACT_RESPONSE,
                retryable=True,
            )
        first = raw_results[0]
        if not isinstance(first, dict):
            raise WebResearchError(
                WebResearchErrorCode.INVALID_EXTRACT_RESPONSE,
                retryable=True,
            )
        raw_content = first.get("raw_content")
        if isinstance(raw_content, str):
            chunks = (raw_content,)
        elif isinstance(raw_content, list) and all(
            isinstance(chunk, str) for chunk in raw_content
        ):
            chunks = tuple(raw_content)
        else:
            raise WebResearchError(
                WebResearchErrorCode.INVALID_EXTRACT_RESPONSE,
                retryable=True,
            )
        result_url = first.get("url") if isinstance(first.get("url"), str) else url
        title = first.get("title") if isinstance(first.get("title"), str) else ""
        try:
            return WebExtractResult(url=result_url, chunks=chunks, title=title)
        except (TypeError, ValueError) as exc:
            raise WebResearchError(
                WebResearchErrorCode.INVALID_EXTRACT_RESPONSE,
                retryable=True,
            ) from exc

    def _post(
        self,
        endpoint: str,
        payload: Mapping[str, object],
        *,
        timeout_seconds: float,
        deadline_at: float | None,
        cancellation_probe: CancellationProbe | None,
        unavailable_code: WebResearchErrorCode,
        invalid_code: WebResearchErrorCode,
    ) -> object:
        _raise_if_cancelled(cancellation_probe)
        effective_timeout = timeout_seconds
        if deadline_at is not None:
            effective_timeout = min(
                effective_timeout,
                max(deadline_at - self._monotonic(), 0.0),
            )
        if effective_timeout <= 0:
            raise WebResearchError(
                WebResearchErrorCode.DEADLINE_EXCEEDED,
                retryable=True,
            )
        try:
            response = self._client.post(
                endpoint,
                json=dict(payload),
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    "X-Tavily-Access-Mode": "keyless",
                    "User-Agent": self._user_agent,
                },
                timeout=effective_timeout,
            )
        except httpx.TimeoutException:
            raise WebResearchError(unavailable_code, retryable=True) from None
        except httpx.HTTPError:
            raise WebResearchError(unavailable_code, retryable=True) from None
        _raise_if_cancelled(cancellation_probe)
        if not 200 <= response.status_code < 300:
            raise WebResearchError(
                unavailable_code,
                retryable=response.status_code == 429 or response.status_code >= 500,
            )
        if len(response.content) > self._max_response_bytes:
            raise WebResearchError(invalid_code, retryable=False)
        try:
            return response.json()
        except (UnicodeError, ValueError):
            raise WebResearchError(invalid_code, retryable=True) from None


class WebResearchRuntime:
    """Run-independent runtime with request-local state kept outside the module."""

    def __init__(
        self,
        *,
        config: WebResearchRuntimeConfig | None = None,
        provider: WebResearchProvider | None = None,
        clock: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if config is not None and not isinstance(config, WebResearchRuntimeConfig):
            raise TypeError("config must be WebResearchRuntimeConfig")
        if clock is not None and not callable(clock):
            raise TypeError("clock must be callable")
        if not callable(monotonic):
            raise TypeError("monotonic must be callable")
        self.config = config or WebResearchRuntimeConfig()
        self.provider = provider or TavilyKeylessProvider(
            user_agent=self.config.user_agent,
            max_response_bytes=self.config.limits.max_response_bytes,
            monotonic=monotonic,
        )
        self._owns_provider = provider is None
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
            self._started = True

    def close(self) -> None:
        with self._lifecycle_lock:
            if self._closed:
                return
            self._started = False
            self._closed = True
        if self._owns_provider and isinstance(self.provider, TavilyKeylessProvider):
            self.provider.close()

    def readiness(self) -> dict[str, bool]:
        with self._lifecycle_lock:
            started = self._started
            closed = self._closed
        return {
            "enabled": self.config.enabled,
            "started": started,
            "closed": closed,
            "ready": self.config.enabled and started and not closed,
            "search_ready": self.config.enabled and not closed,
            "extract_ready": self.config.enabled and not closed,
        }

    def search(
        self,
        query: str,
        *,
        limit: int | None = None,
        allowed_domains: tuple[str, ...] = (),
        deadline_at: float | None = None,
        cancellation_probe: CancellationProbe | None = None,
    ) -> WebResearchResult:
        deadline = self._stage_deadline(deadline_at)
        with self._permit(deadline_at=deadline, cancellation_probe=cancellation_probe):
            return self._search(
                query,
                limit=limit,
                allowed_domains=allowed_domains,
                deadline_at=deadline,
                cancellation_probe=cancellation_probe,
            )

    def _search(
        self,
        query: str,
        *,
        limit: int | None,
        allowed_domains: tuple[str, ...],
        deadline_at: float,
        cancellation_probe: CancellationProbe | None,
    ) -> WebResearchResult:
        self._guard(deadline_at=deadline_at, cancellation_probe=cancellation_probe)
        normalized_query = _bounded_input(
            query,
            field_name="query",
            max_bytes=self.config.limits.max_query_bytes,
        )
        normalized_domains = tuple(
            sorted(
                {
                    domain.strip().casefold()
                    for domain in allowed_domains
                    if isinstance(domain, str) and domain.strip()
                }
            )
        )
        result_limit = self._result_limit(limit)
        try:
            raw_hits = self.provider.search(
                normalized_query,
                limit=result_limit,
                allowed_domains=normalized_domains,
                timeout_seconds=self.config.request_timeout_seconds,
                deadline_at=deadline_at,
                cancellation_probe=cancellation_probe,
            )
            if isinstance(raw_hits, (str, bytes)):
                raise TypeError
            hits = tuple(islice(iter(raw_hits), result_limit + 1))
        except asyncio.CancelledError:
            raise
        except WebResearchError:
            raise
        except Exception:
            raise WebResearchError(
                WebResearchErrorCode.SEARCH_UNAVAILABLE,
                retryable=True,
            ) from None

        evidence: list[WebEvidence] = []
        urls: set[str] = set()
        truncated = len(hits) > result_limit
        retrieved_at = self._now()
        for hit in hits[:result_limit]:
            self._guard(deadline_at=deadline_at, cancellation_probe=cancellation_probe)
            if not isinstance(hit, WebSearchHit) or hit.url in urls:
                truncated = True
                continue
            title = _truncate_utf8(
                _normalize_inline(hit.title),
                self.config.limits.max_title_bytes,
            )
            content = _truncate_utf8(
                _normalize_inline(hit.content) or title,
                self.config.limits.max_content_bytes,
            )
            if not content:
                truncated = True
                continue
            try:
                item = WebEvidence.create(
                    url=hit.url,
                    title=title,
                    content=content,
                    retrieved_at=retrieved_at,
                    limits=self.config.limits,
                )
            except WebResearchContractError:
                truncated = True
                continue
            evidence.append(item)
            urls.add(item.url)
        return WebResearchResult.create(
            evidence,
            truncated=truncated,
            limits=self.config.limits,
        )

    def fetch(
        self,
        url: str,
        *,
        query: str,
        deadline_at: float | None = None,
        cancellation_probe: CancellationProbe | None = None,
    ) -> WebResearchResult:
        deadline = self._stage_deadline(deadline_at)
        with self._permit(deadline_at=deadline, cancellation_probe=cancellation_probe):
            return self._fetch(
                url,
                query=query,
                deadline_at=deadline,
                cancellation_probe=cancellation_probe,
            )

    def _fetch(
        self,
        url: str,
        *,
        query: str,
        deadline_at: float,
        cancellation_probe: CancellationProbe | None,
    ) -> WebResearchResult:
        self._guard(deadline_at=deadline_at, cancellation_probe=cancellation_probe)
        normalized_url = _bounded_input(
            url,
            field_name="url",
            max_bytes=self.config.limits.max_url_bytes,
        )
        normalized_query = _bounded_input(
            query,
            field_name="query",
            max_bytes=self.config.limits.max_query_bytes,
        )
        try:
            extracted = self.provider.extract(
                normalized_url,
                query=normalized_query,
                chunks_per_source=_EXTRACT_CHUNKS_PER_SOURCE,
                timeout_seconds=self.config.request_timeout_seconds,
                deadline_at=deadline_at,
                cancellation_probe=cancellation_probe,
            )
        except asyncio.CancelledError:
            raise
        except WebResearchError:
            raise
        except Exception:
            raise WebResearchError(
                WebResearchErrorCode.FETCH_UNAVAILABLE,
                retryable=True,
            ) from None
        if not isinstance(extracted, WebExtractResult):
            raise WebResearchError(
                WebResearchErrorCode.INVALID_EXTRACT_RESPONSE,
                retryable=True,
            )
        chunks = _bounded_extract_chunks(extracted.chunks)
        if not chunks:
            raise WebResearchError(WebResearchErrorCode.INVALID_CONTENT)
        content = _truncate_utf8(
            "\n\n".join(chunks),
            self.config.limits.max_content_bytes,
        )
        if not content:
            raise WebResearchError(WebResearchErrorCode.INVALID_CONTENT)
        title = _truncate_utf8(
            _normalize_inline(extracted.title),
            self.config.limits.max_title_bytes,
        )
        try:
            evidence = WebEvidence.create(
                url=normalized_url,
                title=title,
                content=content,
                retrieved_at=self._now(),
                limits=self.config.limits,
            )
        except WebResearchContractError as exc:
            raise WebResearchError(WebResearchErrorCode.INVALID_CONTENT) from exc
        return WebResearchResult.create((evidence,), limits=self.config.limits)

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
        maximum = self.config.limits.max_evidence_items
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
    source = getattr(settings, "web_research", settings)
    limits = WebResearchLimits(
        max_query_bytes=getattr(source, "max_query_bytes"),
        max_url_bytes=getattr(source, "max_url_bytes"),
        max_title_bytes=getattr(source, "max_title_bytes"),
        max_content_bytes=getattr(source, "max_content_bytes"),
        max_response_bytes=getattr(source, "max_response_bytes"),
        max_evidence_items=getattr(source, "max_search_results"),
    )
    config = WebResearchRuntimeConfig(
        enabled=getattr(source, "enabled"),
        request_timeout_seconds=getattr(source, "request_timeout_seconds"),
        default_search_results=getattr(source, "default_search_results"),
        max_concurrency=getattr(source, "max_concurrency"),
        user_agent=getattr(source, "user_agent"),
        limits=limits,
    )
    return WebResearchRuntime(config=config)


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


def _bounded_input(value: str, *, field_name: str, max_bytes: int) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    normalized = value.strip()
    if not normalized or "\x00" in normalized:
        raise ValueError(f"{field_name} must be a non-empty safe string")
    try:
        encoded_size = len(normalized.encode("utf-8"))
    except UnicodeEncodeError:
        raise ValueError(f"{field_name} contains invalid Unicode") from None
    if encoded_size > max_bytes:
        raise ValueError(f"{field_name} exceeds its byte limit")
    return normalized


def _normalize_inline(value: str) -> str:
    return " ".join(value.split())


def _truncate_utf8(value: str, max_bytes: int) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value
    return encoded[:max_bytes].decode("utf-8", errors="ignore").rstrip()


def _bounded_extract_chunks(chunks: Sequence[str]) -> tuple[str, ...]:
    bounded: list[str] = []
    seen: set[str] = set()
    for raw in chunks:
        if len(bounded) >= _EXTRACT_CHUNKS_PER_SOURCE:
            break
        normalized = _normalize_inline(raw)
        if not normalized:
            continue
        chunk = normalized[:_EXTRACT_CHUNK_CHARACTERS].rstrip()
        if not chunk or chunk in seen:
            continue
        bounded.append(chunk)
        seen.add(chunk)
    return tuple(bounded)


__all__ = [
    "AppWebResearchSettingsLike",
    "CancellationProbe",
    "TavilyKeylessProvider",
    "WebExtractResult",
    "WebResearchError",
    "WebResearchErrorCode",
    "WebResearchProvider",
    "WebResearchRuntime",
    "WebResearchRuntimeConfig",
    "WebResearchSettingsLike",
    "WebSearchHit",
    "build_web_research_runtime",
]
