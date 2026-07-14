import unittest

from backend.core.errors import ErrorCode, public_error_from_exception


class ErrorContractTests(unittest.TestCase):
    def test_unhandled_exception_is_redacted(self):
        error = public_error_from_exception(RuntimeError("secret upstream body"))
        self.assertEqual(ErrorCode.INTERNAL_ERROR, error.code)
        self.assertNotIn("secret upstream body", error.message)

    def test_model_rate_limit_has_stable_code(self):
        error = public_error_from_exception(RuntimeError("Error code: 429 raw payload"))
        self.assertEqual(ErrorCode.MODEL_RATE_LIMITED, error.code)
        self.assertEqual(429, error.status_code)
        self.assertNotIn("raw payload", error.message)


if __name__ == "__main__":
    unittest.main()
