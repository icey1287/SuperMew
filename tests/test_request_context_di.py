import asyncio
import ast
import importlib.util
import unittest
from datetime import datetime, timezone
from pathlib import Path

from backend.runs.request_context import RunRequestContext
from backend.web_research.contracts import WebEvidence, WebResearchResult

REPO_ROOT = Path(__file__).resolve().parents[1]


class RunRequestContextTests(unittest.IsolatedAsyncioTestCase):
    async def test_two_request_contexts_do_not_share_rag_steps(self):
        queue_a = asyncio.Queue()
        queue_b = asyncio.Queue()
        ctx_a = RunRequestContext.for_stream(
            user_id="a",
            thread_id="s1",
            output_queue=queue_a,
        )
        ctx_b = RunRequestContext.for_stream(
            user_id="b",
            thread_id="s2",
            output_queue=queue_b,
        )

        try:
            ctx_a.emit_rag_step(
                "A",
                "from A",
                "detail A",
                group="group A",
                group_label="真实子问题 A",
            )
            ctx_b.emit_rag_step("B", "from B", "detail B", group="group B")
            await asyncio.sleep(0)

            event_a = await queue_a.get()
            event_b = await queue_b.get()

            self.assertEqual(event_a["type"], "rag_step")
            self.assertEqual(event_a["step"]["icon"], "A")
            self.assertEqual(event_a["step"]["group"], "group A")
            self.assertEqual(event_a["step"]["group_label"], "真实子问题 A")
            self.assertGreaterEqual(event_a["step"]["elapsed_ms"], 0)
            self.assertGreaterEqual(event_a["step"]["stage_elapsed_ms"], 0)
            self.assertEqual(event_b["type"], "rag_step")
            self.assertEqual(event_b["step"]["icon"], "B")
            self.assertEqual(event_b["step"]["group"], "group B")
            self.assertTrue(queue_a.empty())
            self.assertTrue(queue_b.empty())
        finally:
            ctx_a.close()
            ctx_b.close()

    async def test_web_fetch_capabilities_are_request_owned_and_cannot_rebind(self):
        ctx_a = RunRequestContext.for_sync(user_id="a", thread_id="s1")
        ctx_b = RunRequestContext.for_sync(user_id="b", thread_id="s2")
        evidence = WebEvidence.create(
            canonical_url="https://research.dev/article",
            title="Research",
            snippet="Evidence",
            content="Evidence body",
            retrieved_at=datetime(2026, 7, 16, tzinfo=timezone.utc),
        )
        evidence_id = evidence.evidence_id
        url = evidence.canonical_url

        ctx_a.record_web_search_result(WebResearchResult.create([evidence]))

        self.assertEqual(url, ctx_a.resolve_web_evidence(evidence_id))
        self.assertIsNone(ctx_b.resolve_web_evidence(evidence_id))
        self.assertNotIn(url, repr(ctx_a))

        ctx_a.close()
        self.assertIsNone(ctx_a.resolve_web_evidence(evidence_id))


class KnowledgeToolFactoryTests(unittest.TestCase):
    def test_knowledge_tool_counter_is_per_context(self):
        ctx_a = RunRequestContext.for_sync(user_id="a", thread_id="s1")
        ctx_b = RunRequestContext.for_sync(user_id="b", thread_id="s2")

        try:
            self.assertTrue(ctx_a.acquire_knowledge_tool_slot())
            self.assertFalse(ctx_a.acquire_knowledge_tool_slot())
            self.assertTrue(ctx_b.acquire_knowledge_tool_slot())
            self.assertFalse(ctx_b.acquire_knowledge_tool_slot())
        finally:
            ctx_a.close()
            ctx_b.close()


class RouteImportTests(unittest.TestCase):
    def test_thread_route_uses_application_module(self):
        path = REPO_ROOT / "backend" / "api" / "routes" / "threads.py"
        spec = importlib.util.spec_from_file_location("threads_route_under_test", path)
        threads = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(threads)

        self.assertTrue(callable(threads.thread_service.create_thread))
        self.assertTrue(callable(threads.thread_service.list_threads))
        self.assertTrue(callable(threads.thread_service.recent_messages))
        self.assertTrue(callable(threads.thread_service.delete_thread))


class ImportShapeTests(unittest.TestCase):
    def test_backend_imports_do_not_pull_child_modules_from_packages(self):
        backend_root = REPO_ROOT / "backend"
        files = list(backend_root.rglob("*.py")) + list(
            (REPO_ROOT / "tests").glob("test_*.py")
        )
        offenders = []

        for path in files:
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.ImportFrom) or not node.module:
                    continue
                if not node.module.startswith("backend.") and node.module != "backend":
                    continue

                package_path = REPO_ROOT / Path(*node.module.split("."))
                for alias in node.names:
                    if alias.name == "*":
                        continue
                    child_file = package_path / f"{alias.name}.py"
                    child_package = package_path / alias.name / "__init__.py"
                    if child_file.exists() or child_package.exists():
                        offenders.append(
                            f"{path.relative_to(REPO_ROOT)}:{node.lineno} {node.module}.{alias.name}"
                        )

        self.assertEqual([], offenders)


if __name__ == "__main__":
    unittest.main()
