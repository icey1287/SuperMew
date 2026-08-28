from __future__ import annotations

import pytest

from backend.sandbox import (
    SandboxError,
    SandboxErrorCode,
    SandboxExecutionRequest,
    SandboxExecutionResult,
    SandboxExecutionSpec,
    SandboxIdentity,
    SandboxLanguage,
    SandboxLimits,
    SandboxReadiness,
    SandboxRuntimeConfig,
    validate_image_digest,
)


IMAGE = "registry.example/supermew-sandbox@sha256:" + ("a" * 64)


def _identity() -> SandboxIdentity:
    return SandboxIdentity(
        user_id="private-user",
        tenant_id="default",
        thread_id="thread-private",
        run_id="run-private",
    )


def test_identity_and_request_reprs_redact_source_and_run_context():
    identity = _identity()
    request = SandboxExecutionRequest(
        identity=identity,
        language="python",
        source="print('do-not-log')",
    )

    rendered = f"{identity!r} {request!r}"
    assert "private-user" not in rendered
    assert "thread-private" not in rendered
    assert "run-private" not in rendered
    assert "do-not-log" not in rendered
    assert len(identity.binding_hash) == 64


def test_image_requires_an_immutable_digest():
    assert validate_image_digest(IMAGE) == IMAGE
    local_image_id = "sha256:" + ("f" * 64)
    assert validate_image_digest(local_image_id) == local_image_id
    with_port = "registry.example:5000/team/image@sha256:" + ("d" * 64)
    assert validate_image_digest(with_port) == with_port

    for invalid in (
        "python:3.12",
        "registry.example/image@sha256:short",
        "https://registry.example/image@sha256:" + ("a" * 64),
        "registry.example/UPPER@sha256:" + ("a" * 64),
    ):
        with pytest.raises(ValueError, match="immutable"):
            validate_image_digest(invalid)


def test_limits_enforce_workspace_and_file_budget_relationships():
    with pytest.raises(ValueError, match="max_file_bytes"):
        SandboxLimits(max_file_bytes=20, max_total_file_bytes=10)

    with pytest.raises(ValueError, match="workspace_bytes"):
        SandboxLimits(
            workspace_bytes=1024 * 1024,
            max_file_bytes=2 * 1024 * 1024,
            max_total_file_bytes=2 * 1024 * 1024,
        )

    limits = SandboxLimits(max_output_bytes=1024)
    assert limits.protocol_output_bytes > limits.max_output_bytes
    assert limits.runner_payload()["max_output_bytes"] == 1024


def test_execution_spec_and_result_expose_only_bounded_public_state():
    request = SandboxExecutionRequest(
        identity=_identity(),
        language=SandboxLanguage.SH,
        source="printf safe",
    )
    spec = SandboxExecutionSpec(
        invocation_id="sbx_" + ("1" * 32),
        identity_binding=request.identity.binding_hash,
        language=request.language,
        source=request.source,
        image=IMAGE,
        limits=SandboxLimits(),
    )
    result = SandboxExecutionResult(
        exit_code=0,
        stdout="safe\n",
        stderr="",
        duration_ms=12,
        files_created=1,
    )

    assert "printf safe" not in repr(spec)
    assert "safe\n" not in repr(result)
    assert result.to_public_dict()["stdout"] == "safe\n"
    assert result.observability_metadata() == {
        "exit_code": 0,
        "output_bytes": 5,
        "files_created": 1,
        "truncated": False,
    }


def test_errors_and_readiness_use_stable_safe_metadata_only():
    error = SandboxError(
        SandboxErrorCode.ADAPTER_UNAVAILABLE,
        retryable=True,
        safe_details={"stage": "startup", "attempts": 1},
    )
    assert str(error) == "SANDBOX_ADAPTER_UNAVAILABLE"
    assert error.retryable is True
    assert dict(error.safe_details) == {"stage": "startup", "attempts": 1}

    with pytest.raises(ValueError, match="unknown key"):
        SandboxError(
            SandboxErrorCode.EXECUTION_FAILED,
            safe_details={"path": "x" * 129},
        )

    with pytest.raises(ValueError, match="bounded"):
        SandboxError(
            SandboxErrorCode.EXECUTION_FAILED,
            safe_details={"stage": "/private/host/path"},
        )

    readiness = SandboxReadiness(
        enabled=False,
        started=False,
        closed=False,
        ready=False,
        adapter="disabled",
        daemon_reachable=False,
        image_available=False,
        active_executions=0,
    )
    assert readiness.to_dict()["adapter"] == "disabled"


def test_runtime_config_repr_redacts_image_and_docker_endpoint() -> None:
    endpoint = "unix:///private/run/sandbox/docker.sock"
    config = SandboxRuntimeConfig(
        enabled=True,
        image=IMAGE,
        docker_host=endpoint,
    )

    rendered = repr(config)
    assert IMAGE not in rendered
    assert endpoint not in rendered
    assert "image_pinned=True" in rendered
