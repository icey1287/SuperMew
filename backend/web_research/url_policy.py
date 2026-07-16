"""SSRF policy Implementation behind the Web Research URL Seam.

The Module concentrates canonicalization, DNS pinning and redirect validation
to provide Locality for every network Adapter.
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import math
import re
import socket
import threading
import time
from collections.abc import Collection, Mapping, Sequence
from concurrent.futures import Future, TimeoutError as FutureTimeoutError
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Callable, Protocol, runtime_checkable
from urllib.parse import urljoin, urlsplit, urlunsplit

from backend.web_research.contracts import DEFAULT_WEB_RESEARCH_LIMITS


_DEFAULT_PORTS = {"http": 80, "https": 443}
_UNRESERVED = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"
)
_PATH_SAFE = _UNRESERVED | frozenset("/!$&'()*+,;=:@")
_QUERY_SAFE = _PATH_SAFE | frozenset("?")
_ASCII_HOST_LABEL_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
_HEX_RE = re.compile(r"[0-9A-Fa-f]{2}")
_MAX_RESOLVED_ADDRESSES = 32
_MAX_URL_BYTES = 16 * 1024

_SPECIAL_USE_SUFFIXES = frozenset(
    {
        "alt",
        "arpa",
        "corp",
        "home",
        "internal",
        "invalid",
        "lan",
        "local",
        "localdomain",
        "localhost",
        "onion",
        "private",
        "test",
        "example",
        "example.com",
        "example.net",
        "example.org",
    }
)
_NAT64_WELL_KNOWN = ipaddress.ip_network("64:ff9b::/96")
_NAT64_LOCAL_USE = ipaddress.ip_network("64:ff9b:1::/48")


CancellationProbe = Callable[[], bool]


class WebUrlPolicyCode(StrEnum):
    URL_TOO_LONG = "WEB_URL_TOO_LONG"
    INVALID_URL = "WEB_INVALID_URL"
    SCHEME_DENIED = "WEB_SCHEME_DENIED"
    USERINFO_DENIED = "WEB_USERINFO_DENIED"
    HOST_DENIED = "WEB_HOST_DENIED"
    PORT_DENIED = "WEB_PORT_DENIED"
    DNS_RESOLUTION_FAILED = "WEB_DNS_RESOLUTION_FAILED"
    ADDRESS_DENIED = "WEB_ADDRESS_DENIED"
    DNS_PIN_MISMATCH = "WEB_DNS_PIN_MISMATCH"
    REDIRECT_DENIED = "WEB_REDIRECT_DENIED"


class WebUrlPolicyError(ValueError):
    """Stable, redacted URL policy failure safe to expose as a policy code."""

    def __init__(
        self,
        code: WebUrlPolicyCode | str,
        message: str,
        *,
        safe_details: dict[str, int | bool] | None = None,
    ) -> None:
        self.code = WebUrlPolicyCode(code)
        self.safe_details = dict(safe_details or {})
        super().__init__(message)


@runtime_checkable
class WebDnsResolver(Protocol):
    """DNS Adapter seam used to make resolution complete and testable."""

    def resolve(
        self,
        host: str,
        port: int,
        *,
        deadline_at: float | None = None,
        cancellation_probe: CancellationProbe | None = None,
    ) -> Sequence[str]: ...


class SystemWebDnsResolver:
    """Bounded daemon-thread DNS Adapter with no unbounded work queue.

    ``getaddrinfo`` has no portable cancellation primitive. A timed-out lookup
    may finish in its daemon thread, but it retains one of a fixed number of
    slots; further calls fail fast once all slots are occupied.
    """

    def __init__(
        self,
        *,
        timeout_seconds: float = 2.0,
        max_concurrency: int = 4,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(float(timeout_seconds))
            or timeout_seconds <= 0
        ):
            raise ValueError("timeout_seconds must be positive and finite")
        if (
            isinstance(max_concurrency, bool)
            or not isinstance(max_concurrency, int)
            or not 1 <= max_concurrency <= 32
        ):
            raise ValueError("max_concurrency must be between 1 and 32")
        if not callable(monotonic):
            raise TypeError("monotonic must be callable")
        self.timeout_seconds = float(timeout_seconds)
        self.max_concurrency = max_concurrency
        self._monotonic = monotonic
        self._slots = threading.BoundedSemaphore(max_concurrency)
        self._state_lock = threading.Lock()
        self._closed = False
        self._thread_sequence = 0

    def resolve(
        self,
        host: str,
        port: int,
        *,
        deadline_at: float | None = None,
        cancellation_probe: CancellationProbe | None = None,
    ) -> tuple[str, ...]:
        _raise_if_cancelled(cancellation_probe)
        now = self._monotonic()
        effective_deadline = now + self.timeout_seconds
        if deadline_at is not None:
            if not isinstance(deadline_at, (int, float)) or not math.isfinite(
                float(deadline_at)
            ):
                raise ValueError("deadline_at must be finite")
            effective_deadline = min(effective_deadline, float(deadline_at))
        if effective_deadline <= now:
            raise TimeoutError("DNS resolution deadline exceeded")
        with self._state_lock:
            if self._closed:
                raise RuntimeError("DNS resolver is closed")
            if not self._slots.acquire(blocking=False):
                raise TimeoutError("DNS resolver capacity is exhausted")
            self._thread_sequence += 1
            sequence = self._thread_sequence

        future: Future[tuple[str, ...]] = Future()

        def lookup() -> None:
            try:
                if not future.set_running_or_notify_cancel():
                    return
                # A trailing root label prevents local resolver search suffixes
                # from changing a canonical multi-label host.
                absolute_host = f"{host}."
                records = socket.getaddrinfo(
                    absolute_host,
                    port,
                    family=socket.AF_UNSPEC,
                    type=socket.SOCK_STREAM,
                    proto=socket.IPPROTO_TCP,
                )
                future.set_result(tuple(record[4][0] for record in records))
            except BaseException as exc:
                if future.running():
                    future.set_exception(exc)
            finally:
                self._slots.release()

        thread = threading.Thread(
            target=lookup,
            name=f"web-dns-{sequence}",
            daemon=True,
        )
        try:
            thread.start()
        except BaseException:
            future.cancel()
            self._slots.release()
            raise

        while True:
            _raise_if_cancelled(cancellation_probe)
            remaining = effective_deadline - self._monotonic()
            if remaining <= 0:
                future.cancel()
                raise TimeoutError("DNS resolution deadline exceeded")
            try:
                return future.result(timeout=min(remaining, 0.05))
            except FutureTimeoutError:
                if future.done():
                    return future.result()

    def close(self) -> None:
        with self._state_lock:
            self._closed = True


class _InvalidAddress(ValueError):
    pass


class _UnsafeAddress(ValueError):
    pass


def _canonical_json_fingerprint(payload: object) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def _raise_if_cancelled(cancellation_probe: CancellationProbe | None) -> None:
    if cancellation_probe is None:
        return
    try:
        cancelled = cancellation_probe()
    except asyncio.CancelledError:
        raise
    except Exception:
        raise asyncio.CancelledError("DNS cancellation probe failed") from None
    if cancelled:
        raise asyncio.CancelledError("DNS resolution cancelled")


def _integer_port(value: int, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{field} must be an integer")
    if not 1 <= value <= 65535:
        raise ValueError(f"{field} must be between 1 and 65535")
    return value


def _utf8_size(value: str, *, field: str) -> int:
    if not isinstance(value, str):
        raise TypeError(f"{field} must be a string")
    try:
        return len(value.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise WebUrlPolicyError(
            WebUrlPolicyCode.INVALID_URL,
            f"{field} contains invalid Unicode",
        ) from exc


def _validate_raw_url_text(value: str, *, field: str, max_bytes: int) -> None:
    size = _utf8_size(value, field=field)
    if size > max_bytes:
        raise WebUrlPolicyError(
            WebUrlPolicyCode.URL_TOO_LONG,
            f"{field} exceeds its size limit",
            safe_details={"max_url_bytes": max_bytes},
        )
    if not value or value != value.strip():
        raise WebUrlPolicyError(
            WebUrlPolicyCode.INVALID_URL,
            f"{field} must be a non-empty canonical string",
        )
    if "\\" in value or "\x00" in value:
        raise WebUrlPolicyError(
            WebUrlPolicyCode.INVALID_URL,
            f"{field} contains a forbidden character",
        )
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        raise WebUrlPolicyError(
            WebUrlPolicyCode.INVALID_URL,
            f"{field} contains a control character",
        )


def _canonicalize_url_component(value: str, *, safe: frozenset[str]) -> str:
    output: list[str] = []
    index = 0
    while index < len(value):
        character = value[index]
        if character == "%":
            escape = value[index + 1 : index + 3]
            if len(escape) != 2 or not _HEX_RE.fullmatch(escape):
                raise WebUrlPolicyError(
                    WebUrlPolicyCode.INVALID_URL,
                    "URL contains an invalid percent escape",
                )
            byte = int(escape, 16)
            decoded = chr(byte)
            output.append(decoded if decoded in _UNRESERVED else f"%{escape.upper()}")
            index += 3
            continue
        if character in safe:
            output.append(character)
        else:
            try:
                encoded = character.encode("utf-8")
            except UnicodeEncodeError as exc:
                raise WebUrlPolicyError(
                    WebUrlPolicyCode.INVALID_URL,
                    "URL contains invalid Unicode",
                ) from exc
            output.extend(f"%{byte:02X}" for byte in encoded)
        index += 1
    return "".join(output)


def _remove_dot_segments(path: str) -> str:
    remaining = path
    output = ""
    while remaining:
        if remaining.startswith("../"):
            remaining = remaining[3:]
        elif remaining.startswith("./"):
            remaining = remaining[2:]
        elif remaining.startswith("/./"):
            remaining = f"/{remaining[3:]}"
        elif remaining == "/.":
            remaining = "/"
        elif remaining.startswith("/../"):
            remaining = f"/{remaining[4:]}"
            output = output.rpartition("/")[0]
        elif remaining == "/..":
            remaining = "/"
            output = output.rpartition("/")[0]
        elif remaining in {".", ".."}:
            remaining = ""
        else:
            separator = remaining.find("/", 1 if remaining.startswith("/") else 0)
            if separator < 0:
                output += remaining
                remaining = ""
            else:
                output += remaining[:separator]
                remaining = remaining[separator:]
    return output or "/"


def _is_special_domain(host: str) -> bool:
    return any(
        host == suffix or host.endswith(f".{suffix}")
        for suffix in _SPECIAL_USE_SUFFIXES
    )


def _canonical_domain(host: str) -> str:
    candidate = host[:-1] if host.endswith(".") else host
    if not candidate or "%" in candidate or ":" in candidate:
        raise WebUrlPolicyError(
            WebUrlPolicyCode.HOST_DENIED,
            "URL host is not a standard domain name",
        )
    try:
        ascii_host = candidate.encode("idna").decode("ascii").lower()
    except (UnicodeError, UnicodeEncodeError) as exc:
        raise WebUrlPolicyError(
            WebUrlPolicyCode.HOST_DENIED,
            "URL host cannot be canonicalized with IDNA",
        ) from exc
    labels = ascii_host.split(".")
    if len(labels) < 2 or any(not label for label in labels):
        raise WebUrlPolicyError(
            WebUrlPolicyCode.HOST_DENIED,
            "Single-label and malformed hosts are not allowed",
        )
    if len(ascii_host.encode("ascii")) > 253:
        raise WebUrlPolicyError(
            WebUrlPolicyCode.HOST_DENIED,
            "URL host exceeds the DNS name limit",
        )
    if all(label.isdigit() for label in labels):
        raise WebUrlPolicyError(
            WebUrlPolicyCode.HOST_DENIED,
            "Non-standard numeric hosts are not allowed",
        )
    for label in labels:
        if not _ASCII_HOST_LABEL_RE.fullmatch(label):
            raise WebUrlPolicyError(
                WebUrlPolicyCode.HOST_DENIED,
                "URL host contains a non-standard DNS label",
            )
        if label.startswith("xn--"):
            try:
                decoded = label.encode("ascii").decode("idna")
                round_trip = decoded.encode("idna").decode("ascii").lower()
            except UnicodeError as exc:
                raise WebUrlPolicyError(
                    WebUrlPolicyCode.HOST_DENIED,
                    "URL host contains invalid IDNA",
                ) from exc
            if round_trip != label:
                raise WebUrlPolicyError(
                    WebUrlPolicyCode.HOST_DENIED,
                    "URL host contains non-canonical IDNA",
                )
    if _is_special_domain(ascii_host):
        raise WebUrlPolicyError(
            WebUrlPolicyCode.HOST_DENIED,
            "Special-use domains are not allowed",
        )
    return ascii_host


def _reject_transition_address(address: ipaddress.IPv6Address) -> None:
    if address.sixtofour is not None or address.teredo is not None:
        raise _UnsafeAddress("IPv6 transition addresses are not allowed")
    if address in _NAT64_WELL_KNOWN or address in _NAT64_LOCAL_USE:
        raise _UnsafeAddress("IPv4-embedded NAT64 addresses are not allowed")


def _canonical_global_ip(value: str) -> str:
    if not isinstance(value, str) or not value or "%" in value:
        raise _InvalidAddress("IP address is invalid")
    try:
        address = ipaddress.ip_address(value)
    except ValueError as exc:
        raise _InvalidAddress("IP address is invalid") from exc
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is not None:
            address = address.ipv4_mapped
        else:
            _reject_transition_address(address)
    denied = (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
        or getattr(address, "is_site_local", False)
        or not address.is_global
    )
    if denied:
        raise _UnsafeAddress("IP address is not globally routable")
    return str(address)


def _canonical_host(host: str) -> tuple[str, bool]:
    if not isinstance(host, str) or not host:
        raise WebUrlPolicyError(
            WebUrlPolicyCode.HOST_DENIED,
            "URL host is missing",
        )
    candidate = host[:-1] if host.endswith(".") else host
    try:
        return _canonical_global_ip(candidate), True
    except _UnsafeAddress as exc:
        raise WebUrlPolicyError(
            WebUrlPolicyCode.ADDRESS_DENIED,
            "URL address is not globally routable",
        ) from exc
    except _InvalidAddress:
        return _canonical_domain(host), False


def _address_sort_key(value: str) -> tuple[int, bytes]:
    address = ipaddress.ip_address(value)
    return address.version, address.packed


@dataclass(frozen=True, slots=True)
class DnsPinSnapshot:
    host: str = field(repr=False)
    port: int
    addresses: tuple[str, ...] = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.host, str) or not self.host or not self.host.isascii():
            raise ValueError("DNS pin host must be a canonical ASCII host")
        if self.host != self.host.lower() or self.host.endswith("."):
            raise ValueError("DNS pin host is not canonical")
        try:
            canonical_host, _ = _canonical_host(self.host)
        except WebUrlPolicyError as exc:
            raise ValueError("DNS pin host is invalid") from exc
        if canonical_host != self.host:
            raise ValueError("DNS pin host is not canonical")
        _integer_port(self.port, field="DNS pin port")
        raw_addresses = tuple(self.addresses)
        if not raw_addresses or len(raw_addresses) > _MAX_RESOLVED_ADDRESSES:
            raise ValueError("DNS pin must contain a bounded non-empty address set")
        try:
            addresses = tuple(
                sorted(
                    {_canonical_global_ip(value) for value in raw_addresses},
                    key=_address_sort_key,
                )
            )
        except (_InvalidAddress, _UnsafeAddress) as exc:
            raise ValueError("DNS pin contains an invalid or unsafe address") from exc
        object.__setattr__(self, "addresses", addresses)

    @property
    def fingerprint(self) -> str:
        return _canonical_json_fingerprint(
            {
                "addresses": self.addresses,
                "host": self.host,
                "port": self.port,
                "schema_version": 1,
            }
        )

    def contains(self, peer_ip: str) -> bool:
        try:
            candidate = _canonical_global_ip(peer_ip)
        except (_InvalidAddress, _UnsafeAddress):
            return False
        return candidate in self.addresses


@dataclass(frozen=True, slots=True)
class ResolvedWebUrl:
    canonical_url: str = field(repr=False)
    scheme: str
    host: str = field(repr=False)
    port: int
    authority: str = field(repr=False)
    request_target: str = field(repr=False)
    pin: DnsPinSnapshot = field(repr=False)

    def __post_init__(self) -> None:
        if self.scheme not in _DEFAULT_PORTS:
            raise ValueError("ResolvedWebUrl scheme is invalid")
        _integer_port(self.port, field="ResolvedWebUrl port")
        if self.pin.host != self.host or self.pin.port != self.port:
            raise ValueError("ResolvedWebUrl DNS pin does not match its authority")
        if not self.request_target.startswith("/") or "#" in self.request_target:
            raise ValueError("ResolvedWebUrl request_target is invalid")
        default_port = _DEFAULT_PORTS[self.scheme]
        bracketed_host = f"[{self.host}]" if ":" in self.host else self.host
        expected_authority = (
            bracketed_host
            if self.port == default_port
            else f"{bracketed_host}:{self.port}"
        )
        if self.authority != expected_authority:
            raise ValueError("ResolvedWebUrl authority is inconsistent")
        if self.canonical_url != (
            f"{self.scheme}://{self.authority}{self.request_target}"
        ):
            raise ValueError("ResolvedWebUrl canonical_url is inconsistent")

    @property
    def pinned_addresses(self) -> tuple[str, ...]:
        return self.pin.addresses


class WebUrlPolicy:
    """Deep SSRF Module: canonicalize, resolve, pin and revalidate every hop."""

    def __init__(
        self,
        resolver: WebDnsResolver,
        *,
        allowed_scheme_ports: Mapping[str, Collection[int]] | None = None,
        max_url_bytes: int = DEFAULT_WEB_RESEARCH_LIMITS.max_url_bytes,
        max_resolved_addresses: int = _MAX_RESOLVED_ADDRESSES,
    ) -> None:
        if not isinstance(resolver, WebDnsResolver):
            raise TypeError("resolver must satisfy WebDnsResolver")
        configured_ports = (
            {
                "http": frozenset({80}),
                "https": frozenset({443}),
            }
            if allowed_scheme_ports is None
            else allowed_scheme_ports
        )
        if not isinstance(configured_ports, Mapping) or not configured_ports:
            raise TypeError("allowed_scheme_ports must be a non-empty mapping")
        normalized_ports: dict[str, frozenset[int]] = {}
        for raw_scheme, raw_ports in configured_ports.items():
            if not isinstance(raw_scheme, str):
                raise TypeError("allowed scheme names must be strings")
            scheme = raw_scheme.strip().lower()
            if scheme not in _DEFAULT_PORTS or scheme != raw_scheme:
                raise ValueError("allowed schemes must be canonical http or https")
            ports = frozenset(
                _integer_port(port, field="allowed port") for port in raw_ports
            )
            if not ports:
                raise ValueError("allowed scheme port sets cannot be empty")
            normalized_ports[scheme] = ports
        if isinstance(max_url_bytes, bool) or not isinstance(max_url_bytes, int):
            raise TypeError("max_url_bytes must be an integer")
        if not 1 <= max_url_bytes <= _MAX_URL_BYTES:
            raise ValueError(f"max_url_bytes must be between 1 and {_MAX_URL_BYTES}")
        if isinstance(max_resolved_addresses, bool) or not isinstance(
            max_resolved_addresses,
            int,
        ):
            raise TypeError("max_resolved_addresses must be an integer")
        if not 1 <= max_resolved_addresses <= _MAX_RESOLVED_ADDRESSES:
            raise ValueError(
                "max_resolved_addresses must be between 1 and "
                f"{_MAX_RESOLVED_ADDRESSES}"
            )
        self._resolver = resolver
        self.allowed_scheme_ports = MappingProxyType(normalized_ports)
        self.max_url_bytes = max_url_bytes
        self.max_resolved_addresses = max_resolved_addresses
        self._state_lock = threading.Lock()
        self._closed = False

    def resolve(
        self,
        url: str,
        *,
        deadline_at: float | None = None,
        cancellation_probe: CancellationProbe | None = None,
    ) -> ResolvedWebUrl:
        with self._state_lock:
            if self._closed:
                raise WebUrlPolicyError(
                    WebUrlPolicyCode.DNS_RESOLUTION_FAILED,
                    "URL policy is closed",
                )
        _raise_if_cancelled(cancellation_probe)
        _validate_raw_url_text(url, field="URL", max_bytes=self.max_url_bytes)
        try:
            parsed = urlsplit(url)
        except ValueError as exc:
            raise WebUrlPolicyError(
                WebUrlPolicyCode.INVALID_URL,
                "URL cannot be parsed",
            ) from exc
        scheme = parsed.scheme.lower()
        if scheme not in _DEFAULT_PORTS:
            raise WebUrlPolicyError(
                WebUrlPolicyCode.SCHEME_DENIED,
                "Only http and https URLs are allowed",
            )
        if not parsed.netloc:
            raise WebUrlPolicyError(
                WebUrlPolicyCode.HOST_DENIED,
                "URL host is missing",
            )
        if (
            "@" in parsed.netloc
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise WebUrlPolicyError(
                WebUrlPolicyCode.USERINFO_DENIED,
                "URL user information is not allowed",
            )
        try:
            parsed_port = parsed.port
            parsed_host = parsed.hostname
        except ValueError as exc:
            raise WebUrlPolicyError(
                WebUrlPolicyCode.INVALID_URL,
                "URL authority is invalid",
            ) from exc
        host, is_ip_literal = _canonical_host(parsed_host or "")
        port = parsed_port if parsed_port is not None else _DEFAULT_PORTS[scheme]
        if port not in self.allowed_scheme_ports.get(scheme, frozenset()):
            raise WebUrlPolicyError(
                WebUrlPolicyCode.PORT_DENIED,
                "URL port is outside the allowlist",
                safe_details={"port": port},
            )

        path = _remove_dot_segments(
            _canonicalize_url_component(parsed.path or "/", safe=_PATH_SAFE)
        )
        query = _canonicalize_url_component(parsed.query, safe=_QUERY_SAFE)
        bracketed_host = f"[{host}]" if ":" in host else host
        authority = (
            bracketed_host
            if port == _DEFAULT_PORTS[scheme]
            else f"{bracketed_host}:{port}"
        )
        canonical_url = urlunsplit((scheme, authority, path, query, ""))
        if _utf8_size(canonical_url, field="canonical URL") > self.max_url_bytes:
            raise WebUrlPolicyError(
                WebUrlPolicyCode.URL_TOO_LONG,
                "Canonical URL exceeds its size limit",
                safe_details={"max_url_bytes": self.max_url_bytes},
            )

        if is_ip_literal:
            addresses = (host,)
        else:
            addresses = self._resolve_all(
                host,
                port,
                deadline_at=deadline_at,
                cancellation_probe=cancellation_probe,
            )
        _raise_if_cancelled(cancellation_probe)
        pin = DnsPinSnapshot(host=host, port=port, addresses=addresses)
        request_target = path if not query else f"{path}?{query}"
        return ResolvedWebUrl(
            canonical_url=canonical_url,
            scheme=scheme,
            host=host,
            port=port,
            authority=authority,
            request_target=request_target,
            pin=pin,
        )

    def validate_redirect(
        self,
        base: ResolvedWebUrl | str,
        location: str,
        *,
        deadline_at: float | None = None,
        cancellation_probe: CancellationProbe | None = None,
    ) -> ResolvedWebUrl:
        _raise_if_cancelled(cancellation_probe)
        if isinstance(base, ResolvedWebUrl):
            base_url = base.canonical_url
        elif isinstance(base, str):
            base_url = self.resolve(
                base,
                deadline_at=deadline_at,
                cancellation_probe=cancellation_probe,
            ).canonical_url
        else:
            raise TypeError("base must be ResolvedWebUrl or str")
        try:
            _validate_raw_url_text(
                location,
                field="redirect location",
                max_bytes=self.max_url_bytes,
            )
            redirect_url = urljoin(base_url, location)
        except WebUrlPolicyError as exc:
            if exc.code is WebUrlPolicyCode.URL_TOO_LONG:
                raise
            raise WebUrlPolicyError(
                WebUrlPolicyCode.REDIRECT_DENIED,
                "Redirect location is invalid",
            ) from exc
        except (TypeError, ValueError) as exc:
            raise WebUrlPolicyError(
                WebUrlPolicyCode.REDIRECT_DENIED,
                "Redirect location is invalid",
            ) from exc
        return self.resolve(
            redirect_url,
            deadline_at=deadline_at,
            cancellation_probe=cancellation_probe,
        )

    def verify_peer(self, resolved: ResolvedWebUrl, peer_ip: str) -> None:
        if not isinstance(resolved, ResolvedWebUrl):
            raise TypeError("resolved must be ResolvedWebUrl")
        try:
            candidate = _canonical_global_ip(peer_ip)
        except (_InvalidAddress, _UnsafeAddress) as exc:
            raise WebUrlPolicyError(
                WebUrlPolicyCode.ADDRESS_DENIED,
                "Connected peer address is not globally routable",
            ) from exc
        if candidate not in resolved.pin.addresses:
            raise WebUrlPolicyError(
                WebUrlPolicyCode.DNS_PIN_MISMATCH,
                "Connected peer does not match the DNS pin snapshot",
            )

    def close(self) -> None:
        """Idempotently close the owned DNS Adapter, when it is closable."""

        with self._state_lock:
            if self._closed:
                return
            self._closed = True
        close = getattr(self._resolver, "close", None)
        if callable(close):
            close()

    def _resolve_all(
        self,
        host: str,
        port: int,
        *,
        deadline_at: float | None,
        cancellation_probe: CancellationProbe | None,
    ) -> tuple[str, ...]:
        try:
            raw_addresses = self._resolver.resolve(
                host,
                port,
                deadline_at=deadline_at,
                cancellation_probe=cancellation_probe,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise WebUrlPolicyError(
                WebUrlPolicyCode.DNS_RESOLUTION_FAILED,
                "DNS resolution failed",
            ) from exc
        if isinstance(raw_addresses, (str, bytes)):
            raise WebUrlPolicyError(
                WebUrlPolicyCode.DNS_RESOLUTION_FAILED,
                "DNS resolver returned an invalid address set",
            )
        try:
            values = tuple(raw_addresses)
        except TypeError as exc:
            raise WebUrlPolicyError(
                WebUrlPolicyCode.DNS_RESOLUTION_FAILED,
                "DNS resolver returned an invalid address set",
            ) from exc
        if not values or len(values) > self.max_resolved_addresses:
            raise WebUrlPolicyError(
                WebUrlPolicyCode.DNS_RESOLUTION_FAILED,
                "DNS resolver returned an empty or oversized address set",
                safe_details={"max_addresses": self.max_resolved_addresses},
            )
        canonical: set[str] = set()
        for value in values:
            try:
                canonical.add(_canonical_global_ip(value))
            except _InvalidAddress as exc:
                raise WebUrlPolicyError(
                    WebUrlPolicyCode.DNS_RESOLUTION_FAILED,
                    "DNS resolver returned an invalid address",
                ) from exc
            except _UnsafeAddress as exc:
                raise WebUrlPolicyError(
                    WebUrlPolicyCode.ADDRESS_DENIED,
                    "DNS resolver returned a non-global address",
                ) from exc
        return tuple(sorted(canonical, key=_address_sort_key))


__all__ = [
    "CancellationProbe",
    "DnsPinSnapshot",
    "ResolvedWebUrl",
    "SystemWebDnsResolver",
    "WebDnsResolver",
    "WebUrlPolicy",
    "WebUrlPolicyCode",
    "WebUrlPolicyError",
]
