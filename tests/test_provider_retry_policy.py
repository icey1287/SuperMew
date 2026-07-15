import asyncio
import time
import unittest

from backend.providers import (
    ProviderCallContext,
    ProviderCode,
    ProviderError,
    ProviderExecutor,
    ProviderOperation,
    ProviderPolicy,
)


class FakeResponse:
    def __init__(self, status_code, headers=None):
        self.status_code = status_code
        self.headers = headers or {}


class FakeHttpError(RuntimeError):
    def __init__(self, status_code, headers=None):
        super().__init__("raw upstream failure")
        self.response = FakeResponse(status_code, headers=headers)


class FakeTime:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds

    async def asleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds
        await asyncio.sleep(0)


def _context(operation=ProviderOperation.MODEL, **kwargs):
    return ProviderCallContext(provider="test-provider", operation=operation, **kwargs)


class ProviderPolicyTests(unittest.TestCase):
    def test_policy_rejects_unbounded_or_invalid_values(self):
        invalid = [
            {"max_attempts": 0},
            {"max_attempts": 101},
            {"initial_backoff_seconds": -1},
            {"initial_backoff_seconds": 2, "max_backoff_seconds": 1},
            {"max_retry_after_seconds": -1},
            {"backoff_multiplier": 0.5},
            {"cancellation_poll_seconds": 0},
        ]
        for kwargs in invalid:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                ProviderPolicy(**kwargs)


class SyncProviderExecutorTests(unittest.TestCase):
    def setUp(self):
        self.time = FakeTime()
        self.executor = ProviderExecutor(
            clock=self.time.clock,
            sleeper=self.time.sleep,
            async_sleeper=self.time.asleep,
        )

    def test_transient_failure_retries_with_bounded_exponential_backoff(self):
        calls = 0

        def operation():
            nonlocal calls
            calls += 1
            if calls < 3:
                raise FakeHttpError(503)
            return "ok"

        result = self.executor.call(
            operation,
            context=_context(),
            policy=ProviderPolicy(
                max_attempts=3,
                initial_backoff_seconds=0.1,
                max_backoff_seconds=1,
                backoff_multiplier=2,
            ),
        )

        self.assertEqual("ok", result)
        self.assertEqual(3, calls)
        self.assertEqual([0.1, 0.2], self.time.sleeps)

    def test_retry_after_is_honored_without_retrying_early(self):
        calls = 0

        def operation():
            nonlocal calls
            calls += 1
            if calls == 1:
                raise FakeHttpError(429, {"Retry-After": "5"})
            return "ok"

        result = self.executor.call(
            operation,
            context=_context(),
            policy=ProviderPolicy(
                max_attempts=2,
                initial_backoff_seconds=0.1,
                max_backoff_seconds=0.75,
                max_retry_after_seconds=10,
            ),
        )

        self.assertEqual("ok", result)
        self.assertEqual([5.0], self.time.sleeps)

    def test_retry_after_above_local_wait_limit_is_not_retried_early(self):
        calls = 0

        def operation():
            nonlocal calls
            calls += 1
            raise FakeHttpError(429, {"Retry-After": "120"})

        with self.assertRaises(ProviderError) as raised:
            self.executor.call(
                operation,
                context=_context(),
                policy=ProviderPolicy(max_attempts=3, max_retry_after_seconds=30),
            )

        self.assertEqual(ProviderCode.MODEL_RATE_LIMITED, raised.exception.code)
        self.assertEqual(1, calls)
        self.assertEqual([], self.time.sleeps)

    def test_exhaustion_preserves_typed_error_and_attempt_count(self):
        calls = 0

        def operation():
            nonlocal calls
            calls += 1
            raise FakeHttpError(429)

        with self.assertRaises(ProviderError) as raised:
            self.executor.call(
                operation,
                context=_context(),
                policy=ProviderPolicy(max_attempts=3),
            )

        self.assertEqual(ProviderCode.MODEL_RATE_LIMITED, raised.exception.code)
        self.assertEqual(3, raised.exception.attempts)
        self.assertEqual(3, raised.exception.max_attempts)
        self.assertEqual(3, calls)

    def test_authentication_and_policy_failures_are_not_retried(self):
        cases = [
            lambda context: FakeHttpError(401),
            lambda context: ProviderError.policy_denied(context),
        ]

        for factory in cases:
            with self.subTest(factory=factory):
                calls = 0
                context = _context(ProviderOperation.TOOL)

                def operation():
                    nonlocal calls
                    calls += 1
                    raise factory(context)

                with self.assertRaises(ProviderError) as raised:
                    self.executor.call(operation, context=context)
                self.assertFalse(raised.exception.retryable)
                self.assertEqual(1, calls)

    def test_retry_is_skipped_when_backoff_would_cross_deadline(self):
        calls = 0

        def operation():
            nonlocal calls
            calls += 1
            raise FakeHttpError(503)

        with self.assertRaises(ProviderError) as raised:
            self.executor.call(
                operation,
                context=_context(deadline=0.05),
                policy=ProviderPolicy(initial_backoff_seconds=0.1),
            )

        self.assertEqual(ProviderCode.PROVIDER_DEADLINE_EXCEEDED, raised.exception.code)
        self.assertEqual(1, calls)
        self.assertEqual([], self.time.sleeps)

    def test_cancel_during_backoff_aborts_without_another_attempt(self):
        calls = 0
        cancelled = False

        def cancellation():
            return cancelled

        def sleeper(seconds):
            nonlocal cancelled
            self.time.sleep(seconds)
            cancelled = True

        executor = ProviderExecutor(clock=self.time.clock, sleeper=sleeper)

        def operation():
            nonlocal calls
            calls += 1
            raise FakeHttpError(503)

        with self.assertRaises(asyncio.CancelledError):
            executor.call(
                operation,
                context=_context(cancellation=cancellation),
                policy=ProviderPolicy(
                    initial_backoff_seconds=0.2,
                    cancellation_poll_seconds=0.05,
                ),
            )

        self.assertEqual(1, calls)
        self.assertEqual([0.05], self.time.sleeps)


