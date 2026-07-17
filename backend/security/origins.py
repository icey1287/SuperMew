"""Canonical HTTP Origin parsing shared by settings and auth ingress."""

from __future__ import annotations

from urllib.parse import urlsplit


def canonical_http_origin(
    value: str | None,
    *,
    allow_resource_url: bool = False,
) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = urlsplit(value.strip())
        port = parsed.port
    except ValueError:
        return None
    scheme = parsed.scheme.lower()
    hostname = parsed.hostname
    if (
        scheme not in {"http", "https"}
        or hostname is None
        or parsed.username is not None
        or parsed.password is not None
    ):
        return None
    if not allow_resource_url and (
        parsed.path not in {"", "/"} or parsed.query or parsed.fragment
    ):
        return None

    hostname = hostname.casefold()
    host = f"[{hostname}]" if ":" in hostname else hostname
    if port is None or (scheme, port) in {("http", 80), ("https", 443)}:
        return f"{scheme}://{host}"
    return f"{scheme}://{host}:{port}"


__all__ = ["canonical_http_origin"]
