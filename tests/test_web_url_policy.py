from __future__ import annotations

import asyncio
import socket
import threading
import time
from collections.abc import Iterable

import pytest

from backend.web_research.url_policy import (
    DnsPinSnapshot,
    SystemWebDnsResolver,
    WebUrlPolicy,
    WebUrlPolicyCode,
    WebUrlPolicyError,
)


class FakeDnsResolver:
    def __init__(
        self,
        responses: dict[str, Iterable[Iterable[str]] | Iterable[str] | Exception],
    ) -> None:
        self.responses = responses
        self.calls: list[tuple[str, int]] = []
        self._indexes: dict[str, int] = {}

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
        response = self.responses[host]
        if isinstance(response, Exception):
            raise response
        values = tuple(response)
        if values and isinstance(values[0], (tuple, list)):
            index = self._indexes.get(host, 0)
            self._indexes[host] = index + 1
            return tuple(values[min(index, len(values) - 1)])
        return tuple(str(value) for value in values)


def policy(
    responses: dict[str, Iterable[Iterable[str]] | Iterable[str] | Exception]
    | None = None,
    **kwargs: object,
) -> tuple[WebUrlPolicy, FakeDnsResolver]:
    resolver = FakeDnsResolver(responses or {"public.research.dev": ("8.8.8.8",)})
    return WebUrlPolicy(resolver, **kwargs), resolver


def assert_denied(
    candidate: str,
    code: WebUrlPolicyCode,
    *,
    responses: dict[str, Iterable[Iterable[str]] | Iterable[str] | Exception]
    | None = None,
) -> None:
    url_policy, _ = policy(responses)
    with pytest.raises(WebUrlPolicyError) as captured:
        url_policy.resolve(candidate)
    assert captured.value.code is code
    assert candidate not in str(captured.value)


def test_resolve_canonicalizes_idna_default_port_path_query_and_fragment() -> None:
    url_policy, resolver = policy({"xn--bcher-kva.de": ("8.8.8.8",)})

    resolved = url_policy.resolve("HTTPS://BÜCHER.DE.:443/a b/%7e?q=✓#client-fragment")

    assert resolved.canonical_url == ("https://xn--bcher-kva.de/a%20b/~?q=%E2%9C%93")
    assert resolved.scheme == "https"
    assert resolved.host == "xn--bcher-kva.de"
    assert resolved.port == 443
    assert resolved.authority == "xn--bcher-kva.de"
    assert resolved.request_target == "/a%20b/~?q=%E2%9C%93"
    assert resolved.pinned_addresses == ("8.8.8.8",)
    assert resolver.calls == [("xn--bcher-kva.de", 443)]
    assert "xn--bcher-kva.de" not in repr(resolved)
    assert "q=%E2%9C%93" not in repr(resolved)
    assert "8.8.8.8" not in repr(resolved.pin)


def test_canonical_url_removes_literal_and_percent_encoded_dot_segments() -> None:
    url_policy, _ = policy()

    resolved = url_policy.resolve(
        "https://public.research.dev/a/./b/%2e%2e/%2E%2E/final"
    )

    assert resolved.canonical_url == "https://public.research.dev/final"
    assert resolved.request_target == "/final"


@pytest.mark.parametrize(
    ("candidate", "code"),
    [
        ("ftp://public.research.dev/file", WebUrlPolicyCode.SCHEME_DENIED),
        ("//public.research.dev/file", WebUrlPolicyCode.SCHEME_DENIED),
        ("https:///missing-host", WebUrlPolicyCode.HOST_DENIED),
        ("https://user:secret@public.research.dev/", WebUrlPolicyCode.USERINFO_DENIED),
        ("https://@public.research.dev/", WebUrlPolicyCode.USERINFO_DENIED),
        ("https://public.research.dev:0/", WebUrlPolicyCode.PORT_DENIED),
        ("https://public.research.dev:22/", WebUrlPolicyCode.PORT_DENIED),
        ("http://public.research.dev:443/", WebUrlPolicyCode.PORT_DENIED),
        ("https://public.research.dev:80/", WebUrlPolicyCode.PORT_DENIED),
    ],
)
def test_scheme_host_userinfo_and_port_are_fail_closed(
    candidate: str,
    code: WebUrlPolicyCode,
) -> None:
    assert_denied(candidate, code)


