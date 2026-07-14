from enum import StrEnum


class RunStatus(StrEnum):
    QUEUED = "queued"
    PENDING = "pending"
    RUNNING = "running"
    WAITING_INPUT = "waiting_input"
    CANCELLING = "cancelling"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class MultitaskStrategy(StrEnum):
    REJECT = "reject"
    ENQUEUE = "enqueue"
    CANCEL_PREVIOUS = "cancel_previous"


ACTIVE_RUN_STATUSES = {
    RunStatus.PENDING.value,
    RunStatus.RUNNING.value,
    RunStatus.WAITING_INPUT.value,
    RunStatus.CANCELLING.value,
}

TERMINAL_RUN_STATUSES = {
    RunStatus.SUCCEEDED.value,
    RunStatus.FAILED.value,
    RunStatus.CANCELLED.value,
}
