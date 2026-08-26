"""Deterministic route-to-policy matching for inbound request limits."""

from __future__ import annotations

import re
from dataclasses import dataclass

from backend.rate_limits.contracts import RateLimitPolicy


AUTH_LOGIN_POLICY = RateLimitPolicy(
    id="auth-login",
    limit=10,
    window_seconds=60,
)
AUTH_REGISTER_POLICY = RateLimitPolicy(
    id="auth-register",
    limit=5,
    window_seconds=3_600,
)
AUTH_REFRESH_POLICY = RateLimitPolicy(
    id="auth-refresh",
    # Refresh uses a host identity because the opaque credential rotates on
    # every success. Keep this as a coarse abuse ceiling so a large NAT does
    # not turn normal token renewal into an availability incident.
    limit=120,
    window_seconds=60,
)
AUTH_LOGOUT_POLICY = RateLimitPolicy(
    id="auth-logout",
    limit=120,
    window_seconds=60,
)
THREAD_RUN_POLICY = RateLimitPolicy(
    id="thread-run",
    limit=30,
    window_seconds=60,
)
HITL_RESUME_POLICY = RateLimitPolicy(
    id="hitl-resume",
    limit=30,
    window_seconds=60,
)
DOCUMENT_UPLOAD_POLICY = RateLimitPolicy(
    id="document-upload",
    limit=10,
    window_seconds=3_600,
)
GENERAL_API_POLICY = RateLimitPolicy(
    id="api-general",
    limit=120,
    window_seconds=60,
)


@dataclass(frozen=True, slots=True)
class RoutePolicyRule:
    methods: frozenset[str]
    path_pattern: re.Pattern[str]
    policy: RateLimitPolicy

    def __post_init__(self) -> None:
        normalized = frozenset(method.strip().upper() for method in self.methods)
        if not normalized:
            raise ValueError("a route policy rule must include at least one method")
        if any(re.fullmatch(r"[A-Z]{3,12}", method) is None for method in normalized):
            raise ValueError("route policy methods must be uppercase HTTP methods")
        object.__setattr__(self, "methods", normalized)
        if not isinstance(self.path_pattern, re.Pattern):
            raise TypeError("path_pattern must be a compiled regular expression")

    def matches(self, *, method: str, path: str) -> bool:
        return method in self.methods and self.path_pattern.fullmatch(path) is not None


DEFAULT_ROUTE_POLICY_RULES = (
    RoutePolicyRule(
        methods=frozenset({"POST"}),
        path_pattern=re.compile(r"/auth/login/?"),
        policy=AUTH_LOGIN_POLICY,
    ),
    RoutePolicyRule(
        methods=frozenset({"POST"}),
        path_pattern=re.compile(r"/auth/register/?"),
        policy=AUTH_REGISTER_POLICY,
    ),
    RoutePolicyRule(
        methods=frozenset({"POST"}),
        path_pattern=re.compile(r"/auth/refresh/?"),
        policy=AUTH_REFRESH_POLICY,
    ),
    RoutePolicyRule(
        methods=frozenset({"POST"}),
        path_pattern=re.compile(r"/auth/logout(?:-all)?/?"),
        policy=AUTH_LOGOUT_POLICY,
    ),
    RoutePolicyRule(
        methods=frozenset({"POST"}),
        path_pattern=re.compile(r"/v1/threads/[^/]+/runs(?:/stream)?/?"),
        policy=THREAD_RUN_POLICY,
    ),
    RoutePolicyRule(
        methods=frozenset({"POST"}),
        path_pattern=re.compile(r"/chat(?:/stream)?/?"),
        policy=THREAD_RUN_POLICY,
    ),
    RoutePolicyRule(
        methods=frozenset({"POST"}),
        path_pattern=re.compile(r"/v1/runs/[^/]+/resume/?"),
        policy=HITL_RESUME_POLICY,
    ),
    RoutePolicyRule(
        methods=frozenset({"POST"}),
        path_pattern=re.compile(r"/documents/upload/async/?"),
        policy=DOCUMENT_UPLOAD_POLICY,
    ),
)


class RoutePolicyMatcher:
    """Small Interface that hides route ordering and the default policy."""

    def __init__(
        self,
        *,
        rules: tuple[RoutePolicyRule, ...] = DEFAULT_ROUTE_POLICY_RULES,
        fallback: RateLimitPolicy = GENERAL_API_POLICY,
    ) -> None:
        self._rules = tuple(rules)
        self._fallback = fallback

    def match(self, *, method: str, path: str) -> RateLimitPolicy:
        normalized_method = method.strip().upper()
        normalized_path = path.split("?", 1)[0]
        for rule in self._rules:
            if rule.matches(method=normalized_method, path=normalized_path):
                return rule.policy
        return self._fallback


DEFAULT_ROUTE_POLICY_MATCHER = RoutePolicyMatcher()


__all__ = [
    "AUTH_LOGIN_POLICY",
    "AUTH_LOGOUT_POLICY",
    "AUTH_REFRESH_POLICY",
    "AUTH_REGISTER_POLICY",
    "DEFAULT_ROUTE_POLICY_MATCHER",
    "DEFAULT_ROUTE_POLICY_RULES",
    "DOCUMENT_UPLOAD_POLICY",
    "GENERAL_API_POLICY",
    "HITL_RESUME_POLICY",
    "RoutePolicyMatcher",
    "RoutePolicyRule",
    "THREAD_RUN_POLICY",
]
