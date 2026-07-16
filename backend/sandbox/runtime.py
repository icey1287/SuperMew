"""Process-wide orchestration runtime for isolated Sandbox execution."""

from __future__ import annotations

import math
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Protocol, runtime_checkable
from uuid import uuid4

from backend.sandbox.contracts import (
    SandboxError,
    SandboxErrorCode,
    SandboxExecutionRequest,
    SandboxExecutionResult,
    SandboxExecutionSpec,
    SandboxLimits,
    SandboxReadiness,
)


CancellationProbe = Callable[[], bool]


@runtime_checkable
class SandboxAdapter(Protocol):
    """Adapter Seam hidden behind ``SandboxRuntime``."""

    name: str

    def start(self) -> None: ...

    def close(self) -> None: ...

    def readiness(self) -> Mapping[str, object]: ...

    def execute(
        self,
        spec: SandboxExecutionSpec,
        *,
        deadline_at: float,
        cancellation_probe: CancellationProbe | None,
    ) -> SandboxExecutionResult: ...


class DisabledSandboxAdapter:
    """Safe default Adapter that never touches Docker or the host filesystem."""

    name = "disabled"

    def start(self) -> None:
        return None

    def close(self) -> None:
        return None

    def readiness(self) -> Mapping[str, object]:
        return {
            "ready": False,
            "daemon_reachable": False,
            "image_available": False,
        }

    def execute(
        self,
        spec: SandboxExecutionSpec,
        *,
        deadline_at: float,
        cancellation_probe: CancellationProbe | None,
    ) -> SandboxExecutionResult:
        del spec, deadline_at, cancellation_probe
        raise SandboxError(SandboxErrorCode.DISABLED)


@dataclass(frozen=True, slots=True, repr=False)
class SandboxRuntimeConfig:
    enabled: bool = False
    adapter: str = "docker"
    image: str = ""
    limits: SandboxLimits = SandboxLimits()
    docker_binary: str = "docker"
    docker_host: str | None = None
    require_rootless: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.enabled, bool):
            raise TypeError("enabled must be a bool")
        if self.adapter not in {"docker", "disabled"}:
            raise ValueError("adapter must be docker or disabled")
        if not isinstance(self.image, str):
            raise TypeError("image must be a string")
        if not isinstance(self.limits, SandboxLimits):
            raise TypeError("limits must be SandboxLimits")
        if not isinstance(self.docker_binary, str) or not self.docker_binary.strip():
            raise ValueError("docker_binary must be a non-empty string")
        if self.docker_host is not None and not isinstance(self.docker_host, str):
            raise TypeError("docker_host must be a string or None")
        if not isinstance(self.require_rootless, bool):
            raise TypeError("require_rootless must be a bool")

    def __repr__(self) -> str:
        return (
            "SandboxRuntimeConfig("
            f"enabled={self.enabled!r}, adapter={self.adapter!r}, "
            f"image_pinned={bool(self.image)!r}, "
            f"docker_host_configured={self.docker_host is not None!r}, "
            f"require_rootless={self.require_rootless!r})"
        )


