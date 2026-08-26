from __future__ import annotations

import json
import importlib
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.evaluation.rag import (
    RagEvalCase,
    RagEvalDataset,
    RagEvalObservationBundle,
    RagExpectedBehavior,
    RagProviderErrorStage,
)
from backend.evaluation.rag_adapters import (
    LiveRagEvalAdapter,
    PredictionFileAdapter,
    RagEvalExecutionError,
    artifact_tree_fingerprint,
    live_rag_profile_snapshot,
    observation_from_rag_results,
    profile_fingerprint,
    rag_source_fingerprint,
)
from backend.indexing.html_processor import parse_html_file_to_sections


ROOT = Path(__file__).resolve().parents[1]


def _case(*, hitl: str = "none", answers: tuple[str, ...] = ()) -> RagEvalCase:
    route = None if hitl == "none" else hitl
    return RagEvalCase(
        id="case_1",
        question="question",
        expected=RagExpectedBehavior(
            route=route,
            hitl=hitl,
            acceptable_abstention=hitl != "none",
            hitl_resolution_success=True if hitl != "none" else None,
            hitl_final_outcome="ANSWERABLE" if hitl != "none" else None,
        ),
        hitl_answers=answers,
    )


def test_projection_keeps_identity_and_hash_but_never_chunk_text():
    case = _case()
    manifest_hash = "A1" * 32
    initial = {
        "route": "answer",
        "retrieval_outcome": "ANSWERABLE",
        "rag_trace": {
            "complexity": "simple",
            "retrieved_chunks": [
                {
                    "chunk_id": "doc::p0::l3::0",
                    "content_hash": manifest_hash,
                    "filename": "doc.html",
                    "text": "private evidence body",
                    "endpoint": "https://secret.example.test",
                }
            ],
            "initial_retrieved_chunks": [
                {
                    "chunk_id": "doc::p0::l3::0",
                    "filename": "doc.html",
                    "text": "private evidence body",
                }
            ],
        },
    }

    observation = observation_from_rag_results(
        case,
        initial=initial,
        final=initial,
        duration_ms=12.5,
    )

    assert observation.route.value == "answer"
    assert observation.complexity.value == "simple"
    assert observation.retrieved_chunks[0].chunk_id == "doc::p0::l3::0"
    assert observation.retrieved_chunks[0].canonical_name == "doc.html"
    assert observation.retrieved_chunks[0].content_sha256 == manifest_hash.lower()
    serialized = json.dumps(observation.model_dump(mode="json"))
    assert "private evidence body" not in serialized
    assert "secret.example.test" not in serialized


def test_projection_hashes_text_when_manifest_content_hash_is_invalid():
    case = _case()
    result = {
        "route": "answer",
        "retrieval_outcome": "ANSWERABLE",
        "rag_trace": {
            "retrieved_chunks": [
                {
                    "chunk_id": "doc::p0::l3::0",
                    "content_hash": "not-a-sha256",
                    "filename": "doc.html",
                    "text": "  private   evidence  ",
                }
            ]
        },
    }

    observation = observation_from_rag_results(
        case,
        initial=result,
        final=result,
        duration_ms=1,
    )

    assert observation.retrieved_chunks[0].content_sha256 == (
        "f7711d1542c029371e0ad7159632c392465700aed13126774e9e8a9575b20078"
    )


def test_projection_rejects_versioned_chunk_without_manifest_hash():
    case = _case()
    result = {
        "route": "answer",
        "retrieval_outcome": "ANSWERABLE",
        "rag_trace": {
            "retrieved_chunks": [
                {
                    "chunk_id": "version-v2::chunk",
                    "document_version_id": "version-v2",
                    "content_hash": "",
                    "filename": "doc.html",
                    "text": "evidence",
                }
            ]
        },
    }

    with pytest.raises(RagEvalExecutionError, match="manifest content_hash"):
        observation_from_rag_results(
            case,
            initial=result,
            final=result,
            duration_ms=1,
        )