@pytest.mark.parametrize(
    "candidate",
    [
        "http://localhost/",
        "http://intranet/",
        "http://foo.local/",
        "http://foo.localhost/",
        "http://foo.internal/",
        "http://foo.test/",
        "http://foo.onion/",
        "http://example.com/",
        "http://subdomain.example.net/",
        "http://foo_test.com/",
        "http://-bad.example.dev/",
        "http://0177.0.0.1/",
        "http://xn--.de/",
    ],
)
def test_non_standard_single_label_and_special_hosts_are_denied(
    candidate: str,
) -> None:
    assert_denied(candidate, WebUrlPolicyCode.HOST_DENIED)


@pytest.mark.parametrize(
    "candidate",
    [
        "http://127.0.0.1/",
        "http://10.0.0.1/",
        "http://169.254.169.254/latest/meta-data/",
        "http://224.0.0.1/",
        "http://240.0.0.1/",
        "http://0.0.0.0/",
        "http://[::1]/",
        "http://[fc00::1]/",
        "http://[fe80::1]/",
        "http://[ff02::1]/",
        "http://[::]/",
        "http://[::ffff:127.0.0.1]/",
        "http://[64:ff9b::a00:1]/",
    ],
)
def test_non_global_ipv4_ipv6_and_mapped_literals_are_denied(
    candidate: str,
) -> None:
    assert_denied(candidate, WebUrlPolicyCode.ADDRESS_DENIED)


def test_public_ip_literals_are_pinned_without_dns() -> None:
    url_policy, resolver = policy({})

    ipv4 = url_policy.resolve("http://8.8.8.8/resource")
    ipv6 = url_policy.resolve("https://[2606:4700:4700::1111]/resource")
    mapped = url_policy.resolve("https://[::ffff:8.8.8.8]/resource")

    assert ipv4.pinned_addresses == ("8.8.8.8",)
    assert ipv6.pinned_addresses == ("2606:4700:4700::1111",)
    assert ipv6.authority == "[2606:4700:4700::1111]"
    assert mapped.canonical_url == "https://8.8.8.8/resource"
    assert mapped.pinned_addresses == ("8.8.8.8",)
    assert resolver.calls == []


def test_dns_snapshot_contains_all_deduplicated_global_addresses() -> None:
    url_policy, _ = policy(
        {
            "public.research.dev": (
                "2606:4700:4700::1111",
                "8.8.8.8",
                "8.8.8.8",
            )
        }
    )

    resolved = url_policy.resolve("https://public.research.dev/")

    assert resolved.pinned_addresses == (
        "8.8.8.8",
        "2606:4700:4700::1111",
    )
    assert len(resolved.pin.fingerprint) == 64


def test_system_resolver_uses_an_absolute_dns_name(monkeypatch) -> None:
    observed: list[tuple[str, int]] = []

    def fake_getaddrinfo(host: str, port: int, **kwargs: object):
        observed.append((host, port))
        assert kwargs["family"] == socket.AF_UNSPEC
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", port))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)

    addresses = SystemWebDnsResolver().resolve("public.research.dev", 443)

    assert addresses == ("8.8.8.8",)
    assert observed == [("public.research.dev.", 443)]


def test_system_resolver_times_out_and_bounds_stuck_lookup_threads(
    monkeypatch,
) -> None:
    started = threading.Event()
    release = threading.Event()

    def blocking_getaddrinfo(*args: object, **kwargs: object):
        del args, kwargs
        started.set()
        release.wait(timeout=1)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))]

    monkeypatch.setattr(socket, "getaddrinfo", blocking_getaddrinfo)
    resolver = SystemWebDnsResolver(timeout_seconds=0.02, max_concurrency=1)
    url_policy = WebUrlPolicy(resolver)
    began = time.monotonic()
    try:
        with pytest.raises(WebUrlPolicyError) as timed_out:
            url_policy.resolve("https://public.research.dev/")
        assert timed_out.value.code is WebUrlPolicyCode.DNS_RESOLUTION_FAILED
        assert time.monotonic() - began < 0.5
        assert started.is_set()

        with pytest.raises(WebUrlPolicyError) as saturated:
            url_policy.resolve("https://public.research.dev/")
        assert saturated.value.code is WebUrlPolicyCode.DNS_RESOLUTION_FAILED
    finally:
        release.set()
        resolver.close()


