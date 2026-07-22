from __future__ import annotations

import asyncio
import gzip
import socket
import ssl
import threading
import time
from collections.abc import Mapping

import pytest

from backend.web_research.http import (
    PinnedWebHttpTransport,
    SafeWebHttpClient,
    WebHttpError,
    WebHttpErrorCode,
    WebHttpFetch,
    WebHttpResponse,
    _RequestWatchdog,
    _numeric_socket,
)
from backend.web_research.url_policy import (
    WebUrlPolicy,
    WebUrlPolicyCode,
    WebUrlPolicyError,
)


class _Resolver:
    def __init__(self, addresses: Mapping[str, tuple[str, ...]]) -> None:
        self.addresses = dict(addresses)
        self.calls: list[tuple[str, int]] = []

    def resolve(
        self,
        host: str,
        port: int,
        *,
        deadline_at: float | None = None,
        cancellation_probe=None,
    ) -> tuple[str, ...]:
        del deadline_at, cancellation_probe
        self.calls.append((host, port))
        return self.addresses[host]


class _ScriptedTransport:
    def __init__(self, responses: list[WebHttpResponse]) -> None:
        self.responses = list(responses)
        self.requests = []
        self.request_options = []

    def get(self, resolved, **kwargs) -> WebHttpResponse:
        self.requests.append(resolved)
        self.request_options.append(kwargs)
        return self.responses.pop(0)

    def post(self, resolved, **kwargs) -> WebHttpResponse:
        self.requests.append(resolved)
        self.request_options.append(kwargs)
        return self.responses.pop(0)


class _PinnedSocket:
    """Socket proxy whose peer identity matches the public DNS test pin."""

    def __init__(self, value: socket.socket) -> None:
        self._value = value

    def getpeername(self) -> tuple[str, int]:
        return ("93.184.216.34", 443)

    def __getattr__(self, name: str):
        return getattr(self._value, name)


class _PeerPolicy:
    @staticmethod
    def verify_peer(resolved, peer_ip) -> None:
        del resolved, peer_ip


def _start_slow_sender(
    sock: socket.socket,
    chunks: list[bytes],
    *,
    interval_seconds: float = 0.02,
) -> tuple[threading.Event, threading.Thread]:
    stop = threading.Event()

    def send() -> None:
        try:
            for chunk in chunks:
                if stop.wait(interval_seconds):
                    return
                sock.sendall(chunk)
        except OSError:
            return

    thread = threading.Thread(target=send, name="test-slow-web-peer", daemon=True)
    thread.start()
    return stop, thread


def _get(client: SafeWebHttpClient, url: str, **overrides):
    values = {
        "allowed_content_types": frozenset({"text/plain"}),
        "max_compressed_bytes": 1024,
        "max_response_bytes": 1024,
        "max_redirects": 3,
        "timeout_seconds": 1.0,
    }
    values.update(overrides)
    return client.get(url, **values)


def _post(client: SafeWebHttpClient, url: str, **overrides):
    values = {
        "body": b'{"query":"safe"}',
        "allowed_content_types": frozenset({"application/json"}),
        "max_compressed_bytes": 1024,
        "max_response_bytes": 1024,
        "max_redirects": 0,
        "timeout_seconds": 1.0,
    }
    values.update(overrides)
    return client.post(url, **values)


def test_safe_client_posts_bounded_body_to_the_pinned_origin() -> None:
    resolver = _Resolver({"api.tavily.com": ("1.1.1.1",)})
    transport = _ScriptedTransport(
        [
            WebHttpResponse(
                200,
                {"Content-Type": "application/json"},
                b'{"results":[]}',
                "1.1.1.1",
            )
        ]
    )

    result = _post(
        SafeWebHttpClient(WebUrlPolicy(resolver), transport=transport),
        "https://api.tavily.com/search",
        headers={
            "Content-Type": "application/json",
            "X-Tavily-Access-Mode": "keyless",
        },
    )

    assert result.body == b'{"results":[]}'
    assert resolver.calls == [("api.tavily.com", 443)]
    assert transport.request_options[0]["body"] == b'{"query":"safe"}'
    assert transport.request_options[0]["headers"]["X-Tavily-Access-Mode"] == (
        "keyless"
    )


