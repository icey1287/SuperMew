from __future__ import annotations

import asyncio
import re
import unittest

from backend.rate_limits.adapters import (
    InMemoryRateLimitAdapter,
    RedisRateLimitAdapter,
)
from backend.rate_limits.contracts import (
    RateLimitCheck,
    RateLimitPolicy,
    RateLimitSnapshot,
    RateLimitUnavailable,
)
from backend.rate_limits.limiter import RateLimiter
from backend.rate_limits.policy import (
    AUTH_LOGIN_POLICY,
    AUTH_LOGOUT_POLICY,
    AUTH_REFRESH_POLICY,
    AUTH_REGISTER_POLICY,
    DOCUMENT_UPLOAD_POLICY,
    GENERAL_API_POLICY,
    HITL_RESUME_POLICY,
    THREAD_RUN_POLICY,
    RoutePolicyMatcher,
)


HMAC_KEY = b"rate-limit-test-key-material-32-bytes-minimum"


class ManualClock:
    def __init__(self, value: float) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


class RecordingAdapter:
    def __init__(self) -> None:
        self.keys: list[str] = []
        self.closed = False

    async def consume(self, *, key, policy, cost):
        self.keys.append(key)
        return RateLimitSnapshot(
            allowed=True,
            remaining=policy.limit - cost,
            reset_at=120,
            observed_at=100,
        )

    async def close(self):
        self.closed = True


class FakeRedisClient:
    def __init__(self, response=None, error: Exception | None = None) -> None:
        self.response = response or [1, 4, 120_000, 115_000]
        self.error = error
        self.calls: list[tuple[str, int, tuple[object, ...]]] = []
        self.closed = False

    async def eval(self, script, numkeys, *keys_and_args):
        self.calls.append((script, numkeys, keys_and_args))
        if self.error is not None:
            raise self.error
        return self.response

    async def aclose(self):
        self.closed = True


class RateLimitContractTests(unittest.TestCase):
    def test_check_repr_redacts_client_identity(self):
        check = RateLimitCheck(
            method="post",
            path="/auth/login?from=browser",
            client_identity="Bearer highly-sensitive-token",
        )

        self.assertEqual("POST", check.method)
        self.assertEqual("/auth/login", check.path)
        self.assertNotIn("highly-sensitive-token", repr(check))

    def test_policy_rejects_invalid_limits_and_identity_is_bounded(self):
        with self.assertRaises(ValueError):
            RateLimitPolicy(id="Invalid Policy", limit=1, window_seconds=1)
        with self.assertRaises(ValueError):
            RateLimitPolicy(id="valid", limit=0, window_seconds=1)
        with self.assertRaises(ValueError):
            RateLimitCheck(method="POST", path="/", client_identity="")


class RoutePolicyMatcherTests(unittest.TestCase):
    def test_sensitive_routes_select_their_specific_policy(self):
        matcher = RoutePolicyMatcher()
        cases = (
            ("POST", "/auth/login", AUTH_LOGIN_POLICY),
            ("POST", "/auth/register/", AUTH_REGISTER_POLICY),
            ("POST", "/auth/refresh", AUTH_REFRESH_POLICY),
            ("POST", "/auth/logout", AUTH_LOGOUT_POLICY),
            ("POST", "/auth/logout-all", AUTH_LOGOUT_POLICY),
            ("POST", "/v1/threads/thread_1/runs", THREAD_RUN_POLICY),
            ("POST", "/v1/threads/thread_1/runs/stream", THREAD_RUN_POLICY),
            ("POST", "/v1/runs/run_1/resume", HITL_RESUME_POLICY),
            ("POST", "/documents/upload/async", DOCUMENT_UPLOAD_POLICY),
        )

        for method, path, expected in cases:
            with self.subTest(path=path):
                self.assertIs(expected, matcher.match(method=method, path=path))

        self.assertEqual(
            (120, 60),
            (AUTH_REFRESH_POLICY.limit, AUTH_REFRESH_POLICY.window_seconds),
        )
        self.assertEqual(
            (120, 60),
            (AUTH_LOGOUT_POLICY.limit, AUTH_LOGOUT_POLICY.window_seconds),
        )

    def test_unknown_or_wrong_method_routes_use_general_fallback(self):
        matcher = RoutePolicyMatcher()

        self.assertIs(
            GENERAL_API_POLICY,
            matcher.match(method="GET", path="/auth/login"),
        )
        self.assertIs(
            GENERAL_API_POLICY,
            matcher.match(method="POST", path="/documents/upload"),
        )
        self.assertIs(
            GENERAL_API_POLICY,
            matcher.match(method="POST", path="/removed-route"),
        )
        self.assertIs(
            GENERAL_API_POLICY,
            matcher.match(method="POST", path="/v1/threads"),
        )