def test_system_resolver_honors_run_deadline_and_cancellation(monkeypatch) -> None:
    release = threading.Event()

    def blocking_getaddrinfo(*args: object, **kwargs: object):
        del args, kwargs
        release.wait(timeout=1)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))]

    monkeypatch.setattr(socket, "getaddrinfo", blocking_getaddrinfo)
    resolver = SystemWebDnsResolver(timeout_seconds=5, max_concurrency=2)
    url_policy = WebUrlPolicy(resolver)
    try:
        with pytest.raises(WebUrlPolicyError) as deadline:
            url_policy.resolve(
                "https://public.research.dev/",
                deadline_at=time.monotonic() + 0.02,
            )
        assert deadline.value.code is WebUrlPolicyCode.DNS_RESOLUTION_FAILED

        probes = 0

        def cancellation_probe() -> bool:
            nonlocal probes
            probes += 1
            return probes >= 2

        with pytest.raises(asyncio.CancelledError):
            url_policy.resolve(
                "https://public.research.dev/",
                cancellation_probe=cancellation_probe,
            )
    finally:
        release.set()
        resolver.close()


def test_cancellation_probe_failure_is_fail_closed_without_starting_dns(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: pytest.fail("DNS must not start"),
    )
    url_policy = WebUrlPolicy(SystemWebDnsResolver())

    def broken_probe() -> bool:
        raise RuntimeError("private cancellation detail")

    with pytest.raises(asyncio.CancelledError) as captured:
        url_policy.resolve(
            "https://public.research.dev/secret",
            cancellation_probe=broken_probe,
        )
    assert "private cancellation detail" not in str(captured.value)


@pytest.mark.parametrize(
    ("response", "code"),
    [
        (("8.8.8.8", "10.0.0.1"), WebUrlPolicyCode.ADDRESS_DENIED),
        (("::ffff:127.0.0.1",), WebUrlPolicyCode.ADDRESS_DENIED),
        (("not-an-ip",), WebUrlPolicyCode.DNS_RESOLUTION_FAILED),
        ((), WebUrlPolicyCode.DNS_RESOLUTION_FAILED),
        (tuple("8.8.8.8" for _ in range(33)), WebUrlPolicyCode.DNS_RESOLUTION_FAILED),
        (OSError("private resolver detail"), WebUrlPolicyCode.DNS_RESOLUTION_FAILED),
    ],
)
def test_dns_fails_closed_if_any_answer_is_unsafe_or_resolution_is_uncertain(
    response: Iterable[str] | Exception,
    code: WebUrlPolicyCode,
) -> None:
    candidate = "https://public.research.dev/private-query-token"
    assert_denied(
        candidate,
        code,
        responses={"public.research.dev": response},
    )


def test_dns_pin_identity_is_stable_and_peer_must_match_snapshot() -> None:
    first = DnsPinSnapshot(
        host="public.research.dev",
        port=443,
        addresses=("2606:4700:4700::1111", "8.8.8.8"),
    )
    second = DnsPinSnapshot(
        host="public.research.dev",
        port=443,
        addresses=("8.8.8.8", "2606:4700:4700::1111"),
    )
    assert first.fingerprint == second.fingerprint

    url_policy, _ = policy()
    resolved = url_policy.resolve("https://public.research.dev/")
    url_policy.verify_peer(resolved, "::ffff:8.8.8.8")

    with pytest.raises(WebUrlPolicyError) as mismatch:
        url_policy.verify_peer(resolved, "1.1.1.1")
    assert mismatch.value.code is WebUrlPolicyCode.DNS_PIN_MISMATCH

    with pytest.raises(WebUrlPolicyError) as unsafe:
        url_policy.verify_peer(resolved, "127.0.0.1")
    assert unsafe.value.code is WebUrlPolicyCode.ADDRESS_DENIED


def test_each_redirect_is_joined_canonicalized_and_resolved_again() -> None:
    url_policy, resolver = policy(
        {
            "public.research.dev": (
                ("8.8.8.8",),
                ("1.1.1.1",),
            )
        }
    )
    initial = url_policy.resolve("https://public.research.dev/a/start")

    redirected = url_policy.validate_redirect(initial, "../next?q=two words#part")

    assert redirected.canonical_url == (
        "https://public.research.dev/next?q=two%20words"
    )
    assert initial.pinned_addresses == ("8.8.8.8",)
    assert redirected.pinned_addresses == ("1.1.1.1",)
    assert resolver.calls == [
        ("public.research.dev", 443),
        ("public.research.dev", 443),
    ]


