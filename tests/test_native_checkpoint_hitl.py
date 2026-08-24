import json
import sys
import unittest
from contextlib import contextmanager
from unittest.mock import patch

from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.runs.request_context import RunRequestContext
from backend.core.errors import AppError, ErrorCode
from backend.db.models import Base, Message, Run, RunCheckpoint, RunEvent, User
from backend.rag.checkpoint_runner import (
    CheckpointedRagRunner,
    HitlCheckpointRepository,
    _assert_checkpoint_tenant,
)
from backend.runs.repository import RunRepository
from backend.runs.resume import RunResumeCoordinator
from backend.runs.service import RunService
from backend.runs.state import RunStatus
from tests.support import static_model_control
from test_rag_short_circuit import FakeStructuredModel, _doc, _meta, load_pipeline


class NativeCheckpointGraphTests(unittest.TestCase):
    QUESTION = (
        "请基于当前知识库中关于角色设定的完整说明，确认文档里提到的那个角色"
        "在最新版本中的具体属性类型，并只返回对应属性名称"
    )

    @staticmethod
    def _pipeline(*, clarify_rounds: int):
        calls = {"complexity": 0, "retrieve": [], "grade": 0}

        def retrieve(query, top_k=5):
            calls["retrieve"].append(query)
            return {"docs": [_doc(f"evidence for {query}")], "meta": _meta(1)}

        def complexity(schema, prompt):
            calls["complexity"] += 1
            return {"complexity": "simple", "reason": "native-checkpoint-test"}

        def grade(schema, prompt):
            calls["grade"] += 1
            if calls["grade"] <= clarify_rounds:
                return {
                    "relevance": "strong",
                    "answerability": "partial",
                    "ambiguity": "missing_slot",
                    "route": "clarify",
                    "confidence": 0.7,
                    "missing_slots": ["角色名"],
                    "hitl_prompt": "请补充角色名",
                    "hitl_options": ["丹瑾", "丹恒"],
                }
            return {
                "relevance": "strong",
                "answerability": "sufficient",
                "ambiguity": "none",
                "route": "answer",
                "confidence": 0.95,
            }

        pipeline = load_pipeline(retrieve_documents=retrieve)
        pipeline._get_complexity_model = lambda *_: FakeStructuredModel(complexity)
        pipeline._get_grader_model = lambda *_: FakeStructuredModel(grade)
        return pipeline, calls

    def test_rag_state_is_json_serializable_and_contains_no_request_object(self):
        pipeline, _ = self._pipeline(clarify_rounds=0)
        state = pipeline._initial_state(
            "丹瑾是什么属性？",
            tenant_id="tenant-a",
            runtime_context_id="ragctx_test",
        )

        serialized = json.dumps(state, ensure_ascii=False)

        self.assertIn("ragctx_test", serialized)
        self.assertNotIn("request_context", state)
        self.assertFalse(
            any(isinstance(value, RunRequestContext) for value in state.values())
        )

    def test_durable_resume_rejects_a_different_or_missing_tenant_context(self):
        context_a = RunRequestContext.for_sync(
            user_id="alice",
            thread_id="thread-1",
            tenant_id="tenant-a",
        )
        context_b = RunRequestContext.for_sync(
            user_id="alice",
            thread_id="thread-1",
            tenant_id="tenant-b",
        )
        tenantless = RunRequestContext.for_sync(
            user_id="alice",
            thread_id="thread-1",
        )
        try:
            self.assertEqual(
                "tenant-a",
                _assert_checkpoint_tenant({"tenant_id": "tenant-a"}, context_a),
            )
            with self.assertRaises(AppError) as mismatched:
                _assert_checkpoint_tenant({"tenant_id": "tenant-a"}, context_b)
            with self.assertRaises(AppError) as missing_context:
                _assert_checkpoint_tenant({"tenant_id": "tenant-a"}, tenantless)
            with self.assertRaises(AppError) as missing_checkpoint:
                _assert_checkpoint_tenant({}, context_a)
        finally:
            context_a.close()
            context_b.close()
            tenantless.close()

        self.assertEqual(ErrorCode.RUN_STATE_CONFLICT, mismatched.exception.code)
        self.assertEqual(ErrorCode.RUN_STATE_CONFLICT, missing_context.exception.code)
        self.assertEqual(
            ErrorCode.RUN_STATE_CONFLICT, missing_checkpoint.exception.code
        )

    def test_new_graph_instance_resumes_without_repeating_completed_nodes(self):
        pipeline, calls = self._pipeline(clarify_rounds=1)
        saver = InMemorySaver()
        config = {"configurable": {"thread_id": "run_native_resume"}}
        context = RunRequestContext.for_sync(
            user_id="alice",
            thread_id="thread-1",
            tenant_id="tenant-a",
        )
        try:
            graph = pipeline.build_rag_graph(checkpointer=saver)
            with pipeline.bind_rag_runtime_context(context) as runtime_context_id:
                paused = graph.invoke(
                    pipeline._initial_state(
                        self.QUESTION,
                        tenant_id=context.require_tenant_id(),
                        runtime_context_id=runtime_context_id,
                    ),
                    config=config,
                )
            self.assertEqual(1, len(paused.get("__interrupt__", [])))
            snapshot = graph.get_state(config)

            rebuilt_graph = pipeline.build_rag_graph(checkpointer=saver)
            with pipeline.bind_rag_runtime_context(
                context, snapshot.values["runtime_context_id"]
            ):
                resumed = rebuilt_graph.invoke(
                    pipeline.Command(resume="丹瑾"),
                    config=config,
                )
        finally:
            context.close()

        self.assertFalse(resumed.get("__interrupt__"))
        self.assertEqual("answerable", resumed["retrieval_status"])
        self.assertEqual(1, calls["complexity"])
        self.assertEqual(2, calls["grade"])
        self.assertEqual(2, len(calls["retrieve"]))
        self.assertEqual(self.QUESTION, calls["retrieve"][0])
        self.assertIn("丹瑾", calls["retrieve"][1])

    def test_multiple_interrupts_resume_from_each_exact_checkpoint(self):
        pipeline, calls = self._pipeline(clarify_rounds=2)
        saver = InMemorySaver()
        config = {"configurable": {"thread_id": "run_multi_hitl"}}
        context = RunRequestContext.for_sync(
            user_id="alice",
            thread_id="thread-1",
            tenant_id="tenant-a",
        )
        try:
            graph1 = pipeline.build_rag_graph(checkpointer=saver)
            with pipeline.bind_rag_runtime_context(context) as runtime_context_id:
                first = graph1.invoke(
                    pipeline._initial_state(
                        self.QUESTION,
                        tenant_id=context.require_tenant_id(),
                        runtime_context_id=runtime_context_id,
                    ),
                    config=config,
                )
            first_snapshot = graph1.get_state(config)

            graph2 = pipeline.build_rag_graph(checkpointer=saver)
            with pipeline.bind_rag_runtime_context(
                context, first_snapshot.values["runtime_context_id"]
            ):
                second = graph2.invoke(
                    pipeline.Command(resume="角色是丹瑾"),
                    config=config,
                )
            second_snapshot = graph2.get_state(config)

            graph3 = pipeline.build_rag_graph(checkpointer=saver)
            with pipeline.bind_rag_runtime_context(
                context, second_snapshot.values["runtime_context_id"]
            ):
                completed = graph3.invoke(
                    pipeline.Command(resume="查询当前版本"),
                    config=config,
                )
        finally:
            context.close()

        self.assertEqual(1, len(first.get("__interrupt__", [])))
        self.assertEqual(1, len(second.get("__interrupt__", [])))
        self.assertNotEqual(
            first_snapshot.config["configurable"]["checkpoint_id"],
            second_snapshot.config["configurable"]["checkpoint_id"],
        )
        self.assertFalse(completed.get("__interrupt__"))
        self.assertEqual(["角色是丹瑾", "查询当前版本"], completed["hitl_answers"])
        self.assertEqual(1, calls["complexity"])
        self.assertEqual(3, calls["grade"])
        self.assertEqual(3, len(calls["retrieve"]))