def test_safe_client_denies_cross_origin_post_redirects() -> None:
    resolver = _Resolver(
        {
            "api.tavily.com": ("1.1.1.1",),
            "other.cloudflare.com": ("93.184.216.34",),
        }
    )
    transport = _ScriptedTransport(
        [
            WebHttpResponse(
                307,
                {"Location": "https://other.cloudflare.com/search"},
                b"",
                "1.1.1.1",
            )
        ]
    )

    with pytest.raises(WebHttpError) as raised:
        _post(
            SafeWebHttpClient(WebUrlPolicy(resolver), transport=transport),
            "https://api.tavily.com/search",
            max_redirects=1,
        )

    assert raised.value.code == WebHttpErrorCode.REDIRECT_DENIED.value
    assert len(transport.requests) == 1


def test_safe_client_re_resolves_and_revalidates_every_redirect_hop() -> None:
    resolver = _Resolver(
        {
            "first.openai.com": ("93.184.216.34",),
            "second.cloudflare.com": ("1.1.1.1",),
        }
    )
    policy = WebUrlPolicy(resolver)
    transport = _ScriptedTransport(
        [
            WebHttpResponse(
                302,
                {"Location": "https://second.cloudflare.com/article"},
                b"",
                "93.184.216.34",
            ),
            WebHttpResponse(
                200,
                {"Content-Type": "text/plain"},
                b"bounded body",
                "1.1.1.1",
            ),
        ]
    )

    result = _get(
        SafeWebHttpClient(policy, transport=transport),
        "https://first.openai.com/start",
    )

    assert result.resolved.canonical_url == "https://second.cloudflare.com/article"
    assert result.redirects == 1
    assert result.body == b"bounded body"
    assert resolver.calls == [
        ("first.openai.com", 443),
        ("second.cloudflare.com", 443),
    ]
    assert [item.pinned_addresses for item in transport.requests] == [
        ("93.184.216.34",),
        ("1.1.1.1",),
    ]


def test_redirect_to_private_address_is_rejected_before_second_request() -> None:
    resolver = _Resolver(
        {
            "first.openai.com": ("93.184.216.34",),
            "private.openai.com": ("127.0.0.1",),
        }
    )
    policy = WebUrlPolicy(resolver)
    transport = _ScriptedTransport(
        [
            WebHttpResponse(
                302,
                {"Location": "http://private.openai.com/admin"},
                b"",
                "93.184.216.34",
            )
        ]
    )

    with pytest.raises(WebUrlPolicyError) as raised:
        _get(
            SafeWebHttpClient(policy, transport=transport),
            "https://first.openai.com/start",
        )

    assert raised.value.code is WebUrlPolicyCode.ADDRESS_DENIED
    assert len(transport.requests) == 1


def test_transport_peer_must_match_the_dns_snapshot() -> None:
    resolver = _Resolver({"public.openai.com": ("93.184.216.34",)})
    policy = WebUrlPolicy(resolver)
    transport = _ScriptedTransport(
        [
            WebHttpResponse(
                200,
                {"Content-Type": "text/plain"},
                b"body",
                "1.1.1.1",
            )
        ]
    )

    with pytest.raises(WebUrlPolicyError) as raised:
        _get(
            SafeWebHttpClient(policy, transport=transport),
            "https://public.openai.com/",
        )

    assert raised.value.code is WebUrlPolicyCode.DNS_PIN_MISMATCH


def test_cancel_and_deadline_are_checked_before_dns_resolution() -> None:
    resolver = _Resolver({"public.openai.com": ("93.184.216.34",)})
    client = SafeWebHttpClient(WebUrlPolicy(resolver), transport=_ScriptedTransport([]))

    with pytest.raises(asyncio.CancelledError):
        _get(
            client,
            "https://public.openai.com/secret?token=value",
            cancellation_probe=lambda: True,
        )
    assert resolver.calls == []

    def broken_probe() -> bool:
        raise RuntimeError("secret probe detail")

    with pytest.raises(asyncio.CancelledError) as cancelled:
        _get(
            client,
            "https://public.openai.com/secret?token=value",
            cancellation_probe=broken_probe,
        )
    assert "secret" not in str(cancelled.value)
    assert resolver.calls == []

    with pytest.raises(WebHttpError) as raised:
        _get(
            client,
            "https://public.openai.com/secret?token=value",
            deadline_at=9.0,
        )
    assert raised.value.code == WebHttpErrorCode.DEADLINE_EXCEEDED.value
    assert "token" not in str(raised.value)
    assert resolver.calls == []


