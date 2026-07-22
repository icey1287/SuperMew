from __future__ import annotations

import ipaddress
import json
import math
import os
from collections.abc import Mapping
from functools import partial
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from langchain_core.tools import StructuredTool

from backend.capabilities.control_contracts import ManagedHttpToolRecord
from backend.runs.request_context import RunRequestContext
from backend.tools.contracts import (
    TOOL_RESULT_V1_SCHEMA,
    ToolResultV1,
    new_tool_failure,
    new_tool_success,
)
from backend.tools.registry import ToolDescriptor, ToolExposure, ToolRegistry
from backend.web_research.http import SafeWebHttpClient, WebHttpError
from backend.web_research.url_policy import (
    SystemWebDnsResolver,
    WebUrlPolicy,
    WebUrlPolicyError,
)


_JSON_CONTENT_TYPES = frozenset({"application/json", "application/problem+json"})
_SPECIAL_SUFFIXES = (".local", ".localhost", ".test", ".example", ".invalid")


class _CustomToolConfigurationError(RuntimeError):
    pass


def validate_custom_http_endpoint(value: str) -> str:
    if not isinstance(value, str):
        raise TypeError("endpoint must be a string")
    endpoint = value.strip()
    if not endpoint or len(endpoint) > 2_048 or "\\" in endpoint:
        raise ValueError("endpoint must be a bounded HTTPS URL")
    try:
        parsed = urlsplit(endpoint)
        port = parsed.port
    except ValueError:
        raise ValueError("endpoint must be a valid HTTPS URL") from None
    if (
        parsed.scheme.casefold() != "https"
        or not parsed.hostname
        or port not in {None, 443}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise ValueError("endpoint must use public HTTPS without credentials or fragments")
    host = parsed.hostname.casefold().rstrip(".")
    if host == "localhost" or host.endswith(_SPECIAL_SUFFIXES):
        raise ValueError("endpoint cannot target a special-use host")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if "." not in host:
            raise ValueError("endpoint must use a fully-qualified public host") from None
    else:
        if not address.is_global:
            raise ValueError("endpoint cannot target a non-global IP address")
    return endpoint


class CustomHttpToolRuntime:
    """Shared pinned HTTP runtime for declarative custom JSON tools."""

    def __init__(
        self,
        *,
        dns_timeout_seconds: float = 2.0,
        dns_max_concurrency: int = 4,
        max_dns_addresses: int = 8,
        user_agent: str = "SuperMew-CustomTool/1.0",
    ) -> None:
        self._resolver = SystemWebDnsResolver(
            timeout_seconds=dns_timeout_seconds,
            max_concurrency=dns_max_concurrency,
        )
        self._policy = WebUrlPolicy(
            self._resolver,
            allowed_scheme_ports={"https": frozenset({443})},
            max_url_bytes=16_384,
            max_resolved_addresses=max_dns_addresses,
        )
        self._client = SafeWebHttpClient(self._policy)
        self._user_agent = user_agent

    def close(self) -> None:
        self._policy.close()

    def invoke(
        self,
        profile: ManagedHttpToolRecord,
        arguments: Mapping[str, object],
        context: RunRequestContext,
    ) -> ToolResultV1:
        deadline_at, cancellation_probe = context.provider_runtime()
        try:
            headers = self._headers(profile)
            if profile.method == "GET":
                url = _append_query(profile.endpoint, arguments)
                body = None
            else:
                body = json.dumps(
                    dict(arguments),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                ).encode("utf-8")
                if len(body) > 1_048_576:
                    return new_tool_failure(
                        error_code="CUSTOM_TOOL_INPUT_TOO_LARGE",
                        retryable=False,
                    )
                url = profile.endpoint
        except _CustomToolConfigurationError:
            return new_tool_failure(
                error_code="CUSTOM_TOOL_NOT_CONFIGURED",
                retryable=False,
            )
        except (UnicodeError, TypeError, ValueError):
            return new_tool_failure(
                error_code="CUSTOM_TOOL_INVALID_INPUT",
                retryable=False,
            )

        try:
            if profile.method == "GET":
                fetched = self._client.get(
                    url,
                    headers=headers,
                    allowed_content_types=_JSON_CONTENT_TYPES,
                    max_compressed_bytes=profile.max_response_bytes,
                    max_response_bytes=profile.max_response_bytes,
                    max_redirects=0,
                    timeout_seconds=profile.timeout_seconds,
                    deadline_at=deadline_at,
                    cancellation_probe=cancellation_probe,
                )
            else:
                fetched = self._client.post(
                    url,
                    headers={"Content-Type": "application/json", **headers},
                    body=body or b"{}",
                    allowed_content_types=_JSON_CONTENT_TYPES,
                    max_compressed_bytes=profile.max_response_bytes,
                    max_response_bytes=profile.max_response_bytes,
                    max_redirects=0,
                    timeout_seconds=profile.timeout_seconds,
                    deadline_at=deadline_at,
                    cancellation_probe=cancellation_probe,
                )
        except WebUrlPolicyError as exc:
            return new_tool_failure(error_code=exc.code.value, retryable=False)
        except WebHttpError as exc:
            return new_tool_failure(error_code=exc.code, retryable=exc.retryable)

        try:
            payload = json.loads(fetched.body)
        except (UnicodeError, TypeError, ValueError, json.JSONDecodeError):
            return new_tool_failure(
                error_code="CUSTOM_TOOL_INVALID_RESPONSE",
                retryable=False,
            )
        return new_tool_success(
            data=payload,
            observability_metadata={
                "response_bytes": len(fetched.body),
                "status_code": fetched.status_code,
            },
        )

    def _headers(self, profile: ManagedHttpToolRecord) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "User-Agent": self._user_agent,
            **profile.static_headers,
        }
        for header_name, secret_name in profile.secret_headers.items():
            value = os.getenv(secret_name, "").strip()
            if not value:
                raise _CustomToolConfigurationError
            if len(value) > 4_096 or any(marker in value for marker in ("\r", "\n", "\x00")):
                raise _CustomToolConfigurationError
            headers[header_name] = value
        return headers


