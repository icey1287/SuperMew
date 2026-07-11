import asyncio
import ast
import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.chat.request_context import ChatRequestContext
from backend.tools.knowledge import make_search_knowledge_base

REPO_ROOT = Path(__file__).resolve().parents[1]


class ChatRequestContextTests(unittest.IsolatedAsyncioTestCase):
    async def test_two_request_contexts_do_not_share_rag_steps(self):
        queue_a = asyncio.Queue()
        queue_b = asyncio.Queue()
        ctx_a = ChatRequestContext.for_stream(
            user_id="a",
            session_id="s1",
            output_queue=queue_a,
        )
        ctx_b = ChatRequestContext.for_stream(
            user_id="b",
            session_id="s2",
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


class KnowledgeToolFactoryTests(unittest.TestCase):
    def test_knowledge_tool_counter_is_per_context(self):
        ctx_a = ChatRequestContext.for_sync(user_id="a", session_id="s1")
        ctx_b = ChatRequestContext.for_sync(user_id="b", session_id="s2")

        try:
            self.assertTrue(ctx_a.acquire_knowledge_tool_slot())
            self.assertFalse(ctx_a.acquire_knowledge_tool_slot())
            self.assertTrue(ctx_b.acquire_knowledge_tool_slot())
            self.assertFalse(ctx_b.acquire_knowledge_tool_slot())
        finally:
            ctx_a.close()
            ctx_b.close()

    def test_tool_closure_records_trace_to_own_context(self):
        fake_rag = types.ModuleType("backend.rag")
        fake_rag.__path__ = []
        fake_pipeline = types.ModuleType("backend.rag.pipeline")

        def run_rag_graph(query, ctx):
            return {
                "docs": [
                    {
                        "filename": f"{query}.txt",
                        "page_number": 1,
                        "text": f"{query} body",
                    }
                ],
                "rag_trace": {"query": query, "session_id": ctx.session_id},
            }

        fake_pipeline.run_rag_graph = run_rag_graph

        ctx_a = ChatRequestContext.for_sync(user_id="a", session_id="s1")
        ctx_b = ChatRequestContext.for_sync(user_id="b", session_id="s2")

        try:
            tool_a = make_search_knowledge_base(ctx_a)
            tool_b = make_search_knowledge_base(ctx_b)

            with patch.dict(
                sys.modules,
                {
                    "backend.rag": fake_rag,
                    "backend.rag.pipeline": fake_pipeline,
                },
            ):
                output_a = tool_a.invoke({"query": "A"})
                output_b = tool_b.invoke({"query": "B"})

            self.assertIn("A.txt", output_a)
            self.assertIn("B.txt", output_b)
            self.assertEqual(ctx_a.take_rag_trace()["rag_trace"]["query"], "A")
            self.assertEqual(ctx_b.take_rag_trace()["rag_trace"]["query"], "B")
        finally:
            ctx_a.close()
            ctx_b.close()


class RouteImportTests(unittest.TestCase):
    def test_sessions_route_uses_storage_instance(self):
        path = REPO_ROOT / "backend" / "api" / "routes" / "sessions.py"
        spec = importlib.util.spec_from_file_location("sessions_route_under_test", path)
        sessions = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(sessions)

        self.assertTrue(callable(sessions.storage.list_session_infos))
        self.assertTrue(callable(sessions.storage.get_session_messages))
        self.assertTrue(callable(sessions.storage.delete_session))


class ImportShapeTests(unittest.TestCase):
    def test_backend_imports_do_not_pull_child_modules_from_packages(self):
        backend_root = REPO_ROOT / "backend"
        files = list(backend_root.rglob("*.py")) + list((REPO_ROOT / "tests").glob("test_*.py"))
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
                        offenders.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno} {node.module}.{alias.name}")

        self.assertEqual([], offenders)


if __name__ == "__main__":
    unittest.main()
