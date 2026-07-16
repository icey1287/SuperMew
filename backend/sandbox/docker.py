"""Docker CLI Adapter for the isolated Sandbox Module.

The Adapter never invokes a host shell, never bind-mounts a host path, and
never forwards the host environment.  Source travels only through a bounded
stdin protocol to a fixed, digest-pinned image entrypoint.
"""

from __future__ import annotations

import base64
import json
import math
import os
import re
import subprocess
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from backend.sandbox.contracts import (
    SandboxError,
    SandboxErrorCode,
    SandboxExecutionResult,
    SandboxExecutionSpec,
    validate_image_digest,
)
from backend.sandbox.runtime import CancellationProbe, SandboxRuntimeConfig


_CONTAINER_ID_RE = re.compile(r"[0-9a-f]{12,64}\Z")
_SAFE_BINARY_RE = re.compile(r"[^\x00-\x20\x7f]{1,1024}\Z")
_EXPECTED_ENTRYPOINT = (
    "/usr/local/bin/python",
    "-I",
    "-B",
    "/opt/supermew/runner.py",
)
_RUNNER_KEYS = frozenset(
    {
        "schema_version",
        "success",
        "error_code",
        "exit_code",
        "stdout_b64",
        "stderr_b64",
        "duration_ms",
        "files_created",
        "stdout_truncated",
        "stderr_truncated",
    }
)


