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


ALLOWED_TRANSITIONS = {
    RunStatus.QUEUED.value: {
        RunStatus.PENDING.value,
        RunStatus.CANCELLED.value,
    },
    RunStatus.PENDING.value: {
        RunStatus.RUNNING.value,
        RunStatus.CANCELLING.value,
        RunStatus.CANCELLED.value,
        RunStatus.FAILED.value,
    },
    RunStatus.RUNNING.value: {
        RunStatus.WAITING_INPUT.value,
        RunStatus.CANCELLING.value,
        RunStatus.SUCCEEDED.value,
        RunStatus.FAILED.value,
        RunStatus.CANCELLED.value,
    },
    RunStatus.WAITING_INPUT.value: {
        RunStatus.RUNNING.value,
        RunStatus.CANCELLING.value,
        RunStatus.CANCELLED.value,
        RunStatus.FAILED.value,
    },
    RunStatus.CANCELLING.value: {
        RunStatus.CANCELLED.value,
        RunStatus.FAILED.value,
    },
    RunStatus.SUCCEEDED.value: set(),
    RunStatus.FAILED.value: set(),
    RunStatus.CANCELLED.value: set(),
}


def can_transition(current: str, target: str) -> bool:
    return current == target or target in ALLOWED_TRANSITIONS.get(current, set())
