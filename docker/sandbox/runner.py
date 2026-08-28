#!/usr/local/bin/python
"""Trusted in-container runner for one bounded Sandbox execution.

This file is intentionally standalone.  It receives one ASCII JSON frame on
stdin, captures the untrusted child process, validates the tmpfs workspace, and
returns one bounded ASCII JSON frame on stdout.  It never emits paths or file
contents.
"""

from __future__ import annotations

import base64
import binascii
import json
import math
import os
import re
import resource
import signal
import stat
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path, PurePosixPath


_WORKSPACE = Path("/workspace")
_MAX_FRAME_BYTES = 6 * 1024 * 1024
_SAFE_SEGMENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
_LANGUAGES = {"python", "sh"}
_ERROR_CODES = {
    "SANDBOX_INVALID_REQUEST",
    "SANDBOX_TIMEOUT",
    "SANDBOX_OUTPUT_LIMIT",
    "SANDBOX_FILE_LIMIT",
    "SANDBOX_DISK_LIMIT",
    "SANDBOX_UNSAFE_FILE",
    "SANDBOX_EXECUTION_FAILED",
}


class RunnerFailure(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code if code in _ERROR_CODES else "SANDBOX_EXECUTION_FAILED"
        super().__init__(self.code)


def _bounded_int(
    value: object,
    *,
    minimum: int,
    maximum: int,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise RunnerFailure("SANDBOX_INVALID_REQUEST")
    if not minimum <= value <= maximum:
        raise RunnerFailure("SANDBOX_INVALID_REQUEST")
    return value


def _bounded_float(
    value: object,
    *,
    minimum: float,
    maximum: float,
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RunnerFailure("SANDBOX_INVALID_REQUEST")
    normalized = float(value)
    if not math.isfinite(normalized) or not minimum <= normalized <= maximum:
        raise RunnerFailure("SANDBOX_INVALID_REQUEST")
    return normalized


def _validate_relative_path(
    value: str,
    *,
    max_path_bytes: int,
    max_path_depth: int,
) -> PurePosixPath:
    if not isinstance(value, str) or not value or "\x00" in value or "\\" in value:
        raise RunnerFailure("SANDBOX_UNSAFE_FILE")
    try:
        encoded = value.encode("ascii")
    except UnicodeEncodeError:
        raise RunnerFailure("SANDBOX_UNSAFE_FILE") from None
    if len(encoded) > max_path_bytes:
        raise RunnerFailure("SANDBOX_UNSAFE_FILE")
    raw_parts = value.split("/")
    if any(part in {"", ".", ".."} for part in raw_parts):
        raise RunnerFailure("SANDBOX_UNSAFE_FILE")
    path = PurePosixPath(value)
    if path.is_absolute() or len(path.parts) > max_path_depth:
        raise RunnerFailure("SANDBOX_UNSAFE_FILE")
    if any(_SAFE_SEGMENT.fullmatch(part) is None for part in path.parts):
        raise RunnerFailure("SANDBOX_UNSAFE_FILE")
    return path


def _load_request(stream) -> dict[str, object]:
    frame = stream.buffer.readline(_MAX_FRAME_BYTES + 1)
    if not frame or len(frame) > _MAX_FRAME_BYTES or not frame.endswith(b"\n"):
        raise RunnerFailure("SANDBOX_INVALID_REQUEST")
    if stream.buffer.read(1):
        raise RunnerFailure("SANDBOX_INVALID_REQUEST")
    try:
        payload = json.loads(frame.decode("ascii"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise RunnerFailure("SANDBOX_INVALID_REQUEST") from None
    if not isinstance(payload, dict) or set(payload) != {
        "schema_version",
        "language",
        "source_b64",
        "limits",
    }:
        raise RunnerFailure("SANDBOX_INVALID_REQUEST")
    if payload.get("schema_version") != 1 or payload.get("language") not in _LANGUAGES:
        raise RunnerFailure("SANDBOX_INVALID_REQUEST")
    if not isinstance(payload.get("limits"), dict):
        raise RunnerFailure("SANDBOX_INVALID_REQUEST")
    return payload


def _normalize_limits(value: dict[str, object]) -> dict[str, int | float]:
    expected = {
        "timeout_seconds",
        "max_output_bytes",
        "max_files",
        "max_file_bytes",
        "max_total_file_bytes",
        "max_path_bytes",
        "max_path_depth",
    }
    if set(value) != expected:
        raise RunnerFailure("SANDBOX_INVALID_REQUEST")
    limits: dict[str, int | float] = {
        "timeout_seconds": _bounded_float(
            value["timeout_seconds"],
            minimum=0.1,
            maximum=600.0,
        ),
        "max_output_bytes": _bounded_int(
            value["max_output_bytes"],
            minimum=1,
            maximum=16 * 1024 * 1024,
        ),
        "max_files": _bounded_int(value["max_files"], minimum=1, maximum=4096),
        "max_file_bytes": _bounded_int(
            value["max_file_bytes"],
            minimum=1,
            maximum=512 * 1024 * 1024,
        ),
        "max_total_file_bytes": _bounded_int(
            value["max_total_file_bytes"],
            minimum=1,
            maximum=1024 * 1024 * 1024,
        ),
        "max_path_bytes": _bounded_int(
            value["max_path_bytes"],
            minimum=16,
            maximum=4096,
        ),
        "max_path_depth": _bounded_int(
            value["max_path_depth"],
            minimum=1,
            maximum=64,
        ),
    }
    if limits["max_file_bytes"] > limits["max_total_file_bytes"]:
        raise RunnerFailure("SANDBOX_INVALID_REQUEST")
    return limits


def _decode_source(value: object) -> bytes:
    if not isinstance(value, str) or len(value) > 6 * 1024 * 1024:
        raise RunnerFailure("SANDBOX_INVALID_REQUEST")
    try:
        source = base64.b64decode(value, validate=True)
        source.decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        raise RunnerFailure("SANDBOX_INVALID_REQUEST") from None
    if not source or b"\x00" in source or len(source) > 4 * 1024 * 1024:
        raise RunnerFailure("SANDBOX_INVALID_REQUEST")
    return source


def _write_source(workspace: Path, language: str, source: bytes) -> Path:
    name = "main.py" if language == "python" else "main.sh"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    directory_flags = (
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        directory_fd = os.open(workspace, directory_flags)
    except OSError:
        raise RunnerFailure("SANDBOX_EXECUTION_FAILED") from None
    try:
        fd = os.open(name, flags, 0o400, dir_fd=directory_fd)
    except OSError:
        raise RunnerFailure("SANDBOX_EXECUTION_FAILED") from None
    finally:
        os.close(directory_fd)
    try:
        written = 0
        while written < len(source):
            written += os.write(fd, source[written:])
        os.fsync(fd)
    except OSError:
        raise RunnerFailure("SANDBOX_EXECUTION_FAILED") from None
    finally:
        os.close(fd)
    return workspace / name


def _resource_limits(timeout_seconds: float, max_file_bytes: int) -> None:
    cpu_seconds = max(int(math.ceil(timeout_seconds)), 1)
    resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds + 1))
    resource.setrlimit(resource.RLIMIT_FSIZE, (max_file_bytes, max_file_bytes))
    resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


def _kill_group(process: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            process.kill()
        except OSError:
            pass


def _namespace_process_ids(
    *,
    proc_root: Path = Path("/proc"),
    current_pid: int | None = None,
) -> set[int]:
    own_pid = os.getpid() if current_pid is None else current_pid
    try:
        entries = tuple(os.scandir(proc_root))
    except OSError:
        raise RunnerFailure("SANDBOX_EXECUTION_FAILED") from None
    result: set[int] = set()
    for entry in entries:
        if not entry.name.isascii() or not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if pid > 0 and pid != own_pid:
            result.add(pid)
    return result


def _reap_adopted_children(
    *,
    waitpid: Callable[[int, int], tuple[int, int]] = os.waitpid,
) -> None:
    while True:
        try:
            pid, _status = waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return
        except OSError:
            raise RunnerFailure("SANDBOX_EXECUTION_FAILED") from None
        if pid <= 0:
            return


def _terminate_remaining_namespace_processes(
    *,
    timeout_seconds: float = 0.5,
    proc_root: Path = Path("/proc"),
    current_pid: int | None = None,
    kill: Callable[[int, int], None] = os.kill,
    waitpid: Callable[[int, int], tuple[int, int]] = os.waitpid,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Kill every remaining process in this container PID namespace.

    The trusted runner is PID 1 in production, so orphaned descendants are
    adopted here even when untrusted code calls ``setsid()`` to escape the
    original process group.  The injectable seams keep host-side unit tests
    away from the real ``/proc`` namespace.
    """

    own_pid = os.getpid() if current_pid is None else current_pid
    deadline = monotonic() + timeout_seconds
    while True:
        _reap_adopted_children(waitpid=waitpid)
        remaining = _namespace_process_ids(
            proc_root=proc_root,
            current_pid=own_pid,
        )
        if not remaining:
            return
        for pid in remaining:
            try:
                kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                continue
            except (PermissionError, OSError):
                raise RunnerFailure("SANDBOX_EXECUTION_FAILED") from None
        _reap_adopted_children(waitpid=waitpid)
        if monotonic() >= deadline:
            raise RunnerFailure("SANDBOX_EXECUTION_FAILED")
        sleep(0.01)


def _capture(
    argv: list[str],
    *,
    workspace: Path,
    timeout_seconds: float,
    max_output_bytes: int,
    max_file_bytes: int,
    namespace_cleanup: Callable[[], None] | None = None,
) -> tuple[int, bytes, bytes, int]:
    started = time.monotonic()
    try:
        process = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=workspace,
            env={
                "HOME": "/workspace",
                "LANG": "C.UTF-8",
                "LC_ALL": "C.UTF-8",
                "PATH": "/usr/local/bin:/usr/bin:/bin",
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONNOUSERSITE": "1",
            },
            close_fds=True,
            start_new_session=True,
            preexec_fn=lambda: _resource_limits(timeout_seconds, max_file_bytes),
        )
    except (OSError, ValueError):
        raise RunnerFailure("SANDBOX_EXECUTION_FAILED") from None

    stdout = bytearray()
    stderr = bytearray()
    lock = threading.Lock()
    overflow = threading.Event()

    def read_stream(stream, destination: bytearray) -> None:
        try:
            while True:
                chunk = stream.read(16 * 1024)
                if not chunk:
                    return
                with lock:
                    remaining = max_output_bytes - len(stdout) - len(stderr)
                    if remaining > 0:
                        destination.extend(chunk[:remaining])
                    if len(chunk) > remaining:
                        overflow.set()
        except OSError:
            return

    readers = [
        threading.Thread(
            target=read_stream, args=(process.stdout, stdout), daemon=True
        ),
        threading.Thread(
            target=read_stream, args=(process.stderr, stderr), daemon=True
        ),
    ]
    for reader in readers:
        reader.start()

    deadline = started + timeout_seconds
    failure_code = None
    while process.poll() is None:
        if overflow.is_set():
            failure_code = "SANDBOX_OUTPUT_LIMIT"
            _kill_group(process)
            break
        if time.monotonic() >= deadline:
            failure_code = "SANDBOX_TIMEOUT"
            _kill_group(process)
            break
        time.sleep(0.01)
    cleanup_failure: RunnerFailure | None = None
    try:
        process.wait(timeout=0.5)
    except subprocess.TimeoutExpired:
        _kill_group(process)
    finally:
        _kill_group(process)
        if namespace_cleanup is not None:
            try:
                namespace_cleanup()
            except RunnerFailure as exc:
                cleanup_failure = exc
            except BaseException:
                cleanup_failure = RunnerFailure("SANDBOX_EXECUTION_FAILED")
        for reader in readers:
            reader.join(timeout=0.5)
        if any(reader.is_alive() for reader in readers):
            cleanup_failure = RunnerFailure("SANDBOX_EXECUTION_FAILED")
    if cleanup_failure is not None:
        raise cleanup_failure
    if failure_code is not None:
        raise RunnerFailure(failure_code)
    if overflow.is_set():
        raise RunnerFailure("SANDBOX_OUTPUT_LIMIT")
    return (
        int(process.returncode or 0),
        bytes(stdout),
        bytes(stderr),
        max(int((time.monotonic() - started) * 1000), 0),
    )


def _scan_workspace(
    workspace: Path,
    *,
    source_path: Path,
    max_files: int,
    max_file_bytes: int,
    max_total_file_bytes: int,
    max_path_bytes: int,
    max_path_depth: int,
) -> int:
    try:
        root_stat = workspace.stat(follow_symlinks=False)
    except OSError:
        raise RunnerFailure("SANDBOX_UNSAFE_FILE") from None
    if not stat.S_ISDIR(root_stat.st_mode):
        raise RunnerFailure("SANDBOX_UNSAFE_FILE")

    stack = [workspace]
    seen_paths: set[str] = set()
    output_files = 0
    entries = 0
    total_bytes = 0
    while stack:
        directory = stack.pop()
        try:
            with os.scandir(directory) as children:
                for child in children:
                    try:
                        relative = child.path.removeprefix(f"{workspace}{os.sep}")
                        normalized = _validate_relative_path(
                            relative,
                            max_path_bytes=max_path_bytes,
                            max_path_depth=max_path_depth,
                        ).as_posix()
                        if normalized in seen_paths:
                            raise RunnerFailure("SANDBOX_UNSAFE_FILE")
                        seen_paths.add(normalized)
                        metadata = child.stat(follow_symlinks=False)
                    except OSError:
                        raise RunnerFailure("SANDBOX_UNSAFE_FILE") from None
                    entries += 1
                    if entries > max_files + 1:
                        raise RunnerFailure("SANDBOX_FILE_LIMIT")
                    mode = metadata.st_mode
                    if stat.S_ISLNK(mode):
                        raise RunnerFailure("SANDBOX_UNSAFE_FILE")
                    if stat.S_ISDIR(mode):
                        stack.append(Path(child.path))
                        continue
                    if not stat.S_ISREG(mode) or metadata.st_nlink != 1:
                        raise RunnerFailure("SANDBOX_UNSAFE_FILE")
                    if metadata.st_size > max_file_bytes:
                        raise RunnerFailure("SANDBOX_DISK_LIMIT")
                    if Path(child.path) == source_path:
                        continue
                    output_files += 1
                    total_bytes += metadata.st_size
                    if output_files > max_files:
                        raise RunnerFailure("SANDBOX_FILE_LIMIT")
                    if total_bytes > max_total_file_bytes:
                        raise RunnerFailure("SANDBOX_DISK_LIMIT")
        except OSError:
            raise RunnerFailure("SANDBOX_UNSAFE_FILE") from None
    return output_files


def _emit(payload: dict[str, object]) -> None:
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("ascii")
    os.write(sys.stdout.fileno(), encoded + b"\n")


def main() -> int:
    started = time.monotonic()
    try:
        request = _load_request(sys.stdin)
        limits = _normalize_limits(request["limits"])
        source = _decode_source(request["source_b64"])
        language = str(request["language"])
        _WORKSPACE.mkdir(mode=0o700, parents=True, exist_ok=True)
        source_path = _write_source(_WORKSPACE, language, source)
        argv = (
            ["/usr/local/bin/python", "-I", "-B", "-u", str(source_path)]
            if language == "python"
            else ["/bin/sh", str(source_path)]
        )
        exit_code, stdout, stderr, duration_ms = _capture(
            argv,
            workspace=_WORKSPACE,
            timeout_seconds=float(limits["timeout_seconds"]),
            max_output_bytes=int(limits["max_output_bytes"]),
            max_file_bytes=int(limits["max_file_bytes"]),
            namespace_cleanup=_terminate_remaining_namespace_processes,
        )
        files_created = _scan_workspace(
            _WORKSPACE,
            source_path=source_path,
            max_files=int(limits["max_files"]),
            max_file_bytes=int(limits["max_file_bytes"]),
            max_total_file_bytes=int(limits["max_total_file_bytes"]),
            max_path_bytes=int(limits["max_path_bytes"]),
            max_path_depth=int(limits["max_path_depth"]),
        )
        _emit(
            {
                "schema_version": 1,
                "success": True,
                "error_code": None,
                "exit_code": exit_code,
                "stdout_b64": base64.b64encode(stdout).decode("ascii"),
                "stderr_b64": base64.b64encode(stderr).decode("ascii"),
                "duration_ms": duration_ms,
                "files_created": files_created,
                "stdout_truncated": False,
                "stderr_truncated": False,
            }
        )
        return 0
    except RunnerFailure as exc:
        _emit(
            {
                "schema_version": 1,
                "success": False,
                "error_code": exc.code,
                "duration_ms": max(int((time.monotonic() - started) * 1000), 0),
            }
        )
        return 0
    except BaseException:
        _emit(
            {
                "schema_version": 1,
                "success": False,
                "error_code": "SANDBOX_EXECUTION_FAILED",
                "duration_ms": max(int((time.monotonic() - started) * 1000), 0),
            }
        )
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