class NativeCheckpointRepositoryTests(unittest.TestCase):
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
        self.run_repository = RunRepository(self.Session)
        self.run_service = RunService(
            self.run_repository,
            model_control=static_model_control,
            _allow_implicit_threads=True,
        )
        self.checkpoints = HitlCheckpointRepository(self.Session)
        self.coordinator = RunResumeCoordinator(
            checkpoints=self.checkpoints,
            run_service=self.run_service,
            access_validator=lambda _state: None,
        )

    def tearDown(self):
        self.engine.dispose()

    def _pause(self, *, thread_id="thread-1", request_key="request-1"):
        reservation = self.run_service.create_run(
            username="alice",
            thread_id=thread_id,
            message="这个角色是什么属性？",
            idempotency_key=request_key,
        )
        claimed = self.run_service.claim_run(
            run_id=reservation.run.id,
            worker_id="worker-1",
        )
        pause = self.checkpoints.record_pause(
            run_id=claimed.id,
            worker_id="worker-1",
            fencing_token=claimed.fencing_token,
            checkpoint_id=f"checkpoint-{request_key}",
            interrupt_id=f"interrupt-{request_key}",
            interrupt_value={
                "prompt": "请补充角色名",
                "options": ["丹瑾", "丹恒"],
                "route": "clarify",
                "retrieval_status": "needs_clarification",
            },
            state={"question": "这个角色是什么属性？", "runtime_context_id": "ctx"},
            next_nodes=("await_hitl",),
        )
        return claimed, pause

    def test_pause_is_idempotent_and_releases_worker_ownership(self):
        claimed, pause = self._pause()
        repeated = self.checkpoints.record_pause(
            run_id=claimed.id,
            worker_id="worker-1",
            fencing_token=claimed.fencing_token,
            checkpoint_id="checkpoint-request-1",
            interrupt_id="interrupt-request-1",
            interrupt_value={"prompt": "请补充角色名"},
            state={"question": "这个角色是什么属性？"},
            next_nodes=("await_hitl",),
        )

        self.assertEqual(pause.hitl_token, repeated.hitl_token)
        with self.Session() as db:
            run = db.query(Run).filter(Run.id == claimed.id).one()
            message = (
                db.query(Message).filter(Message.id == run.assistant_message_id).one()
            )
            self.assertEqual(RunStatus.WAITING_INPUT, run.status)
            self.assertIsNone(run.owner_worker_id)
            self.assertIsNone(run.lease_expires_at)
            self.assertEqual("waiting_input", message.status)
            self.assertEqual(1, db.query(RunCheckpoint).count())
            self.assertEqual(
                1,
                db.query(RunEvent)
                .filter(RunEvent.event_type == "hitl.required")
                .count(),
            )

    def test_resume_token_is_owned_single_use_and_request_idempotent(self):
        claimed, pause = self._pause()

        with self.assertRaises(AppError) as denied:
            self.coordinator.accept(
                username="bob",
                run_id=claimed.id,
                hitl_token=pause.hitl_token,
                answer="丹瑾",
                idempotency_key="resume-1",
            )
        self.assertEqual(ErrorCode.RUN_NOT_FOUND, denied.exception.code)

        accepted = self.coordinator.accept(
            username="alice",
            run_id=claimed.id,
            hitl_token=pause.hitl_token,
            answer="丹瑾",
            idempotency_key="resume-1",
        )
        replayed = self.coordinator.accept(
            username="alice",
            run_id=claimed.id,
            hitl_token=pause.hitl_token,
            answer="丹瑾",
            idempotency_key="resume-1",
        )

        self.assertTrue(accepted.created)
        self.assertFalse(replayed.created)
        self.assertEqual(RunStatus.PENDING, accepted.run.status)
        self.assertEqual(accepted.run.id, replayed.run.id)
        with self.assertRaises(AppError) as conflict:
            self.coordinator.accept(
                username="alice",
                run_id=claimed.id,
                hitl_token=pause.hitl_token,
                answer="丹恒",
                idempotency_key="resume-2",
            )
        self.assertEqual(ErrorCode.IDEMPOTENCY_CONFLICT, conflict.exception.code)

        with self.Session() as db:
            checkpoint = db.query(RunCheckpoint).one()
            self.assertEqual({"answer": "丹瑾"}, checkpoint.resume_payload_json)
            self.assertIsNotNone(checkpoint.consumed_at)
            self.assertEqual(
                1,
                db.query(RunEvent)
                .filter(RunEvent.event_type == "hitl.resumed")
                .count(),
            )

    def test_resume_preflight_rejects_before_checkpoint_run_or_event_writes(self):
        claimed, pause = self._pause()
        with self.Session.begin() as db:
            user = db.query(User).filter(User.username == "alice").one()
            user.role = "admin"
            run = db.query(Run).filter(Run.id == claimed.id).one()
            run.skill_name = "knowledge-base"
            run.skill_version = "1.0.0"
            run.skill_content_hash = "a" * 64
            run.skill_activation_source = "explicit_slash"

        observed = []

        def deny(state):
            observed.append(state)
            raise AppError(
                ErrorCode.POLICY_DENIED,
                "恢复权限已撤销",
                status_code=403,
            )

        coordinator = RunResumeCoordinator(
            checkpoints=self.checkpoints,
            run_service=self.run_service,
            access_validator=deny,
        )
        with self.Session() as db:
            event_count = db.query(RunEvent).count()
            fence = db.query(Run).filter(Run.id == claimed.id).one().fencing_token

        with self.assertRaises(AppError) as denied:
            coordinator.accept(
                username="alice",
                run_id=claimed.id,
                hitl_token=pause.hitl_token,
                answer="丹瑾",
                idempotency_key="resume-denied",
            )

        self.assertEqual(ErrorCode.POLICY_DENIED, denied.exception.code)
        self.assertEqual(1, len(observed))
        self.assertEqual("admin", observed[0].role)
        self.assertEqual("knowledge-base", observed[0].skill_name)
        self.assertEqual("a" * 64, observed[0].skill_content_hash)
        with self.Session() as db:
            checkpoint = db.query(RunCheckpoint).one()
            run = db.query(Run).filter(Run.id == claimed.id).one()
            self.assertIsNone(checkpoint.consumed_at)
            self.assertIsNone(checkpoint.resume_idempotency_key)
            self.assertIsNone(checkpoint.resume_payload_json)
            self.assertEqual(RunStatus.WAITING_INPUT, run.status)
            self.assertIsNone(run.owner_worker_id)
            self.assertIsNone(run.lease_expires_at)
            self.assertEqual(fence, run.fencing_token)
            self.assertEqual(event_count, db.query(RunEvent).count())

    def test_resume_idempotency_key_is_scoped_to_each_run(self):
        first_run, first_pause = self._pause(
            thread_id="thread-1",
            request_key="request-1",
        )
        second_run, second_pause = self._pause(
            thread_id="thread-2",
            request_key="request-2",
        )

        first = self.coordinator.accept(
            username="alice",
            run_id=first_run.id,
            hitl_token=first_pause.hitl_token,
            answer="丹瑾",
            idempotency_key="same-client-key",
        )
        second = self.coordinator.accept(
            username="alice",
            run_id=second_run.id,
            hitl_token=second_pause.hitl_token,
            answer="丹恒",
            idempotency_key="same-client-key",
        )

        self.assertTrue(first.created)
        self.assertTrue(second.created)

    def test_runner_rebuild_uses_same_saver_to_resume_after_process_change(self):
        pipeline, calls = NativeCheckpointGraphTests._pipeline(clarify_rounds=1)
        saver = InMemorySaver()

        @contextmanager
        def saver_factory():
            yield saver

        reservation = self.run_service.create_run(
            username="alice",
            thread_id="thread-runner",
            message="这个角色是什么属性？",
            idempotency_key="request-runner",
        )
        claimed = self.run_service.claim_run(
            run_id=reservation.run.id,
            worker_id="worker-start",
        )
        context = RunRequestContext.for_sync(
            user_id="alice",
            thread_id="thread-runner",
            tenant_id="default",
        )
        runner1 = CheckpointedRagRunner(
            saver_factory=saver_factory,
            checkpoint_repository=self.checkpoints,
        )
        runner2 = CheckpointedRagRunner(
            saver_factory=saver_factory,
            checkpoint_repository=self.checkpoints,
        )
        runner3 = CheckpointedRagRunner(
            saver_factory=saver_factory,
            checkpoint_repository=self.checkpoints,
        )
        try:
            with patch.dict(sys.modules, {"backend.rag.pipeline": pipeline}):
                paused = runner1.start(
                    run_id=claimed.id,
                    question=NativeCheckpointGraphTests.QUESTION,
                    context=context,
                    worker_id="worker-start",
                    fencing_token=claimed.fencing_token,
                )
                accepted = self.coordinator.accept(
                    username="alice",
                    run_id=claimed.id,
                    hitl_token=paused.pause.hitl_token,
                    answer="丹瑾",
                    idempotency_key="resume-runner",
                )
                resumed = runner2.resume(
                    username="alice",
                    run_id=claimed.id,
                    hitl_token=paused.pause.hitl_token,
                    answer="丹瑾",
                    idempotency_key="resume-runner",
                    context=context,
                    worker_id="worker-resume",
                    preflight=lambda _state: None,
                )
                replayed = runner3.resume(
                    username="alice",
                    run_id=claimed.id,
                    hitl_token=paused.pause.hitl_token,
                    answer="丹瑾",
                    idempotency_key="resume-runner",
                    context=context,
                    worker_id="worker-resume",
                    preflight=lambda _state: None,
                )
        finally:
            context.close()

        self.assertIsNotNone(paused.pause)
        self.assertEqual(RunStatus.PENDING, accepted.run.status)
        self.assertIsNone(resumed.pause)
        self.assertEqual("answerable", resumed.result["retrieval_status"])
        self.assertEqual("answerable", replayed.result["retrieval_status"])
        self.assertEqual(1, calls["complexity"])
        self.assertEqual(2, len(calls["retrieve"]))


class ResumeRouteContractTests(unittest.TestCase):
    def test_resume_route_uses_versioned_run_contract(self):
        from backend.api.routes.runs import router
        from backend.schemas.runs import RunResumeResponse

        route = next(
            item for item in router.routes if item.path == "/v1/runs/{run_id}/resume"
        )

        self.assertEqual({"POST"}, route.methods)
        self.assertEqual(202, route.status_code)
        self.assertIs(RunResumeResponse, route.response_model)


if __name__ == "__main__":
    unittest.main()
