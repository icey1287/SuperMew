from __future__ import annotations

import base64
import json
import threading
import time

import pytest

from backend.sandbox import (
    SandboxError,
    SandboxErrorCode,
    SandboxExecutionSpec,
    SandboxLimits,
)
from backend.sandbox.docker import (
    DockerCommandResult,
    DockerSandboxAdapter,
    DockerSandboxConfig,
)


IMAGE = "registry.example/supermew-sandbox@sha256:" + ("c" * 64)
ENTRYPOINT = [
    "/usr/local/bin/python",
    "-I",
    "-B",
    "/opt/supermew/runner.py",
]


def _result(returncode: int = 0, stdout: bytes = b"", stderr: bytes = b""):
    return DockerCommandResult(
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
        duration_ms=1,
    )


def _runner_success(stdout: bytes = b"ok\n") -> bytes:
    return json.dumps(
        {
            "schema_version": 1,
            "success": True,
            "error_code": None,
            "exit_code": 0,
            "stdout_b64": base64.b64encode(stdout).decode("ascii"),
            "stderr_b64": "",
            "duration_ms": 5,
            "files_created": 0,
            "stdout_truncated": False,
            "stderr_truncated": False,
        },
        sort_keys=True,
    ).encode("ascii")


class FakeDockerRunner:
    def __init__(self, *, start_failure: SandboxError | None = None) -> None:
        self.calls: list[dict] = []
        self.start_failure = start_failure

    def run(
        self,
        argv,
        *,
        input_bytes,
        deadline_at,
        max_output_bytes,
        env,
        cancellation_probe=None,
    ):
        self.calls.append(
            {
                "argv": list(argv),
                "input_bytes": input_bytes,
                "deadline_at": deadline_at,
                "max_output_bytes": max_output_bytes,
                "env": dict(env),
                "cancellation_probe": cancellation_probe,
            }
        )
        command = argv[1]
        if command == "version":
            return _result(stdout=b'{"Version":"test"}')
        if command == "info":
            return _result(stdout=b'["name=rootless"]')
        if command == "image":
            return _result(
                stdout=json.dumps(
                    {
                        "Id": "sha256:" + ("f" * 64),
                        "RepoDigests": [IMAGE],
                        "Config": {
                            "User": "65532:65532",
                            "Entrypoint": ENTRYPOINT,
                        },
                    }
                ).encode("utf-8")
            )
        if command == "container":
            return _result(stdout=b"Total reclaimed space: 0B\n")
        if command == "create":
            return _result(stdout=(b"d" * 64) + b"\n")
        if command == "start":
            if self.start_failure is not None:
                raise self.start_failure
            return _result(stdout=_runner_success())
        if command == "inspect":
            return _result(stdout=b'{"ExitCode":0,"OOMKilled":false}')
        if command == "rm":
            return _result()
        raise AssertionError(f"unexpected Docker command: {argv}")


def _spec(source: str = "print('private-source')") -> SandboxExecutionSpec:
    return SandboxExecutionSpec(
        invocation_id="sbx_" + ("1" * 32),
        identity_binding="e" * 64,
        language="python",
        source=source,
        image=IMAGE,
        limits=SandboxLimits(timeout_seconds=2),
    )


def _started_adapter(runner: FakeDockerRunner | None = None):
    runner = runner or FakeDockerRunner()
    adapter = DockerSandboxAdapter(
        config=DockerSandboxConfig(
            image=IMAGE,
            docker_host="unix:///run/supermew-sandbox/docker.sock",
            require_rootless=True,
        ),
        runner=runner,
    )
    adapter.start()
    return adapter, runner


def test_start_requires_reachable_daemon_pinned_image_and_fixed_entrypoint():
    adapter, runner = _started_adapter()

    assert adapter.readiness() == {
        "ready": True,
        "daemon_reachable": True,
        "image_available": True,
        "rootless": True,
        "active_containers": 0,
    }
    assert [call["argv"][1] for call in runner.calls] == [
        "version",
        "info",
        "image",
        "container",
    ]
    image_call = next(call for call in runner.calls if call["argv"][1] == "image")
    assert image_call["argv"][-1] == IMAGE