def test_request_timeout_is_one_budget_across_all_hops() -> None:
    now = {"value": 0.0}
    resolver = _Resolver(
        {
            "first.openai.com": ("93.184.216.34",),
            "second.cloudflare.com": ("1.1.1.1",),
        }
    )

    class _AdvancingTransport:
        def get(self, resolved, **kwargs) -> WebHttpResponse:
            del resolved, kwargs
            now["value"] = 2.0
            return WebHttpResponse(
                302,
                {"location": "https://second.cloudflare.com/next"},
                b"",
                "93.184.216.34",
            )

    client = SafeWebHttpClient(
        WebUrlPolicy(resolver),
        transport=_AdvancingTransport(),
        monotonic=lambda: now["value"],
    )

    with pytest.raises(WebHttpError) as raised:
        _get(client, "https://first.openai.com/start", timeout_seconds=1.0)

    assert raised.value.code == WebHttpErrorCode.DEADLINE_EXCEEDED.value
    assert resolver.calls == [("first.openai.com", 443)]


def test_transport_rebinds_socket_timeout_to_remaining_deadline_before_headers() -> (
    None
):
    """A slow connect must not leave a full stale timeout for response headers."""

    client_socket, server_socket = socket.socketpair()

    policy = WebUrlPolicy(_Resolver({"public.openai.com": ("93.184.216.34",)}))
    resolved = policy.resolve("http://public.openai.com/")

    def delayed_connector(ip: str, port: int, timeout: float) -> socket.socket:
        del ip, port
        time.sleep(0.15)
        client_socket.settimeout(timeout)
        return _PinnedSocket(client_socket)  # type: ignore[return-value]

    transport = PinnedWebHttpTransport(
        _PeerPolicy(),  # type: ignore[arg-type]
        connector=delayed_connector,
    )
    started = time.monotonic()
    deadline_at = started + 0.20
    try:
        with pytest.raises(WebHttpError) as raised:
            transport.get(
                resolved,
                headers={},
                allowed_content_types=frozenset({"text/plain"}),
                max_compressed_bytes=1024,
                max_response_bytes=1024,
                timeout_seconds=1.0,
                deadline_at=deadline_at,
                cancellation_probe=None,
            )
        elapsed = time.monotonic() - started
    finally:
        client_socket.close()
        server_socket.close()

    assert raised.value.code in {
        WebHttpErrorCode.DEADLINE_EXCEEDED.value,
        WebHttpErrorCode.REQUEST_TIMEOUT.value,
    }
    assert elapsed < 0.28


def test_transport_rebinds_socket_timeout_before_each_body_read() -> None:
    """A bounded body read must use the time left after connect and headers."""

    client_socket, server_socket = socket.socketpair()
    server_socket.sendall(
        b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: 1\r\n\r\n"
    )
    policy = WebUrlPolicy(_Resolver({"public.openai.com": ("93.184.216.34",)}))
    resolved = policy.resolve("http://public.openai.com/")

    def delayed_connector(ip: str, port: int, timeout: float) -> socket.socket:
        del ip, port
        time.sleep(0.15)
        client_socket.settimeout(timeout)
        return _PinnedSocket(client_socket)  # type: ignore[return-value]

    transport = PinnedWebHttpTransport(
        _PeerPolicy(),  # type: ignore[arg-type]
        connector=delayed_connector,
    )
    started = time.monotonic()
    deadline_at = started + 0.20
    try:
        with pytest.raises(WebHttpError) as raised:
            transport.get(
                resolved,
                headers={},
                allowed_content_types=frozenset({"text/plain"}),
                max_compressed_bytes=1024,
                max_response_bytes=1024,
                timeout_seconds=1.0,
                deadline_at=deadline_at,
                cancellation_probe=None,
            )
        elapsed = time.monotonic() - started
    finally:
        client_socket.close()
        server_socket.close()

    assert raised.value.code in {
        WebHttpErrorCode.DEADLINE_EXCEEDED.value,
        WebHttpErrorCode.REQUEST_TIMEOUT.value,
    }
    assert elapsed < 0.28


