import unittest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.core.errors import AppError, ErrorCode
from backend.db.models import (
    Base,
    Message,
    Run,
    RunEvent,
    TransactionOutbox,
    User,
)
from backend.events.bus import PersistentEventBus
from backend.events.journal import RunEventJournal
from backend.events.outbox import OutboxPublisher
from backend.events.redis_transport import RedisEventTransport
from backend.events.sse import format_sse_event
from backend.runs.repository import RunRepository
from backend.runs.service import RunService
from tests.support import static_model_control


class FakeTransport:
    def __init__(self):
        self.events = []

    async def publish(self, event):
        self.events.append(event)

    async def close(self):
        return None


class FakeRedisClient:
    def __init__(self):
        self.added = []

    async def xadd(self, key, fields, **kwargs):
        self.added.append((key, fields, kwargs))
        return kwargs["id"]

    async def xread(self, _streams, **_kwargs):
        key, fields, kwargs = self.added[-1]
        return [(key, [(kwargs["id"], fields)])]

    async def aclose(self):
        return None


class EventBusTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)
        with self.Session.begin() as db:
            db.add_all(
                [
                    User(username="alice", password_hash="hash", role="user"),
                    User(username="bob", password_hash="hash", role="user"),
                ]
            )
        self.repository = RunRepository(self.Session)
        self.service = RunService(
            self.repository,
            model_control=static_model_control,
            _allow_implicit_threads=True,
        )
        self.journal = RunEventJournal(self.Session)

    def tearDown(self):
        self.engine.dispose()

    def create_run(self):
        return self.service.create_run(
            username="alice",
            thread_id="thread-1",
            message="hello",
            idempotency_key="request-1",
        )

    async def test_lifecycle_events_are_monotonic_and_terminal_is_last(self):
        reservation = self.create_run()
        claimed = self.service.claim_run(
            run_id=reservation.run.id,
            worker_id="worker-1",
        )
        self.service.complete_run(
            run_id=claimed.id,
            content="answer",
            fencing_token=claimed.fencing_token,
            input_tokens=2,
            output_tokens=3,
        )

        events = self.journal.read_after(
            username="alice",
            run_id=claimed.id,
        )
        self.assertEqual(
            list(range(1, len(events) + 1)), [item.sequence for item in events]
        )
        self.assertEqual(
            [
                "run.created",
                "run.started",
                "usage.updated",
                "message.completed",
                "run.completed",
            ],
            [item.type.value for item in events],
        )
        with self.Session() as db:
            run = db.query(Run).filter(Run.id == claimed.id).one()
            message = (
                db.query(Message).filter(Message.id == run.assistant_message_id).one()
            )
            self.assertEqual("answer", message.content)
            self.assertEqual(len(events), run.last_event_sequence)
            self.assertEqual(len(events), db.query(TransactionOutbox).count())

    async def test_replay_after_sequence_and_sse_projection(self):
        reservation = self.create_run()
        claimed = self.service.claim_run(
            run_id=reservation.run.id,
            worker_id="worker-1",
        )
        self.service.complete_run(
            run_id=claimed.id,
            content="done",
            fencing_token=claimed.fencing_token,
        )
        bus = PersistentEventBus(self.journal, transport=None)
        replay = []
        async for item in bus.subscribe(
            username="alice",
            run_id=claimed.id,
            after=1,
            heartbeat_seconds=0.01,
        ):
            if item is not None:
                replay.append(item)
        self.assertEqual([2, 3, 4], [item.sequence for item in replay])
        frame = format_sse_event(replay[-1])
        self.assertIn("id: 4\n", frame)
        self.assertIn("event: run.completed\n", frame)

    async def test_nonterminal_subscription_emits_heartbeat(self):
        reservation = self.create_run()
        bus = PersistentEventBus(self.journal, transport=None)
        stream = bus.subscribe(
            username="alice",
            run_id=reservation.run.id,
            after=1,
            heartbeat_seconds=0.01,
        )
        self.assertIsNone(await anext(stream))
        await stream.aclose()

    async def test_outbox_publisher_is_idempotent(self):
        self.create_run()
        transport = FakeTransport()
        publisher = OutboxPublisher(transport, self.Session)
        self.assertEqual(1, await publisher.publish_pending())
        self.assertEqual(0, await publisher.publish_pending())
        self.assertEqual(1, len(transport.events))
        with self.Session() as db:
            row = db.query(TransactionOutbox).one()
            self.assertIsNotNone(row.published_at)

    async def test_redis_transport_uses_sequence_as_stream_id(self):
        reservation = self.create_run()
        event = self.journal.read_after(
            username="alice",
            run_id=reservation.run.id,
        )[0]
        transport = RedisEventTransport("redis://unused")
        fake = FakeRedisClient()
        transport._client = fake
        await transport.publish(event)
        self.assertEqual("1-0", fake.added[0][2]["id"])
        replay = await transport.wait_after(
            run_id=event.run_id,
            after=0,
            block_ms=1,
        )
        self.assertEqual(event, replay[0])

    async def test_event_ownership_is_enforced(self):
        reservation = self.create_run()
        with self.assertRaises(AppError) as raised:
            self.journal.read_after(
                username="bob",
                run_id=reservation.run.id,
            )
        self.assertEqual(ErrorCode.RUN_NOT_FOUND, raised.exception.code)

    async def test_duplicate_finalize_does_not_append_terminal_event_twice(self):
        reservation = self.create_run()
        claimed = self.service.claim_run(
            run_id=reservation.run.id,
            worker_id="worker-1",
        )
        self.service.complete_run(
            run_id=claimed.id,
            content="done",
            fencing_token=claimed.fencing_token,
        )
        self.service.complete_run(
            run_id=claimed.id,
            content="done",
            fencing_token=claimed.fencing_token,
        )
        with self.Session() as db:
            terminal_count = (
                db.query(RunEvent)
                .filter(
                    RunEvent.run_id == claimed.id,
                    RunEvent.event_type == "run.completed",
                )
                .count()
            )
            self.assertEqual(1, terminal_count)


if __name__ == "__main__":
    unittest.main()