def register_custom_http_tools(
    registry: ToolRegistry,
    profiles: tuple[ManagedHttpToolRecord, ...],
    runtime: CustomHttpToolRuntime,
) -> None:
    for profile in profiles:
        if not profile.enabled:
            continue
        descriptor = ToolDescriptor(
            name=profile.name,
            description=profile.description,
            group=profile.group,
            version=profile.version,
            input_schema=profile.input_schema,
            output_schema=TOOL_RESULT_V1_SCHEMA,
            timeout=profile.timeout_seconds,
            max_concurrency=4,
            idempotent=profile.idempotent,
            required_roles=frozenset(profile.required_roles),
            required_secrets=frozenset(profile.secret_headers.values()),
            requires_approval=profile.requires_approval,
            network_policy="restricted",
            result_size_limit=profile.max_response_bytes + 65_536,
            resource_scope="public-web",
            observability_metadata_keys=frozenset(
                {"response_bytes", "status_code"}
            ),
        )
        registry.register(
            descriptor,
            partial(_custom_http_factory, profile=profile, runtime=runtime),
            exposure=ToolExposure.DEFERRED,
        )


def _custom_http_factory(
    context: RunRequestContext,
    *,
    profile: ManagedHttpToolRecord,
    runtime: CustomHttpToolRuntime,
) -> StructuredTool:
    def invoke(**kwargs) -> ToolResultV1:
        return runtime.invoke(profile, kwargs, context)

    return StructuredTool.from_function(
        func=invoke,
        name=profile.name,
        description=profile.description,
        args_schema=dict(profile.input_schema),
        infer_schema=False,
    )


def _append_query(endpoint: str, arguments: Mapping[str, object]) -> str:
    parsed = urlsplit(endpoint)
    pairs = parse_qsl(parsed.query, keep_blank_values=True)
    for name in sorted(arguments):
        value = arguments[name]
        if value is None:
            continue
        if isinstance(value, bool):
            rendered = "true" if value else "false"
        elif isinstance(value, (str, int)):
            rendered = str(value)
        elif isinstance(value, float) and math.isfinite(value):
            rendered = str(value)
        else:
            rendered = json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
        pairs.append((name, rendered))
    return urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, urlencode(pairs), "")
    )


__all__ = [
    "CustomHttpToolRuntime",
    "register_custom_http_tools",
    "validate_custom_http_endpoint",
]
