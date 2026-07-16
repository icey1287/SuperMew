import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import backend.api.routes.health as health


def _runtime(
    *,
    running: bool,
    embedding_ready: bool,
    warmup: bool,
    worker_required: bool = True,
    sql_enabled: bool = False,
    web_enabled: bool = False,
):
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
        settings=SimpleNamespace(
            embedding=SimpleNamespace(warmup_on_start=warmup),
            worker=SimpleNamespace(
                indexing_worker_required=worker_required,
                indexing_readiness_ttl_seconds=45,
            ),
            sql_assistant=SimpleNamespace(enabled=sql_enabled),
            web_research=SimpleNamespace(enabled=web_enabled),
        ),
        readiness=lambda: snapshot,
    )


def _catalog(
    *,
    complete: bool,
    state_exists: bool = True,
    collection: str | None = None,
    knowledge_base_name: str | None = None,
    worker_ready: bool = True,
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
        ),
        worker_readiness=lambda **_kwargs: SimpleNamespace(
            worker_kind="indexing",
            ready=worker_ready,
            fresh_workers=1 if worker_ready else 0,
            latest_heartbeat_at=(
                SimpleNamespace(isoformat=lambda: "2026-07-15T12:00:00")
                if worker_ready
                else None
            ),
            queue_counts={
                "index_pending": 0,
                "index_running": 0,
                "cleanup_pending": 0,
                "cleanup_running": 0,
            },
            oldest_ready_at=None,
            incompatible_fresh_workers=0,
            expected_build_fingerprint=(
                health.document_publication.config.build_profile.fingerprint
            ),
        ),
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

    async def test_ready_fails_without_a_fresh_indexing_worker_heartbeat(self):
        with (
            patch.object(
                health,
                "provider_runtime",
                _runtime(running=True, embedding_ready=True, warmup=True),
            ),
            patch.object(
                health,
                "document_catalog",
                _catalog(complete=True, worker_ready=False),
            ),
        ):
            response = await health.ready()

        payload = json.loads(response.body)
        self.assertEqual(503, response.status_code)
        self.assertFalse(payload["indexing_worker"]["ready"])

    async def test_ready_requires_a_worker_with_the_current_build_profile(self):
        expected = health.document_publication.config.build_profile.fingerprint
        captured: list[dict] = []

        def worker_readiness(**kwargs):
            captured.append(kwargs)
            matching = len(captured) > 1
            return SimpleNamespace(
                worker_kind="indexing",
                ready=matching,
                fresh_workers=1 if matching else 0,
                incompatible_fresh_workers=0 if matching else 1,
                expected_build_fingerprint=expected,
                latest_heartbeat_at=None,
                queue_counts={},
                oldest_ready_at=None,
            )

        catalog = _catalog(complete=True)
        catalog.worker_readiness = worker_readiness
        with (
            patch.object(
                health,
                "provider_runtime",
                _runtime(running=True, embedding_ready=True, warmup=True),
            ),
            patch.object(health, "document_catalog", catalog),
        ):
            incompatible = await health.ready()
            matching = await health.ready()

        self.assertEqual(503, incompatible.status_code)
        self.assertEqual(200, matching.status_code)
        self.assertEqual(expected, captured[0]["expected_build_fingerprint"])
        payload = json.loads(incompatible.body)
        self.assertEqual(1, payload["indexing_worker"]["incompatible_fresh_workers"])

    async def test_ready_can_explicitly_disable_the_indexing_worker_gate(self):
        with (
            patch.object(
                health,
                "provider_runtime",
                _runtime(
                    running=True,
                    embedding_ready=True,
                    warmup=True,
                    worker_required=False,
                ),
            ),
            patch.object(
                health,
                "document_catalog",
                _catalog(complete=True, worker_ready=False),
            ),
        ):
            response = await health.ready()

        self.assertEqual(200, response.status_code)

    async def test_ready_gates_enabled_sql_runtime_and_exposes_only_safe_state(self):
        runtime = _runtime(
            running=True,
            embedding_ready=True,
            warmup=True,
            sql_enabled=True,
        )
        sql_snapshots = iter(
            (
                SimpleNamespace(
                    ready=False,
                    catalog_hash=None,
                    database={"role": "must-not-escape"},
                ),
                SimpleNamespace(
                    ready=True,
                    catalog_hash="b" * 64,
                    database={"dsn": "must-not-escape"},
                ),
            )
        )
        sql_runtime = SimpleNamespace(readiness=lambda: next(sql_snapshots))
        with (
            patch.object(health, "provider_runtime", runtime),
            patch.object(health, "document_catalog", _catalog(complete=True)),
            patch.object(
                health,
                "get_sql_assistant_runtime",
                return_value=sql_runtime,
            ),
        ):
            unavailable = await health.ready()
            ready = await health.ready()

        self.assertEqual(503, unavailable.status_code)
        self.assertEqual(200, ready.status_code)
        payload = json.loads(ready.body)
        self.assertEqual(
            {
                "enabled": True,
                "ready": True,
                "catalog_hash": "b" * 64,
            },
            payload["sql_assistant"],
        )

    async def test_ready_gates_enabled_web_runtime_and_exposes_only_safe_state(self):
        runtime = _runtime(
            running=True,
            embedding_ready=True,
            warmup=True,
            web_enabled=True,
        )
        snapshots = iter(
            (
                {
                    "ready": False,
                    "search_ready": False,
                    "api_key": "must-not-escape",
                },
                {
                    "ready": True,
                    "search_ready": True,
                    "query": "must-not-escape",
                },
            )
        )
        web_runtime = SimpleNamespace(readiness=lambda: next(snapshots))
        with (
            patch.object(health, "provider_runtime", runtime),
            patch.object(health, "document_catalog", _catalog(complete=True)),
            patch.object(
                health,
                "get_web_research_runtime",
                return_value=web_runtime,
            ),
        ):
            unavailable = await health.ready()
            ready = await health.ready()

        self.assertEqual(503, unavailable.status_code)
        self.assertEqual(200, ready.status_code)
        payload = json.loads(ready.body)
        self.assertEqual(
            {
                "enabled": True,
                "ready": True,
                "search_ready": True,
            },
            payload["web_research"],
        )

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
