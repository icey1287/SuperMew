import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import backend.api.routes.health as health


def _runtime(*, running: bool, embedding_ready: bool, warmup: bool):
    snapshot = SimpleNamespace(
        running=running,
        embedding=SimpleNamespace(
            ready=embedding_ready,
            model_loaded=embedding_ready,
            dimension=1024 if embedding_ready else None,
            queue_depth=0,
            inflight=0,
        ),
        rerank_enabled=False,
        rerank_model=None,
    )
    return SimpleNamespace(
        settings=SimpleNamespace(embedding=SimpleNamespace(warmup_on_start=warmup)),
        readiness=lambda: snapshot,
    )


class HealthRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_live_is_process_only(self):
        self.assertEqual({"status": "live"}, await health.live())

    async def test_ready_rejects_unloaded_embedding_even_when_warmup_is_optional(self):
        with patch.object(
            health,
            "provider_runtime",
            _runtime(running=True, embedding_ready=False, warmup=False),
        ):
            response = await health.ready()

        self.assertEqual(503, response.status_code)
        self.assertEqual("not_ready", json.loads(response.body)["status"])

    async def test_ready_fails_when_required_embedding_warmup_is_incomplete(self):
        with patch.object(
            health,
            "provider_runtime",
            _runtime(running=True, embedding_ready=False, warmup=True),
        ):
            response = await health.ready()

        self.assertEqual(503, response.status_code)
        self.assertEqual("not_ready", json.loads(response.body)["status"])


if __name__ == "__main__":
    unittest.main()
