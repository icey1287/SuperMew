"""Request-scoped tool catalog, authorization, and progressive disclosure.

The registry owns tool metadata and request-owned Adapter factories.  A bound
``ToolSession`` is an immutable catalog snapshot plus mutable, Run-local
visibility state.  Keeping these responsibilities together prevents schema
exposure and execution authorization from drifting apart as the catalog grows.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from copy import deepcopy
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError as JsonSchemaValidationError
from langchain_core.tools import BaseTool, StructuredTool

from backend.tools.contracts import ToolResultV1, new_tool_failure, new_tool_success


_TOOL_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")
_SEARCH_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_\-.]+")
_SEMVER_PATTERN = re.compile(
    r"^(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)"
    r"(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$"
)
_ROLE_PATTERN = re.compile(r"^[a-z][a-z0-9:_-]{0,127}$")
_SECRET_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")
_POLICY_PATTERN = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")


class ToolExposure(StrEnum):
    """How an authorized tool enters a model's visible tool set."""

    RESIDENT = "resident"
    CONTROL = "control"
    DEFERRED = "deferred"


def _normalize_string_set(values: Iterable[str], *, field_name: str) -> frozenset[str]:
    normalized: set[str] = set()
    for value in values:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{field_name} must contain only non-empty strings")
        normalized.add(value.strip())
    return frozenset(normalized)


