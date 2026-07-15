import asyncio
import unittest
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import httpx
import openai

from backend.core.errors import AppError, ErrorCode, error_payload
from backend.providers import (
    ProviderCallContext,
    ProviderCode,
    ProviderError,
    ProviderOperation,
    classify_provider_exception,
)


class FakeResponse:
    def __init__(self, status_code, headers=None, text=""):
        self.status_code = status_code
        self.headers = headers or {}
        self.text = text


class FakeHttpError(RuntimeError):
    def __init__(self, status_code, *, headers=None, body="secret upstream body"):
        super().__init__(body)
        self.response = FakeResponse(status_code, headers=headers, text=body)


def _context(operation, provider="test-provider"):
    return ProviderCallContext(provider=provider, operation=operation)


class ProviderErrorTaxonomyTests(unittest.TestCase):
    def test_timeout_code_is_selected_by_operation(self):
        cases = {
            ProviderOperation.EMBEDDING: ProviderCode.EMBEDDING_UNAVAILABLE,
            ProviderOperation.VECTOR_SEARCH: ProviderCode.VECTOR_STORE_UNAVAILABLE,
            ProviderOperation.RERANK: ProviderCode.RERANK_TIMEOUT,
            ProviderOperation.MODEL: ProviderCode.MODEL_TIMEOUT,
            ProviderOperation.TOOL: ProviderCode.TOOL_TIMEOUT,
        }

        for operation, expected in cases.items():
            with self.subTest(operation=operation):
                error = classify_provider_exception(
                    TimeoutError("raw timeout detail"),
                    context=_context(operation),
                )
                self.assertEqual(expected, error.code)
                self.assertTrue(error.retryable)
                self.assertNotIn("raw timeout detail", error.message)

    def test_openai_api_timeout_is_classified_by_type(self):
        raw = openai.APITimeoutError(
            request=httpx.Request("POST", "https://provider.test/v1/chat")
        )

        error = classify_provider_exception(
            raw,
            context=_context(ProviderOperation.MODEL),
        )

        self.assertEqual(ProviderCode.MODEL_TIMEOUT, error.code)

    def test_httpx_timeout_in_exception_cause_is_classified_without_message_parsing(
        self,
    ):
        request = httpx.Request("POST", "https://provider.test/v1/chat")
        timeout = httpx.ReadTimeout("secret provider detail", request=request)
        try:
            raise RuntimeError("generic wrapper") from timeout
        except RuntimeError as wrapped:
            error = classify_provider_exception(
                wrapped,
                context=_context(ProviderOperation.MODEL),
            )

        self.assertEqual(ProviderCode.MODEL_TIMEOUT, error.code)
        self.assertNotIn("secret", error.message)

    def test_rate_limit_code_and_retry_after_are_operation_specific(self):
        cases = {
            ProviderOperation.MODEL: ProviderCode.MODEL_RATE_LIMITED,
            ProviderOperation.RERANK: ProviderCode.RERANK_RATE_LIMITED,
            ProviderOperation.EMBEDDING: ProviderCode.EMBEDDING_UNAVAILABLE,
        }

        for operation, expected in cases.items():
            with self.subTest(operation=operation):
                error = classify_provider_exception(
                    FakeHttpError(429, headers={"Retry-After": "1.25"}),
                    context=_context(operation),
                    attempts=2,
                    max_attempts=4,
                )
                self.assertEqual(expected, error.code)
                self.assertEqual(1.25, error.retry_after_seconds)
                self.assertEqual(2, error.attempts)
                self.assertEqual(4, error.max_attempts)

    def test_http_date_retry_after_is_supported(self):
        retry_at = datetime.now(UTC) + timedelta(seconds=30)
        error = classify_provider_exception(
            FakeHttpError(429, headers={"Retry-After": format_datetime(retry_at)}),
            context=_context(ProviderOperation.MODEL),
        )

        self.assertIsNotNone(error.retry_after_seconds)
        self.assertGreater(error.retry_after_seconds, 25)
        self.assertLessEqual(error.retry_after_seconds, 30)

    def test_authentication_and_policy_failures_are_not_retryable(self):
        auth = classify_provider_exception(
            FakeHttpError(401),
            context=_context(ProviderOperation.MODEL),
        )
        denied = classify_provider_exception(
            AppError(ErrorCode.PERMISSION_DENIED, "raw policy detail", status_code=403),
            context=_context(ProviderOperation.TOOL),
        )

        self.assertEqual(ProviderCode.PROVIDER_AUTHENTICATION_FAILED, auth.code)
        self.assertFalse(auth.retryable)
        self.assertEqual(ProviderCode.POLICY_DENIED, denied.code)
        self.assertFalse(denied.retryable)

    def test_public_payload_never_contains_raw_exception_or_response_body(self):
        raw_secret = "sk-secret-raw-upstream-body"
        error = classify_provider_exception(
            FakeHttpError(503, body=raw_secret),
            context=_context(ProviderOperation.MODEL),
        )
        payload = error_payload(error)

        self.assertEqual("MODEL_UNAVAILABLE", payload["error"]["code"])
        self.assertEqual("provider", payload["error"]["category"])
        self.assertEqual("model", payload["error"]["stage"])
        self.assertEqual("test-provider", payload["error"]["provider"])
        self.assertNotIn(raw_secret, str(error))
        self.assertNotIn(raw_secret, repr(payload))
        self.assertEqual("test-provider", payload["error"]["details"]["provider"])

    def test_classifier_does_not_parse_status_from_exception_message(self):
        error = classify_provider_exception(
            RuntimeError("Error code: 429 raw payload"),
            context=_context(ProviderOperation.MODEL),
        )

        self.assertEqual(ProviderCode.MODEL_UNAVAILABLE, error.code)

    def test_cancelled_error_is_preserved(self):
        cancelled = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError) as raised:
            classify_provider_exception(
                cancelled,
                context=_context(ProviderOperation.MODEL),
            )
        self.assertIs(cancelled, raised.exception)

    def test_no_knowledge_is_not_a_provider_failure_code(self):
        values = {item.value for item in ProviderCode}
        self.assertNotIn("NO_KNOWLEDGE", values)
        self.assertNotIn("INSUFFICIENT_EVIDENCE", values)

    def test_provider_error_factory_uses_stable_safe_contract(self):
        context = _context(
            ProviderOperation.TOOL, provider="unsafe/provider?token=secret"
        )
        error = ProviderError.policy_denied(context)

        self.assertEqual(ProviderCode.POLICY_DENIED, error.code)
        self.assertEqual(403, error.status_code)
        self.assertFalse(error.retryable)
        self.assertEqual("unknown-provider", error.safe_details["provider"])
        self.assertNotIn("secret", repr(error.safe_details))

    def test_safe_snapshot_round_trips_without_exception_objects(self):
        original = ProviderError.from_code(
            ProviderCode.RERANK_RATE_LIMITED,
            provider="rerank-service",
            operation=ProviderOperation.RERANK,
            retry_after_seconds=2.5,
            attempts=3,
            max_attempts=3,
        )

        snapshot = original.to_snapshot()
        restored = ProviderError.from_snapshot(snapshot)

        self.assertEqual(
            {
                "code": "RERANK_RATE_LIMITED",
                "provider": "rerank-service",
                "operation": "rerank",
                "retry_after_seconds": 2.5,
                "attempts": 3,
                "max_attempts": 3,
            },
            snapshot,
        )
        self.assertEqual(original.code, restored.code)
        self.assertEqual(original.message, restored.message)
        self.assertEqual(original.safe_details, restored.safe_details)


if __name__ == "__main__":
    unittest.main()
