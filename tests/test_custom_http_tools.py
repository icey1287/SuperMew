from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from backend.capabilities.control_contracts import ManagedHttpToolRecord
from backend.tools.custom_http import CustomHttpToolRuntime
from backend.web_research.url_policy import WebUrlPolicyCode, WebUrlPolicyError


def _profile(**overrides) -> ManagedHttpToolRecord:
    values = {
        "name": "release_lookup",
        "version": "1.0.0",
        "description": "Look up public release metadata.",
        "group": "custom-http",
        "endpoint": "https://api.cloudflare.com/releases",
        "method": "GET",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "additionalProperties": False,
        },
        "static_headers": {},
        "secret_headers": {},
        "required_roles": (),
        "requires_approval": False,
        "idempotent": True,
        "timeout_seconds": 20,
        "max_response_bytes": 65_536,
        "enabled": True,
        "created_at": datetime.now(UTC),
        "updated_at": datetime.now(UTC),
    }
    values.update(overrides)
    return ManagedHttpToolRecord(**values)


def _runtime(client) -> CustomHttpToolRuntime:
    runtime = object.__new__(CustomHttpToolRuntime)
    runtime._client = client
    runtime._user_agent = "SuperMew-Test/1.0"
    return runtime


def _context():
    return SimpleNamespace(provider_runtime=lambda: (None, None))


def test_custom_http_tool_preserves_url_policy_error_codes() -> None:
    class Client:
        def get(self, *_args, **_kwargs):
            raise WebUrlPolicyError(
                WebUrlPolicyCode.HOST_DENIED,
                "denied",
            )

    result = _runtime(Client()).invoke(
        _profile(),
        {"query": "release"},
        _context(),
    )

    assert result.success is False
    assert result.error_code == "WEB_HOST_DENIED"
    assert result.retryable is False


def test_custom_http_tool_reports_missing_secret_without_calling_network(
    monkeypatch,
) -> None:
    class Client:
        def get(self, *_args, **_kwargs):
            raise AssertionError("network must not be called")

    monkeypatch.delenv("RELEASE_API_TOKEN", raising=False)
    result = _runtime(Client()).invoke(
        _profile(secret_headers={"Authorization": "RELEASE_API_TOKEN"}),
        {"query": "release"},
        _context(),
    )

    assert result.success is False
    assert result.error_code == "CUSTOM_TOOL_NOT_CONFIGURED"


def test_custom_http_tool_distinguishes_invalid_input_from_invalid_response() -> None:
    class InvalidResponseClient:
        def post(self, *_args, **_kwargs):
            return SimpleNamespace(body=b"not-json", status_code=200)

    profile = _profile(method="POST")
    invalid_input = _runtime(InvalidResponseClient()).invoke(
        profile,
        {"query": object()},
        _context(),
    )
    invalid_response = _runtime(InvalidResponseClient()).invoke(
        profile,
        {"query": "release"},
        _context(),
    )

    assert invalid_input.error_code == "CUSTOM_TOOL_INVALID_INPUT"
    assert invalid_response.error_code == "CUSTOM_TOOL_INVALID_RESPONSE"
