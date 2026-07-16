"""Pinned, fail-closed HTTP Adapter for Web Research.

The module deliberately does not use environment proxy settings.  Every
request is resolved by :class:`WebUrlPolicy`, connected to a numeric address
from that immutable DNS snapshot, and verified again against the actual peer.
"""

from __future__ import annotations

import asyncio
import http.client
import ipaddress
import math
import socket
import ssl
import threading
import time
import zlib
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Protocol

from backend.web_research.url_policy import ResolvedWebUrl, WebUrlPolicy


CancellationProbe = Callable[[], bool]
_HARD_MAX_REDIRECTS = 10
_HARD_MAX_BODY_BYTES = 8 * 1024 * 1024
_WATCHDOG_POLL_SECONDS = 0.01
_WATCHDOG_JOIN_SECONDS = 0.2


class WebHttpErrorCode(StrEnum):
    CONNECTION_FAILED = "WEB_CONNECTION_FAILED"
    TLS_FAILED = "WEB_TLS_FAILED"
    REQUEST_TIMEOUT = "WEB_REQUEST_TIMEOUT"
    DEADLINE_EXCEEDED = "WEB_DEADLINE_EXCEEDED"
    INVALID_RESPONSE = "WEB_INVALID_RESPONSE"
    HTTP_STATUS = "WEB_HTTP_STATUS"
    CONTENT_TYPE_DENIED = "WEB_CONTENT_TYPE_DENIED"
    CONTENT_ENCODING_DENIED = "WEB_CONTENT_ENCODING_DENIED"
    RESPONSE_TOO_LARGE = "WEB_RESPONSE_TOO_LARGE"
    REDIRECT_DENIED = "WEB_REDIRECT_DENIED"
    REDIRECT_LIMIT_EXCEEDED = "WEB_REDIRECT_LIMIT_EXCEEDED"


class WebHttpError(RuntimeError):
    """Stable error without URL, response body, or transport exception text."""

    def __init__(
        self,
        code: WebHttpErrorCode | str,
        *,
        retryable: bool = False,
        safe_details: Mapping[str, str | int] | None = None,
    ) -> None:
        self.code = WebHttpErrorCode(code).value
        self.retryable = bool(retryable)
        self.safe_details = dict(safe_details or {})
        super().__init__(self.code)


@dataclass(frozen=True, slots=True)
class WebHttpResponse:
    status_code: int
    headers: Mapping[str, str] = field(repr=False)
    body: bytes = field(repr=False)
    peer_ip: str

    def __post_init__(self) -> None:
        if not 100 <= self.status_code <= 599:
            raise ValueError("status_code must be a valid HTTP status")
        normalized: dict[str, str] = {}
        for name, value in self.headers.items():
            key = str(name).strip().lower()
            if not key or key in normalized:
                raise ValueError("response headers must have unique names")
            normalized[key] = str(value).strip()
        object.__setattr__(self, "headers", MappingProxyType(normalized))
        object.__setattr__(self, "body", bytes(self.body))
        object.__setattr__(self, "peer_ip", str(self.peer_ip).strip())

    def header(self, name: str) -> str | None:
        return self.headers.get(name.casefold())


@dataclass(frozen=True, slots=True)
class WebHttpFetch:
    resolved: ResolvedWebUrl = field(repr=False)
    status_code: int
    headers: Mapping[str, str] = field(repr=False)
    body: bytes = field(repr=False)
    redirects: int

    @property
    def content_type(self) -> str:
        value = self.headers.get("content-type", "")
        return value.partition(";")[0].strip().casefold()


class WebHttpTransport(Protocol):
    """Adapter Seam that must connect only to ``resolved.pinned_addresses``."""

    def get(
        self,
        resolved: ResolvedWebUrl,
        *,
        headers: Mapping[str, str],
        allowed_content_types: frozenset[str],
        max_compressed_bytes: int,
        max_response_bytes: int,
        timeout_seconds: float,
        deadline_at: float | None,
        cancellation_probe: CancellationProbe | None,
    ) -> WebHttpResponse: ...


