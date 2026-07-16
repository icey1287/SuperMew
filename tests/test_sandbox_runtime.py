from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest

from backend.sandbox import (
    SandboxError,
    SandboxExecutionRequest,
    SandboxExecutionResult,
    SandboxIdentity,
    SandboxLimits,
    SandboxRuntime,
    build_sandbox_runtime,
    clear_sandbox_runtime,
    get_sandbox_runtime,
    install_sandbox_runtime,
)


IMAGE = "registry.example/supermew-sandbox@sha256:" + ("b" * 64)


class FakeAdapter:
    name = "fake"

    def __init__(self, *, ready: bool = True, result: object | None = None) -> None:
        self.ready = ready
        self.result = result or SandboxExecutionResult(
            exit_code=0,
            stdout="ok",
            stderr="",
            duration_ms=1,
        )
        self.start_calls = 0
        self.close_calls = 0
        self.execute_calls = 0
        self.last_spec = None

    def start(self) -> None:
        self.start_calls += 1

    def close(self) -> None:
        self.close_calls += 1

    def readiness(self):
        return {
            "ready": self.ready and self.start_calls > self.close_calls,
            "daemon_reachable": self.ready,
            "image_available": self.ready,
        }

    def execute(self, spec, *, deadline_at, cancellation_probe):
        self.execute_calls += 1
        self.last_spec = spec
        assert deadline_at > 0
        return self.result


def _request(source: str = "print('ok')") -> SandboxExecutionRequest:
    return SandboxExecutionRequest(
        identity=SandboxIdentity(
            user_id="user",
            tenant_id="default",
            thread_id="thread",
            run_id="run",
        ),
        language="python",
        source=source,
    )


def test_disabled_runtime_never_probes_or_executes_adapter():
    adapter = FakeAdapter()
    runtime = SandboxRuntime(enabled=False, adapter=adapter)

    runtime.start()
    snapshot = runtime.readiness()
    assert snapshot.enabled is False
    assert snapshot.ready is False
    assert adapter.start_calls == 0

    with pytest.raises(SandboxError) as raised:
        runtime.execute(_request())
    assert raised.value.code == "SANDBOX_DISABLED"
    assert adapter.execute_calls == 0


def test_runtime_starts_executes_and_closes_through_small_interface():
    adapter = FakeAdapter()
    runtime = SandboxRuntime(
        enabled=True,
        image=IMAGE,
        limits=SandboxLimits(timeout_seconds=1),
        adapter=adapter,
    )
    runtime.configure_concurrency(1)

    runtime.start()
    assert runtime.readiness().ready is True
    result = runtime.execute(_request())
    assert result.stdout == "ok"
    assert adapter.last_spec.source == "print('ok')"
    assert adapter.last_spec.image == IMAGE
    assert runtime.readiness().active_executions == 0

    runtime.close()
    assert adapter.close_calls == 1
    assert runtime.readiness().closed is True
    with pytest.raises(SandboxError) as raised:
        runtime.execute(_request())
    assert raised.value.code == "SANDBOX_CLOSED"


def test_runtime_close_failure_remains_retryable_until_cleanup_succeeds() -> None:
    class FlakyCloseAdapter(FakeAdapter):
        def __init__(self) -> None:
            super().__init__()
            self.fail_close = True

        def close(self) -> None:
            self.close_calls += 1
            if self.fail_close:
                raise SandboxError("SANDBOX_CLEANUP_FAILED")

    adapter = FlakyCloseAdapter()
    runtime = SandboxRuntime(enabled=True, image=IMAGE, adapter=adapter)
    runtime.start()

    with pytest.raises(SandboxError) as first:
        runtime.close()
    assert first.value.code == "SANDBOX_CLEANUP_FAILED"
    assert runtime.readiness().closed is False
    assert runtime.readiness().ready is False

    adapter.fail_close = False
    runtime.close()
    assert adapter.close_calls == 2
    assert runtime.readiness().closed is True