def test_transport_reads_body_when_server_closes_connection() -> None:
    client_socket, server_socket = socket.socketpair()
    server_socket.sendall(
        b"HTTP/1.1 200 OK\r\n"
        b"Content-Type: application/json\r\n"
        b"Content-Length: 14\r\n"
        b"Connection: close\r\n\r\n"
        b'{"results":[]}'
    )
    server_socket.shutdown(socket.SHUT_WR)
    policy = WebUrlPolicy(_Resolver({"api.tavily.com": ("93.184.216.34",)}))
    resolved = policy.resolve("http://api.tavily.com/search")

    def connector(ip: str, port: int, timeout: float) -> socket.socket:
        del ip, port
        client_socket.settimeout(timeout)
        return _PinnedSocket(client_socket)  # type: ignore[return-value]

    transport = PinnedWebHttpTransport(
        _PeerPolicy(),  # type: ignore[arg-type]
        connector=connector,
    )
    try:
        response = transport.post(
            resolved,
            headers={"Content-Type": "application/json"},
            body=b'{}',
            allowed_content_types=frozenset({"application/json"}),
            max_compressed_bytes=1024,
            max_response_bytes=1024,
            timeout_seconds=1.0,
            deadline_at=None,
            cancellation_probe=None,
        )
    finally:
        client_socket.close()
        server_socket.close()

    assert response.status_code == 200
    assert response.body == b'{"results":[]}'


def test_transport_absolute_deadline_interrupts_slow_response_headers() -> None:
    client_socket, server_socket = socket.socketpair()
    policy = WebUrlPolicy(_Resolver({"public.openai.com": ("93.184.216.34",)}))
    resolved = policy.resolve("http://public.openai.com/")
    response = (
        b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: 0\r\n\r\n"
    )
    stop, sender = _start_slow_sender(
        server_socket,
        [response[index : index + 1] for index in range(len(response))],
    )

    def connector(ip: str, port: int, timeout: float) -> socket.socket:
        del ip, port
        client_socket.settimeout(timeout)
        return _PinnedSocket(client_socket)  # type: ignore[return-value]

    transport = PinnedWebHttpTransport(
        _PeerPolicy(),  # type: ignore[arg-type]
        connector=connector,
    )
    started = time.monotonic()
    try:
        with pytest.raises(WebHttpError) as raised:
            transport.get(
                resolved,
                headers={},
                allowed_content_types=frozenset({"text/plain"}),
                max_compressed_bytes=1024,
                max_response_bytes=1024,
                timeout_seconds=1.0,
                deadline_at=started + 0.10,
                cancellation_probe=None,
            )
        elapsed = time.monotonic() - started
    finally:
        stop.set()
        client_socket.close()
        server_socket.close()
        sender.join(timeout=0.2)

    assert raised.value.code == WebHttpErrorCode.DEADLINE_EXCEEDED.value
    assert elapsed < 0.35


