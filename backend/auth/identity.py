"""Canonical username handling shared by Auth and abuse-prevention Adapters."""

from __future__ import annotations

import unicodedata


def normalize_username(value: str | None) -> str:
    """Return the stored/login identity without changing its case semantics."""

    return unicodedata.normalize("NFKC", value or "").strip()


def normalize_username_for_rate_limit(value: str | None) -> str:
    """Collapse harmless variants so an attacker cannot evade username limits."""

    return normalize_username(value).casefold()


__all__ = ["normalize_username", "normalize_username_for_rate_limit"]
