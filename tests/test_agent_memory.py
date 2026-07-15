import unittest
from types import SimpleNamespace

from langchain_core.messages import AIMessage

from backend.agent.memory import PersistentMemoryManager
from backend.providers import ProviderExecutor, ProviderPolicy


class _Models:
    def __init__(self, model):
        self.model = model

    def get(self, role):
        return self.model


class _FailingModel:
    model_name = "fast-model"

    def __init__(self):
        self.calls = 0

    def invoke(self, messages):
        self.calls += 1
        raise RuntimeError("secret-token https://provider.test/raw-body")


class AgentMemoryProviderTests(unittest.TestCase):
    def _manager(self, model):
        settings = SimpleNamespace(
            agent=SimpleNamespace(memory_message_threshold=2),
            models=SimpleNamespace(timeout_seconds=1.0),
        )
        return PersistentMemoryManager(
            settings=settings,
            models=_Models(model),
            executor=ProviderExecutor(sleeper=lambda _: None),
            policy=ProviderPolicy(max_attempts=2),
        )

    def test_memory_model_failure_retries_once_and_logs_only_stable_code(self):
        model = _FailingModel()
        manager = self._manager(model)

        with self.assertLogs("backend.agent.memory", level="WARNING") as logs:
            note = manager.update_sync("keep me", "user", "answer")

        self.assertEqual("keep me", note)
        self.assertEqual(2, model.calls)
        self.assertIn("MODEL_UNAVAILABLE", "\n".join(logs.output))
        self.assertNotIn("secret-token", "\n".join(logs.output))
        self.assertNotIn("provider.test", "\n".join(logs.output))
        for record in logs.records:
            self.assertIsNone(record.exc_info)

    def test_memory_model_success_returns_compacted_note(self):
        model = SimpleNamespace(
            model_name="fast-model",
            invoke=lambda messages: AIMessage(content="  compact note  "),
        )

        note = self._manager(model).update_sync("", "user", "answer")

        self.assertEqual("compact note", note)


if __name__ == "__main__":
    unittest.main()
