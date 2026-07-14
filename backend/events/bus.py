from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from backend.core.settings import get_settings
from backend.events.generated.run_event_v1 import RunEventType, RunEventV1
from backend.events.journal import RunEventJournal, journal
from backend.events.outbox import default_transport
from backend.events.redis_transport import RedisEventTransport


TERMINAL_EVENT_TYPES = {
    RunEventType.RUN_COMPLETED,
    RunEventType.RUN_FAILED,
    RunEventType.RUN_CANCELLED,
}


class PersistentEventBus:
    def __init__(
        self,
        event_journal: RunEventJournal = journal,
        transport: RedisEventTransport | None = default_transport,
    ) -> None:
        self.journal = event_journal
        self.transport = transport

    async def publish(
        self,
        *,
        run_id: str,
        event_type: RunEventType | str,
        data: dict | None = None,
    ) -> RunEventV1:
        appended = await asyncio.to_thread(
            self.journal.append,
            run_id=run_id,
            event_type=event_type,
            data=data,
        )
        if self.transport is not None:
            try:
                await self.transport.publish(appended.event)
                await asyncio.to_thread(
                    self.journal.mark_outbox_published,
                    appended.outbox_id,
                )
            except Exception:
                pass
        return appended.event

    async def subscribe(
        self,
        *,
        username: str,
        run_id: str,
        after: int = 0,
        heartbeat_seconds: float | None = None,
    ) -> AsyncIterator[RunEventV1 | None]:
        await asyncio.to_thread(
            self.journal.assert_access, username=username, run_id=run_id
        )
        settings = get_settings().runs
        heartbeat = heartbeat_seconds or settings.heartbeat_seconds
        poll_interval = settings.event_poll_interval_seconds
        cursor = max(after, 0)
        loop = asyncio.get_running_loop()
        heartbeat_deadline = loop.time() + heartbeat

        while True:
            events = await asyncio.to_thread(
                self.journal.read_after,
                run_id=run_id,
                after=cursor,
                limit=500,
            )
            if events:
                for event in events:
                    if event.sequence <= cursor:
                        continue
                    cursor = event.sequence
                    yield event
                    if event.type in TERMINAL_EVENT_TYPES:
                        return
                heartbeat_deadline = loop.time() + heartbeat
                continue

            remaining = heartbeat_deadline - loop.time()
            if remaining <= 0:
                yield None
                heartbeat_deadline = loop.time() + heartbeat
                continue

            wait_seconds = min(poll_interval, remaining)
            if self.transport is not None:
                try:
                    await self.transport.wait_after(
                        run_id=run_id,
                        after=cursor,
                        block_ms=max(1, int(wait_seconds * 1000)),
                    )
                    continue
                except Exception:
                    pass
            await asyncio.sleep(wait_seconds)


event_bus = PersistentEventBus()
