from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Callable

from sqlalchemy.orm import Session

from backend.core.settings import get_settings
from backend.db.models import TransactionOutbox, utcnow
from backend.events.generated.run_event_v1 import RunEventV1
from backend.events.redis_transport import RedisEventTransport
from backend.infra.database import SessionLocal


logger = logging.getLogger(__name__)
SessionFactory = Callable[[], Session]


@dataclass(frozen=True)
class OutboxItem:
    id: int
    payload: dict


class OutboxPublisher:
    def __init__(
        self,
        transport: RedisEventTransport,
        session_factory: SessionFactory = SessionLocal,
    ) -> None:
        self.transport = transport
        self._session_factory = session_factory

    def _load_pending(self, limit: int) -> list[OutboxItem]:
        db = self._session_factory()
        try:
            rows = (
                db.query(TransactionOutbox)
                .filter(
                    TransactionOutbox.topic == "run_event",
                    TransactionOutbox.published_at.is_(None),
                )
                .order_by(TransactionOutbox.id.asc())
                .limit(limit)
                .all()
            )
            return [
                OutboxItem(id=row.id, payload=dict(row.payload_json or {}))
                for row in rows
            ]
        finally:
            db.close()

    def _mark_published(self, outbox_id: int) -> None:
        db = self._session_factory()
        try:
            with db.begin():
                row = (
                    db.query(TransactionOutbox)
                    .filter(TransactionOutbox.id == outbox_id)
                    .with_for_update()
                    .first()
                )
                if row and row.published_at is None:
                    row.published_at = utcnow()
        finally:
            db.close()

    def _mark_attempt(self, outbox_id: int) -> None:
        db = self._session_factory()
        try:
            with db.begin():
                row = (
                    db.query(TransactionOutbox)
                    .filter(TransactionOutbox.id == outbox_id)
                    .first()
                )
                if row:
                    row.attempts += 1
        finally:
            db.close()

    async def publish_pending(self, limit: int | None = None) -> int:
        batch_size = limit or get_settings().runs.outbox_batch_size
        items = await asyncio.to_thread(self._load_pending, batch_size)
        published = 0
        for item in items:
            try:
                event = RunEventV1.model_validate(item.payload)
                await self.transport.publish(event)
                await asyncio.to_thread(self._mark_published, item.id)
                published += 1
            except Exception:
                await asyncio.to_thread(self._mark_attempt, item.id)
                logger.warning(
                    "run event outbox publish failed id=%s", item.id, exc_info=True
                )
                break
        return published

    async def run(self, stop_event: asyncio.Event) -> None:
        interval = get_settings().runs.event_poll_interval_seconds
        while not stop_event.is_set():
            try:
                published = await self.publish_pending()
            except Exception:
                logger.warning("run event outbox loop failed", exc_info=True)
                published = 0
            if published:
                continue
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
            except TimeoutError:
                continue

    async def close(self) -> None:
        await self.transport.close()


default_transport = RedisEventTransport()
default_publisher = OutboxPublisher(default_transport)