class InMemoryRateLimitTests(unittest.IsolatedAsyncioTestCase):
    async def test_fixed_window_limit_retry_reset_and_expiry(self):
        clock = ManualClock(100)
        policy = RateLimitPolicy(id="test-window", limit=2, window_seconds=10)
        matcher = RoutePolicyMatcher(rules=(), fallback=policy)
        limiter = RateLimiter(
            InMemoryRateLimitAdapter(clock=clock),
            identity_hmac_key=HMAC_KEY,
            matcher=matcher,
        )
        check = RateLimitCheck(method="GET", path="/resource", client_identity="alice")

        first = await limiter.check(check)
        second = await limiter.check(check)
        denied = await limiter.check(check)

        self.assertTrue(first.allowed)
        self.assertEqual(
            (2, 1, 0, 110),
            (first.limit, first.remaining, first.retry_after, first.reset),
        )
        self.assertTrue(second.allowed)
        self.assertEqual(0, second.remaining)
        self.assertFalse(denied.allowed)
        self.assertEqual(
            (0, 10, 110), (denied.remaining, denied.retry_after, denied.reset)
        )

        clock.value = 109.25
        almost_reset = await limiter.check(check)
        self.assertFalse(almost_reset.allowed)
        self.assertEqual(1, almost_reset.retry_after)

        clock.value = 110
        renewed = await limiter.check(check)
        self.assertTrue(renewed.allowed)
        self.assertEqual((1, 120), (renewed.remaining, renewed.reset))

    async def test_concurrent_checks_consume_one_atomic_window(self):
        clock = ManualClock(500)
        policy = RateLimitPolicy(id="test-concurrent", limit=7, window_seconds=60)
        limiter = RateLimiter(
            InMemoryRateLimitAdapter(clock=clock),
            identity_hmac_key=HMAC_KEY,
            matcher=RoutePolicyMatcher(rules=(), fallback=policy),
        )
        check = RateLimitCheck(method="POST", path="/work", client_identity="alice")

        decisions = await asyncio.gather(*(limiter.check(check) for _ in range(100)))

        self.assertEqual(7, sum(decision.allowed for decision in decisions))
        self.assertEqual(93, sum(not decision.allowed for decision in decisions))
        self.assertEqual(0, decisions[-1].remaining)

    async def test_different_identities_are_isolated(self):
        policy = RateLimitPolicy(id="test-identity", limit=1, window_seconds=60)
        limiter = RateLimiter(
            InMemoryRateLimitAdapter(clock=ManualClock(20)),
            identity_hmac_key=HMAC_KEY,
            matcher=RoutePolicyMatcher(rules=(), fallback=policy),
        )

        alice = RateLimitCheck(method="GET", path="/", client_identity="alice")
        bob = RateLimitCheck(method="GET", path="/", client_identity="bob")
        self.assertTrue((await limiter.check(alice)).allowed)
        self.assertFalse((await limiter.check(alice)).allowed)
        self.assertTrue((await limiter.check(bob)).allowed)

    async def test_close_is_idempotent_and_fail_closed(self):
        limiter = RateLimiter(
            InMemoryRateLimitAdapter(clock=ManualClock(1)),
            identity_hmac_key=HMAC_KEY,
        )
        await limiter.close()
        await limiter.close()

        with self.assertRaises(RateLimitUnavailable) as raised:
            await limiter.check(
                RateLimitCheck(method="GET", path="/", client_identity="alice")
            )
        self.assertEqual("closed", raised.exception.reason)

    async def test_invalid_clock_fails_closed_with_typed_error(self):
        def broken_clock():
            raise RuntimeError("clock internals")

        limiter = RateLimiter(
            InMemoryRateLimitAdapter(clock=broken_clock),
            identity_hmac_key=HMAC_KEY,
        )

        with self.assertRaises(RateLimitUnavailable) as raised:
            await limiter.check(
                RateLimitCheck(method="GET", path="/", client_identity="alice")
            )
        self.assertEqual("clock_invalid", raised.exception.reason)


