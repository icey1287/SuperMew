import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.core.errors import (
    ErrorCode,
    PublicError,
    deserialize_public_error,
    error_payload,
    install_exception_handlers,
    public_error_from_exception,
    serialize_public_error,
)
from backend.providers import ProviderCode, ProviderError, ProviderOperation


class FakeProviderError(Exception):
    def __init__(self):
        super().__init__("secret upstream body token=abc")
        self.public_error = PublicError(
            code=ErrorCode.MODEL_RATE_LIMITED,
            message="模型服务繁忙，请稍后重试",
            status_code=429,
            retryable=True,
            category="provider",
            stage="generation",
            provider="ark",
            retry_after=2.5,
        )


class ErrorContractTests(unittest.TestCase):
    def test_unhandled_exception_is_redacted(self):
        error = public_error_from_exception(RuntimeError("secret upstream body"))
        self.assertEqual(ErrorCode.INTERNAL_ERROR, error.code)
        self.assertNotIn("secret upstream body", error.message)

    def test_provider_public_error_round_trips_without_raw_exception(self):
        error = public_error_from_exception(FakeProviderError())
        encoded = serialize_public_error(error)
        restored = deserialize_public_error(encoded)

        self.assertIsNotNone(restored)
        self.assertEqual(ErrorCode.MODEL_RATE_LIMITED, restored.code)
        self.assertEqual("provider", restored.category)
        self.assertEqual("generation", restored.stage)
        self.assertEqual("ark", restored.provider)
        self.assertEqual(2.5, restored.retry_after)
        self.assertNotIn("secret", encoded)
        self.assertNotIn("token=abc", error_payload(error)["error"]["message"])

    def test_http_handler_preserves_typed_fields_and_retry_after(self):
        app = FastAPI()
        install_exception_handlers(app)

        @app.get("/provider-failure")
        async def provider_failure():
            raise FakeProviderError()

        response = TestClient(app, raise_server_exceptions=False).get(
            "/provider-failure"
        )

        self.assertEqual(429, response.status_code)
        self.assertEqual("2.5", response.headers["retry-after"])
        payload = response.json()["error"]
        self.assertEqual("MODEL_RATE_LIMITED", payload["code"])
        self.assertTrue(payload["retryable"])
        self.assertEqual("provider", payload["category"])
        self.assertEqual("generation", payload["stage"])
        self.assertEqual("ark", payload["provider"])
        self.assertEqual(2.5, payload["retry_after"])
        self.assertNotIn("secret", response.text)
        self.assertNotIn("token=abc", response.text)

    def test_real_provider_error_is_enriched_for_cross_seam_contract(self):
        error = ProviderError.from_code(
            ProviderCode.RERANK_RATE_LIMITED,
            provider="rerank-service",
            operation=ProviderOperation.RERANK,
            retry_after_seconds=3,
            attempts=2,
            max_attempts=3,
        )

        public = public_error_from_exception(error)

        self.assertEqual("RERANK_RATE_LIMITED", public.code)
        self.assertEqual("provider", public.category)
        self.assertEqual("rerank", public.stage)
        self.assertEqual("rerank-service", public.provider)
        self.assertEqual(3, public.retry_after)
        self.assertEqual(2, public.details["attempts"])


if __name__ == "__main__":
    unittest.main()