def test_redirect_rebinding_to_private_address_is_denied() -> None:
    url_policy, _ = policy(
        {
            "public.research.dev": (
                ("8.8.8.8",),
                ("127.0.0.1",),
            )
        }
    )
    initial = url_policy.resolve("https://public.research.dev/start")

    with pytest.raises(WebUrlPolicyError) as captured:
        url_policy.validate_redirect(initial, "/next")

    assert captured.value.code is WebUrlPolicyCode.ADDRESS_DENIED


@pytest.mark.parametrize(
    "location",
    [
        " ../private",
        "\\\\127.0.0.1\\share",
        "https://public.research.dev/\r\nInjected: value",
        "",
    ],
)
def test_malformed_redirect_locations_are_denied(location: str) -> None:
    url_policy, _ = policy()
    initial = url_policy.resolve("https://public.research.dev/start")

    with pytest.raises(WebUrlPolicyError) as captured:
        url_policy.validate_redirect(initial, location)

    assert captured.value.code is WebUrlPolicyCode.REDIRECT_DENIED


def test_url_length_and_port_allowlist_are_strictly_configurable() -> None:
    url_policy, resolver = policy(
        {"public.research.dev": ("8.8.8.8",)},
        allowed_scheme_ports={"https": frozenset({8443})},
        max_url_bytes=128,
    )

    resolved = url_policy.resolve("https://public.research.dev:8443/")
    assert resolved.port == 8443
    assert resolver.calls == [("public.research.dev", 8443)]

    with pytest.raises(WebUrlPolicyError) as too_long:
        url_policy.resolve(f"https://public.research.dev/{'x' * 128}")
    assert too_long.value.code is WebUrlPolicyCode.URL_TOO_LONG

    with pytest.raises(WebUrlPolicyError) as denied_port:
        url_policy.resolve("https://public.research.dev/")
    assert denied_port.value.code is WebUrlPolicyCode.PORT_DENIED


def test_dns_address_count_has_a_configurable_fail_closed_ceiling() -> None:
    url_policy, _ = policy(
        {"public.research.dev": ("8.8.8.8", "1.1.1.1")},
        max_resolved_addresses=1,
    )

    with pytest.raises(WebUrlPolicyError) as captured:
        url_policy.resolve("https://public.research.dev/")

    assert captured.value.code is WebUrlPolicyCode.DNS_RESOLUTION_FAILED
    assert captured.value.safe_details == {"max_addresses": 1}


def test_scheme_port_policy_rejects_empty_unknown_and_unsafe_configuration() -> None:
    resolver = FakeDnsResolver({"public.research.dev": ("8.8.8.8",)})
    with pytest.raises(TypeError, match="non-empty mapping"):
        WebUrlPolicy(resolver, allowed_scheme_ports={})
    with pytest.raises(ValueError, match="canonical http or https"):
        WebUrlPolicy(resolver, allowed_scheme_ports={"ftp": {21}})
    with pytest.raises(ValueError, match="max_resolved_addresses"):
        WebUrlPolicy(resolver, max_resolved_addresses=33)


def test_policy_close_is_idempotent_and_closes_owned_dns_adapter() -> None:
    class ClosableResolver(FakeDnsResolver):
        def __init__(self) -> None:
            super().__init__({"public.research.dev": ("8.8.8.8",)})
            self.close_calls = 0

        def close(self) -> None:
            self.close_calls += 1

    resolver = ClosableResolver()
    url_policy = WebUrlPolicy(resolver)
    url_policy.close()
    url_policy.close()

    assert resolver.close_calls == 1
    with pytest.raises(WebUrlPolicyError) as captured:
        url_policy.resolve("https://public.research.dev/")
    assert captured.value.code is WebUrlPolicyCode.DNS_RESOLUTION_FAILED
    assert resolver.calls == []


@pytest.mark.parametrize(
    "candidate",
    [
        " https://public.research.dev/",
        "https://public.research.dev/a\\b",
        "https://public.research.dev/a\x00b",
        "https://public.research.dev/a\tb",
        "https://public.research.dev/%zz",
    ],
)
def test_parser_differential_and_control_character_inputs_are_denied(
    candidate: str,
) -> None:
    assert_denied(candidate, WebUrlPolicyCode.INVALID_URL)