def test_enabled_adapter_fails_closed_when_image_is_missing_or_untrusted():
    class MissingImageRunner(FakeDockerRunner):
        def run(self, argv, **kwargs):
            if argv[1] == "image":
                return _result(returncode=1, stderr=b"redacted by adapter")
            return super().run(argv, **kwargs)

    adapter = DockerSandboxAdapter(
        config=DockerSandboxConfig(image=IMAGE),
        runner=MissingImageRunner(),
    )
    with pytest.raises(SandboxError) as missing:
        adapter.start()
    assert missing.value.code == "SANDBOX_IMAGE_UNAVAILABLE"
    assert "redacted" not in str(missing.value)
    assert adapter.readiness()["ready"] is False

    class WrongEntrypointRunner(FakeDockerRunner):
        def run(self, argv, **kwargs):
            if argv[1] == "image":
                return _result(
                    stdout=json.dumps(
                        {
                            "RepoDigests": [IMAGE],
                            "Config": {
                                "User": "65532:65532",
                                "Entrypoint": ["/bin/sh"],
                            },
                        }
                    ).encode("utf-8")
                )
            return super().run(argv, **kwargs)

    untrusted = DockerSandboxAdapter(
        config=DockerSandboxConfig(image=IMAGE),
        runner=WrongEntrypointRunner(),
    )
    with pytest.raises(SandboxError) as wrong:
        untrusted.start()
    assert wrong.value.code == "SANDBOX_IMAGE_UNAVAILABLE"


def test_create_uses_full_isolation_without_host_mounts_or_host_environment():
    adapter, runner = _started_adapter()
    source = "print('private-source')"

    result = adapter.execute(
        _spec(source),
        deadline_at=time.monotonic() + 5,
        cancellation_probe=lambda: False,
    )
    assert result.stdout == "ok\n"

    create = next(call for call in runner.calls if call["argv"][1] == "create")
    argv = create["argv"]
    joined = " ".join(argv)
    for expected in (
        "--pull=never",
        "--network none",
        "--read-only",
        "--cap-drop ALL",
        "no-new-privileges:true",
        "--pids-limit 32",
        "--memory 268435456",
        "--memory-swap 268435456",
        "--cpus 0.5",
        "--ipc none",
        "--user 65532:65532",
        "--log-driver none",
    ):
        assert expected in joined
    assert "--tmpfs /workspace:" in joined
    assert "noexec" in joined
    assert "nosuid" in joined
    assert "nodev" in joined
    assert "--mount" not in argv
    assert "-v" not in argv
    assert "docker.sock" not in joined
    assert source not in joined

    start = next(call for call in runner.calls if call["argv"][1] == "start")
    assert source.encode("utf-8") not in start["input_bytes"]
    frame = json.loads(start["input_bytes"].decode("ascii"))
    assert base64.b64decode(frame["source_b64"]).decode("utf-8") == source
    assert set(start["env"]) == {
        "PATH",
        "LANG",
        "LC_ALL",
        "HOME",
        "DOCKER_CONFIG",
        "DOCKER_HOST",
    }
    assert start["env"]["HOME"] == "/nonexistent"
    assert start["env"]["DOCKER_HOST"].startswith("unix://")
    assert runner.calls[-1]["argv"][1:4] == ["rm", "--force", "--volumes"]


def test_timeout_or_cancellation_still_force_removes_container():
    runner = FakeDockerRunner(start_failure=SandboxError(SandboxErrorCode.TIMEOUT))
    adapter, runner = _started_adapter(runner)

    with pytest.raises(SandboxError) as raised:
        adapter.execute(
            _spec(),
            deadline_at=time.monotonic() + 5,
            cancellation_probe=lambda: False,
        )
    assert raised.value.code == "SANDBOX_TIMEOUT"
    assert any(call["argv"][1] == "rm" for call in runner.calls)
    assert adapter.readiness()["active_containers"] == 0


def test_cleanup_failure_keeps_orphan_tracked_and_blocks_until_retry() -> None:
    class FlakyCleanupRunner(FakeDockerRunner):
        def __init__(self) -> None:
            super().__init__()
            self.fail_cleanup = True

        def run(self, argv, **kwargs):
            if argv[1] == "rm" and self.fail_cleanup:
                self.calls.append({"argv": list(argv), **kwargs})
                return _result(returncode=1, stderr=b"daemon unavailable")
            return super().run(argv, **kwargs)

    runner = FlakyCleanupRunner()
    adapter, runner = _started_adapter(runner)

    with pytest.raises(SandboxError) as cleanup:
        adapter.execute(
            _spec(),
            deadline_at=time.monotonic() + 5,
            cancellation_probe=None,
        )

    assert cleanup.value.code == "SANDBOX_CLEANUP_FAILED"
    assert adapter.readiness()["ready"] is False
    assert adapter.readiness()["active_containers"] == 1
    create_calls = sum(call["argv"][1] == "create" for call in runner.calls)

    with pytest.raises(SandboxError) as blocked:
        adapter.execute(
            _spec(),
            deadline_at=time.monotonic() + 5,
            cancellation_probe=None,
        )
    assert blocked.value.code == "SANDBOX_CLEANUP_FAILED"
    assert sum(call["argv"][1] == "create" for call in runner.calls) == create_calls

    with pytest.raises(SandboxError) as first_close:
        adapter.close()
    assert first_close.value.code == "SANDBOX_CLEANUP_FAILED"
    assert adapter.readiness()["active_containers"] == 1

    runner.fail_cleanup = False
    adapter.close()
    assert adapter.readiness()["active_containers"] == 0
    with pytest.raises(SandboxError) as closed:
        adapter.start()
    assert closed.value.code == "SANDBOX_CLOSED"