def test_transport_absolute_deadline_interrupts_slow_response_body() -> None:
    client_socket, server_socket = socket.socketpair()
    policy = WebUrlPolicy(_Resolver({"public.openai.com": ("93.184.216.34",)}))
    resolved = policy.resolve("http://public.openai.com/")
    server_socket.sendall(
        b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: 40\r\n\r\n"
    )
    stop, sender = _start_slow_sender(server_socket, [b"x"] * 40)

    def connector(ip: str, port: int, timeout: float) -> socket.socket:
        del ip, port
        client_socket.settimeout(timeout)
        return _PinnedSocket(client_socket)  # type: ignore[return-value]

    transport = PinnedWebHttpTransport(
        _PeerPolicy(),  # type: ignore[arg-type]
        connector=connector,
    )
    started = time.monotonic()
    try:
        with pytest.raises(WebHttpError) as raised:
            transport.get(
                resolved,
                headers={},
                allowed_content_types=frozenset({"text/plain"}),
                max_compressed_bytes=1024,
                max_response_bytes=1024,
                timeout_seconds=1.0,
                deadline_at=started + 0.10,
                cancellation_probe=None,
            )
        elapsed = time.monotonic() - started
    finally:
        stop.set()
        client_socket.close()
        server_socket.close()
        sender.join(timeout=0.2)

    assert raised.value.code == WebHttpErrorCode.DEADLINE_EXCEEDED.value
    assert elapsed < 0.35


def test_transport_absolute_deadline_interrupts_slow_tls_handshake() -> None:
    client_socket, server_socket = socket.socketpair()
    policy = WebUrlPolicy(_Resolver({"public.openai.com": ("93.184.216.34",)}))
    resolved = policy.resolve("https://public.openai.com/")
    stop, sender = _start_slow_sender(server_socket, [b"x"] * 40)

    class _TlsContext:
        verify_mode = ssl.CERT_REQUIRED
        check_hostname = True

        @staticmethod
        def wrap_socket(sock, *, server_hostname, do_handshake_on_connect):
            assert server_hostname == "public.openai.com"
            assert do_handshake_on_connect is False

            class _SlowTlsSocket:
                def do_handshake(self) -> None:
                    while sock.recv(1):
                        pass
                    raise ssl.SSLError("sensitive TLS peer detail")

                def __getattr__(self, name: str):
                    return getattr(sock, name)

            return _SlowTlsSocket()

    def connector(ip: str, port: int, timeout: float) -> socket.socket:
        del ip, port
        client_socket.settimeout(timeout)
        return _PinnedSocket(client_socket)  # type: ignore[return-value]

    transport = PinnedWebHttpTransport(
        _PeerPolicy(),  # type: ignore[arg-type]
        connector=connector,
        ssl_context=_TlsContext(),  # type: ignore[arg-type]
    )
    started = time.monotonic()
    try:
        with pytest.raises(WebHttpError) as raised:
            transport.get(
                resolved,
                headers={},
                allowed_content_types=frozenset({"text/plain"}),
                max_compressed_bytes=1024,
                max_response_bytes=1024,
                timeout_seconds=1.0,
                deadline_at=started + 0.10,
                cancellation_probe=None,
            )
        elapsed = time.monotonic() - started
    finally:
        stop.set()
        client_socket.close()
        server_socket.close()
        sender.join(timeout=0.2)

    assert raised.value.code == WebHttpErrorCode.DEADLINE_EXCEEDED.value
    assert "sensitive" not in str(raised.value)
    assert elapsed < 0.35


def test_transport_cancellation_interrupts_slow_body_without_leaking_probe_error() -> (
    None
):
    client_socket, server_socket = socket.socketpair()
    policy = WebUrlPolicy(_Resolver({"public.openai.com": ("93.184.216.34",)}))
    resolved = policy.resolve("http://public.openai.com/secret?token=value")
    server_socket.sendall(
        b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: 40\r\n\r\n"
    )
    stop, sender = _start_slow_sender(server_socket, [b"x"] * 40)

    def connector(ip: str, port: int, timeout: float) -> socket.socket:
        del ip, port
        client_socket.settimeout(timeout)
        return _PinnedSocket(client_socket)  # type: ignore[return-value]

    transport = PinnedWebHttpTransport(
        _PeerPolicy(),  # type: ignore[arg-type]
        connector=connector,
    )
    started = time.monotonic()

    def cancellation_probe() -> bool:
        if time.monotonic() - started >= 0.10:
            raise RuntimeError("sensitive cancellation detail")
        return False

    try:
        with pytest.raises(asyncio.CancelledError) as cancelled:
            transport.get(
                resolved,
                headers={},
                allowed_content_types=frozenset({"text/plain"}),
                max_compressed_bytes=1024,
                max_response_bytes=1024,
                timeout_seconds=1.0,
                deadline_at=started + 1.0,
                cancellation_probe=cancellation_probe,
            )
        elapsed = time.monotonic() - started
    finally:
        stop.set()
        client_socket.close()
        server_socket.close()
        sender.join(timeout=0.2)

    assert "sensitive" not in str(cancelled.value)
    assert "token" not in str(cancelled.value)
    assert elapsed < 0.35


