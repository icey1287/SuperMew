from __future__ import annotations

import signal
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import backend.launcher as launcher


ROOT = Path(__file__).resolve().parents[1]


@dataclass
class _FakeProcess:
    pid: int
    exit_code: int | None = None
    signals: list[int] = field(default_factory=list)

    def poll(self) -> int | None:
        return self.exit_code

    def wait(self, timeout: float | None = None) -> int:
        del timeout
        if self.exit_code is None:
            self.exit_code = 0
        return self.exit_code


def test_build_service_commands_starts_api_and_indexing_worker() -> None:
    commands = launcher.build_service_commands(
        python_executable="/venv/bin/python",
        host="127.0.0.1",
        port=8123,
        reload_enabled=True,
    )

    assert commands == (
        launcher.ServiceCommand(
            name="api",
            argv=(
                "/venv/bin/python",
                "-m",
                "uvicorn",
                "backend.app:app",
                "--host",
                "127.0.0.1",
                "--port",
                "8123",
                "--reload",
            ),
        ),
        launcher.ServiceCommand(
            name="indexing-worker",
            argv=(
                "/venv/bin/python",
                "-m",
                "backend.workers.indexing",
            ),
        ),
    )


def test_supervisor_stops_sibling_when_one_service_exits(monkeypatch) -> None:
    api = _FakeProcess(pid=101, exit_code=7)
    worker = _FakeProcess(pid=102)
    spawned = iter((api, worker))

    def process_factory(*_args, **_kwargs):
        return next(spawned)

    def send_signal(process: _FakeProcess, signum: int) -> None:
        process.signals.append(signum)
        process.exit_code = 0

    monkeypatch.setattr(launcher, "_send_process_signal", send_signal)

    exit_code = launcher.supervise_services(
        (
            launcher.ServiceCommand(name="api", argv=("api",)),
            launcher.ServiceCommand(name="indexing-worker", argv=("worker",)),
        ),
        process_factory=process_factory,
        poll_seconds=0,
        shutdown_timeout_seconds=0,
        install_signal_handlers=False,
    )

    assert exit_code == 7
    assert api.signals == []
    assert worker.signals == [signal.SIGTERM]


def test_repository_start_script_exposes_the_unified_launcher() -> None:
    completed = subprocess.run(
        [ROOT / "scripts" / "start.sh", "--help"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert completed.returncode == 0, completed.stderr
    assert "同时启动 SuperMew API 与持久化 indexing worker" in completed.stdout
