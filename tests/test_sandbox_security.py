from __future__ import annotations

import importlib.util
import os
import signal
import sys
from pathlib import Path

import pytest

from backend.sandbox import SandboxError, SandboxErrorCode
from backend.sandbox.docker import SubprocessDockerCommandRunner


RUNNER_PATH = Path(__file__).parents[1] / "docker" / "sandbox" / "runner.py"
DOCKERFILE_PATH = Path(__file__).parents[1] / "docker" / "sandbox" / "Dockerfile"
SPEC = importlib.util.spec_from_file_location("supermew_sandbox_runner", RUNNER_PATH)
assert SPEC is not None and SPEC.loader is not None
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)


def test_sandbox_image_requires_an_explicit_base_and_fixed_non_root_runner():
    dockerfile = DOCKERFILE_PATH.read_text(encoding="utf-8")
    assert "ARG PYTHON_BASE_IMAGE" in dockerfile
    assert "FROM ${PYTHON_BASE_IMAGE}" in dockerfile
    assert "USER 65532:65532" in dockerfile
    assert (
        'ENTRYPOINT ["/usr/local/bin/python", "-I", "-B", "/opt/supermew/runner.py"]'
    ) in dockerfile
    assert "VOLUME" not in dockerfile
    assert "ADD " not in dockerfile


@pytest.mark.parametrize(
    "value",
    [
        "/etc/passwd",
        "../secret",
        "safe/../../secret",
        r"safe\secret",
        "safe/./file",
        "safe//file",
        "safe/\x00file",
        "C:/windows/system.ini",
        "unicode-文件.txt",
        ".hidden",
    ],
)
def test_runner_rejects_unsafe_or_ambiguous_paths(value):
    with pytest.raises(runner.RunnerFailure) as raised:
        runner._validate_relative_path(
            value,
            max_path_bytes=240,
            max_path_depth=8,
        )
    assert raised.value.code == "SANDBOX_UNSAFE_FILE"


def test_runner_accepts_only_bounded_ascii_relative_paths():
    path = runner._validate_relative_path(
        "reports/result-01.json",
        max_path_bytes=240,
        max_path_depth=8,
    )
    assert path.as_posix() == "reports/result-01.json"


def _scan(workspace: Path, source: Path, **overrides) -> int:
    values = {
        "max_files": 8,
        "max_file_bytes": 1024,
        "max_total_file_bytes": 4096,
        "max_path_bytes": 240,
        "max_path_depth": 8,
    }
    values.update(overrides)
    return runner._scan_workspace(workspace, source_path=source, **values)


def test_workspace_scan_counts_regular_files_without_returning_paths(tmp_path):
    source = tmp_path / "main.py"
    source.write_text("print('ok')", encoding="utf-8")
    output = tmp_path / "result.txt"
    output.write_text("safe", encoding="utf-8")

    assert _scan(tmp_path, source) == 1


def test_workspace_scan_rejects_symlinks_hardlinks_and_special_files(tmp_path):
    source = tmp_path / "main.py"
    source.write_text("print('ok')", encoding="utf-8")

    symlink = tmp_path / "link.txt"
    symlink.symlink_to(source)
    with pytest.raises(runner.RunnerFailure) as linked:
        _scan(tmp_path, source)
    assert linked.value.code == "SANDBOX_UNSAFE_FILE"
    symlink.unlink()

    hardlink = tmp_path / "hard.txt"
    os.link(source, hardlink)
    with pytest.raises(runner.RunnerFailure) as hardened:
        _scan(tmp_path, source)
    assert hardened.value.code == "SANDBOX_UNSAFE_FILE"
    hardlink.unlink()

    fifo = tmp_path / "pipe"
    os.mkfifo(fifo)
    with pytest.raises(runner.RunnerFailure) as special:
        _scan(tmp_path, source)
    assert special.value.code == "SANDBOX_UNSAFE_FILE"