def test_request_watchdog_socket_handoff_and_cleanup_are_race_safe() -> None:
    class _ObservedSocket:
        def __init__(self) -> None:
            self.shutdown_called = threading.Event()

        def shutdown(self, how: int) -> None:
            assert how == socket.SHUT_RDWR
            self.shutdown_called.set()

    raw = _ObservedSocket()
    tls = _ObservedSocket()
    watchdog = _RequestWatchdog(
        deadline_at=time.monotonic() + 0.03,
        cancellation_probe=None,
        monotonic=time.monotonic,
    )
    watchdog.start()
    thread = watchdog._thread  # noqa: SLF001 - lifecycle regression assertion
    watchdog.track_socket(raw)  # type: ignore[arg-type]
    assert raw.shutdown_called.wait(timeout=0.2)

    with pytest.raises(WebHttpError) as raised:
        watchdog.track_socket(tls)  # type: ignore[arg-type]
    watchdog.close()

    assert raised.value.code == WebHttpErrorCode.DEADLINE_EXCEEDED.value
    assert tls.shutdown_called.is_set()
    assert thread is not None and not thread.is_alive()


def test_request_watchdog_cleanup_prevents_late_socket_shutdown() -> None:
    class _ObservedSocket:
        def __init__(self) -> None:
            self.shutdown_called = threading.Event()

        def shutdown(self, how: int) -> None:
            del how
            self.shutdown_called.set()

    sock = _ObservedSocket()
    watchdog = _RequestWatchdog(
        deadline_at=time.monotonic() + 0.08,
        cancellation_probe=None,
        monotonic=time.monotonic,
    )
    watchdog.start()
    thread = watchdog._thread  # noqa: SLF001 - lifecycle regression assertion
    watchdog.track_socket(sock)  # type: ignore[arg-type]
    watchdog.close()
    time.sleep(0.10)

    assert not sock.shutdown_called.is_set()
    assert thread is not None and not thread.is_alive()


def test_transport_refreshes_timeout_before_tls_handshake() -> None:
    client_socket, server_socket = socket.socketpair()
    policy = WebUrlPolicy(_Resolver({"public.openai.com": ("93.184.216.34",)}))
    resolved = policy.resolve("https://public.openai.com/")
    observed_timeouts: list[float] = []

    class _TlsContext:
        verify_mode = ssl.CERT_REQUIRED
        check_hostname = True

        @staticmethod
        def wrap_socket(sock, *, server_hostname, do_handshake_on_connect):
            assert server_hostname == "public.openai.com"
            assert do_handshake_on_connect is False
            observed_timeouts.append(sock.gettimeout())

            class _TlsSocket:
                def do_handshake(self) -> None:
                    raise ssl.SSLError("controlled handshake stop")

                def __getattr__(self, name: str):
                    return getattr(sock, name)

            return _TlsSocket()

    def delayed_connector(ip: str, port: int, timeout: float) -> socket.socket:
        del ip, port
        time.sleep(0.15)
        client_socket.settimeout(timeout)
        return _PinnedSocket(client_socket)  # type: ignore[return-value]

    transport = PinnedWebHttpTransport(
        _PeerPolicy(),  # type: ignore[arg-type]
        connector=delayed_connector,
        ssl_context=_TlsContext(),  # type: ignore[arg-type]
    )
    started = time.monotonic()
    try:
        with pytest.raises(WebHttpError) as raised:
            transport.get(
                resolved,
                headers={},
                allowed_content_types=frozenset({"text/plain"}),
                max_compressed_bytes=1024,
                max_response_bytes=1024,
                timeout_seconds=1.0,
                deadline_at=started + 0.20,
                cancellation_probe=None,
            )
    finally:
        client_socket.close()
        server_socket.close()

    assert raised.value.code == WebHttpErrorCode.TLS_FAILED.value
    assert observed_timeouts and observed_timeouts[0] < 0.10