def test_runtime_close_blocks_new_work_and_waits_for_active_execution() -> None:
    class BlockingAdapter(FakeAdapter):
        def __init__(self) -> None:
            super().__init__()
            self.execute_started = threading.Event()
            self.release_execute = threading.Event()
            self.close_started = threading.Event()

        def execute(self, spec, *, deadline_at, cancellation_probe):
            self.execute_calls += 1
            self.last_spec = spec
            self.execute_started.set()
            assert self.release_execute.wait(timeout=2)
            return self.result

        def close(self) -> None:
            self.close_calls += 1
            self.close_started.set()

    adapter = BlockingAdapter()
    runtime = SandboxRuntime(enabled=True, image=IMAGE, adapter=adapter)
    runtime.start()
    execution_errors: list[BaseException] = []
    close_errors: list[BaseException] = []

    def execute() -> None:
        try:
            runtime.execute(_request())
        except BaseException as exc:  # pragma: no cover - assertion below
            execution_errors.append(exc)

    def close() -> None:
        try:
            runtime.close()
        except BaseException as exc:  # pragma: no cover - assertion below
            close_errors.append(exc)

    execution_thread = threading.Thread(target=execute)
    execution_thread.start()
    assert adapter.execute_started.wait(timeout=1)

    close_thread = threading.Thread(target=close)
    close_thread.start()
    deadline = time.monotonic() + 1
    while runtime.readiness().ready and time.monotonic() < deadline:
        time.sleep(0.01)

    assert runtime.readiness().ready is False
    assert adapter.close_started.is_set() is False
    with pytest.raises(SandboxError) as blocked:
        runtime.execute(_request())
    assert blocked.value.code == "SANDBOX_CLEANUP_FAILED"
    assert adapter.execute_calls == 1

    adapter.release_execute.set()
    execution_thread.join(timeout=2)
    close_thread.join(timeout=2)

    assert execution_thread.is_alive() is False
    assert close_thread.is_alive() is False
    assert not execution_errors
    assert not close_errors
    assert adapter.close_started.is_set() is True
    assert runtime.readiness().closed is True


def test_enabled_runtime_fails_closed_when_adapter_is_not_ready():
    adapter = FakeAdapter(ready=False)
    runtime = SandboxRuntime(enabled=True, image=IMAGE, adapter=adapter)

    with pytest.raises(SandboxError) as raised:
        runtime.start()
    assert raised.value.code == "SANDBOX_NOT_READY"
    assert adapter.close_calls == 1


def test_runtime_rejects_oversized_source_before_adapter_execution():
    adapter = FakeAdapter()
    limits = SandboxLimits(max_source_bytes=4)
    runtime = SandboxRuntime(
        enabled=True,
        image=IMAGE,
        limits=limits,
        adapter=adapter,
    )
    runtime.start()

    with pytest.raises(SandboxError) as raised:
        runtime.execute(_request("12345"))
    assert raised.value.code == "SANDBOX_INVALID_REQUEST"
    assert adapter.execute_calls == 0


def test_runtime_checks_cancellation_before_allocating_a_slot():
    adapter = FakeAdapter()
    runtime = SandboxRuntime(enabled=True, image=IMAGE, adapter=adapter)
    runtime.start()

    with pytest.raises(SandboxError) as raised:
        runtime.execute(_request(), cancellation_probe=lambda: True)
    assert raised.value.code == "SANDBOX_CANCELLED"
    assert adapter.execute_calls == 0


def test_runtime_rejects_adapter_results_that_break_output_contract():
    adapter = FakeAdapter(
        result=SandboxExecutionResult(
            exit_code=0,
            stdout="too-large",
            stderr="",
            duration_ms=1,
        )
    )
    runtime = SandboxRuntime(
        enabled=True,
        image=IMAGE,
        limits=SandboxLimits(max_output_bytes=4),
        adapter=adapter,
    )
    runtime.start()

    with pytest.raises(SandboxError) as raised:
        runtime.execute(_request())
    assert raised.value.code == "SANDBOX_PROTOCOL_ERROR"


def test_runtime_rejects_adapter_duration_outside_execution_budget() -> None:
    adapter = FakeAdapter(
        result=SandboxExecutionResult(
            exit_code=0,
            stdout="ok",
            stderr="",
            duration_ms=2_001,
        )
    )
    runtime = SandboxRuntime(
        enabled=True,
        image=IMAGE,
        limits=SandboxLimits(timeout_seconds=1),
        adapter=adapter,
    )
    runtime.start()

    with pytest.raises(SandboxError) as raised:
        runtime.execute(_request())
    assert raised.value.code == "SANDBOX_PROTOCOL_ERROR"


def test_builder_defaults_to_disabled_without_importing_or_probing_docker():
    runtime = build_sandbox_runtime()
    runtime.start()
    assert runtime.readiness().to_dict() == {
        "enabled": False,
        "started": False,
        "closed": False,
        "ready": False,
        "adapter": "disabled",
        "daemon_reachable": False,
        "image_available": False,
        "active_executions": 0,
    }

    configured = build_sandbox_runtime(
        SimpleNamespace(enabled=False, max_concurrency=1)
    )
    assert configured.readiness().adapter == "disabled"


def test_installed_runtime_is_explicit_and_clearable():
    clear_sandbox_runtime()
    with pytest.raises(SandboxError) as missing:
        get_sandbox_runtime()
    assert missing.value.code == "SANDBOX_NOT_CONFIGURED"

    runtime = SandboxRuntime.disabled()
    install_sandbox_runtime(runtime)
    assert get_sandbox_runtime() is runtime
    clear_sandbox_runtime(runtime)