class NumericSocketConnector(Protocol):
    def __call__(self, ip: str, port: int, timeout: float) -> socket.socket: ...


def _numeric_socket(ip: str, port: int, timeout: float) -> socket.socket:
    """Connect without invoking hostname resolution in the operating system."""

    address = ipaddress.ip_address(ip)
    family = socket.AF_INET6 if address.version == 6 else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM)
    try:
        sock.settimeout(timeout)
        target: tuple[str, int] | tuple[str, int, int, int]
        if address.version == 6:
            target = (address.compressed, port, 0, 0)
        else:
            target = (address.compressed, port)
        sock.connect(target)
        return sock
    except BaseException:
        sock.close()
        raise


def _checkpoint(
    *,
    deadline_at: float | None,
    cancellation_probe: CancellationProbe | None,
    monotonic: Callable[[], float],
) -> None:
    if cancellation_probe is not None:
        try:
            cancelled = bool(cancellation_probe())
        except asyncio.CancelledError:
            raise
        except Exception:
            raise asyncio.CancelledError("web cancellation probe failed") from None
        if cancelled:
            raise asyncio.CancelledError("web request cancelled")
    if deadline_at is not None and monotonic() >= deadline_at:
        raise WebHttpError(
            WebHttpErrorCode.DEADLINE_EXCEEDED,
            retryable=True,
        )


def _remaining_timeout(
    configured: float,
    *,
    deadline_at: float | None,
    cancellation_probe: CancellationProbe | None,
    monotonic: Callable[[], float],
) -> float:
    _checkpoint(
        deadline_at=deadline_at,
        cancellation_probe=cancellation_probe,
        monotonic=monotonic,
    )
    if deadline_at is None:
        return configured
    remaining = deadline_at - monotonic()
    if remaining <= 0:
        raise WebHttpError(
            WebHttpErrorCode.DEADLINE_EXCEEDED,
            retryable=True,
        )
    return max(min(configured, remaining), 0.001)


def _timeout_failure(
    *,
    deadline_at: float | None,
    monotonic: Callable[[], float],
) -> WebHttpError:
    code = (
        WebHttpErrorCode.DEADLINE_EXCEEDED
        if deadline_at is not None and monotonic() >= deadline_at
        else WebHttpErrorCode.REQUEST_TIMEOUT
    )
    return WebHttpError(code, retryable=True)


class _WatchdogReason(StrEnum):
    CANCELLED = "cancelled"
    DEADLINE = "deadline"


