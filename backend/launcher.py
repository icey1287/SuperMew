from __future__ import annotations

# 联合入口必须先加载项目环境，再读取 AppSettings 并派生子进程环境。
# ruff: noqa: E402

import argparse
import os
import shlex
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from types import FrameType
from typing import cast

from backend.env import PROJECT_ROOT, load_env


load_env()

from backend.core.settings import get_settings


@dataclass(frozen=True, slots=True)
class ServiceCommand:
    name: str
    argv: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ManagedService:
    command: ServiceCommand
    process: subprocess.Popen[bytes]


ProcessFactory = Callable[..., subprocess.Popen[bytes]]
SignalHandler = signal.Handlers | Callable[[int, FrameType | None], object]


def build_service_commands(
    *,
    python_executable: str,
    host: str,
    port: int,
    reload_enabled: bool,
) -> tuple[ServiceCommand, ServiceCommand, ServiceCommand]:
    api_argv = [
        python_executable,
        "-m",
        "uvicorn",
        "backend.app:app",
        "--host",
        host,
        "--port",
        str(port),
    ]
    if reload_enabled:
        api_argv.append("--reload")
    return (
        ServiceCommand(name="api", argv=tuple(api_argv)),
        ServiceCommand(
            name="indexing-worker",
            argv=(
                python_executable,
                "-m",
                "backend.workers.indexing",
            ),
        ),
        ServiceCommand(
            name="rag-evaluation-worker",
            argv=(
                python_executable,
                "-m",
                "backend.workers.evaluation",
            ),
        ),
    )


def _send_process_signal(process: subprocess.Popen[bytes], signum: int) -> None:
    if process.poll() is not None:
        return
    if os.name == "posix":
        try:
            os.killpg(process.pid, signum)
            return
        except ProcessLookupError:
            return
    process.send_signal(signum)


def _shutdown_services(
    services: Sequence[ManagedService],
    *,
    timeout_seconds: float,
) -> None:
    running = [service for service in services if service.process.poll() is None]
    for service in running:
        _send_process_signal(service.process, signal.SIGTERM)

    deadline = time.monotonic() + timeout_seconds
    for service in running:
        remaining = max(deadline - time.monotonic(), 0.0)
        try:
            service.process.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            _send_process_signal(service.process, signal.SIGKILL)
            service.process.wait()


def _normalized_exit_code(exit_code: int) -> int:
    if exit_code >= 0:
        return exit_code
    return 128 + abs(exit_code)


def supervise_services(
    commands: Sequence[ServiceCommand],
    *,
    process_factory: ProcessFactory = subprocess.Popen,
    poll_seconds: float = 0.2,
    shutdown_timeout_seconds: float = 15.0,
    install_signal_handlers: bool = True,
    cwd: Path = PROJECT_ROOT,
) -> int:
    if not commands:
        raise ValueError("at least one service command is required")

    services: list[ManagedService] = []
    stop_event = Event()
    requested_signal = 0
    previous_handlers: dict[signal.Signals, SignalHandler] = {}

    def request_stop(signum: int, _frame: FrameType | None) -> None:
        nonlocal requested_signal
        requested_signal = signum
        stop_event.set()

    if install_signal_handlers:
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous_handlers[signum] = cast(
                SignalHandler,
                signal.getsignal(signum),
            )
            signal.signal(signum, request_stop)

    try:
        for command in commands:
            print(f"[supermew] starting {command.name}: {shlex.join(command.argv)}")
            process = process_factory(
                command.argv,
                cwd=cwd,
                start_new_session=os.name == "posix",
            )
            services.append(ManagedService(command=command, process=process))

        while True:
            for service in services:
                exit_code = service.process.poll()
                if exit_code is None:
                    continue
                print(
                    f"[supermew] {service.command.name} exited with code "
                    f"{exit_code}; stopping remaining services",
                    file=sys.stderr if exit_code else sys.stdout,
                )
                return _normalized_exit_code(exit_code)

            if stop_event.wait(poll_seconds):
                return 128 + requested_signal if requested_signal else 0
    except OSError as exc:
        print(f"[supermew] failed to start services: {exc}", file=sys.stderr)
        return 1
    finally:
        _shutdown_services(
            services,
            timeout_seconds=shutdown_timeout_seconds,
        )
        for registered_signum, previous_handler in previous_handlers.items():
            signal.signal(registered_signum, previous_handler)


def _argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="supermew-start",
        description="同时启动 SuperMew API、索引 worker 与 RAG 评估 worker",
    )
    parser.add_argument("--host", help="覆盖 API 监听地址")
    parser.add_argument("--port", type=int, help="覆盖 API 监听端口")
    reload_group = parser.add_mutually_exclusive_group()
    reload_group.add_argument(
        "--reload",
        dest="reload_enabled",
        action="store_true",
        help="启用 Uvicorn 自动重载（默认）",
    )
    reload_group.add_argument(
        "--no-reload",
        dest="reload_enabled",
        action="store_false",
        help="关闭 Uvicorn 自动重载",
    )
    parser.set_defaults(reload_enabled=True)
    parser.add_argument(
        "--shutdown-timeout-seconds",
        type=float,
        default=15.0,
        help="等待 API 与全部 worker 优雅退出的最长秒数",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _argument_parser().parse_args(argv)
    settings = get_settings()
    host = args.host or settings.app.host
    port = args.port or settings.app.port
    if not 1 <= port <= 65535:
        raise SystemExit("--port 必须位于 1..65535")
    if args.shutdown_timeout_seconds < 0:
        raise SystemExit("--shutdown-timeout-seconds 不能为负数")

    commands = build_service_commands(
        python_executable=sys.executable,
        host=host,
        port=port,
        reload_enabled=args.reload_enabled,
    )
    return supervise_services(
        commands,
        shutdown_timeout_seconds=args.shutdown_timeout_seconds,
    )


if __name__ == "__main__":
    raise SystemExit(main())
