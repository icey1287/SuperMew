from __future__ import annotations

# 独立进程入口必须先把项目 .env 写入 os.environ，再导入运行时模块。
# ruff: noqa: E402

import logging
import signal
from collections.abc import Sequence
from threading import Event

from backend.env import load_env


load_env()

from backend.core.settings import get_settings
from backend.evaluation.worker import RagEvaluationWorker
from backend.infra.database import init_db
from backend.model_control import model_control_service
from backend.providers.runtime import provider_runtime


logger = logging.getLogger(__name__)


def main(argv: Sequence[str] | None = None) -> int:
    del argv
    settings = get_settings()
    settings.validate_startup()
    logging.basicConfig(
        level=getattr(logging, settings.observability.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    init_db()
    model_control_service.ensure_environment_defaults()
    stop_event = Event()

    def request_stop(signum, _frame) -> None:
        logger.info("RAG evaluation worker draining", extra={"signal": signum})
        stop_event.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)

    provider_runtime.start_sync()
    worker = RagEvaluationWorker(settings=settings)
    try:
        worker.run_forever(stop_event)
        return 0
    finally:
        provider_runtime.close_sync()


if __name__ == "__main__":
    raise SystemExit(main())