class _RequestWatchdog:
    """Interrupt one blocking HTTP request at its absolute Run boundary.

    ``http.client`` performs synchronous buffered reads.  A peer that sends a
    byte before every socket timeout can otherwise keep response parsing alive
    indefinitely because the timeout is restarted for each ``recv``.  This
    bounded daemon watches the request-owned deadline/cancellation signal and
    shuts down only the currently registered socket from outside that read.
    """

    def __init__(
        self,
        *,
        deadline_at: float | None,
        cancellation_probe: CancellationProbe | None,
        monotonic: Callable[[], float],
    ) -> None:
        self._deadline_at = deadline_at
        self._cancellation_probe = cancellation_probe
        self._monotonic = monotonic
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._socket: socket.socket | None = None
        self._reason: _WatchdogReason | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._deadline_at is None and self._cancellation_probe is None:
            return
        thread = threading.Thread(
            target=self._watch,
            name="web-http-watchdog",
            daemon=True,
        )
        self._thread = thread
        try:
            thread.start()
        except BaseException:
            self._thread = None
            self._stop.set()
            raise WebHttpError(
                WebHttpErrorCode.CONNECTION_FAILED,
                retryable=True,
            ) from None

    def track_socket(self, sock: socket.socket) -> None:
        """Register the raw or TLS socket, closing a hand-off race fail-closed."""

        with self._lock:
            if self._stop.is_set():
                return
            self._socket = sock
            interrupted = self._reason is not None
        if interrupted:
            self._shutdown(sock)
            self.raise_if_tripped()

    def raise_if_tripped(self) -> None:
        with self._lock:
            reason = self._reason
        if reason is _WatchdogReason.CANCELLED:
            raise asyncio.CancelledError("web request cancelled")
        if reason is _WatchdogReason.DEADLINE:
            raise WebHttpError(
                WebHttpErrorCode.DEADLINE_EXCEEDED,
                retryable=True,
            )

    def close(self) -> None:
        self._stop.set()
        with self._lock:
            self._socket = None
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=_WATCHDOG_JOIN_SECONDS)
        self._thread = None

    def _watch(self) -> None:
        while not self._stop.is_set():
            reason = self._current_reason()
            if reason is not None:
                self._interrupt(reason)
                return
            wait_seconds = _WATCHDOG_POLL_SECONDS
            if self._deadline_at is not None:
                remaining = self._deadline_at - self._monotonic()
                if remaining <= 0:
                    self._interrupt(_WatchdogReason.DEADLINE)
                    return
                wait_seconds = min(wait_seconds, remaining)
            if self._stop.wait(wait_seconds):
                return

    def _current_reason(self) -> _WatchdogReason | None:
        if self._cancellation_probe is not None:
            try:
                if self._cancellation_probe():
                    return _WatchdogReason.CANCELLED
            except (asyncio.CancelledError, Exception):
                return _WatchdogReason.CANCELLED
        if self._deadline_at is not None and self._monotonic() >= self._deadline_at:
            return _WatchdogReason.DEADLINE
        return None

    def _interrupt(self, reason: _WatchdogReason) -> None:
        with self._lock:
            if self._stop.is_set() or self._reason is not None:
                return
            self._reason = reason
            sock = self._socket
        if sock is not None:
            self._shutdown(sock)

    @staticmethod
    def _shutdown(sock: socket.socket) -> None:
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except Exception:
            pass


class _PinnedConnection(http.client.HTTPConnection):
    """``HTTPConnection`` whose connect path accepts numeric pins only."""

    def __init__(
        self,
        resolved: ResolvedWebUrl,
        *,
        policy: WebUrlPolicy,
        timeout_seconds: float,
        deadline_at: float | None,
        cancellation_probe: CancellationProbe | None,
        connector: NumericSocketConnector,
        ssl_context: ssl.SSLContext,
        monotonic: Callable[[], float],
        watchdog: _RequestWatchdog,
    ) -> None:
        super().__init__(resolved.host, resolved.port, timeout=timeout_seconds)
        self._resolved = resolved
        self._policy = policy
        self._configured_timeout = timeout_seconds
        self._deadline_at = deadline_at
        self._cancellation_probe = cancellation_probe
        self._connector = connector
        self._ssl_context = ssl_context
        self._monotonic = monotonic
        self._watchdog = watchdog
        self.peer_ip = ""

    def refresh_timeout(self) -> None:
        """Bind the live socket to the remaining request/Run time budget."""

        if self.sock is None:
            raise WebHttpError(
                WebHttpErrorCode.CONNECTION_FAILED,
                retryable=True,
            )
        self._refresh_socket_timeout(self.sock)

    def _refresh_socket_timeout(self, sock: socket.socket) -> None:
        timeout = _remaining_timeout(
            self._configured_timeout,
            deadline_at=self._deadline_at,
            cancellation_probe=self._cancellation_probe,
            monotonic=self._monotonic,
        )
        try:
            sock.settimeout(timeout)
        except OSError:
            raise WebHttpError(
                WebHttpErrorCode.CONNECTION_FAILED,
                retryable=True,
            ) from None
        self.timeout = timeout

    def connect(self) -> None:
        saw_tls_failure = False
        saw_timeout = False
        for pinned_ip in self._resolved.pinned_addresses:
            timeout = _remaining_timeout(
                self._configured_timeout,
                deadline_at=self._deadline_at,
                cancellation_probe=self._cancellation_probe,
                monotonic=self._monotonic,
            )
            raw: socket.socket | None = None
            try:
                raw = self._connector(pinned_ip, self._resolved.port, timeout)
                self._watchdog.track_socket(raw)
                peer_ip = str(raw.getpeername()[0])
                self._policy.verify_peer(self._resolved, peer_ip)
                if self._resolved.scheme == "https":
                    self._refresh_socket_timeout(raw)
                    raw = self._ssl_context.wrap_socket(
                        raw,
                        server_hostname=self._resolved.host,
                        do_handshake_on_connect=False,
                    )
                    self._watchdog.track_socket(raw)
                    raw.do_handshake()
                    peer_ip = str(raw.getpeername()[0])
                    self._policy.verify_peer(self._resolved, peer_ip)
                self._refresh_socket_timeout(raw)
                self.sock = raw
                self.peer_ip = peer_ip
                return
            except asyncio.CancelledError:
                if raw is not None:
                    raw.close()
                raise
            except ssl.SSLError:
                self._watchdog.raise_if_tripped()
                saw_tls_failure = True
                if raw is not None:
                    raw.close()
            except (TimeoutError, socket.timeout):
                self._watchdog.raise_if_tripped()
                saw_timeout = True
                if raw is not None:
                    raw.close()
            except WebHttpError as exc:
                if raw is not None:
                    raw.close()
                if exc.code == WebHttpErrorCode.DEADLINE_EXCEEDED.value:
                    raise
            except OSError:
                self._watchdog.raise_if_tripped()
                if raw is not None:
                    raw.close()
            except BaseException:
                if raw is not None:
                    raw.close()
                raise
        self._watchdog.raise_if_tripped()
        if saw_tls_failure:
            raise WebHttpError(WebHttpErrorCode.TLS_FAILED)
        if saw_timeout:
            raise _timeout_failure(
                deadline_at=self._deadline_at,
                monotonic=self._monotonic,
            )
        raise WebHttpError(
            WebHttpErrorCode.CONNECTION_FAILED,
            retryable=True,
        )


