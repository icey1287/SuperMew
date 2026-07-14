from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import uuid4

from backend.core.settings import get_settings
from backend.runs.repository import RunRecord, RunRepository, RunReservation, repository
from backend.runs.state import MultitaskStrategy, RunStatus


class RunService:
    """Run 生命周期的应用 interface；HTTP、worker 与兼容入口共享。"""

    def __init__(self, run_repository: RunRepository = repository):
        self.repository = run_repository

    def create_thread(
        self,
        *,
        username: str,
        thread_id: str | None = None,
        title: str | None = None,
    ) -> dict:
        resolved_id = thread_id or f"thread_{uuid4().hex}"
        return self.repository.create_thread(
            username=username,
            thread_id=resolved_id,
            title=title,
        )

    def create_run(
        self,
        *,
        username: str,
        thread_id: str,
        message: str,
        idempotency_key: str,
        expected_thread_version: int | None = None,
        multitask_strategy: MultitaskStrategy | str | None = None,
        on_disconnect: str | None = None,
    ) -> RunReservation:
        compact_message = message.strip()
        settings = get_settings()
        return self.repository.reserve(
            username=username,
            thread_id=thread_id,
            message=compact_message,
            idempotency_key=idempotency_key,
            expected_thread_version=expected_thread_version,
            model_name=settings.models.answer_model,
            on_disconnect=on_disconnect,
            multitask_strategy=multitask_strategy,
            title=(" ".join(compact_message.split())[:16] or "新会话"),
        )

    def get_run(self, *, username: str, run_id: str) -> RunRecord:
        return self.repository.get(username=username, run_id=run_id)

    def claim_run(self, *, run_id: str, worker_id: str) -> RunRecord:
        return self.repository.claim(run_id=run_id, worker_id=worker_id)

    def heartbeat(
        self,
        *,
        run_id: str,
        worker_id: str,
        fencing_token: int,
    ) -> RunRecord:
        return self.repository.heartbeat(
            run_id=run_id,
            worker_id=worker_id,
            fencing_token=fencing_token,
        )

    def wait_for_input(
        self,
        *,
        run_id: str,
        worker_id: str,
        fencing_token: int,
    ) -> RunRecord:
        return self.repository.set_waiting_input(
            run_id=run_id,
            worker_id=worker_id,
            fencing_token=fencing_token,
        )

    def complete_run(
        self,
        *,
        run_id: str,
        content: str,
        fencing_token: int | None = None,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cost: Decimal | str | float = Decimal("0"),
        rag_trace: dict | None = None,
    ) -> RunRecord:
        return self.repository.finalize(
            run_id=run_id,
            target_status=RunStatus.SUCCEEDED,
            content=content,
            fencing_token=fencing_token,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost=cost,
            rag_trace=rag_trace,
        )

    def fail_run(
        self,
        *,
        run_id: str,
        error_code: str,
        message: str = "运行失败，请稍后重试。",
        fencing_token: int | None = None,
        partial: bool = False,
    ) -> RunRecord:
        return self.repository.finalize(
            run_id=run_id,
            target_status=RunStatus.FAILED,
            content=message,
            fencing_token=fencing_token,
            error_code=error_code,
            error_detail_redacted=error_code,
            partial=partial,
        )

    def reconcile_orphans(self, *, now: datetime | None = None) -> list[str]:
        return self.repository.reconcile_orphans(now=now)

    def request_cancel(self, *, username: str, run_id: str) -> RunRecord:
        current = self.repository.get(username=username, run_id=run_id)
        if current.status in {
            RunStatus.SUCCEEDED.value,
            RunStatus.FAILED.value,
            RunStatus.CANCELLED.value,
        }:
            return current
        if current.status in {
            RunStatus.QUEUED.value,
            RunStatus.PENDING.value,
            RunStatus.WAITING_INPUT.value,
        }:
            return self.repository.finalize(
                run_id=run_id,
                target_status=RunStatus.CANCELLED,
                content="运行已由用户取消。",
                error_code="RUN_CANCELLED",
                error_detail_redacted="cancelled by user",
                partial=True,
            )
        return self.repository.mark_cancelling(username=username, run_id=run_id)


service = RunService()