def _json_schema_copy(schema: Mapping[str, Any], *, field_name: str) -> dict[str, Any]:
    if not isinstance(schema, Mapping):
        raise TypeError(f"{field_name} must be a JSON Schema mapping")
    copied = deepcopy(dict(schema))
    Draft202012Validator.check_schema(copied)
    try:
        json.dumps(copied, ensure_ascii=False, sort_keys=True, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be JSON serializable") from exc
    return copied


@dataclass(frozen=True)
class ToolDescriptor:
    """Stable metadata and deterministic policy requirements for one tool."""

    name: str
    description: str
    group: str
    version: str
    input_schema: Mapping[str, Any]
    output_schema: Mapping[str, Any]
    timeout: float
    max_concurrency: int
    idempotent: bool
    required_roles: frozenset[str]
    required_secrets: frozenset[str]
    requires_approval: bool
    network_policy: str
    result_size_limit: int

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not _TOOL_NAME_PATTERN.fullmatch(
            self.name
        ):
            raise ValueError("name must be a valid tool identifier")
        for field_name in ("description", "group", "version", "network_policy"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field_name} must be a non-empty string")
            object.__setattr__(self, field_name, value.strip())
        if len(self.description) > 1_000:
            raise ValueError("description must be at most 1000 characters")
        if len(self.group) > 128:
            raise ValueError("group must be at most 128 characters")
        if len(self.version) > 64:
            raise ValueError("version must be at most 64 characters")
        if _SEMVER_PATTERN.fullmatch(self.version) is None:
            raise ValueError("version must be semantic version text")
        if _POLICY_PATTERN.fullmatch(self.network_policy) is None:
            raise ValueError("network_policy must be a stable policy identifier")
        if isinstance(self.timeout, bool) or self.timeout <= 0:
            raise ValueError("timeout must be greater than zero")
        if self.timeout > 3_600:
            raise ValueError("timeout must not exceed 3600 seconds")
        if isinstance(self.max_concurrency, bool) or self.max_concurrency <= 0:
            raise ValueError("max_concurrency must be greater than zero")
        if self.max_concurrency > 256:
            raise ValueError("max_concurrency must not exceed 256")
        if not isinstance(self.idempotent, bool):
            raise TypeError("idempotent must be a bool")
        if not isinstance(self.requires_approval, bool):
            raise TypeError("requires_approval must be a bool")
        if isinstance(self.result_size_limit, bool) or self.result_size_limit <= 0:
            raise ValueError("result_size_limit must be greater than zero")
        if self.result_size_limit > 104_857_600:
            raise ValueError("result_size_limit must not exceed 100 MiB")

        object.__setattr__(
            self,
            "input_schema",
            _json_schema_copy(self.input_schema, field_name="input_schema"),
        )
        object.__setattr__(
            self,
            "output_schema",
            _json_schema_copy(self.output_schema, field_name="output_schema"),
        )
        object.__setattr__(
            self,
            "required_roles",
            _normalize_string_set(self.required_roles, field_name="required_roles"),
        )
        object.__setattr__(
            self,
            "required_secrets",
            _normalize_string_set(self.required_secrets, field_name="required_secrets"),
        )
        if any(_ROLE_PATTERN.fullmatch(role) is None for role in self.required_roles):
            raise ValueError("required_roles contains an invalid role identifier")
        if any(
            _SECRET_PATTERN.fullmatch(secret) is None
            for secret in self.required_secrets
        ):
            raise ValueError("required_secrets contains an invalid secret identifier")

    def catalog_record(self, *, exposure: ToolExposure) -> dict[str, Any]:
        """Return a canonical JSON-compatible catalog representation."""

        return {
            "name": self.name,
            "description": self.description,
            "group": self.group,
            "version": self.version,
            "input_schema": deepcopy(dict(self.input_schema)),
            "output_schema": deepcopy(dict(self.output_schema)),
            "timeout": self.timeout,
            "max_concurrency": self.max_concurrency,
            "idempotent": self.idempotent,
            "required_roles": sorted(self.required_roles),
            "required_secrets": sorted(self.required_secrets),
            "requires_approval": self.requires_approval,
            "network_policy": self.network_policy,
            "result_size_limit": self.result_size_limit,
            "exposure": exposure.value,
        }


@dataclass(frozen=True)
class ToolAccess:
    """Caller capabilities supplied to deterministic tool authorization."""

    roles: frozenset[str] = field(default_factory=frozenset)
    available_secrets: frozenset[str] = field(default_factory=frozenset)
    caller_allowed_tools: frozenset[str] = field(default_factory=frozenset)
    approved_tools: frozenset[str] = field(default_factory=frozenset)
    allowed_network_policies: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        for field_name in (
            "roles",
            "available_secrets",
            "caller_allowed_tools",
            "approved_tools",
            "allowed_network_policies",
        ):
            object.__setattr__(
                self,
                field_name,
                _normalize_string_set(getattr(self, field_name), field_name=field_name),
            )
        if any(_ROLE_PATTERN.fullmatch(role) is None for role in self.roles):
            raise ValueError("roles contains an invalid role identifier")
        if any(
            _SECRET_PATTERN.fullmatch(secret) is None
            for secret in self.available_secrets
        ):
            raise ValueError("available_secrets contains an invalid secret identifier")
        for field_name in ("caller_allowed_tools", "approved_tools"):
            if any(
                _TOOL_NAME_PATTERN.fullmatch(name) is None
                for name in getattr(self, field_name)
            ):
                raise ValueError(f"{field_name} contains an invalid tool identifier")
        if any(
            _POLICY_PATTERN.fullmatch(policy) is None
            for policy in self.allowed_network_policies
        ):
            raise ValueError(
                "allowed_network_policies contains an invalid policy identifier"
            )


ToolFactory = Callable[[object], BaseTool]


def _descriptor_copy(descriptor: ToolDescriptor) -> ToolDescriptor:
    """Return a detached descriptor so mutable schema dicts never escape."""

    return replace(
        descriptor,
        input_schema=deepcopy(dict(descriptor.input_schema)),
        output_schema=deepcopy(dict(descriptor.output_schema)),
    )


@dataclass(frozen=True)
class _RegisteredTool:
    descriptor: ToolDescriptor
    factory: ToolFactory
    exposure: ToolExposure
    executor: ThreadPoolExecutor = field(compare=False, repr=False)
    gate: threading.BoundedSemaphore = field(compare=False, repr=False)


def _adapter_name(adapter: object) -> str | None:
    name = getattr(adapter, "name", None)
    return name if isinstance(name, str) else None


def _search_score(descriptor: ToolDescriptor, query: str) -> int:
    normalized_query = query.casefold().strip()
    if not normalized_query:
        return 0
    name = descriptor.name.casefold()
    group = descriptor.group.casefold()
    description = descriptor.description.casefold()
    haystack = f"{name} {group} {description}"
    tokens = [token.casefold() for token in _SEARCH_TOKEN_PATTERN.findall(query)]

    score = 0
    if normalized_query == name:
        score += 1_000
    elif normalized_query in name:
        score += 300
    if normalized_query in group:
        score += 120
    if normalized_query in description:
        score += 80
    for token in tokens:
        if token == name:
            score += 200
        elif token in name:
            score += 80
        if token == group:
            score += 50
        elif token in group:
            score += 20
        if token in haystack:
            score += 10
    return score


def _failure_text(
    descriptor: ToolDescriptor,
    *,
    error_code: str,
    retryable: bool,
    duration_ms: int,
    message: str,
    result_size: int | None = None,
) -> str:
    metadata: dict[str, Any] = {
        "tool_name": descriptor.name,
        "tool_version": descriptor.version,
    }
    if result_size is not None:
        metadata["result_size"] = result_size
    return new_tool_failure(
        error_code=error_code,
        retryable=retryable,
        duration_ms=duration_ms,
        data={"message": message},
        observability_metadata=metadata,
    ).model_dump_json()


def _exception_failure_text(
    descriptor: ToolDescriptor,
    exc: Exception,
    *,
    duration_ms: int,
) -> str:
    raw_code = getattr(exc, "code", None)
    code = getattr(raw_code, "value", raw_code)
    normalized = str(code or "").strip().upper()
    if normalized in {"RUN_STATE_CONFLICT", "RUN_CANCELLED"}:
        raise exc
    if isinstance(exc, TimeoutError):
        normalized = "TOOL_TIMEOUT"
    if re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", normalized) is None:
        normalized = "TOOL_UNAVAILABLE"
    return _failure_text(
        descriptor,
        error_code=normalized,
        retryable=bool(getattr(exc, "retryable", False))
        or normalized == "TOOL_TIMEOUT",
        duration_ms=duration_ms,
        message="工具执行失败，未向模型暴露内部异常。",
    )


def _validate_result(
    descriptor: ToolDescriptor,
    result: Any,
    *,
    duration_ms: int,
) -> Any:
    if isinstance(result, ToolResultV1):
        payload = result.model_dump(mode="json")
        try:
            Draft202012Validator(descriptor.output_schema).validate(payload)
        except JsonSchemaValidationError:
            return _failure_text(
                descriptor,
                error_code="TOOL_OUTPUT_INVALID",
                retryable=False,
                duration_ms=duration_ms,
                message="工具结果不符合已注册的输出契约。",
            )
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        if len(encoded) > descriptor.result_size_limit:
            return _failure_text(
                descriptor,
                error_code="TOOL_RESULT_TOO_LARGE",
                retryable=False,
                duration_ms=duration_ms,
                message="工具结果超过当前 Run 的大小限制。",
                result_size=len(encoded),
            )
        observability_metadata = dict(result.observability_metadata)
        observability_metadata.update(
            {
                "tool_name": descriptor.name,
                "tool_version": descriptor.version,
                "result_size": len(encoded),
            }
        )
        return result.model_copy(
            update={
                "duration_ms": duration_ms,
                "observability_metadata": observability_metadata,
            }
        ).model_dump_json()
    try:
        Draft202012Validator(descriptor.output_schema).validate(result)
    except JsonSchemaValidationError:
        return _failure_text(
            descriptor,
            error_code="TOOL_OUTPUT_INVALID",
            retryable=False,
            duration_ms=duration_ms,
            message="工具结果不符合已注册的输出契约。",
        )
    try:
        if isinstance(result, str):
            encoded = result.encode("utf-8")
        else:
            encoded = json.dumps(
                result,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
    except (TypeError, ValueError):
        return _failure_text(
            descriptor,
            error_code="TOOL_OUTPUT_INVALID",
            retryable=False,
            duration_ms=duration_ms,
            message="工具结果不是可序列化的 JSON 数据。",
        )
    if len(encoded) > descriptor.result_size_limit:
        return _failure_text(
            descriptor,
            error_code="TOOL_RESULT_TOO_LARGE",
            retryable=False,
            duration_ms=duration_ms,
            message="工具结果超过当前 Run 的大小限制。",
            result_size=len(encoded),
        )
    return new_tool_success(
        data=result,
        duration_ms=duration_ms,
        observability_metadata={
            "tool_name": descriptor.name,
            "tool_version": descriptor.version,
            "result_size": len(encoded),
        },
    ).model_dump_json()


def _govern_base_tool(
    adapter: BaseTool,
    registration: _RegisteredTool,
) -> BaseTool:
    descriptor = registration.descriptor

    def invoke(**kwargs):
        started = time.monotonic()
        if not registration.gate.acquire(timeout=descriptor.timeout):
            return _failure_text(
                descriptor,
                error_code="TOOL_TIMEOUT",
                retryable=True,
                duration_ms=int((time.monotonic() - started) * 1000),
                message="工具等待并发配额时超过已注册的超时限制。",
            )
        try:
            future = registration.executor.submit(adapter.invoke, kwargs)
        except Exception:
            registration.gate.release()
            raise
        future.add_done_callback(lambda _future: registration.gate.release())
        remaining = max(descriptor.timeout - (time.monotonic() - started), 0.0)
        try:
            result = future.result(timeout=remaining)
        except FutureTimeoutError:
            future.cancel()
            duration_ms = int((time.monotonic() - started) * 1000)
            return _failure_text(
                descriptor,
                error_code="TOOL_TIMEOUT",
                retryable=True,
                duration_ms=duration_ms,
                message="工具执行超过已注册的超时限制。",
            )
        except Exception as exc:
            return _exception_failure_text(
                descriptor,
                exc,
                duration_ms=int((time.monotonic() - started) * 1000),
            )
        duration_ms = int((time.monotonic() - started) * 1000)
        return _validate_result(descriptor, result, duration_ms=duration_ms)

    async def ainvoke(**kwargs):
        loop = asyncio.get_running_loop()
        started = loop.time()
        acquired = False
        while True:
            acquired = registration.gate.acquire(blocking=False)
            if acquired:
                break
            remaining = descriptor.timeout - (loop.time() - started)
            if remaining <= 0:
                break
            await asyncio.sleep(min(0.01, remaining))
        if not acquired:
            return _failure_text(
                descriptor,
                error_code="TOOL_TIMEOUT",
                retryable=True,
                duration_ms=int((loop.time() - started) * 1000),
                message="工具等待并发配额时超过已注册的超时限制。",
            )
        remaining = max(descriptor.timeout - (loop.time() - started), 0.0)
        coroutine = getattr(adapter, "coroutine", None)
        if coroutine is not None:
            try:
                result = await asyncio.wait_for(
                    adapter.ainvoke(kwargs),
                    timeout=remaining,
                )
            except TimeoutError:
                duration_ms = int((loop.time() - started) * 1000)
                return _failure_text(
                    descriptor,
                    error_code="TOOL_TIMEOUT",
                    retryable=True,
                    duration_ms=duration_ms,
                    message="工具执行超过已注册的超时限制。",
                )
            except Exception as exc:
                return _exception_failure_text(
                    descriptor,
                    exc,
                    duration_ms=int((loop.time() - started) * 1000),
                )
            finally:
                registration.gate.release()
            duration_ms = int((loop.time() - started) * 1000)
            return _validate_result(descriptor, result, duration_ms=duration_ms)

        try:
            concurrent_future = registration.executor.submit(adapter.invoke, kwargs)
        except Exception:
            registration.gate.release()
            raise
        concurrent_future.add_done_callback(lambda _future: registration.gate.release())
        future = asyncio.wrap_future(concurrent_future, loop=loop)
        try:
            result = await asyncio.wait_for(future, timeout=remaining)
        except TimeoutError:
            duration_ms = int((loop.time() - started) * 1000)
            return _failure_text(
                descriptor,
                error_code="TOOL_TIMEOUT",
                retryable=True,
                duration_ms=duration_ms,
                message="工具执行超过已注册的超时限制。",
            )
        except Exception as exc:
            return _exception_failure_text(
                descriptor,
                exc,
                duration_ms=int((loop.time() - started) * 1000),
            )
        duration_ms = int((loop.time() - started) * 1000)
        return _validate_result(descriptor, result, duration_ms=duration_ms)

    return StructuredTool.from_function(
        func=invoke,
        coroutine=ainvoke,
        name=descriptor.name,
        description=descriptor.description,
        args_schema=deepcopy(dict(descriptor.input_schema)),
        infer_schema=False,
        return_direct=bool(getattr(adapter, "return_direct", False)),
        metadata=deepcopy(getattr(adapter, "metadata", None)),
        tags=deepcopy(getattr(adapter, "tags", None)),
    )


def _search_registrations(
    registrations: Mapping[str, _RegisteredTool],
    query: str,
    *,
    allowed_names: frozenset[str],
    limit: int,
    exposure: ToolExposure | None = None,
) -> tuple[_RegisteredTool, ...]:
    if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
        raise ValueError("limit must be a positive integer")
    if not isinstance(query, str) or not query.strip():
        return ()

    matches: list[tuple[int, _RegisteredTool]] = []
    for name in sorted(allowed_names):
        registration = registrations.get(name)
        if registration is None:
            continue
        if exposure is not None and registration.exposure is not exposure:
            continue
        score = _search_score(registration.descriptor, query)
        if score:
            matches.append((score, registration))
    matches.sort(key=lambda item: (-item[0], item[1].descriptor.name))
    return tuple(registration for _, registration in matches[:limit])


class ToolRegistry:
    """Tool metadata Module with one authorization path for every consumer."""

    def __init__(self) -> None:
        self._registrations: dict[str, _RegisteredTool] = {}
        self._lock = threading.RLock()
        self._frozen = False

    @property
    def is_frozen(self) -> bool:
        with self._lock:
            return self._frozen

    def freeze(self) -> None:
        """Prevent catalog mutation once the Registry is published to Runs."""

        with self._lock:
            self._frozen = True

    def register(
        self,
        descriptor: ToolDescriptor,
        factory: ToolFactory,
        *,
        exposure: ToolExposure | str = ToolExposure.DEFERRED,
    ) -> None:
        with self._lock:
            if self._frozen:
                raise RuntimeError("tool registry is frozen")
            if descriptor.name in self._registrations:
                raise ValueError(f"tool already registered: {descriptor.name}")
            if not callable(factory):
                raise TypeError("factory must be callable")
            normalized_exposure = ToolExposure(exposure)
            internal_descriptor = _descriptor_copy(descriptor)
            self._registrations[internal_descriptor.name] = _RegisteredTool(
                descriptor=internal_descriptor,
                factory=factory,
                exposure=normalized_exposure,
                executor=ThreadPoolExecutor(
                    max_workers=internal_descriptor.max_concurrency,
                    thread_name_prefix=f"tool-{internal_descriptor.name}",
                ),
                gate=threading.BoundedSemaphore(
                    internal_descriptor.max_concurrency
                ),
            )

    @property
    def names(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(sorted(self._registrations))

    def descriptor(self, name: str) -> ToolDescriptor | None:
        with self._lock:
            registration = self._registrations.get(name)
            return (
                None
                if registration is None
                else _descriptor_copy(registration.descriptor)
            )

    def exposure(self, name: str) -> ToolExposure | None:
        with self._lock:
            registration = self._registrations.get(name)
            return None if registration is None else registration.exposure

    def authorize(self, name: str, access: ToolAccess) -> bool:
        with self._lock:
            registration = self._registrations.get(name)
            if registration is None:
                return False
            descriptor = registration.descriptor
            if name not in access.caller_allowed_tools:
                return False
            if not descriptor.required_roles.issubset(access.roles):
                return False
            if not descriptor.required_secrets.issubset(access.available_secrets):
                return False
            if descriptor.requires_approval and name not in access.approved_tools:
                return False
            if descriptor.network_policy not in access.allowed_network_policies:
                return False
            return True

    def describe(self, name: str, access: ToolAccess) -> ToolDescriptor | None:
        if not self.authorize(name, access):
            return None
        return self.descriptor(name)

    def search(
        self,
        query: str,
        access: ToolAccess,
        *,
        limit: int = 8,
        exposure: ToolExposure | str | None = None,
    ) -> tuple[ToolDescriptor, ...]:
        with self._lock:
            authorized = frozenset(
                name for name in self._registrations if self.authorize(name, access)
            )
            normalized_exposure = None if exposure is None else ToolExposure(exposure)
            matches = _search_registrations(
                self._registrations,
                query,
                allowed_names=authorized,
                limit=limit,
                exposure=normalized_exposure,
            )
            return tuple(_descriptor_copy(item.descriptor) for item in matches)

    def bind(
        self,
        request_context: object,
        access: ToolAccess,
        *,
        overrides: Mapping[str, object] | None = None,
    ) -> ToolSession:
        with self._lock:
            self._frozen = True
            override_map = dict(overrides or {})
            for name, adapter in override_map.items():
                if name not in self._registrations:
                    raise KeyError(f"override references an unregistered tool: {name}")
                actual_name = _adapter_name(adapter)
                if actual_name != name:
                    raise ValueError(
                        f"tool override name mismatch: expected {name!r}, "
                        f"got {actual_name!r}"
                    )

            registrations = {
                name: registration
                for name, registration in self._registrations.items()
                if self.authorize(name, access)
            }
            authorized_overrides = {
                name: adapter
                for name, adapter in override_map.items()
                if name in registrations
            }
        return ToolSession(
            request_context=request_context,
            registrations=registrations,
            overrides=authorized_overrides,
        )

    @property
    def catalog_hash(self) -> str:
        with self._lock:
            records = [
                registration.descriptor.catalog_record(
                    exposure=registration.exposure
                )
                for registration in sorted(
                    self._registrations.values(),
                    key=lambda item: item.descriptor.name,
                )
            ]
        encoded = json.dumps(
            records,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


class ToolSession:
    """Run-local tool visibility and execution state over a catalog snapshot."""

    def __init__(
        self,
        *,
        request_context: object,
        registrations: Mapping[str, _RegisteredTool],
        overrides: Mapping[str, object],
    ) -> None:
        self._request_context = request_context
        self._registrations = dict(registrations)
        self._overrides = dict(overrides)
        self._adapters: dict[str, object] = {}
        self._lock = threading.RLock()
        self._authorized_names = frozenset(self._registrations)
        self._skill_scope = self._authorized_names
        initially_exposed = {
            name
            for name, registration in self._registrations.items()
            if registration.exposure in {ToolExposure.RESIDENT, ToolExposure.CONTROL}
        }
        self._visible_names = set(initially_exposed)
        self._executable_names = set(initially_exposed)

    @property
    def authorized_names(self) -> frozenset[str]:
        return self._authorized_names

    @property
    def visible_names(self) -> frozenset[str]:
        with self._lock:
            return frozenset(self._visible_names)

    @property
    def executable_names(self) -> frozenset[str]:
        with self._lock:
            return frozenset(self._executable_names)

    @property
    def tools(self) -> tuple[object, ...]:
        """Return every authorized Adapter for graph compilation.

        Deferred Adapters are intentionally included.  Model schema filtering
        must use ``visible_names`` and execution policy must use ``is_allowed``.
        """

        with self._lock:
            return tuple(self._adapter(name) for name in sorted(self._authorized_names))

    def _adapter(self, name: str) -> object:
        with self._lock:
            if name not in self._authorized_names:
                raise PermissionError(
                    f"tool is not authorized for this session: {name}"
                )
            cached = self._adapters.get(name)
            if cached is not None:
                return cached

            if name in self._overrides:
                adapter = self._overrides[name]
            else:
                adapter = self._registrations[name].factory(self._request_context)
            actual_name = _adapter_name(adapter)
            if actual_name != name:
                raise ValueError(
                    f"tool factory name mismatch: expected {name!r}, "
                    f"got {actual_name!r}"
                )
            if not isinstance(adapter, BaseTool):
                raise TypeError(
                    f"tool factory must return BaseTool for {name!r}, "
                    f"got {type(adapter).__name__}"
                )
            adapter = _govern_base_tool(adapter, self._registrations[name])
            self._adapters[name] = adapter
            return adapter

    def is_allowed(self, name: str) -> bool:
        """Fail closed for unknown, unauthorized, scoped-out, or hidden tools."""

        with self._lock:
            return name in self._executable_names

    def resolve(self, name: str) -> object:
        if not self.is_allowed(name):
            raise PermissionError(f"tool is not executable in this session: {name}")
        return self._adapter(name)

    def describe(self, name: str) -> ToolDescriptor | None:
        with self._lock:
            if name not in self._visible_names:
                return None
            registration = self._registrations.get(name)
            return (
                None
                if registration is None
                else _descriptor_copy(registration.descriptor)
            )

    def apply_skill(self, allowed_tools: Iterable[str]) -> frozenset[str]:
        """Attenuate the Run to one Skill without granting new capabilities."""

        with self._lock:
            requested = _normalize_string_set(
                allowed_tools, field_name="skill allowed_tools"
            )
            control_names = {
                name
                for name, registration in self._registrations.items()
                if registration.exposure is ToolExposure.CONTROL
            }
            self._skill_scope = frozenset(
                self._authorized_names.intersection(requested.union(control_names))
            )
            self._visible_names.intersection_update(self._skill_scope)
            self._executable_names.intersection_update(self._skill_scope)

            for name in self._skill_scope:
                exposure = self._registrations[name].exposure
                if exposure in {ToolExposure.RESIDENT, ToolExposure.CONTROL}:
                    self._visible_names.add(name)
                    self._executable_names.add(name)
            return self._skill_scope

    def search(
        self,
        query: str,
        *,
        limit: int = 8,
    ) -> tuple[ToolDescriptor, ...]:
        """Reveal matching deferred schemas only after authorization and scope."""

        with self._lock:
            matches = _search_registrations(
                self._registrations,
                query,
                allowed_names=self._skill_scope,
                limit=limit,
                exposure=ToolExposure.DEFERRED,
            )
            for registration in matches:
                name = registration.descriptor.name
                self._visible_names.add(name)
                self._executable_names.add(name)
            return tuple(_descriptor_copy(item.descriptor) for item in matches)


__all__ = [
    "ToolAccess",
    "ToolDescriptor",
    "ToolExposure",
    "ToolFactory",
    "ToolRegistry",
    "ToolSession",
]