@dataclass(frozen=True, slots=True, repr=False)
class DockerSandboxConfig:
    image: str
    docker_binary: str = "docker"
    docker_host: str | None = None
    require_rootless: bool = False
    user: str = "65532:65532"
    managed_label: str = "com.supermew.sandbox.managed=true"

    def __post_init__(self) -> None:
        object.__setattr__(self, "image", validate_image_digest(self.image))
        if (
            not isinstance(self.docker_binary, str)
            or _SAFE_BINARY_RE.fullmatch(self.docker_binary) is None
        ):
            raise ValueError("docker_binary must be bounded executable text")
        if self.docker_host is not None:
            if (
                not isinstance(self.docker_host, str)
                or not self.docker_host.startswith("unix://")
                or len(self.docker_host) > 2048
                or _SAFE_BINARY_RE.fullmatch(self.docker_host) is None
            ):
                raise ValueError("docker_host must be a bounded local Unix endpoint")
        if not isinstance(self.require_rootless, bool):
            raise TypeError("require_rootless must be a bool")
        if self.user != "65532:65532":
            raise ValueError("Sandbox workload user is fixed and non-root")
        if self.managed_label != "com.supermew.sandbox.managed=true":
            raise ValueError("managed_label is fixed")

    def __repr__(self) -> str:
        return (
            "DockerSandboxConfig("
            "image_pinned=True, "
            f"docker_host_configured={self.docker_host is not None!r}, "
            f"require_rootless={self.require_rootless!r})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class DockerCommandResult:
    returncode: int
    stdout: bytes
    stderr: bytes
    duration_ms: int

    def __repr__(self) -> str:
        return (
            "DockerCommandResult("
            f"returncode={self.returncode!r}, duration_ms={self.duration_ms!r}, "
            "output_redacted=True)"
        )


@runtime_checkable
class DockerCommandRunner(Protocol):
    def run(
        self,
        argv: Sequence[str],
        *,
        input_bytes: bytes | None,
        deadline_at: float,
        max_output_bytes: int,
        env: Mapping[str, str],
        cancellation_probe: CancellationProbe | None = None,
    ) -> DockerCommandResult: ...


class SubprocessDockerCommandRunner:
    """Bounded streaming process runner using argv and ``shell=False``."""

    def __init__(
        self,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        poll_seconds: float = 0.02,
    ) -> None:
        self._monotonic = monotonic
        self._poll_seconds = poll_seconds

    def run(
        self,
        argv: Sequence[str],
        *,
        input_bytes: bytes | None,
        deadline_at: float,
        max_output_bytes: int,
        env: Mapping[str, str],
        cancellation_probe: CancellationProbe | None = None,
    ) -> DockerCommandResult:
        if not argv or any(not isinstance(item, str) or not item for item in argv):
            raise SandboxError(SandboxErrorCode.ADAPTER_UNAVAILABLE)
        started = self._monotonic()
        try:
            process = subprocess.Popen(
                list(argv),
                stdin=subprocess.PIPE
                if input_bytes is not None
                else subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=dict(env),
                shell=False,
                close_fds=True,
            )
        except (OSError, ValueError):
            raise SandboxError(
                SandboxErrorCode.ADAPTER_UNAVAILABLE,
                retryable=True,
            ) from None

        stdout = bytearray()
        stderr = bytearray()
        output_lock = threading.Lock()
        overflow = threading.Event()

        def read_stream(stream, destination: bytearray) -> None:
            try:
                while True:
                    chunk = stream.read(16 * 1024)
                    if not chunk:
                        return
                    with output_lock:
                        remaining = max_output_bytes - len(stdout) - len(stderr)
                        if remaining > 0:
                            destination.extend(chunk[:remaining])
                        if len(chunk) > remaining:
                            overflow.set()
            except OSError:
                return

        readers = [
            threading.Thread(
                target=read_stream,
                args=(process.stdout, stdout),
                daemon=True,
            ),
            threading.Thread(
                target=read_stream,
                args=(process.stderr, stderr),
                daemon=True,
            ),
        ]
        for reader in readers:
            reader.start()

        writer = None
        if input_bytes is not None and process.stdin is not None:

            def write_input() -> None:
                try:
                    process.stdin.write(input_bytes)
                    process.stdin.flush()
                except (BrokenPipeError, OSError):
                    pass
                finally:
                    try:
                        process.stdin.close()
                    except OSError:
                        pass

            writer = threading.Thread(target=write_input, daemon=True)
            writer.start()

        failure: SandboxError | None = None
        try:
            while process.poll() is None:
                if overflow.is_set():
                    failure = SandboxError(SandboxErrorCode.OUTPUT_LIMIT)
                    self._terminate(process)
                    break
                if self._cancelled(cancellation_probe):
                    failure = SandboxError(SandboxErrorCode.CANCELLED)
                    self._terminate(process)
                    break
                if self._monotonic() >= deadline_at:
                    failure = SandboxError(SandboxErrorCode.TIMEOUT)
                    self._terminate(process)
                    break
                time.sleep(self._poll_seconds)
            try:
                process.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                self._terminate(process)
        finally:
            if writer is not None:
                writer.join(timeout=0.2)
            for reader in readers:
                reader.join(timeout=0.5)

        if failure is not None:
            raise failure
        if overflow.is_set():
            raise SandboxError(SandboxErrorCode.OUTPUT_LIMIT)
        return DockerCommandResult(
            returncode=int(process.returncode or 0),
            stdout=bytes(stdout),
            stderr=bytes(stderr),
            duration_ms=max(int((self._monotonic() - started) * 1000), 0),
        )

    @staticmethod
    def _cancelled(cancellation_probe: CancellationProbe | None) -> bool:
        if cancellation_probe is None:
            return False
        try:
            return bool(cancellation_probe())
        except Exception:
            return True

    @staticmethod
    def _terminate(process: subprocess.Popen) -> None:
        try:
            process.terminate()
            process.wait(timeout=0.2)
        except (OSError, subprocess.TimeoutExpired):
            try:
                process.kill()
                process.wait(timeout=0.5)
            except (OSError, subprocess.TimeoutExpired):
                pass


class DockerSandboxAdapter:
    """Concrete Docker Adapter with fail-closed startup and cleanup."""

    name = "docker"

    def __init__(
        self,
        *,
        config: DockerSandboxConfig,
        runner: DockerCommandRunner | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = config
        self._runner = runner or SubprocessDockerCommandRunner(monotonic=monotonic)
        if not isinstance(self._runner, DockerCommandRunner):
            raise TypeError("runner must satisfy DockerCommandRunner")
        self._monotonic = monotonic
        self._lock = threading.RLock()
        self._active_containers: set[str] = set()
        self._started = False
        self._closed = False
        self._cleanup_pending = False
        self._daemon_reachable = False
        self._image_available = False
        self._rootless = False

    @classmethod
    def from_runtime_config(
        cls,
        config: SandboxRuntimeConfig,
    ) -> DockerSandboxAdapter:
        return cls(
            config=DockerSandboxConfig(
                image=config.image,
                docker_binary=config.docker_binary,
                docker_host=config.docker_host,
                require_rootless=config.require_rootless,
            )
        )

    def start(self) -> None:
        with self._lock:
            if self._closed:
                raise SandboxError(SandboxErrorCode.CLOSED)
            if self._cleanup_pending:
                raise SandboxError(SandboxErrorCode.CLEANUP_FAILED)
            if self._started:
                return
        deadline = self._monotonic() + 10.0
        version = self._control(
            ["version", "--format", "{{json .Server}}"],
            deadline_at=deadline,
            max_output_bytes=128 * 1024,
        )
        if version.returncode != 0:
            raise SandboxError(
                SandboxErrorCode.ADAPTER_UNAVAILABLE,
                retryable=True,
            )
        try:
            server = json.loads(version.stdout.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise SandboxError(SandboxErrorCode.ADAPTER_UNAVAILABLE) from None
        if not isinstance(server, dict) or not server:
            raise SandboxError(SandboxErrorCode.ADAPTER_UNAVAILABLE)

        info = self._control(
            ["info", "--format", "{{json .SecurityOptions}}"],
            deadline_at=deadline,
            max_output_bytes=64 * 1024,
        )
        if info.returncode != 0:
            raise SandboxError(SandboxErrorCode.ADAPTER_UNAVAILABLE)
        try:
            security_options = json.loads(info.stdout.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise SandboxError(SandboxErrorCode.ADAPTER_UNAVAILABLE) from None
        rootless = isinstance(security_options, list) and any(
            "rootless" in str(value).casefold() for value in security_options
        )
        if self.config.require_rootless and not rootless:
            raise SandboxError(SandboxErrorCode.ADAPTER_UNAVAILABLE)

        inspection = self._control(
            ["image", "inspect", "--format", "{{json .}}", self.config.image],
            deadline_at=deadline,
            max_output_bytes=512 * 1024,
        )
        if inspection.returncode != 0:
            raise SandboxError(SandboxErrorCode.IMAGE_UNAVAILABLE)
        self._validate_image_inspection(inspection.stdout)
        self._reap_stopped_managed_containers(deadline_at=deadline)
        with self._lock:
            self._daemon_reachable = True
            self._image_available = True
            self._rootless = rootless
            self._started = True

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            # Stop new executions before taking the cleanup snapshot.  A failed
            # cleanup keeps this state set so a later close() can retry the
            # exact orphan identities instead of silently reopening execution.
            self._cleanup_pending = True
            self._started = False
            targets = tuple(self._active_containers)
        failed_targets: set[str] = set()
        for target in targets:
            try:
                self._remove(target)
            except SandboxError:
                failed_targets.add(target)
            else:
                with self._lock:
                    self._active_containers.discard(target)
        with self._lock:
            self._daemon_reachable = False
            self._image_available = False
            if not failed_targets:
                self._cleanup_pending = False
                self._closed = True
        if failed_targets:
            raise SandboxError(SandboxErrorCode.CLEANUP_FAILED)

    def readiness(self) -> Mapping[str, object]:
        with self._lock:
            return {
                "ready": (
                    self._started
                    and not self._closed
                    and not self._cleanup_pending
                    and self._daemon_reachable
                    and self._image_available
                ),
                "daemon_reachable": self._daemon_reachable,
                "image_available": self._image_available,
                "rootless": self._rootless,
                "active_containers": len(self._active_containers),
            }

    def execute(
        self,
        spec: SandboxExecutionSpec,
        *,
        deadline_at: float,
        cancellation_probe: CancellationProbe | None,
    ) -> SandboxExecutionResult:
        if not isinstance(spec, SandboxExecutionSpec):
            raise SandboxError(SandboxErrorCode.INVALID_REQUEST)
        if spec.image != self.config.image:
            raise SandboxError(SandboxErrorCode.INVALID_REQUEST)
        if len(spec.source.encode("utf-8")) > spec.limits.max_source_bytes:
            raise SandboxError(SandboxErrorCode.INVALID_REQUEST)
        if (
            isinstance(deadline_at, bool)
            or not isinstance(deadline_at, (int, float))
            or not math.isfinite(float(deadline_at))
            or deadline_at <= self._monotonic()
        ):
            raise SandboxError(SandboxErrorCode.TIMEOUT)
        container_name = f"supermew-{spec.invocation_id}"
        cleanup_target = container_name
        registered = False
        try:
            # Hold the lifecycle lock through Docker create and the name→ID
            # tracking transition. close() can therefore never observe a
            # pre-create name, remove "nothing", and then race with a late
            # successful create that would become an untracked orphan.
            with self._lock:
                if self._closed:
                    raise SandboxError(SandboxErrorCode.CLOSED)
                if self._cleanup_pending:
                    raise SandboxError(SandboxErrorCode.CLEANUP_FAILED)
                if not self._started:
                    raise SandboxError(SandboxErrorCode.NOT_READY, retryable=True)
                self._active_containers.add(cleanup_target)
                registered = True
                created = self._run(
                    self._create_argv(spec, container_name),
                    deadline_at=deadline_at,
                    max_output_bytes=4096,
                    cancellation_probe=cancellation_probe,
                )
                if created.returncode != 0:
                    raise SandboxError(
                        SandboxErrorCode.EXECUTION_FAILED,
                        retryable=True,
                    )
                container_id = created.stdout.decode("ascii", errors="ignore").strip()
                if _CONTAINER_ID_RE.fullmatch(container_id) is None:
                    raise SandboxError(SandboxErrorCode.PROTOCOL_ERROR)
                cleanup_target = container_id
                self._active_containers.discard(container_name)
                self._active_containers.add(container_id)

            attached = self._run(
                [
                    self.config.docker_binary,
                    "start",
                    "--attach",
                    "--interactive",
                    container_id,
                ],
                input_bytes=self._request_frame(spec),
                deadline_at=deadline_at,
                max_output_bytes=spec.limits.protocol_output_bytes,
                cancellation_probe=cancellation_probe,
            )
            state = self._inspect_state(container_id, deadline_at=deadline_at)
            if state.get("OOMKilled") is True:
                raise SandboxError(SandboxErrorCode.MEMORY_LIMIT)
            if attached.returncode != 0 or state.get("ExitCode") not in {0, None}:
                raise SandboxError(SandboxErrorCode.EXECUTION_FAILED)
            return self._parse_runner_response(attached.stdout, spec)
        finally:
            if registered:
                removed = False
                try:
                    self._remove(
                        cleanup_target,
                        timeout_seconds=spec.limits.cleanup_timeout_seconds,
                    )
                    removed = True
                except SandboxError:
                    with self._lock:
                        self._cleanup_pending = True
                        self._started = False
                    raise
                finally:
                    if removed:
                        with self._lock:
                            self._active_containers.discard(container_name)
                            self._active_containers.discard(cleanup_target)

    def _create_argv(
        self,
        spec: SandboxExecutionSpec,
        container_name: str,
    ) -> list[str]:
        limits = spec.limits
        workspace_mount = (
            "rw,noexec,nosuid,nodev,"
            f"size={limits.workspace_bytes},mode=0700,uid=65532,gid=65532"
        )
        tmp_mount = "rw,noexec,nosuid,nodev,size=16777216,mode=0700,uid=65532,gid=65532"
        return [
            self.config.docker_binary,
            "create",
            "--pull=never",
            "--interactive",
            "--name",
            container_name,
            "--label",
            self.config.managed_label,
            "--label",
            f"com.supermew.sandbox.invocation={spec.invocation_id}",
            "--network",
            "none",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges:true",
            "--pids-limit",
            str(limits.pids_limit),
            "--memory",
            str(limits.memory_bytes),
            "--memory-swap",
            str(limits.memory_bytes),
            "--cpus",
            format(limits.cpu_count, "g"),
            "--ulimit",
            "core=0:0",
            "--ulimit",
            "nofile=64:64",
            "--ulimit",
            f"fsize={limits.max_file_bytes}:{limits.max_file_bytes}",
            "--ipc",
            "none",
            "--pid",
            "private",
            "--user",
            self.config.user,
            "--hostname",
            "sandbox",
            "--workdir",
            "/workspace",
            "--tmpfs",
            f"/workspace:{workspace_mount}",
            "--tmpfs",
            f"/tmp:{tmp_mount}",
            "--log-driver",
            "none",
            "--stop-timeout",
            "1",
            "--env",
            "HOME=/workspace",
            "--env",
            "LANG=C.UTF-8",
            "--env",
            "LC_ALL=C.UTF-8",
            "--env",
            "PATH=/usr/local/bin:/usr/bin:/bin",
            "--env",
            "PYTHONDONTWRITEBYTECODE=1",
            "--env",
            "PYTHONNOUSERSITE=1",
            spec.image,
        ]

    def _request_frame(self, spec: SandboxExecutionSpec) -> bytes:
        payload = {
            "schema_version": 1,
            "language": spec.language.value,
            "source_b64": base64.b64encode(spec.source.encode("utf-8")).decode("ascii"),
            "limits": spec.limits.runner_payload(),
        }
        return (
            json.dumps(
                payload,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("ascii")
            + b"\n"
        )

    def _parse_runner_response(
        self,
        raw: bytes,
        spec: SandboxExecutionSpec,
    ) -> SandboxExecutionResult:
        try:
            decoded = raw.decode("ascii").strip()
            payload = json.loads(decoded)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise SandboxError(SandboxErrorCode.PROTOCOL_ERROR) from None
        if (
            not isinstance(payload, dict)
            or set(payload).difference(_RUNNER_KEYS)
            or payload.get("schema_version") != 1
            or not isinstance(payload.get("success"), bool)
        ):
            raise SandboxError(SandboxErrorCode.PROTOCOL_ERROR)
        if payload["success"] is not True:
            try:
                code = SandboxErrorCode(str(payload.get("error_code")))
            except ValueError:
                raise SandboxError(SandboxErrorCode.PROTOCOL_ERROR) from None
            raise SandboxError(code)
        try:
            stdout_bytes = base64.b64decode(payload["stdout_b64"], validate=True)
            stderr_bytes = base64.b64decode(payload["stderr_b64"], validate=True)
        except (KeyError, TypeError, ValueError):
            raise SandboxError(SandboxErrorCode.PROTOCOL_ERROR) from None
        if len(stdout_bytes) + len(stderr_bytes) > spec.limits.max_output_bytes:
            raise SandboxError(SandboxErrorCode.PROTOCOL_ERROR)
        try:
            result = SandboxExecutionResult(
                exit_code=payload["exit_code"],
                stdout=stdout_bytes.decode("utf-8", errors="replace"),
                stderr=stderr_bytes.decode("utf-8", errors="replace"),
                duration_ms=payload["duration_ms"],
                files_created=payload.get("files_created", 0),
                stdout_truncated=payload.get("stdout_truncated", False),
                stderr_truncated=payload.get("stderr_truncated", False),
            )
        except (KeyError, TypeError, ValueError):
            raise SandboxError(SandboxErrorCode.PROTOCOL_ERROR) from None
        if result.files_created > spec.limits.max_files:
            raise SandboxError(SandboxErrorCode.PROTOCOL_ERROR)
        return result

    def _inspect_state(self, target: str, *, deadline_at: float) -> dict[str, object]:
        result = self._run(
            [
                self.config.docker_binary,
                "inspect",
                "--format",
                "{{json .State}}",
                target,
            ],
            deadline_at=min(deadline_at, self._monotonic() + 2.0),
            max_output_bytes=64 * 1024,
        )
        if result.returncode != 0:
            raise SandboxError(SandboxErrorCode.EXECUTION_FAILED)
        try:
            state = json.loads(result.stdout.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise SandboxError(SandboxErrorCode.PROTOCOL_ERROR) from None
        if not isinstance(state, dict):
            raise SandboxError(SandboxErrorCode.PROTOCOL_ERROR)
        return state

    def _reap_stopped_managed_containers(self, *, deadline_at: float) -> None:
        result = self._control(
            [
                "container",
                "prune",
                "--force",
                "--filter",
                f"label={self.config.managed_label}",
            ],
            deadline_at=deadline_at,
            max_output_bytes=512 * 1024,
        )
        if result.returncode != 0:
            raise SandboxError(SandboxErrorCode.CLEANUP_FAILED)

    def _validate_image_inspection(self, raw: bytes) -> None:
        try:
            inspection = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise SandboxError(SandboxErrorCode.IMAGE_UNAVAILABLE) from None
        if not isinstance(inspection, dict):
            raise SandboxError(SandboxErrorCode.IMAGE_UNAVAILABLE)
        digests = inspection.get("RepoDigests")
        image_id = inspection.get("Id")
        config = inspection.get("Config")
        if self.config.image.startswith("sha256:"):
            if image_id != self.config.image:
                raise SandboxError(SandboxErrorCode.IMAGE_UNAVAILABLE)
        elif not isinstance(digests, list) or self.config.image not in digests:
            raise SandboxError(SandboxErrorCode.IMAGE_UNAVAILABLE)
        if not isinstance(config, dict):
            raise SandboxError(SandboxErrorCode.IMAGE_UNAVAILABLE)
        entrypoint = config.get("Entrypoint")
        user = str(config.get("User") or "")
        if tuple(entrypoint or ()) != _EXPECTED_ENTRYPOINT or user not in {
            "65532",
            "65532:65532",
        }:
            raise SandboxError(SandboxErrorCode.IMAGE_UNAVAILABLE)

    def _remove(self, target: str, *, timeout_seconds: float = 3.0) -> None:
        deadline = self._monotonic() + timeout_seconds
        result = self._control(
            ["rm", "--force", "--volumes", target],
            deadline_at=deadline,
            max_output_bytes=16 * 1024,
        )
        # Docker returns non-zero when a pre-create name never existed.  That is
        # already clean; all other cleanup failures are fail-closed.
        if result.returncode != 0 and b"No such container" not in result.stderr:
            raise SandboxError(SandboxErrorCode.CLEANUP_FAILED)

    def _control(
        self,
        args: Sequence[str],
        *,
        deadline_at: float,
        max_output_bytes: int,
    ) -> DockerCommandResult:
        return self._run(
            [self.config.docker_binary, *args],
            deadline_at=deadline_at,
            max_output_bytes=max_output_bytes,
        )

    def _run(
        self,
        argv: Sequence[str],
        *,
        input_bytes: bytes | None = None,
        deadline_at: float,
        max_output_bytes: int,
        cancellation_probe: CancellationProbe | None = None,
    ) -> DockerCommandResult:
        return self._runner.run(
            argv,
            input_bytes=input_bytes,
            deadline_at=deadline_at,
            max_output_bytes=max_output_bytes,
            env=self._minimal_env(),
            cancellation_probe=cancellation_probe,
        )

    def _minimal_env(self) -> dict[str, str]:
        env = {
            "PATH": f"/usr/local/bin:{os.defpath}",
            "LANG": "C",
            "LC_ALL": "C",
            "HOME": "/nonexistent",
            "DOCKER_CONFIG": "/nonexistent",
        }
        if self.config.docker_host is not None:
            env["DOCKER_HOST"] = self.config.docker_host
        return env


__all__ = [
    "DockerCommandResult",
    "DockerCommandRunner",
    "DockerSandboxAdapter",
    "DockerSandboxConfig",
    "SubprocessDockerCommandRunner",
]