def test_close_cannot_finish_before_inflight_container_create_is_tracked() -> None:
    class BlockingCreateRunner(FakeDockerRunner):
        def __init__(self) -> None:
            super().__init__()
            self.create_entered = threading.Event()
            self.release_create = threading.Event()

        def run(self, argv, **kwargs):
            if argv[1] == "create":
                self.calls.append({"argv": list(argv), **kwargs})
                self.create_entered.set()
                assert self.release_create.wait(timeout=2)
                return _result(stdout=(b"d" * 64) + b"\n")
            return super().run(argv, **kwargs)

    runner = BlockingCreateRunner()
    adapter, runner = _started_adapter(runner)
    execution_errors: list[BaseException] = []
    close_errors: list[BaseException] = []

    def execute() -> None:
        try:
            adapter.execute(
                _spec(),
                deadline_at=time.monotonic() + 5,
                cancellation_probe=None,
            )
        except BaseException as exc:  # pragma: no cover - assertion below
            execution_errors.append(exc)

    def close() -> None:
        try:
            adapter.close()
        except BaseException as exc:  # pragma: no cover - assertion below
            close_errors.append(exc)

    execution_thread = threading.Thread(target=execute)
    execution_thread.start()
    assert runner.create_entered.wait(timeout=1)

    close_thread = threading.Thread(target=close)
    close_thread.start()
    time.sleep(0.05)
    assert close_thread.is_alive() is True
    assert not any(call["argv"][1] == "rm" for call in runner.calls)

    runner.release_create.set()
    execution_thread.join(timeout=2)
    close_thread.join(timeout=2)

    assert execution_thread.is_alive() is False
    assert close_thread.is_alive() is False
    assert not execution_errors
    assert not close_errors
    create_index = next(
        index for index, call in enumerate(runner.calls) if call["argv"][1] == "create"
    )
    remove_indexes = [
        index for index, call in enumerate(runner.calls) if call["argv"][1] == "rm"
    ]
    assert remove_indexes
    assert all(index > create_index for index in remove_indexes)
    assert adapter.readiness()["active_containers"] == 0


def test_runner_protocol_is_strict_and_cannot_expand_output_budget():
    class MaliciousRunner(FakeDockerRunner):
        def run(self, argv, **kwargs):
            if argv[1] == "start":
                self.calls.append({"argv": list(argv), **kwargs})
                payload = json.loads(_runner_success())
                payload["host_path"] = "/private/host"
                return _result(stdout=json.dumps(payload).encode("ascii"))
            return super().run(argv, **kwargs)

    adapter, _runner = _started_adapter(MaliciousRunner())
    with pytest.raises(SandboxError) as raised:
        adapter.execute(
            _spec(),
            deadline_at=time.monotonic() + 5,
            cancellation_probe=None,
        )
    assert raised.value.code == "SANDBOX_PROTOCOL_ERROR"


def test_config_rejects_mutable_images_remote_daemons_and_root_workload():
    with pytest.raises(ValueError, match="immutable"):
        DockerSandboxConfig(image="python:3.12")
    with pytest.raises(ValueError, match="Unix"):
        DockerSandboxConfig(image=IMAGE, docker_host="tcp://docker.example:2376")
    with pytest.raises(ValueError, match="non-root"):
        DockerSandboxConfig(image=IMAGE, user="0:0")


def test_docker_config_and_command_result_reprs_redact_host_details() -> None:
    host = "unix:///private/run/supermew/docker.sock"
    config = DockerSandboxConfig(image=IMAGE, docker_host=host)
    result = _result(
        stdout=b"private sandbox output",
        stderr=b"private daemon diagnostic",
    )

    rendered = f"{config!r} {result!r}"
    assert host not in rendered
    assert IMAGE not in rendered
    assert "private sandbox output" not in rendered
    assert "private daemon diagnostic" not in rendered
