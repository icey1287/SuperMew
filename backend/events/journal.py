from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC
from typing import Callable

from sqlalchemy.orm import Session

from backend.core.errors import AppError, ErrorCode
from backend.db.models import (
    Thread,
    Run,
    RunEvent,
    TransactionOutbox,
    User,
)
from backend.events.contracts import RunEventType, RunEventV1, new_run_event
from backend.infra.database import SessionLocal


SessionFactory = Callable[[], Session]


@dataclass(frozen=True)
class JournalAppend:
    event: RunEventV1
    outbox_id: int


def append_event_in_session(
    db: Session,
    *,
    run: Run,
    thread_id: str,
    event_type: RunEventType | str,
    data: dict | None = None,
) -> JournalAppend:
    run.last_event_sequence += 1
    event = new_run_event(
        sequence=run.last_event_sequence,
        run_id=run.id,
        thread_id=thread_id,
        event_type=event_type,
        data=data,
    )
    created_at = event.timestamp.astimezone(UTC).replace(tzinfo=None)
    row = RunEvent(
        event_id=event.event_id,
        run_id=run.id,
        sequence=event.sequence,
        schema_version=event.schema_version,
        event_type=event.type.value,
        payload_json=event.data,
        created_at=created_at,
    )
    outbox = TransactionOutbox(
        topic="run_event",
        aggregate_id=run.id,
        payload_json=event.model_dump(mode="json"),
        attempts=0,
        created_at=created_at,
    )
    db.add_all([row, outbox])
    db.flush()
    return JournalAppend(event=event, outbox_id=outbox.id)


class RunEventJournal:
    """PostgreSQL durable event journal adapter。"""

    def __init__(self, session_factory: SessionFactory = SessionLocal):
        self._session_factory = session_factory

    def append(
        self,
        *,
        run_id: str,
        event_type: RunEventType | str,
        data: dict | None = None,
        worker_id: str | None = None,
        fencing_token: int | None = None,
    ) -> JournalAppend:
        db = self._session_factory()
        try:
            with db.begin():
                run = db.query(Run).filter(Run.id == run_id).with_for_update().first()
                if not run:
                    raise AppError(
                        ErrorCode.RUN_NOT_FOUND, "Run 不存在", status_code=404
                    )
                if worker_id is not None or fencing_token is not None:
                    if (
                        worker_id is None
                        or fencing_token is None
                        or run.owner_worker_id != worker_id
                        or run.fencing_token != fencing_token
                        or run.status not in {"running", "cancelling"}
                    ):
                        raise AppError(
                            ErrorCode.RUN_STATE_CONFLICT,
                            "当前 worker 不再拥有该 Run 的事件写权限",
                            status_code=409,
                        )
                thread = db.query(Thread).filter(Thread.id == run.thread_ref_id).one()
                return append_event_in_session(
                    db,
                    run=run,
                    thread_id=thread.thread_id,
                    event_type=event_type,
                    data=data,
                )
        finally:
            db.close()

    @staticmethod
    def _to_event(row: RunEvent, thread_id: str) -> RunEventV1:
        return RunEventV1(
            schema_version=row.schema_version,
            event_id=row.event_id,
            sequence=row.sequence,
            run_id=row.run_id,
            thread_id=thread_id,
            type=row.event_type,
            timestamp=row.created_at.replace(tzinfo=UTC),
            data=row.payload_json or {},
        )

    def assert_access(self, *, username: str, run_id: str) -> str:
        db = self._session_factory()
        try:
            row = (
                db.query(Thread.thread_id)
                .join(Run, Run.thread_ref_id == Thread.id)
                .join(User, User.id == Run.user_id)
                .filter(Run.id == run_id, User.username == username)
                .first()
            )
            if not row:
                raise AppError(ErrorCode.RUN_NOT_FOUND, "Run 不存在", status_code=404)
            return row[0]
        finally:
            db.close()

    def read_after(
        self,
        *,
        run_id: str,
        after: int = 0,
        limit: int = 500,
        username: str | None = None,
    ) -> list[RunEventV1]:
        db = self._session_factory()
        try:
            query = (
                db.query(RunEvent, Thread.thread_id)
                .join(Run, Run.id == RunEvent.run_id)
                .join(Thread, Thread.id == Run.thread_ref_id)
                .filter(RunEvent.run_id == run_id, RunEvent.sequence > max(after, 0))
            )
            if username is not None:
                query = query.join(User, User.id == Run.user_id).filter(
                    User.username == username
                )
            rows = (
                query.order_by(RunEvent.sequence.asc())
                .limit(max(1, min(limit, 1000)))
                .all()
            )
            if username is not None and not rows:
                self.assert_access(username=username, run_id=run_id)
            return [self._to_event(row, thread_id) for row, thread_id in rows]
        finally:
            db.close()

    def mark_outbox_published(self, outbox_id: int) -> None:
        from backend.db.models import utcnow

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


journal = RunEventJournal()
