from types import SimpleNamespace

from backend.core.settings import SandboxSettings
from backend.sandbox import (
    SandboxExecutionResult,
    SandboxRuntime,
    clear_sandbox_runtime,
    install_sandbox_runtime,
)
from backend.tools.catalog import build_default_tool_registry, configured_secret_names
from backend.tools.contracts import ToolResultV1
from backend.tools.registry import ToolAccess, ToolExposure
from backend.tools.sandbox import make_sandbox_execute


_IMAGE = "sha256:" + ("d" * 64)


class _Adapter:
    name = "fake"

    def __init__(self) -> None:
        self.specs = []
        self.started = False

    def start(self) -> None:
        self.started = True

    def close(self) -> None:
        self.started = False

    def readiness(self):
        return {
            "ready": self.started,
            "daemon_reachable": self.started,
            "image_available": self.started,
        }

    def execute(self, spec, *, deadline_at, cancellation_probe):
        del deadline_at, cancellation_probe
        self.specs.append(spec)
        return SandboxExecutionResult(
            exit_code=0,
            stdout="42\n",
            stderr="",
            duration_ms=7,
            files_created=1,
        )


def _settings(*, enabled: bool) -> SandboxSettings:
    return SandboxSettings(
        _env_file=None,
        SANDBOX_ENABLED=enabled,
        SANDBOX_DOCKER_IMAGE=_IMAGE if enabled else "",
    )


def _access(*, role: str = "admin", approved: bool = True) -> ToolAccess:
    return ToolAccess(
        roles=frozenset({role}),
        available_secrets=frozenset({"SANDBOX_RUNTIME"}),
        caller_allowed_tools=frozenset({"sandbox_execute"}),
        approved_tools=(frozenset({"sandbox_execute"}) if approved else frozenset()),
        allowed_network_policies=frozenset({"none"}),
    )


def test_catalog_registers_one_approval_only_sandbox_tool() -> None:
    settings = _settings(enabled=True)
    registry = build_default_tool_registry(sandbox_settings=settings)
    descriptor = registry.descriptor("sandbox_execute")

    assert descriptor is not None
    assert descriptor.group == "sandbox-execution"
    assert descriptor.resource_scope == "code-execution"
    assert descriptor.network_policy == "none"
    assert descriptor.requires_approval is True
    assert descriptor.idempotent is False
    assert descriptor.required_roles == frozenset({"admin"})
    assert descriptor.required_secrets == frozenset({"SANDBOX_RUNTIME"})
    assert registry.exposure("sandbox_execute") is ToolExposure.DEFERRED
    assert registry.authorize("sandbox_execute", _access()) is True
    assert (
        registry.authorize(
            "sandbox_execute",
            _access(role="user"),
        )
        is False
    )
    assert (
        registry.authorize(
            "sandbox_execute",
            _access(approved=False),
        )
        is False
    )


def test_disabled_sandbox_never_declares_runtime_capability() -> None:
    disabled = _settings(enabled=False)
    registry = build_default_tool_registry(sandbox_settings=disabled)

    assert "SANDBOX_RUNTIME" not in configured_secret_names(
        registry,
        sandbox_settings=disabled,
    )
    assert "SANDBOX_RUNTIME" in configured_secret_names(
        registry,
        sandbox_settings=_settings(enabled=True),
    )


def test_tool_executes_through_installed_runtime_without_exposing_identity() -> None:
    adapter = _Adapter()
    runtime = SandboxRuntime(
        enabled=True,
        image=_IMAGE,
        adapter=adapter,
    )
    runtime.start()
    install_sandbox_runtime(runtime)
    context = SimpleNamespace(
        user_id="admin",
        tenant_id="default",
        thread_id="thread-private",
        run_id="run-private",
        request_context=SimpleNamespace(provider_runtime=lambda: (None, None)),
        check_deadline=lambda: None,
    )
    source = "print(6 * 7)"
    try:
        payload = make_sandbox_execute(context).invoke(
            {"language": "python", "source": source}
        )
    finally:
        clear_sandbox_runtime(runtime)
        runtime.close()

    result = ToolResultV1.model_validate(payload)
    assert result.success is True
    assert result.data["stdout"] == "42\n"
    assert result.duration_ms == 7
    assert result.observability_metadata == {
        "exit_code": 0,
        "files_created": 1,
        "output_bytes": 3,
        "truncated": False,
    }
    assert len(adapter.specs) == 1
    rendered = result.model_dump_json() + repr(adapter.specs[0])
    assert source not in rendered
    assert "thread-private" not in rendered
    assert "run-private" not in rendered


def test_tool_projects_invalid_input_to_a_stable_failure() -> None:
    context = SimpleNamespace(
        user_id="admin",
        tenant_id="default",
        thread_id="thread-1",
        run_id="run-1",
        request_context=SimpleNamespace(provider_runtime=lambda: (None, None)),
        check_deadline=lambda: None,
    )

    payload = make_sandbox_execute(context).invoke(
        {"language": "javascript", "source": "alert(1)"}
    )
    result = ToolResultV1.model_validate(payload)

    assert result.success is False
    assert result.error_code == "SANDBOX_INVALID_REQUEST"
    assert "javascript" not in result.model_dump_json()
