from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from backend.core.errors import (
    AppError,
    ErrorCode,
    PublicError,
    public_error_from_exception,
    serialize_public_error,
)
from backend.model_control import ModelControlService, ModelRole, model_control_service
from backend.runs.repository import RunRecord, RunRepository, RunReservation, repository
from backend.runs.state import MultitaskStrategy, RunStatus


class RunService:
    """Run 生命周期的应用 interface；HTTP 与执行 worker 共享。"""

    def __init__(
        self,
        run_repository: RunRepository = repository,
        *,
        model_control: ModelControlService = model_control_service,
        _allow_implicit_threads: bool = False,
    ) -> None:
        self.repository = run_repository
        self.model_control = model_control
        self._allow_implicit_threads = _allow_implicit_threads

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
        tenant_id: str = "default",
        channel: str = "run",
        approved_tools: frozenset[str] = frozenset(),
    ) -> RunReservation:
        compact_message = message.strip()
        model_snapshot = self.model_control.runtime_snapshot(
            required_roles=frozenset({ModelRole.ANSWER})
        )
        answer_model = model_snapshot.require(ModelRole.ANSWER)
        return self.repository.reserve(
            username=username,
            thread_id=thread_id,
            message=compact_message,
            idempotency_key=idempotency_key,
            expected_thread_version=expected_thread_version,
            model_name=answer_model.model_name,
            model_snapshot=model_snapshot,
            on_disconnect=on_disconnect,
            multitask_strategy=multitask_strategy,
            title=(" ".join(compact_message.split())[:16] or "新会话"),
            tenant_id=tenant_id,
            channel=channel,
            approved_tools=approved_tools,
            _allow_implicit_thread=self._allow_implicit_threads,
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
        error_code: str | None = None,
        message: str | None = None,
        public_error: PublicError | AppError | None = None,
        fencing_token: int | None = None,
        partial: bool = False,
    ) -> RunRecord:
        if isinstance(public_error, AppError):
            resolved_error = public_error_from_exception(public_error)
        elif isinstance(public_error, PublicError):
            resolved_error = public_error
        else:
            resolved_error = PublicError(
                code=error_code or ErrorCode.RUN_EXECUTION_FAILED,
                message=message or "运行失败，请稍后重试。",
                status_code=500,
                retryable=True,
                category="run",
                stage="execution",
            )
        return self.repository.finalize(
            run_id=run_id,
            target_status=RunStatus.FAILED,
            content=message or resolved_error.message,
            fencing_token=fencing_token,
            error_code=str(resolved_error.code),
            error_detail_redacted=serialize_public_error(resolved_error),
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
            public_error = PublicError(
                code=ErrorCode.RUN_CANCELLED,
                message="运行已由用户取消。",
                status_code=409,
                retryable=False,
                category="run",
                stage="cancellation",
            )
            return self.repository.finalize(
                run_id=run_id,
                target_status=RunStatus.CANCELLED,
                content=public_error.message,
                error_code=str(public_error.code),
                error_detail_redacted=serialize_public_error(public_error),
                partial=True,
            )
        return self.repository.mark_cancelling(username=username, run_id=run_id)


service = RunService()