def test_projection_scores_initial_hitl_and_final_resolution_separately():
    case = _case(hitl="clarify", answers=("Orion",))
    initial = {
        "route": "clarify",
        "retrieval_outcome": "INSUFFICIENT_EVIDENCE",
        "hitl_resume_state": {"checkpoint_thread_id": "thread"},
        "rag_trace": {"route": "clarify"},
    }
    final = {
        "route": "answer",
        "retrieval_outcome": "ANSWERABLE",
        "docs": [
            {
                "chunk_id": "orion.html::p0::l3::0",
                "filename": "orion.html",
                "text": "resolved evidence",
            }
        ],
        "rag_trace": {"route": "answer"},
    }

    observation = observation_from_rag_results(
        case,
        initial=initial,
        final=final,
        duration_ms=20,
    )

    assert observation.route.value == "clarify"
    assert observation.hitl.value == "clarify"
    assert observation.hitl_resolution_success is True
    assert observation.outcome.value == "INSUFFICIENT_EVIDENCE"
    assert observation.hitl_final_outcome.value == "ANSWERABLE"
    assert observation.retrieved_chunks[0].canonical_name == "orion.html"


def test_projection_uses_retrieval_status_to_stabilize_hitl_route():
    case = _case(hitl="scope_select", answers=("V2.1",))
    initial = {
        "route": "clarify",
        "retrieval_status": "needs_scope_selection",
        "retrieval_outcome": "INSUFFICIENT_EVIDENCE",
        "hitl_resume_state": {"checkpoint_thread_id": "thread"},
        "rag_trace": {
            "route": "clarify",
            "retrieval_status": "needs_scope_selection",
        },
    }
    final = {
        "route": "answer",
        "retrieval_outcome": "ANSWERABLE",
        "docs": [
            {
                "chunk_id": "orion.html::p0::l3::0",
                "filename": "orion.html",
                "text": "resolved evidence",
            }
        ],
    }

    observation = observation_from_rag_results(
        case,
        initial=initial,
        final=final,
        duration_ms=20,
    )

    assert observation.route.value == "scope_select"
    assert observation.hitl.value == "scope_select"


def test_projection_records_retrieval_stage_for_provider_failure_trace():
    case = _case()
    result = {
        "route": "insufficient_evidence",
        "retrieval_outcome": "INSUFFICIENT_EVIDENCE",
        "rag_trace": {"provider_error_code": "VECTOR_STORE_TIMEOUT"},
    }

    observation = observation_from_rag_results(
        case,
        initial=result,
        final=result,
        duration_ms=20,
    )

    assert observation.provider_error_code == "VECTOR_STORE_TIMEOUT"
    assert observation.provider_error_stage is RagProviderErrorStage.RETRIEVAL


def test_hitl_no_knowledge_does_not_count_as_a_successful_resolution():
    case = _case(hitl="clarify", answers=("Orion",))
    initial = {
        "route": "clarify",
        "retrieval_outcome": "INSUFFICIENT_EVIDENCE",
        "hitl_resume_state": {"checkpoint_thread_id": "thread"},
    }
    final = {"route": "no_knowledge", "retrieval_outcome": "NO_KNOWLEDGE"}

    observation = observation_from_rag_results(
        case,
        initial=initial,
        final=final,
        duration_ms=20,
    )

    assert observation.hitl_resolution_success is False
    assert observation.hitl_final_outcome.value == "NO_KNOWLEDGE"


def test_prediction_adapter_rejects_another_dataset(tmp_path):
    dataset = RagEvalDataset(name="dataset", cases=(_case(),))
    path = tmp_path / "observations.json"
    path.write_text(
        RagEvalObservationBundle(
            dataset_fingerprint="0" * 64,
            observations=(),
        ).model_dump_json(),
        encoding="utf-8",
    )

    with pytest.raises(RagEvalExecutionError, match="different"):
        PredictionFileAdapter(path).execute(dataset)


def test_live_adapter_rejects_non_finite_timeout_without_importing_production_rag():
    with pytest.raises(ValueError, match="finite"):
        LiveRagEvalAdapter(timeout_seconds=float("inf"))

    with pytest.raises(ValueError, match="expected_index_id"):
        LiveRagEvalAdapter(expected_index_id=" ")