def test_workspace_scan_enforces_file_count_and_byte_limits(tmp_path):
    source = tmp_path / "main.py"
    source.write_text("pass", encoding="utf-8")
    (tmp_path / "one.txt").write_text("1", encoding="utf-8")
    (tmp_path / "two.txt").write_text("2", encoding="utf-8")

    with pytest.raises(runner.RunnerFailure) as count:
        _scan(tmp_path, source, max_files=1)
    assert count.value.code == "SANDBOX_FILE_LIMIT"

    with pytest.raises(runner.RunnerFailure) as single:
        _scan(tmp_path, source, max_file_bytes=0)
    assert single.value.code == "SANDBOX_DISK_LIMIT"

    with pytest.raises(runner.RunnerFailure) as total:
        _scan(tmp_path, source, max_total_file_bytes=1)
    assert total.value.code == "SANDBOX_DISK_LIMIT"


def test_in_container_capture_kills_output_floods_and_timeouts(tmp_path):
    with pytest.raises(runner.RunnerFailure) as output:
        runner._capture(
            [sys.executable, "-c", "print('x' * 100000)"],
            workspace=tmp_path,
            timeout_seconds=2,
            max_output_bytes=64,
            max_file_bytes=1024,
        )
    assert output.value.code == "SANDBOX_OUTPUT_LIMIT"

    with pytest.raises(runner.RunnerFailure) as timeout:
        runner._capture(
            [sys.executable, "-c", "import time; time.sleep(2)"],
            workspace=tmp_path,
            timeout_seconds=0.05,
            max_output_bytes=64,
            max_file_bytes=1024,
        )
    assert timeout.value.code == "SANDBOX_TIMEOUT"


def test_namespace_cleanup_kills_every_non_runner_pid(tmp_path):
    proc_root = tmp_path / "proc"
    proc_root.mkdir()
    for name in ("1", "42", "43", "self", "net"):
        (proc_root / name).mkdir()
    killed: list[tuple[int, int]] = []

    def kill(pid: int, sig: int) -> None:
        killed.append((pid, sig))
        (proc_root / str(pid)).rmdir()

    runner._terminate_remaining_namespace_processes(
        proc_root=proc_root,
        current_pid=1,
        kill=kill,
        waitpid=lambda _pid, _flags: (0, 0),
        sleep=lambda _seconds: None,
    )

    assert set(killed) == {(42, signal.SIGKILL), (43, signal.SIGKILL)}


def test_capture_cleans_a_forked_setsid_process_before_reader_join(tmp_path):
    pid_file = tmp_path / "escaped.pid"
    escaped_pid: int | None = None
    cleanup_called = False
    script = "\n".join(
        (
            "import os, pathlib, time",
            f"pid_file = pathlib.Path({str(pid_file)!r})",
            "child = os.fork()",
            "if child == 0:",
            "    os.setsid()",
            "    pid_file.write_text(str(os.getpid()), encoding='ascii')",
            "    time.sleep(30)",
            "    os._exit(0)",
            "deadline = time.monotonic() + 2",
            "while not pid_file.exists() and time.monotonic() < deadline:",
            "    time.sleep(0.01)",
            "print('parent', flush=True)",
        )
    )

    def cleanup() -> None:
        nonlocal cleanup_called, escaped_pid
        cleanup_called = True
        escaped_pid = int(pid_file.read_text(encoding="ascii"))
        assert os.getsid(escaped_pid) == escaped_pid
        os.kill(escaped_pid, signal.SIGKILL)

    try:
        exit_code, stdout, _stderr, _duration = runner._capture(
            [sys.executable, "-c", script],
            workspace=tmp_path,
            timeout_seconds=3,
            max_output_bytes=1024,
            max_file_bytes=1024,
            namespace_cleanup=cleanup,
        )
    finally:
        if escaped_pid is not None:
            try:
                os.kill(escaped_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    assert cleanup_called is True
    assert exit_code == 0
    assert stdout == b"parent\n"


def test_host_command_runner_bounds_output_and_never_uses_a_shell():
    command_runner = SubprocessDockerCommandRunner()
    with pytest.raises(SandboxError) as raised:
        command_runner.run(
            [sys.executable, "-c", "print('x' * 100000)"],
            input_bytes=None,
            deadline_at=runner.time.monotonic() + 2,
            max_output_bytes=64,
            env={"PATH": os.defpath},
        )
    assert raised.value.code == SandboxErrorCode.OUTPUT_LIMIT.value
