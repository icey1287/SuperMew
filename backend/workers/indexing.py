from __future__ import annotations

# 独立进程入口必须先把项目 .env 写入 os.environ，再导入运行时模块。
# ruff: noqa: E402

import argparse
import json
import logging
import signal
from collections.abc import Sequence
from threading import Event

from backend.env import load_env


load_env()

from backend.core.settings import get_settings
from backend.documents.catalog import CleanupBuild, CleanupJobStatus, DocumentCatalog
from backend.documents.worker import IndexingWorker
from backend.infra.database import init_db
from backend.providers.runtime import provider_runtime


logger = logging.getLogger(__name__)


def _argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="supermew-indexing-worker",
        description="运行持久化索引 worker 或执行受控清理队列运维操作",
    )
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("run", help="启动持久化索引 worker（默认）")
    list_cleanup = commands.add_parser(
        "list-cleanup",
        help="列出持久化物理清理任务",
    )
    list_cleanup.add_argument(
        "--status",
        choices=[item.value for item in CleanupJobStatus],
        default=CleanupJobStatus.DEAD_LETTER.value,
    )
    list_cleanup.add_argument("--tenant-id")
    list_cleanup.add_argument("--limit", type=int, default=100)
    requeue_cleanup = commands.add_parser(
        "requeue-cleanup",
        help="将一个 dead-letter 清理任务安全地重新排队",
    )
    requeue_cleanup.add_argument("--job-id", required=True)
    requeue_cleanup.add_argument("--max-attempts", type=int)
    return parser


def _cleanup_payload(build: CleanupBuild) -> dict:
    return {
        "job_id": build.job.id,
        "tenant_id": build.document.tenant_id,
        "document_id": build.document.id,
        "document_version_id": build.version.id,
        "filename": build.document.canonical_name,
        "status": build.job.status,
        "current_step": build.job.current_step,
        "attempts": build.job.attempts,
        "max_attempts": build.job.max_attempts,
        "execution_fence": build.job.execution_fence,
        "error_code": build.job.error_code,
        "next_retry_at": (
            build.job.next_retry_at.isoformat() if build.job.next_retry_at else None
        ),
        "updated_at": build.job.updated_at.isoformat(),
    }


def _run_operator_command(args: argparse.Namespace) -> int:
    catalog = DocumentCatalog()
    if args.command == "list-cleanup":
        builds = catalog.list_cleanup_jobs(
            status=args.status,
            tenant_id=args.tenant_id,
            limit=args.limit,
        )
        print(
            json.dumps(
                [_cleanup_payload(build) for build in builds], ensure_ascii=False
            )
        )
        return 0
    if args.command == "requeue-cleanup":
        build = catalog.requeue_cleanup_job(
            job_id=args.job_id,
            max_attempts=args.max_attempts,
        )
        print(json.dumps(_cleanup_payload(build), ensure_ascii=False))
        return 0
    raise ValueError(f"unsupported operator command: {args.command}")


def main(argv: Sequence[str] | None = None) -> int:
    args = _argument_parser().parse_args(argv)
    settings = get_settings()
    settings.validate_startup()
    logging.basicConfig(
        level=getattr(logging, settings.observability.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    init_db()
    if args.command in {"list-cleanup", "requeue-cleanup"}:
        return _run_operator_command(args)

    stop_event = Event()

    def request_stop(signum, _frame) -> None:
        logger.info("indexing worker draining", extra={"signal": signum})
        stop_event.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)

    settings.storage.upload_dir.mkdir(parents=True, exist_ok=True)
    provider_runtime.start_sync()
    worker = IndexingWorker()
    try:
        worker.run_forever(stop_event)
        return 0
    finally:
        provider_runtime.close_sync()


if __name__ == "__main__":
    raise SystemExit(main())