class AsyncProviderExecutorTests(unittest.IsolatedAsyncioTestCase):
    async def test_async_executor_retries_with_same_policy(self):
        fake_time = FakeTime()
        executor = ProviderExecutor(
            clock=fake_time.clock,
            sleeper=fake_time.sleep,
            async_sleeper=fake_time.asleep,
        )
        calls = 0

        async def operation():
            nonlocal calls
            calls += 1
            if calls == 1:
                raise TimeoutError("raw timeout")
            return "ok"

        result = await executor.acall(
            operation,
            context=_context(ProviderOperation.RERANK),
            policy=ProviderPolicy(max_attempts=2, initial_backoff_seconds=0.2),
        )

        self.assertEqual("ok", result)
        self.assertEqual(2, calls)
        self.assertEqual([0.2], fake_time.sleeps)

    async def test_async_deadline_actively_stops_inflight_call(self):
        executor = ProviderExecutor()
        calls = 0

        async def operation():
            nonlocal calls
            calls += 1
            await asyncio.sleep(1)
            return "late"

        with self.assertRaises(ProviderError) as raised:
            await executor.acall(
                operation,
                context=_context(deadline=time.monotonic() + 0.01),
            )

        self.assertEqual(ProviderCode.PROVIDER_DEADLINE_EXCEEDED, raised.exception.code)
        self.assertEqual(1, calls)

    async def test_cancelled_error_is_never_normalized_or_retried(self):
        calls = 0

        async def operation():
            nonlocal calls
            calls += 1
            raise asyncio.CancelledError

        with self.assertRaises(asyncio.CancelledError):
            await ProviderExecutor().acall(operation, context=_context())
        self.assertEqual(1, calls)

    async def test_async_cancellation_during_backoff_stops_retry(self):
        fake_time = FakeTime()
        cancelled = False
        calls = 0

        def cancellation():
            return cancelled

        async def sleeper(seconds):
            nonlocal cancelled
            await fake_time.asleep(seconds)
            cancelled = True

        executor = ProviderExecutor(clock=fake_time.clock, async_sleeper=sleeper)

        async def operation():
            nonlocal calls
            calls += 1
            raise FakeHttpError(503)

        with self.assertRaises(asyncio.CancelledError):
            await executor.acall(
                operation,
                context=_context(cancellation=cancellation),
                policy=ProviderPolicy(
                    initial_backoff_seconds=0.2,
                    cancellation_poll_seconds=0.05,
                ),
            )

        self.assertEqual(1, calls)
        self.assertEqual([0.05], fake_time.sleeps)


if __name__ == "__main__":
    unittest.main()