def _install_live_modules(monkeypatch, *, runtime, run_graph):
    env_module = types.ModuleType("backend.env")
    env_module.load_env = lambda: None
    monkeypatch.setitem(sys.modules, "backend.env", env_module)

    class FakeContext:
        @classmethod
        def for_sync(cls, **kwargs):
            del kwargs
            return cls()

        def configure_provider_runtime(self, **kwargs):
            del kwargs

        def close(self):
            return None

    context_module = types.ModuleType("backend.runs.request_context")
    context_module.RunRequestContext = FakeContext
    monkeypatch.setitem(sys.modules, "backend.runs.request_context", context_module)

    class FakeProviderError(Exception):
        pass

    core_module = types.ModuleType("backend.providers.core")
    core_module.ProviderError = FakeProviderError
    monkeypatch.setitem(sys.modules, "backend.providers.core", core_module)

    runtime_module = types.ModuleType("backend.providers.runtime")
    runtime_module.provider_runtime = runtime
    monkeypatch.setitem(sys.modules, "backend.providers.runtime", runtime_module)

    pipeline_module = types.ModuleType("backend.rag.pipeline")
    pipeline_module.run_rag_graph = run_graph
    pipeline_module.resume_rag_from_hitl = lambda *args, **kwargs: run_graph(
        *args, **kwargs
    )
    monkeypatch.setitem(sys.modules, "backend.rag.pipeline", pipeline_module)


def test_live_adapter_wraps_start_failure(monkeypatch):
    class Runtime:
        def readiness(self):
            return SimpleNamespace(running=False)

        def start_sync(self):
            raise RuntimeError("secret start detail")

    _install_live_modules(
        monkeypatch,
        runtime=Runtime(),
        run_graph=lambda *args, **kwargs: {},
    )

    with pytest.raises(RagEvalExecutionError, match="could not start"):
        LiveRagEvalAdapter().execute(RagEvalDataset(name="dataset", cases=(_case(),)))


def test_live_adapter_wraps_close_failure_without_masking_primary_error(monkeypatch):
    class Runtime:
        def readiness(self):
            return SimpleNamespace(running=False)

        def start_sync(self):
            return None

        def close_sync(self):
            raise RuntimeError("secret close detail")

    runtime = Runtime()
    _install_live_modules(
        monkeypatch,
        runtime=runtime,
        run_graph=lambda *args, **kwargs: {
            "route": "answer",
            "retrieval_outcome": "ANSWERABLE",
            "docs": [
                {
                    "chunk_id": "doc::p1::l3::0",
                    "filename": "doc.html",
                    "text": "evidence",
                }
            ],
        },
    )
    dataset = RagEvalDataset(name="dataset", cases=(_case(),))

    with pytest.raises(RagEvalExecutionError, match="could not close"):
        LiveRagEvalAdapter().execute(dataset)

    _install_live_modules(
        monkeypatch,
        runtime=runtime,
        run_graph=lambda *args, **kwargs: (_ for _ in ()).throw(
            ValueError("primary failure")
        ),
    )
    with pytest.raises(RagEvalExecutionError, match="case case_1") as raised:
        LiveRagEvalAdapter().execute(dataset)
    assert any("also failed while closing" in note for note in raised.value.__notes__)


def test_live_adapter_rejects_catalog_index_changes_between_cases(monkeypatch):
    class Runtime:
        def readiness(self):
            return SimpleNamespace(running=False)

        def start_sync(self):
            return None

        def close_sync(self):
            return None

    indexes = iter(("catalog-index-v1", "catalog-index-v2"))

    def run_graph(*_args, **_kwargs):
        index_id = next(indexes)
        return {
            "route": "answer",
            "retrieval_outcome": "ANSWERABLE",
            "rag_trace": {
                "retrieval_index_id": index_id,
                "retrieved_chunks": [
                    {
                        "chunk_id": f"{index_id}::chunk",
                        "filename": "doc.html",
                        "text": "evidence",
                    }
                ],
            },
        }

    _install_live_modules(
        monkeypatch,
        runtime=Runtime(),
        run_graph=run_graph,
    )
    first = _case()
    second = first.model_copy(update={"id": "case_2", "question": "question two"})
    dataset = RagEvalDataset(name="dataset", cases=(first, second))

    with pytest.raises(RagEvalExecutionError, match="index changed"):
        LiveRagEvalAdapter(expected_index_id="catalog-index-v1").execute(dataset)


