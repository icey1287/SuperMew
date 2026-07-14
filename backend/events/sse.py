from __future__ import annotations

from datetime import UTC, datetime

from backend.events.generated.run_event_v1 import RunEventV1


def format_sse_event(event: RunEventV1) -> str:
    return (
        f"id: {event.sequence}\n"
        f"event: {event.type.value}\n"
        f"data: {event.model_dump_json()}\n\n"
    )


def format_sse_heartbeat() -> str:
    return f": heartbeat {datetime.now(UTC).isoformat()}\n\n"
