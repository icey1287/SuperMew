import unittest
from datetime import timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.core.errors import AppError, ErrorCode
from backend.db.models import Base, ChatMessage, Run, RunEvent, ToolAudit, User, utcnow
from backend.runs.repository import RunRepository
from backend.runs.service import RunService
from backend.runs.state import MultitaskStrategy, RunStatus, can_transition


class RunServiceTests(unittest.TestCase):
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
        self.service = RunService(self.repository)

    def tearDown(self):
        self.engine.dispose()

    def create(self, key="request-1", **kwargs):
        return self.service.create_run(
            username="alice",
            thread_id="thread-1",
            message=kwargs.pop("message", "hello"),
            idempotency_key=key,
            **kwargs,
        )

    def test_run_lifecycle_finalizes_message_and_run_together(self):
        reservation = self.create()
        claimed = self.service.claim_run(
            run_id=reservation.run.id, worker_id="worker-1"
        )
        completed = self.service.complete_run(
            run_id=claimed.id,
            content="answer",
            fencing_token=claimed.fencing_token,
            input_tokens=10,
            output_tokens=5,
            cost="0.001",
        )

        self.assertEqual(RunStatus.SUCCEEDED, completed.status)
        with self.Session() as db:
            run = db.query(Run).filter(Run.id == completed.id).one()
            message = (
                db.query(ChatMessage)
                .filter(ChatMessage.id == run.assistant_message_id)
                .one()
            )
            self.assertEqual("succeeded", run.status)
            self.assertEqual("answer", message.content)
            self.assertEqual("completed", message.status)

        repeated = self.service.complete_run(
            run_id=claimed.id,
            content="answer",
            fencing_token=claimed.fencing_token,
        )
        self.assertEqual(completed.id, repeated.id)

    def test_stale_fencing_token_cannot_finalize(self):
        reservation = self.create()
        claimed = self.service.claim_run(
            run_id=reservation.run.id, worker_id="worker-1"
        )
        with self.assertRaises(AppError) as raised:
            self.service.complete_run(
                run_id=claimed.id,
                content="stale",
                fencing_token=reservation.run.fencing_token,
            )
        self.assertEqual(ErrorCode.RUN_STATE_CONFLICT, raised.exception.code)

    def test_tool_audit_replay_is_idempotent_and_result_conflicts_fail_closed(self):
        reservation = self.create()
        claimed = self.service.claim_run(
            run_id=reservation.run.id,
            worker_id="worker-1",
        )
        audit = {
            "run_id": claimed.id,
            "worker_id": "worker-1",
            "fencing_token": claimed.fencing_token,
            "audit_key": "a" * 64,
            "tool_call_id": "call-1",
            "tool_name": "search_knowledge_base",
            "tool_version": "1.0.0",
            "decision": "allowed",
            "success": True,
            "error_code": None,
            "duration_ms": 12,
            "result_size": 42,
            "metadata": {"tool_group": "knowledge"},
        }

        self.repository.record_tool_audit(**audit)
        self.repository.record_tool_audit(**audit)

        with self.Session() as db:
            rows = db.query(ToolAudit).filter(ToolAudit.run_id == claimed.id).all()
            self.assertEqual(1, len(rows))
            self.assertEqual("a" * 64, rows[0].audit_key)
            self.assertEqual(42, rows[0].result_size)
            self.assertEqual("", rows[0].metadata_json["skill_name"])

        with self.assertRaises(AppError) as raised:
            self.repository.record_tool_audit(
                **{
                    **audit,
                    "success": False,
                    "error_code": "TOOL_UNAVAILABLE",
                }
            )
        self.assertEqual(ErrorCode.RUN_STATE_CONFLICT, raised.exception.code)

        with self.Session() as db:
            self.assertEqual(
                1,
                db.query(ToolAudit).filter(ToolAudit.run_id == claimed.id).count(),
            )

    def test_tool_audit_rejects_stale_owner_fence_before_writing(self):
        reservation = self.create()
        claimed = self.service.claim_run(
            run_id=reservation.run.id,
            worker_id="worker-1",
        )

        with self.assertRaises(AppError) as raised:
            self.repository.record_tool_audit(
                run_id=claimed.id,
                worker_id="worker-1",
                fencing_token=claimed.fencing_token - 1,
                audit_key="b" * 64,
                tool_call_id="call-stale",
                tool_name="search_knowledge_base",
                tool_version="1.0.0",
                decision="allowed",
                success=True,
                error_code=None,
                duration_ms=1,
                result_size=1,
            )

        self.assertEqual(ErrorCode.RUN_STATE_CONFLICT, raised.exception.code)
        with self.Session() as db:
            self.assertEqual(
                0,
                db.query(ToolAudit).filter(ToolAudit.run_id == claimed.id).count(),
            )

    def test_terminal_run_promotes_oldest_queued_run(self):
        first = self.create("request-1")
        second = self.create(
            "request-2",
            message="second",
            multitask_strategy=MultitaskStrategy.ENQUEUE,
        )
        self.assertEqual(RunStatus.QUEUED, second.run.status)

        claimed = self.service.claim_run(run_id=first.run.id, worker_id="worker-1")
        self.service.complete_run(
            run_id=first.run.id,
            content="done",
            fencing_token=claimed.fencing_token,
        )

        promoted = self.service.get_run(username="alice", run_id=second.run.id)
        self.assertEqual(RunStatus.PENDING, promoted.status)
        with self.Session() as db:
            assistant = (
                db.query(ChatMessage)
                .filter(ChatMessage.id == promoted.assistant_message_id)
                .one()
            )
            self.assertEqual("streaming", assistant.status)

    def test_orphan_reconciler_marks_partial_failure(self):
        reservation = self.create()
        claimed = self.repository.claim(
            run_id=reservation.run.id,
            worker_id="worker-1",
            lease_seconds=1,
        )
        recovered = self.service.reconcile_orphans(now=utcnow() + timedelta(seconds=2))
        self.assertEqual([claimed.id], recovered)
        with self.Session() as db:
            run = db.query(Run).filter(Run.id == claimed.id).one()
            assistant = (
                db.query(ChatMessage)
                .filter(ChatMessage.id == run.assistant_message_id)
                .one()
            )
            self.assertEqual("failed", run.status)
            self.assertEqual("incomplete", assistant.status)
        failed = self.service.get_run(username="alice", run_id=claimed.id)
        self.assertEqual("ORPHAN_RUN", failed.error_code)
        self.assertEqual("run", failed.error["category"])
        self.assertEqual("ownership", failed.error["stage"])
        self.assertTrue(failed.error["retryable"])

    def test_heartbeat_prevents_orphan_recovery(self):
        reservation = self.create()
        claimed = self.repository.claim(
            run_id=reservation.run.id,
            worker_id="worker-1",
            lease_seconds=1,
        )
        self.repository.heartbeat(
            run_id=claimed.id,
            worker_id="worker-1",
            fencing_token=claimed.fencing_token,
            lease_seconds=30,
        )
        recovered = self.service.reconcile_orphans(now=utcnow() + timedelta(seconds=2))
        self.assertEqual([], recovered)

    def test_heartbeat_keeps_ownership_while_run_is_cancelling(self):
        reservation = self.create()
        claimed = self.service.claim_run(
            run_id=reservation.run.id,
            worker_id="worker-1",
        )
        cancelling = self.service.request_cancel(
            username="alice",
            run_id=claimed.id,
        )

        heartbeat = self.repository.heartbeat(
            run_id=claimed.id,
            worker_id="worker-1",
            fencing_token=claimed.fencing_token,
            lease_seconds=30,
        )

        self.assertEqual(RunStatus.CANCELLING, cancelling.status)
        self.assertEqual(RunStatus.CANCELLING, heartbeat.status)
        self.assertEqual("worker-1", heartbeat.owner_worker_id)

    def test_durable_cancelling_wins_completion_race(self):
        reservation = self.create()
        claimed = self.service.claim_run(
            run_id=reservation.run.id,
            worker_id="worker-1",
        )
        cancelling = self.service.request_cancel(
            username="alice",
            run_id=claimed.id,
        )

        terminal = self.service.complete_run(
            run_id=claimed.id,
            content="late completed answer",
            fencing_token=claimed.fencing_token,
        )

        self.assertEqual(RunStatus.CANCELLING, cancelling.status)
        self.assertEqual(RunStatus.CANCELLED, terminal.status)
        self.assertEqual("RUN_CANCELLED", terminal.error_code)
        self.assertEqual("运行已由用户取消。", terminal.error["message"])
        with self.Session() as db:
            run = db.query(Run).filter(Run.id == claimed.id).one()
            assistant = (
                db.query(ChatMessage)
                .filter(ChatMessage.id == run.assistant_message_id)
                .one()
            )
            self.assertEqual("运行已由用户取消。", assistant.content)
            self.assertEqual("incomplete", assistant.status)
            self.assertEqual(
                1,
                db.query(RunEvent)
                .filter(
                    RunEvent.run_id == claimed.id,
                    RunEvent.event_type == "run.cancelled",
                )
                .count(),
            )
            self.assertEqual(
                0,
                db.query(RunEvent)
                .filter(
                    RunEvent.run_id == claimed.id,
                    RunEvent.event_type == "run.completed",
                )
                .count(),
            )

    def test_heartbeat_does_not_emit_waiting_event_and_wait_transition_does(self):
        reservation = self.create()
        claimed = self.repository.claim(
            run_id=reservation.run.id,
            worker_id="worker-1",
            lease_seconds=1,
        )
        self.repository.heartbeat(
            run_id=claimed.id,
            worker_id="worker-1",
            fencing_token=claimed.fencing_token,
            lease_seconds=30,
        )
        waiting = self.service.wait_for_input(
            run_id=claimed.id,
            worker_id="worker-1",
            fencing_token=claimed.fencing_token,
        )

        self.assertEqual(RunStatus.WAITING_INPUT, waiting.status)
        self.assertIsNone(waiting.owner_worker_id)
        with self.Session() as db:
            events = (
                db.query(RunEvent)
                .filter(
                    RunEvent.run_id == claimed.id,
                    RunEvent.event_type == "run.waiting_input",
                )
                .all()
            )
            assistant = (
                db.query(ChatMessage)
                .filter(ChatMessage.id == waiting.assistant_message_id)
                .one()
            )
            self.assertEqual(1, len(events))
            self.assertEqual("waiting_input", assistant.status)

    def test_run_ownership_is_enforced(self):
        reservation = self.create()
        with self.assertRaises(AppError) as raised:
            self.service.get_run(username="bob", run_id=reservation.run.id)
        self.assertEqual(ErrorCode.RUN_NOT_FOUND, raised.exception.code)

    def test_state_machine_rejects_invalid_edge(self):
        reservation = self.create()
        with self.assertRaises(AppError) as raised:
            self.service.complete_run(run_id=reservation.run.id, content="too soon")
        self.assertEqual(ErrorCode.RUN_STATE_CONFLICT, raised.exception.code)
        self.assertFalse(can_transition("pending", "succeeded"))


if __name__ == "__main__":
    unittest.main()
