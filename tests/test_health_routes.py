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


def _catalog(
    *,
    complete: bool,
    state_exists: bool = True,
    collection: str | None = None,
    knowledge_base_name: str | None = None,
):
    return SimpleNamespace(
        legacy_adoption_state=lambda **_kwargs: SimpleNamespace(
            complete=complete,
            state_exists=state_exists,
            legacy_collection=(
                collection
                if collection is not None
                else health.milvus_manager.collection_name
            ),
            knowledge_base_name=(
                knowledge_base_name
                if knowledge_base_name is not None
                else health.document_publication.config.knowledge_base_name
            ),
            fingerprint="a" * 64,
        )
    )


class HealthRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_live_is_process_only(self):
        self.assertEqual({"status": "live"}, await health.live())

    async def test_ready_rejects_unloaded_embedding_even_when_warmup_is_optional(self):
        with (
            patch.object(
                health,
                "provider_runtime",
                _runtime(running=True, embedding_ready=False, warmup=False),
            ),
            patch.object(health, "document_catalog", _catalog(complete=True)),
        ):
            response = await health.ready()

        self.assertEqual(503, response.status_code)
        self.assertEqual("not_ready", json.loads(response.body)["status"])

    async def test_ready_fails_when_required_embedding_warmup_is_incomplete(self):
        with (
            patch.object(
                health,
                "provider_runtime",
                _runtime(running=True, embedding_ready=False, warmup=True),
            ),
            patch.object(health, "document_catalog", _catalog(complete=True)),
        ):
            response = await health.ready()

        self.assertEqual(503, response.status_code)
        self.assertEqual("not_ready", json.loads(response.body)["status"])

    async def test_ready_fails_until_legacy_catalog_adoption_is_complete(self):
        with (
            patch.object(
                health,
                "provider_runtime",
                _runtime(running=True, embedding_ready=True, warmup=True),
            ),
            patch.object(health, "document_catalog", _catalog(complete=False)),
        ):
            response = await health.ready()

        payload = json.loads(response.body)
        self.assertEqual(503, response.status_code)
        self.assertFalse(payload["document_catalog"]["legacy_adoption_complete"])

    async def test_ready_is_read_only_and_fails_when_catalog_state_is_missing(self):
        with (
            patch.object(
                health,
                "provider_runtime",
                _runtime(running=True, embedding_ready=True, warmup=True),
            ),
            patch.object(
                health,
                "document_catalog",
                _catalog(complete=False, state_exists=False),
            ),
        ):
            response = await health.ready()

        payload = json.loads(response.body)
        self.assertEqual(503, response.status_code)
        self.assertFalse(payload["document_catalog"]["state_exists"])

    async def test_ready_fails_when_adoption_target_does_not_match_runtime(self):
        with (
            patch.object(
                health,
                "provider_runtime",
                _runtime(running=True, embedding_ready=True, warmup=True),
            ),
            patch.object(
                health,
                "document_catalog",
                _catalog(complete=True, knowledge_base_name="typo-kb"),
            ),
        ):
            response = await health.ready()

        payload = json.loads(response.body)
        self.assertEqual(503, response.status_code)
        self.assertFalse(payload["document_catalog"]["legacy_target_matches"])

    async def test_ready_redacts_catalog_failure_as_unavailable(self):
        catalog = SimpleNamespace(
            legacy_adoption_state=lambda **_kwargs: (_ for _ in ()).throw(
                ConnectionError("postgres password=secret")
            )
        )
        with (
            patch.object(
                health,
                "provider_runtime",
                _runtime(running=True, embedding_ready=True, warmup=True),
            ),
            patch.object(health, "document_catalog", catalog),
        ):
            response = await health.ready()

        payload = json.loads(response.body)
        self.assertEqual(503, response.status_code)
        self.assertFalse(payload["document_catalog"]["available"])
        self.assertNotIn("secret", response.body.decode())


if __name__ == "__main__":
    unittest.main()