class RateLimitKeyTests(unittest.IsolatedAsyncioTestCase):
    async def test_storage_key_contains_only_policy_and_hmac_identity(self):
        adapter = RecordingAdapter()
        limiter = RateLimiter(
            adapter,
            identity_hmac_key=HMAC_KEY,
            key_prefix="test",
        )
        secret_identity = "alice@example.com:Bearer-secret-token"

        await limiter.check(
            RateLimitCheck(
                method="POST",
                path="/auth/login",
                client_identity=secret_identity,
            )
        )

        self.assertEqual(1, len(adapter.keys))
        key = adapter.keys[0]
        self.assertNotIn("alice", key)
        self.assertNotIn("Bearer", key)
        self.assertNotIn("secret-token", key)
        self.assertRegex(
            key,
            re.compile(r"^test:rate_limit:v1:auth-login:[0-9a-f]{64}$"),
        )

    async def test_same_identity_has_policy_scoped_fingerprints(self):
        adapter = RecordingAdapter()
        limiter = RateLimiter(adapter, identity_hmac_key=HMAC_KEY)

        await limiter.check(
            RateLimitCheck(method="POST", path="/auth/login", client_identity="alice")
        )
        await limiter.check(
            RateLimitCheck(
                method="POST",
                path="/auth/register",
                client_identity="alice",
            )
        )

        self.assertNotEqual(adapter.keys[0], adapter.keys[1])


class RedisRateLimitTests(unittest.IsolatedAsyncioTestCase):
    async def test_redis_adapter_uses_one_atomic_lua_evaluation(self):
        client = FakeRedisClient(response=[1, 4, 120_000, 115_000])
        adapter = RedisRateLimitAdapter(client=client, close_client=True)
        policy = RateLimitPolicy(id="redis-test", limit=5, window_seconds=60)

        snapshot = await adapter.consume(key="opaque-key", policy=policy, cost=1)

        self.assertTrue(snapshot.allowed)
        self.assertEqual(4, snapshot.remaining)
        self.assertEqual((120, 115), (snapshot.reset_at, snapshot.observed_at))
        self.assertEqual(1, len(client.calls))
        script, numkeys, arguments = client.calls[0]
        self.assertEqual(1, numkeys)
        self.assertEqual(("opaque-key", 5, 60_000, 1), arguments)
        self.assertIn("redis.call('TIME')", script)
        self.assertIn("redis.call('HSET'", script)
        self.assertIn("redis.call('PEXPIREAT'", script)
        self.assertNotIn("INCR", script)

        await adapter.close()
        self.assertTrue(client.closed)
        await adapter.close()

    async def test_redis_failure_is_typed_and_does_not_echo_key(self):
        client = FakeRedisClient(error=ConnectionError("redis endpoint secret"))
        adapter = RedisRateLimitAdapter(client=client)

        with self.assertRaises(RateLimitUnavailable) as raised:
            await adapter.consume(
                key="opaque-secret-key",
                policy=RateLimitPolicy(id="redis-test", limit=1, window_seconds=1),
                cost=1,
            )

        self.assertEqual("RATE_LIMIT_UNAVAILABLE", raised.exception.code.value)
        self.assertNotIn("opaque-secret-key", str(raised.exception))
        self.assertEqual(
            {"adapter": "redis", "reason": "unavailable"}, raised.exception.safe_details
        )

    async def test_closed_redis_adapter_fails_closed(self):
        adapter = RedisRateLimitAdapter(client=FakeRedisClient())
        await adapter.close()

        with self.assertRaises(RateLimitUnavailable):
            await adapter.consume(
                key="opaque-key",
                policy=RateLimitPolicy(id="redis-test", limit=1, window_seconds=1),
                cost=1,
            )


if __name__ == "__main__":
    unittest.main()