def test_rag_source_fingerprint_is_stable_and_content_addressed():
    first = rag_source_fingerprint(".")
    second = rag_source_fingerprint(".")

    assert first == second
    assert len(first) == 64
    assert set(first) <= set("0123456789abcdef")


def test_live_profile_uses_catalog_snapshot_as_effective_index(monkeypatch):
    settings = SimpleNamespace(
        app=SimpleNamespace(default_tenant_id="tenant-a"),
        models=SimpleNamespace(
            answer_model="answer",
            fast_model="fast",
            grade_model="grade",
            base_url="https://models.example.test",
            timeout_seconds=30.0,
        ),
        rag=SimpleNamespace(model_dump=lambda **_kwargs: {"top_k": 8}),
        embedding=SimpleNamespace(
            model="embedding",
            revision="rev-1",
            device="cpu",
            dimension=1024,
            cache_namespace="docs",
        ),
        rerank=SimpleNamespace(
            enabled=False,
            model="",
            binding_host="",
            timeout_seconds=5.0,
            min_score=0.0,
            candidate_limit=20,
            max_document_characters=4000,
            max_total_characters=20000,
        ),
    )
    monkeypatch.setattr("backend.core.settings.get_settings", lambda: settings)
    snapshot = SimpleNamespace(
        index_id="catalog-index-v2",
        targets=(
            SimpleNamespace(collection_name="catalog_v1"),
            SimpleNamespace(collection_name="archive_catalog_v1"),
        ),
    )
    utils = SimpleNamespace(
        resolve_retrieval_snapshot=lambda **_kwargs: snapshot,
        RETRIEVAL_TOP_K=8,
        RETRIEVAL_CANDIDATE_MULTIPLIER=3,
        _RETRIEVAL_CANDIDATE_K_RAW="",
        LEAF_RETRIEVE_LEVEL=3,
        AUTO_MERGE_ENABLED=True,
        AUTO_MERGE_THRESHOLD=2,
    )
    milvus_module = types.ModuleType("backend.indexing.milvus_client")
    milvus_module.MilvusSettings = SimpleNamespace(
        from_env=lambda: SimpleNamespace(
            collection_name="catalog_v1",
            uri="http://milvus.example.test",
        )
    )
    models_module = types.ModuleType("backend.agent.models")
    models_module.model_registry = SimpleNamespace(
        environment_snapshot=lambda: SimpleNamespace(assignments={})
    )
    monkeypatch.setitem(sys.modules, "backend.indexing.milvus_client", milvus_module)
    monkeypatch.setitem(sys.modules, "backend.agent.models", models_module)

    real_import_module = importlib.import_module

    def import_module(name, package=None):
        if name == "backend.rag.utils":
            return utils
        return real_import_module(name, package)

    monkeypatch.setattr(
        "backend.evaluation.rag_adapters.importlib.import_module",
        import_module,
    )

    profile = live_rag_profile_snapshot(
        profile_id="release",
        index_id="catalog-index-v2",
    )

    assert profile["index_id"] == "catalog-index-v2"
    assert profile["retrieval"]["embedding_scope_index_id"] == "catalog-index-v2"
    assert profile["retrieval"]["collection_names"] == [
        "archive_catalog_v1",
        "catalog_v1",
    ]
    assert profile["retrieval"]["target_count"] == 2

    with pytest.raises(RagEvalExecutionError, match="effective RAG index"):
        live_rag_profile_snapshot(profile_id="release", index_id="stale-index")


def test_corpus_and_profile_fingerprints_change_with_identity(tmp_path):
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    document = corpus / "doc.html"
    document.write_text("first", encoding="utf-8")
    first = artifact_tree_fingerprint(corpus)
    document.write_text("second", encoding="utf-8")
    second = artifact_tree_fingerprint(corpus)

    assert first != second
    assert profile_fingerprint({"index_id": "v1"}) != profile_fingerprint(
        {"index_id": "v2"}
    )


def test_controlled_corpus_has_one_stable_page_and_leaf_chunk_per_document():
    corpus = ROOT / "evals/rag/corpus"

    for path in sorted(corpus.glob("*.html")):
        sections = parse_html_file_to_sections(path)
        assert len(sections) == 1, path.name
        assert sections[0]["page"] == 1, path.name
        assert len(sections[0]["text"]) < 600, path.name
