from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from backend.events.generated.run_event_v1 import RunEventType, RunEventV1


def new_run_event(
    *,
    sequence: int,
    run_id: str,
    thread_id: str,
    event_type: RunEventType | str,
    data: dict | None = None,
    event_id: str | None = None,
    timestamp: datetime | None = None,
) -> RunEventV1:
    return RunEventV1(
        event_id=event_id or f"evt_{uuid4().hex}",
        sequence=sequence,
        run_id=run_id,
        thread_id=thread_id,
        type=RunEventType(event_type),
        timestamp=timestamp or datetime.now(UTC),
        data=data or {},
    )
