import asyncio
import time
import unittest
from unittest.mock import Mock, patch

import backend.tools.weather as weather
from backend.providers import ProviderExecutor


class WeatherProviderErrorTests(unittest.TestCase):
    def setUp(self):
        self.api = weather.AMAP_WEATHER_API
        self.key = weather.AMAP_API_KEY
        self.executor = weather._provider_executor
        weather.AMAP_WEATHER_API = "https://weather.example.test/v3/weather"
        weather.AMAP_API_KEY = "secret-key"
        weather._provider_executor = ProviderExecutor(sleeper=lambda _: None)

    def tearDown(self):
        weather.AMAP_WEATHER_API = self.api
        weather.AMAP_API_KEY = self.key
        weather._provider_executor = self.executor

    def test_timeout_retries_and_returns_stable_code_without_raw_details(self):
        with patch.object(
            weather.requests,
            "get",
            side_effect=weather.requests.exceptions.Timeout(
                "secret-key https://weather.example.test"
            ),
        ) as get:
            result = weather.get_current_weather("上海")

        self.assertEqual(2, get.call_count)
        self.assertIn("TOOL_TIMEOUT", result)
        self.assertNotIn("secret-key", result)
        self.assertNotIn("weather.example.test", result)

    def test_upstream_body_and_logical_error_are_redacted(self):
        response = Mock(status_code=500, text="secret upstream body", headers={})
        response.raise_for_status.side_effect = weather.requests.HTTPError(
            "secret upstream body", response=response
        )
        with patch.object(weather.requests, "get", return_value=response):
            result = weather.get_current_weather("上海")
        self.assertIn("TOOL_UNAVAILABLE", result)
        self.assertNotIn("secret upstream body", result)

        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"status": "0", "info": "secret provider info"}
        with patch.object(weather.requests, "get", return_value=response):
            result = weather.get_current_weather("上海")
        self.assertIn("TOOL_UNAVAILABLE", result)
        self.assertNotIn("secret provider info", result)

    def test_request_owned_tool_propagates_typed_failure_for_runtime_trace(self):
        context = weather.ChatRequestContext.for_sync(
            user_id="alice", session_id="thread-1"
        )
        tool = weather.make_weather_tool(context)
        try:
            with patch.object(
                weather.requests,
                "get",
                side_effect=weather.requests.exceptions.Timeout("secret timeout"),
            ):
                with self.assertRaises(weather.ProviderError) as raised:
                    tool.invoke({"location": "上海"})
        finally:
            context.close()

        self.assertEqual(weather.ProviderCode.TOOL_TIMEOUT, raised.exception.code)

    def test_request_owned_tool_uses_run_deadline_and_cancellation_probe(self):
        context = weather.ChatRequestContext.for_sync(
            user_id="alice", session_id="thread-1"
        )
        cancelled = False
        context.configure_provider_runtime(
            deadline_at=time.monotonic() + 0.5,
            cancellation_probe=lambda: cancelled,
        )
        tool = weather.make_weather_tool(context)
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "status": "1",
            "lives": [{"city": "上海", "weather": "晴"}],
        }
        try:
            with patch.object(weather.requests, "get", return_value=response) as get:
                result = tool.invoke({"location": "上海"})
            self.assertIn("上海", result)
            self.assertLessEqual(get.call_args.kwargs["timeout"], 0.5)

            cancelled = True
            with self.assertRaises(asyncio.CancelledError):
                tool.invoke({"location": "上海"})
        finally:
            context.close()


if __name__ == "__main__":
    unittest.main()