def test_transport_cancellation_after_connect_prevents_tls_handshake() -> None:
    client_socket, server_socket = socket.socketpair()
    policy = WebUrlPolicy(_Resolver({"public.openai.com": ("93.184.216.34",)}))
    resolved = policy.resolve("https://public.openai.com/")
    connected = False
    handshake_started = False

    class _TlsContext:
        verify_mode = ssl.CERT_REQUIRED
        check_hostname = True

        @staticmethod
        def wrap_socket(sock, *, server_hostname):
            del sock, server_hostname
            nonlocal handshake_started
            handshake_started = True
            raise AssertionError("cancelled request must not start TLS")

    def connector(ip: str, port: int, timeout: float) -> socket.socket:
        del ip, port
        nonlocal connected
        connected = True
        client_socket.settimeout(timeout)
        return _PinnedSocket(client_socket)  # type: ignore[return-value]

    transport = PinnedWebHttpTransport(
        _PeerPolicy(),  # type: ignore[arg-type]
        connector=connector,
        ssl_context=_TlsContext(),  # type: ignore[arg-type]
    )
    try:
        with pytest.raises(asyncio.CancelledError):
            transport.get(
                resolved,
                headers={},
                allowed_content_types=frozenset({"text/plain"}),
                max_compressed_bytes=1024,
                max_response_bytes=1024,
                timeout_seconds=1.0,
                deadline_at=None,
                cancellation_probe=lambda: connected,
            )
        assert client_socket.fileno() == -1
    finally:
        client_socket.close()
        server_socket.close()

    assert handshake_started is False


def test_redirect_count_is_bounded() -> None:
    resolver = _Resolver({"public.openai.com": ("93.184.216.34",)})
    transport = _ScriptedTransport(
        [
            WebHttpResponse(
                302,
                {"Location": "/next"},
                b"",
                "93.184.216.34",
            )
        ]
    )

    with pytest.raises(WebHttpError) as raised:
        _get(
            SafeWebHttpClient(WebUrlPolicy(resolver), transport=transport),
            "https://public.openai.com/start",
            max_redirects=0,
        )

    assert raised.value.code == WebHttpErrorCode.REDIRECT_LIMIT_EXCEEDED.value

    with pytest.raises(ValueError, match="between"):
        _get(
            SafeWebHttpClient(WebUrlPolicy(resolver), transport=transport),
            "https://public.openai.com/start",
            max_redirects=11,
        )


def test_cross_origin_redirect_drops_all_headers_except_explicit_safe_list() -> None:
    resolver = _Resolver(
        {
            "first.openai.com": ("93.184.216.34",),
            "second.cloudflare.com": ("1.1.1.1",),
        }
    )
    transport = _ScriptedTransport(
        [
            WebHttpResponse(
                302,
                {"location": "https://second.cloudflare.com/result"},
                b"",
                "93.184.216.34",
            ),
            WebHttpResponse(
                200,
                {"content-type": "text/plain"},
                b"result",
                "1.1.1.1",
            ),
        ]
    )

    _get(
        SafeWebHttpClient(WebUrlPolicy(resolver), transport=transport),
        "https://first.openai.com/search",
        headers={
            "Accept": "text/plain",
            "Authorization": "Bearer secret",
            "Cookie": "session=secret",
            "User-Agent": "test-agent",
            "X-Provider-Token": "provider-secret",
            "X-Unknown-Secret": "secret",
        },
    )

    assert transport.request_options[1]["headers"] == {
        "Accept": "text/plain",
        "User-Agent": "test-agent",
    }


