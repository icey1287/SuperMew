from __future__ import annotations

from dataclasses import dataclass

from backend.rag.checkpoint_runner import (
    HitlCheckpointRepository,
    ResumeAccessState,
    ResumeAccessValidator,
    checkpoint_repository,
)
from backend.runs.repository import RunRecord
from backend.runs.service import RunService, service


@dataclass(frozen=True)
class RunResumeAcceptance:
    run: RunRecord
    checkpoint_id: str
    created: bool


def _validate_current_runtime_access(state: ResumeAccessState) -> None:
    from backend.capabilities.control_service import capability_control_service

    with capability_control_service.acquire_factory() as factory:
        factory.validate_resume_access(state)


class RunResumeCoordinator:
    """Atomic interface for accepting one HITL answer and re-queueing its Run."""

    def __init__(
        self,
        *,
        checkpoints: HitlCheckpointRepository = checkpoint_repository,
        run_service: RunService = service,
        access_validator: ResumeAccessValidator = _validate_current_runtime_access,
    ) -> None:
        self.checkpoints = checkpoints
        self.run_service = run_service
        self.access_validator = access_validator

    def accept(
        self,
        *,
        username: str,
        run_id: str,
        hitl_token: str,
        answer: str,
        idempotency_key: str,
    ) -> RunResumeAcceptance:
        consumed = self.checkpoints.consume_resume(
            username=username,
            run_id=run_id,
            hitl_token=hitl_token,
            answer=answer,
            idempotency_key=idempotency_key,
            worker_id=None,
            preflight=self.access_validator,
        )
        return RunResumeAcceptance(
            run=self.run_service.get_run(username=username, run_id=run_id),
            checkpoint_id=consumed.checkpoint_id,
            created=not consumed.already_consumed,
        )


resume_coordinator = RunResumeCoordinator()