class SandboxRuntime:
    """Deep Module hiding lifecycle, budgets, concurrency, and Adapter details."""

    def __init__(
        self,
        *,
        enabled: bool = False,
        image: str = "",
        limits: SandboxLimits | None = None,
        adapter: SandboxAdapter | None = None,
        max_concurrency: int = 2,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if not isinstance(enabled, bool):
            raise TypeError("enabled must be a bool")
        self.enabled = enabled
        self.image = image.strip() if isinstance(image, str) else image
        if not isinstance(self.image, str):
            raise TypeError("image must be a string")
        self.limits = limits or SandboxLimits()
        self.adapter = adapter or DisabledSandboxAdapter()
        if not isinstance(self.adapter, SandboxAdapter):
            raise TypeError("adapter must satisfy SandboxAdapter")
        if (
            isinstance(max_concurrency, bool)
            or not isinstance(max_concurrency, int)
            or max_concurrency <= 0
        ):
            raise ValueError("max_concurrency must be a positive integer")
        self._monotonic = monotonic
        self._lifecycle_lock = threading.RLock()
        self._lifecycle_condition = threading.Condition(self._lifecycle_lock)
        self._gate = threading.BoundedSemaphore(max_concurrency)
        self._max_concurrency = max_concurrency
        self._active_executions = 0
        self._started = False
        self._closed = False
        self._closing = False

    @classmethod
    def disabled(cls) -> SandboxRuntime:
        return cls(enabled=False, adapter=DisabledSandboxAdapter())

    def configure_concurrency(self, maximum: int) -> None:
        if isinstance(maximum, bool) or not isinstance(maximum, int) or maximum <= 0:
            raise ValueError("maximum concurrency must be a positive integer")
        with self._lifecycle_lock:
            if self._started or self._closing or self._active_executions:
                raise RuntimeError("Sandbox concurrency is immutable after start")
            self._gate = threading.BoundedSemaphore(maximum)
            self._max_concurrency = maximum

    def start(self) -> None:
        with self._lifecycle_lock:
            if self._closed:
                raise SandboxError(SandboxErrorCode.CLOSED)
            if self._closing:
                raise SandboxError(SandboxErrorCode.CLEANUP_FAILED)
            if self._started:
                return
            if not self.enabled:
                # The disabled path intentionally performs no Adapter probe.
                return
            try:
                self.adapter.start()
                snapshot = dict(self.adapter.readiness())
            except SandboxError as exc:
                self._close_after_failed_start(exc)
            except Exception:
                self._close_after_failed_start(
                    SandboxError(
                        SandboxErrorCode.ADAPTER_UNAVAILABLE,
                        retryable=True,
                    )
                )
            if snapshot.get("ready") is not True:
                self._close_after_failed_start(
                    SandboxError(
                        SandboxErrorCode.NOT_READY,
                        retryable=True,
                    )
                )
            self._started = True

    def _close_after_failed_start(self, failure: SandboxError) -> None:
        try:
            self.adapter.close()
        except Exception:
            raise SandboxError(SandboxErrorCode.CLEANUP_FAILED) from failure
        raise failure

    def close(self) -> None:
        with self._lifecycle_condition:
            if self._closed:
                return
            self._closing = True
            self._started = False
            while self._active_executions:
                self._lifecycle_condition.wait()
            try:
                self.adapter.close()
            except SandboxError:
                raise
            except Exception:
                raise SandboxError(SandboxErrorCode.CLEANUP_FAILED) from None
            self._closing = False
            self._closed = True
            self._lifecycle_condition.notify_all()

    def readiness(self) -> SandboxReadiness:
        with self._lifecycle_lock:
            started = self._started
            closed = self._closed
            closing = self._closing
            active = self._active_executions
        try:
            adapter_snapshot = dict(self.adapter.readiness())
        except Exception:
            adapter_snapshot = {}
        adapter_ready = adapter_snapshot.get("ready") is True
        return SandboxReadiness(
            enabled=self.enabled,
            started=started,
            closed=closed,
            ready=(
                self.enabled
                and started
                and not closing
                and not closed
                and adapter_ready
            ),
            adapter=str(getattr(self.adapter, "name", "unknown")),
            daemon_reachable=adapter_snapshot.get("daemon_reachable") is True,
            image_available=adapter_snapshot.get("image_available") is True,
            active_executions=active,
        )

    def execute(
        self,
        request: SandboxExecutionRequest,
        *,
        deadline_at: float | None = None,
        cancellation_probe: CancellationProbe | None = None,
    ) -> SandboxExecutionResult:
        if not isinstance(request, SandboxExecutionRequest):
            raise SandboxError(SandboxErrorCode.INVALID_REQUEST)
        source_size = len(request.source.encode("utf-8"))
        if source_size > self.limits.max_source_bytes:
            raise SandboxError(SandboxErrorCode.INVALID_REQUEST)

        with self._lifecycle_lock:
            if self._closed:
                raise SandboxError(SandboxErrorCode.CLOSED)
            if self._closing:
                raise SandboxError(SandboxErrorCode.CLEANUP_FAILED)
            if not self.enabled:
                raise SandboxError(SandboxErrorCode.DISABLED)
            if not self._started:
                raise SandboxError(SandboxErrorCode.NOT_READY, retryable=True)

        self._raise_if_cancelled(cancellation_probe)
        local_deadline = self._monotonic() + self.limits.timeout_seconds
        if deadline_at is not None and (
            isinstance(deadline_at, bool)
            or not isinstance(deadline_at, (int, float))
            or not math.isfinite(float(deadline_at))
        ):
            raise SandboxError(SandboxErrorCode.INVALID_REQUEST)
        effective_deadline = local_deadline
        if deadline_at is not None:
            effective_deadline = min(local_deadline, float(deadline_at))
        if effective_deadline <= self._monotonic():
            raise SandboxError(SandboxErrorCode.TIMEOUT)
        self._acquire(effective_deadline, cancellation_probe)
        with self._lifecycle_condition:
            if self._closed:
                self._gate.release()
                raise SandboxError(SandboxErrorCode.CLOSED)
            if self._closing:
                self._gate.release()
                raise SandboxError(SandboxErrorCode.CLEANUP_FAILED)
            if not self._started:
                self._gate.release()
                raise SandboxError(SandboxErrorCode.NOT_READY, retryable=True)
            self._active_executions += 1
        try:
            spec = SandboxExecutionSpec(
                invocation_id=f"sbx_{uuid4().hex}",
                identity_binding=request.identity.binding_hash,
                language=request.language,
                source=request.source,
                image=self.image,
                limits=self.limits,
            )
            try:
                result = self.adapter.execute(
                    spec,
                    deadline_at=effective_deadline,
                    cancellation_probe=cancellation_probe,
                )
            except SandboxError:
                raise
            except Exception:
                raise SandboxError(SandboxErrorCode.EXECUTION_FAILED) from None
            if not isinstance(result, SandboxExecutionResult):
                raise SandboxError(SandboxErrorCode.PROTOCOL_ERROR)
            if result.output_bytes > self.limits.max_output_bytes:
                raise SandboxError(SandboxErrorCode.PROTOCOL_ERROR)
            if result.files_created > self.limits.max_files:
                raise SandboxError(SandboxErrorCode.PROTOCOL_ERROR)
            if result.duration_ms > int(self.limits.timeout_seconds * 1000) + 1_000:
                raise SandboxError(SandboxErrorCode.PROTOCOL_ERROR)
            return result
        finally:
            with self._lifecycle_condition:
                self._active_executions -= 1
                self._lifecycle_condition.notify_all()
            self._gate.release()

    def _acquire(
        self,
        deadline_at: float,
        cancellation_probe: CancellationProbe | None,
    ) -> None:
        while True:
            self._raise_if_cancelled(cancellation_probe)
            with self._lifecycle_lock:
                if self._closed:
                    raise SandboxError(SandboxErrorCode.CLOSED)
                if self._closing:
                    raise SandboxError(SandboxErrorCode.CLEANUP_FAILED)
                if not self._started:
                    raise SandboxError(SandboxErrorCode.NOT_READY, retryable=True)
            remaining = deadline_at - self._monotonic()
            if remaining <= 0:
                raise SandboxError(SandboxErrorCode.BUSY, retryable=True)
            if self._gate.acquire(timeout=min(remaining, 0.05)):
                return

    @staticmethod
    def _raise_if_cancelled(cancellation_probe: CancellationProbe | None) -> None:
        if cancellation_probe is None:
            return
        try:
            cancelled = bool(cancellation_probe())
        except SandboxError:
            raise
        except Exception:
            raise SandboxError(SandboxErrorCode.CANCELLED) from None
        if cancelled:
            raise SandboxError(SandboxErrorCode.CANCELLED)


def build_sandbox_runtime(
    settings: object | None = None,
    *,
    adapter_factory: Callable[[SandboxRuntimeConfig], SandboxAdapter] | None = None,
) -> SandboxRuntime:
    """Map application-like settings to a runtime without importing Settings."""

    source = getattr(settings, "sandbox", settings) if settings is not None else None
    if source is None:
        config = SandboxRuntimeConfig()
    else:
        limits = SandboxLimits(
            timeout_seconds=getattr(source, "timeout_seconds", 15.0),
            cpu_count=getattr(source, "cpu_limit", 0.5),
            memory_bytes=getattr(source, "memory_bytes", 256 * 1024 * 1024),
            pids_limit=getattr(source, "pids_limit", 32),
            workspace_bytes=getattr(source, "workspace_bytes", 64 * 1024 * 1024),
            max_source_bytes=getattr(source, "max_source_bytes", 64 * 1024),
            max_output_bytes=getattr(source, "max_output_bytes", 64 * 1024),
            max_files=getattr(source, "max_files", 32),
            max_file_bytes=getattr(source, "max_file_bytes", 8 * 1024 * 1024),
            max_total_file_bytes=getattr(
                source,
                "max_total_file_bytes",
                32 * 1024 * 1024,
            ),
            max_path_bytes=getattr(source, "max_path_bytes", 240),
            max_path_depth=getattr(source, "max_path_depth", 8),
            cleanup_timeout_seconds=getattr(source, "cleanup_timeout_seconds", 3.0),
        )
        config = SandboxRuntimeConfig(
            enabled=bool(getattr(source, "enabled", False)),
            adapter=str(getattr(source, "adapter", "docker")),
            image=str(getattr(source, "docker_image", getattr(source, "image", ""))),
            limits=limits,
            docker_binary=str(getattr(source, "docker_binary", "docker")),
            docker_host=getattr(source, "docker_host", None),
            require_rootless=bool(getattr(source, "require_rootless", False)),
        )

    if not config.enabled or config.adapter == "disabled":
        runtime = SandboxRuntime.disabled()
    else:
        if adapter_factory is None:
            from backend.sandbox.docker import DockerSandboxAdapter

            adapter = DockerSandboxAdapter.from_runtime_config(config)
        else:
            adapter = adapter_factory(config)
        runtime = SandboxRuntime(
            enabled=True,
            image=config.image,
            limits=config.limits,
            adapter=adapter,
            max_concurrency=2,
        )
    concurrency = int(getattr(source, "max_concurrency", 2)) if source else 2
    runtime.configure_concurrency(concurrency)
    return runtime


_runtime_lock = threading.RLock()
_installed_runtime: SandboxRuntime | None = None


def install_sandbox_runtime(runtime: SandboxRuntime) -> None:
    if not isinstance(runtime, SandboxRuntime):
        raise TypeError("runtime must be SandboxRuntime")
    global _installed_runtime
    with _runtime_lock:
        _installed_runtime = runtime


def clear_sandbox_runtime(runtime: SandboxRuntime | None = None) -> None:
    global _installed_runtime
    with _runtime_lock:
        if runtime is None or _installed_runtime is runtime:
            _installed_runtime = None


def get_sandbox_runtime() -> SandboxRuntime:
    with _runtime_lock:
        runtime = _installed_runtime
    if runtime is None:
        raise SandboxError(SandboxErrorCode.NOT_CONFIGURED)
    return runtime


__all__ = [
    "CancellationProbe",
    "DisabledSandboxAdapter",
    "SandboxAdapter",
    "SandboxRuntime",
    "SandboxRuntimeConfig",
    "build_sandbox_runtime",
    "clear_sandbox_runtime",
    "get_sandbox_runtime",
    "install_sandbox_runtime",
]