def test_same_origin_redirect_can_retain_provider_credentials() -> None:
    resolver = _Resolver({"first.openai.com": ("93.184.216.34",)})
    transport = _ScriptedTransport(
        [
            WebHttpResponse(
                302,
                {"location": "/v2/search"},
                b"",
                "93.184.216.34",
            ),
            WebHttpResponse(
                200,
                {"content-type": "text/plain"},
                b"result",
                "93.184.216.34",
            ),
        ]
    )

    _get(
        SafeWebHttpClient(WebUrlPolicy(resolver), transport=transport),
        "https://first.openai.com/v1/search",
        headers={"X-Provider-Token": "provider-secret"},
    )

    assert transport.request_options[1]["headers"] == {
        "X-Provider-Token": "provider-secret"
    }


def test_https_redirect_cannot_downgrade_to_plain_http() -> None:
    resolver = _Resolver({"first.openai.com": ("93.184.216.34",)})
    transport = _ScriptedTransport(
        [
            WebHttpResponse(
                302,
                {"location": "http://first.openai.com/insecure"},
                b"",
                "93.184.216.34",
            )
        ]
    )

    with pytest.raises(WebHttpError) as raised:
        _get(
            SafeWebHttpClient(WebUrlPolicy(resolver), transport=transport),
            "https://first.openai.com/secure",
        )

    assert raised.value.code == WebHttpErrorCode.REDIRECT_DENIED.value
    assert len(transport.requests) == 1


class _ReadResponse:
    def __init__(self, body: bytes) -> None:
        self.body = body
        self.offset = 0

    def read(self, size: int) -> bytes:
        chunk = self.body[self.offset : self.offset + size]
        self.offset += len(chunk)
        return chunk


def test_wire_and_decompressed_body_limits_fail_closed() -> None:
    resolver = _Resolver({"public.openai.com": ("93.184.216.34",)})
    transport = PinnedWebHttpTransport(WebUrlPolicy(resolver))

    with pytest.raises(WebHttpError) as wire_error:
        transport._read_body(  # noqa: SLF001 - focused boundary test
            _ReadResponse(b"12345"),
            {},
            max_compressed_bytes=4,
            max_response_bytes=10,
            deadline_at=None,
            cancellation_probe=None,
        )
    assert wire_error.value.code == WebHttpErrorCode.RESPONSE_TOO_LARGE.value

    compressed = gzip.compress(b"a" * 200)
    with pytest.raises(WebHttpError) as decoded_error:
        transport._read_body(  # noqa: SLF001 - focused boundary test
            _ReadResponse(compressed),
            {"content-encoding": "gzip"},
            max_compressed_bytes=len(compressed),
            max_response_bytes=10,
            deadline_at=None,
            cancellation_probe=None,
        )
    assert decoded_error.value.code == WebHttpErrorCode.RESPONSE_TOO_LARGE.value


def test_numeric_connector_never_invokes_hostname_resolution(monkeypatch) -> None:
    connected: list[tuple[str, int]] = []

    class _Socket:
        def settimeout(self, value: float) -> None:
            assert value == 1.0

        def connect(self, target) -> None:
            connected.append(target)

        def close(self) -> None:
            pass

    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("DNS called")),
    )
    monkeypatch.setattr(socket, "socket", lambda *args, **kwargs: _Socket())

    _numeric_socket("93.184.216.34", 443, 1.0)

    assert connected == [("93.184.216.34", 443)]


def test_transport_rejects_an_insecure_tls_context() -> None:
    resolver = _Resolver({"public.openai.com": ("93.184.216.34",)})
    insecure = ssl._create_unverified_context()

    with pytest.raises(ValueError, match="verify"):
        PinnedWebHttpTransport(WebUrlPolicy(resolver), ssl_context=insecure)


def test_http_value_repr_never_contains_url_headers_or_body() -> None:
    policy = WebUrlPolicy(_Resolver({"public.openai.com": ("93.184.216.34",)}))
    response = WebHttpResponse(
        200,
        {"location": "https://public.openai.com/?token=secret"},
        b"secret body",
        "93.184.216.34",
    )
    fetch = WebHttpFetch(
        resolved=policy.resolve("https://public.openai.com/?token=secret"),
        status_code=200,
        headers={"content-type": "text/plain", "x-secret": "secret"},
        body=b"secret body",
        redirects=0,
    )

    assert "secret" not in repr(response)
    assert "secret" not in repr(fetch)
    assert "public.openai.com" not in repr(fetch)