class PinnedWebHttpTransport:
    """Direct HTTP/1.1 Adapter with TLS hostname checks and DNS pinning."""

    _BLOCKED_REQUEST_HEADERS = frozenset(
        {
            "accept-encoding",
            "connection",
            "content-length",
            "host",
            "proxy-authorization",
            "proxy-connection",
            "transfer-encoding",
        }
    )

    def __init__(
        self,
        policy: WebUrlPolicy,
        *,
        connector: NumericSocketConnector = _numeric_socket,
        ssl_context: ssl.SSLContext | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if not callable(connector):
            raise TypeError("connector must be callable")
        if not callable(monotonic):
            raise TypeError("monotonic must be callable")
        self._policy = policy
        self._connector = connector
        self._ssl_context = ssl_context or ssl.create_default_context()
        if (
            self._ssl_context.verify_mode != ssl.CERT_REQUIRED
            or not self._ssl_context.check_hostname
        ):
            raise ValueError("ssl_context must verify certificates and hostnames")
        self._monotonic = monotonic

    def get(
        self,
        resolved: ResolvedWebUrl,
        *,
        headers: Mapping[str, str],
        allowed_content_types: frozenset[str],
        max_compressed_bytes: int,
        max_response_bytes: int,
        timeout_seconds: float,
        deadline_at: float | None,
        cancellation_probe: CancellationProbe | None,
    ) -> WebHttpResponse:
        _validate_body_limit(max_compressed_bytes, "max_compressed_bytes")
        _validate_body_limit(max_response_bytes, "max_response_bytes")
        _validate_positive_float(timeout_seconds, "timeout_seconds")
        timeout = _remaining_timeout(
            timeout_seconds,
            deadline_at=deadline_at,
            cancellation_probe=cancellation_probe,
            monotonic=self._monotonic,
        )
        request_headers = self._request_headers(resolved, headers)
        watchdog = _RequestWatchdog(
            deadline_at=deadline_at,
            cancellation_probe=cancellation_probe,
            monotonic=self._monotonic,
        )
        connection = _PinnedConnection(
            resolved,
            policy=self._policy,
            timeout_seconds=timeout,
            deadline_at=deadline_at,
            cancellation_probe=cancellation_probe,
            connector=self._connector,
            ssl_context=self._ssl_context,
            monotonic=self._monotonic,
            watchdog=watchdog,
        )
        try:
            watchdog.start()
            connection.request(
                "GET",
                resolved.request_target,
                headers=request_headers,
            )
            connection.refresh_timeout()
            response = connection.getresponse()
            response_headers = _response_headers(response.getheaders())
            status = int(response.status)
            if 300 <= status <= 399 or not 200 <= status <= 299:
                body = b""
            else:
                _validate_content_type(response_headers, allowed_content_types)
                body = self._read_body(
                    response,
                    response_headers,
                    max_compressed_bytes=max_compressed_bytes,
                    max_response_bytes=max_response_bytes,
                    deadline_at=deadline_at,
                    cancellation_probe=cancellation_probe,
                    refresh_timeout=connection.refresh_timeout,
                )
            watchdog.raise_if_tripped()
            _checkpoint(
                deadline_at=deadline_at,
                cancellation_probe=cancellation_probe,
                monotonic=self._monotonic,
            )
            return WebHttpResponse(
                status_code=status,
                headers=response_headers,
                body=body,
                peer_ip=connection.peer_ip,
            )
        except asyncio.CancelledError:
            raise
        except WebHttpError:
            watchdog.raise_if_tripped()
            raise
        except (TimeoutError, socket.timeout):
            watchdog.raise_if_tripped()
            raise _timeout_failure(
                deadline_at=deadline_at,
                monotonic=self._monotonic,
            ) from None
        except (http.client.HTTPException, OSError, ValueError):
            watchdog.raise_if_tripped()
            raise WebHttpError(
                WebHttpErrorCode.CONNECTION_FAILED,
                retryable=True,
            ) from None
        finally:
            watchdog.close()
            connection.close()

    def _read_body(
        self,
        response: http.client.HTTPResponse,
        headers: Mapping[str, str],
        *,
        max_compressed_bytes: int,
        max_response_bytes: int,
        deadline_at: float | None,
        cancellation_probe: CancellationProbe | None,
        refresh_timeout: Callable[[], None] | None = None,
    ) -> bytes:
        content_length = headers.get("content-length")
        if content_length is not None:
            if not content_length.isdecimal():
                raise WebHttpError(WebHttpErrorCode.INVALID_RESPONSE)
            if int(content_length) > max_compressed_bytes:
                raise WebHttpError(WebHttpErrorCode.RESPONSE_TOO_LARGE)

        encoding = headers.get("content-encoding", "identity").strip().casefold()
        if encoding in {"", "identity"}:
            decoder: zlib.decompressobj | None = None
        elif encoding == "gzip":
            decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
        else:
            raise WebHttpError(WebHttpErrorCode.CONTENT_ENCODING_DENIED)

        compressed = 0
        decoded = bytearray()
        while True:
            _checkpoint(
                deadline_at=deadline_at,
                cancellation_probe=cancellation_probe,
                monotonic=self._monotonic,
            )
            if refresh_timeout is not None:
                refresh_timeout()
            chunk = response.read(min(65_536, max_compressed_bytes + 1 - compressed))
            if not chunk:
                break
            compressed += len(chunk)
            if compressed > max_compressed_bytes:
                raise WebHttpError(WebHttpErrorCode.RESPONSE_TOO_LARGE)
            if decoder is None:
                decoded.extend(chunk)
            else:
                allowance = max_response_bytes + 1 - len(decoded)
                try:
                    decoded.extend(decoder.decompress(chunk, max(allowance, 1)))
                except zlib.error:
                    raise WebHttpError(WebHttpErrorCode.INVALID_RESPONSE) from None
                if decoder.unconsumed_tail:
                    raise WebHttpError(WebHttpErrorCode.RESPONSE_TOO_LARGE)
            if len(decoded) > max_response_bytes:
                raise WebHttpError(WebHttpErrorCode.RESPONSE_TOO_LARGE)
        if decoder is not None:
            try:
                decoded.extend(decoder.flush(max_response_bytes + 1 - len(decoded)))
            except zlib.error:
                raise WebHttpError(WebHttpErrorCode.INVALID_RESPONSE) from None
            if not decoder.eof:
                raise WebHttpError(WebHttpErrorCode.INVALID_RESPONSE)
            if decoder.unused_data:
                raise WebHttpError(WebHttpErrorCode.INVALID_RESPONSE)
        if len(decoded) > max_response_bytes:
            raise WebHttpError(WebHttpErrorCode.RESPONSE_TOO_LARGE)
        return bytes(decoded)

    @classmethod
    def _request_headers(
        cls,
        resolved: ResolvedWebUrl,
        headers: Mapping[str, str],
    ) -> dict[str, str]:
        normalized = {
            "Host": resolved.authority,
            "Connection": "close",
            "Accept-Encoding": "gzip",
        }
        for raw_name, raw_value in headers.items():
            name = str(raw_name).strip()
            value = str(raw_value).strip()
            if not name or name.casefold() in cls._BLOCKED_REQUEST_HEADERS:
                continue
            if "\r" in name or "\n" in name or "\r" in value or "\n" in value:
                raise ValueError("invalid request header")
            normalized[name] = value
        return normalized


class SafeWebHttpClient:
    """Redirect-aware client that re-runs URL policy at every hop."""

    def __init__(
        self,
        policy: WebUrlPolicy,
        *,
        transport: WebHttpTransport | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if not callable(monotonic):
            raise TypeError("monotonic must be callable")
        self.policy = policy
        self.transport = transport or PinnedWebHttpTransport(
            policy,
            monotonic=monotonic,
        )
        self._monotonic = monotonic

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
    ) -> WebHttpFetch:
        _validate_body_limit(max_compressed_bytes, "max_compressed_bytes")
        _validate_body_limit(max_response_bytes, "max_response_bytes")
        _validate_positive_float(timeout_seconds, "timeout_seconds")
        if isinstance(max_redirects, bool) or not isinstance(max_redirects, int):
            raise TypeError("max_redirects must be an integer")
        if not 0 <= max_redirects <= _HARD_MAX_REDIRECTS:
            raise ValueError(
                f"max_redirects must be between 0 and {_HARD_MAX_REDIRECTS}"
            )
        if deadline_at is not None and (
            isinstance(deadline_at, bool)
            or not isinstance(deadline_at, (int, float))
            or not math.isfinite(float(deadline_at))
        ):
            raise ValueError("deadline_at must be finite")
        request_deadline = self._monotonic() + timeout_seconds
        effective_deadline = (
            request_deadline
            if deadline_at is None
            else min(request_deadline, deadline_at)
        )
        _checkpoint(
            deadline_at=effective_deadline,
            cancellation_probe=cancellation_probe,
            monotonic=self._monotonic,
        )
        current = self.policy.resolve(
            url,
            deadline_at=effective_deadline,
            cancellation_probe=cancellation_probe,
        )
        _checkpoint(
            deadline_at=effective_deadline,
            cancellation_probe=cancellation_probe,
            monotonic=self._monotonic,
        )
        visited: set[str] = set()
        request_headers = dict(headers or {})
        for redirects in range(max_redirects + 1):
            _checkpoint(
                deadline_at=effective_deadline,
                cancellation_probe=cancellation_probe,
                monotonic=self._monotonic,
            )
            if current.canonical_url in visited:
                raise WebHttpError(WebHttpErrorCode.REDIRECT_LIMIT_EXCEEDED)
            visited.add(current.canonical_url)
            response = self.transport.get(
                current,
                headers=request_headers,
                allowed_content_types=allowed_content_types,
                max_compressed_bytes=max_compressed_bytes,
                max_response_bytes=max_response_bytes,
                timeout_seconds=timeout_seconds,
                deadline_at=effective_deadline,
                cancellation_probe=cancellation_probe,
            )
            _checkpoint(
                deadline_at=effective_deadline,
                cancellation_probe=cancellation_probe,
                monotonic=self._monotonic,
            )
            self.policy.verify_peer(current, response.peer_ip)
            if len(response.body) > max_response_bytes:
                raise WebHttpError(WebHttpErrorCode.RESPONSE_TOO_LARGE)
            if response.status_code in {301, 302, 303, 307, 308}:
                location = response.header("location")
                if location is None or redirects >= max_redirects:
                    raise WebHttpError(WebHttpErrorCode.REDIRECT_LIMIT_EXCEEDED)
                redirected = self.policy.validate_redirect(
                    current,
                    location,
                    deadline_at=effective_deadline,
                    cancellation_probe=cancellation_probe,
                )
                _checkpoint(
                    deadline_at=effective_deadline,
                    cancellation_probe=cancellation_probe,
                    monotonic=self._monotonic,
                )
                if current.scheme == "https" and redirected.scheme != "https":
                    raise WebHttpError(WebHttpErrorCode.REDIRECT_DENIED)
                if _origin(current) != _origin(redirected):
                    request_headers = {
                        name: value
                        for name, value in request_headers.items()
                        if name.casefold() in {"accept", "user-agent"}
                    }
                current = redirected
                continue
            if not 200 <= response.status_code <= 299:
                raise WebHttpError(
                    WebHttpErrorCode.HTTP_STATUS,
                    retryable=response.status_code in {408, 425, 429}
                    or response.status_code >= 500,
                    safe_details={"status_code": response.status_code},
                )
            _validate_content_type(response.headers, allowed_content_types)
            encoding = response.headers.get("content-encoding", "identity")
            if encoding.strip().casefold() not in {"", "identity", "gzip"}:
                raise WebHttpError(WebHttpErrorCode.CONTENT_ENCODING_DENIED)
            return WebHttpFetch(
                resolved=current,
                status_code=response.status_code,
                headers=response.headers,
                body=response.body,
                redirects=redirects,
            )
        raise WebHttpError(WebHttpErrorCode.REDIRECT_LIMIT_EXCEEDED)


def _response_headers(values: Iterable[tuple[str, str]]) -> Mapping[str, str]:
    retained = {
        "content-encoding",
        "content-length",
        "content-type",
        "location",
    }
    normalized: dict[str, str] = {}
    for raw_name, raw_value in values:
        name = raw_name.strip().casefold()
        if name not in retained:
            continue
        value = raw_value.strip()
        if name in normalized:
            if name in {
                "content-encoding",
                "content-length",
                "content-type",
                "location",
            }:
                raise WebHttpError(WebHttpErrorCode.INVALID_RESPONSE)
            normalized[name] = f"{normalized[name]}, {value}"
        else:
            normalized[name] = value
    return MappingProxyType(normalized)


def _origin(resolved: ResolvedWebUrl) -> tuple[str, str, int]:
    return resolved.scheme, resolved.host, resolved.port


def _validate_content_type(
    headers: Mapping[str, str],
    allowed: frozenset[str],
) -> None:
    raw = headers.get("content-type", "")
    media_type = raw.partition(";")[0].strip().casefold()
    if not media_type or media_type not in allowed:
        raise WebHttpError(WebHttpErrorCode.CONTENT_TYPE_DENIED)


def _validate_positive_int(value: int, field: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field} must be a positive integer")


def _validate_body_limit(value: int, field: str) -> None:
    _validate_positive_int(value, field)
    if value > _HARD_MAX_BODY_BYTES:
        raise ValueError(f"{field} exceeds the hard response byte limit")


def _validate_positive_float(value: float, field: str) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or value <= 0
    ):
        raise ValueError(f"{field} must be positive")


__all__ = [
    "CancellationProbe",
    "PinnedWebHttpTransport",
    "SafeWebHttpClient",
    "WebHttpError",
    "WebHttpErrorCode",
    "WebHttpFetch",
    "WebHttpResponse",
    "WebHttpTransport",
]
