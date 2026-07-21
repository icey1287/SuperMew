from __future__ import annotations

import hashlib
import importlib
import json
import math
import re
import time
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol

from backend.evaluation.rag import (
    RagEvalCase,
    RagEvalDataset,
    RagEvalObservation,
    RagEvalObservationBundle,
    RagHitlKind,
    RagOutcome,
    RagProviderErrorStage,
    RagRetrievedChunk,
    RagRoute,
    dataset_fingerprint,
    load_rag_eval_observations,
)


class RagEvalExecutionError(RuntimeError):
    """Raised when a live evaluation cannot produce a valid Observation."""


class RagEvalExecutor(Protocol):
    """Execution Interface shared by offline and live evaluation Adapters."""

    def execute(self, dataset: RagEvalDataset) -> RagEvalObservationBundle: ...


class PredictionFileAdapter:
    """Offline Adapter that replays a sanitized Observation bundle."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def execute(self, dataset: RagEvalDataset) -> RagEvalObservationBundle:
        bundle = load_rag_eval_observations(self.path)
        expected = dataset_fingerprint(dataset)
        if bundle.dataset_fingerprint != expected:
            raise RagEvalExecutionError(
                "prediction file belongs to a different evaluation dataset"
            )
        return bundle


class LiveRagEvalAdapter:
    """Serial Adapter that observes the current production RAG graph.

    Production imports are deliberately lazy so importing the offline scoring
    Module cannot start models, Provider loops, Milvus clients, or network I/O.
    One Adapter instance owns one process-level ProviderRuntime profile.
    """

    def __init__(
        self,
        *,
        timeout_seconds: float = 60.0,
        user_id: str = "rag_eval",
        expected_index_id: str | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if timeout_seconds <= 0 or not math.isfinite(timeout_seconds):
            raise ValueError("timeout_seconds must be positive and finite")
        self.timeout_seconds = float(timeout_seconds)
        self.user_id = str(user_id).strip() or "rag_eval"
        self.expected_index_id = (
            str(expected_index_id).strip() if expected_index_id is not None else None
        )
        if self.expected_index_id == "":
            raise ValueError("expected_index_id must not be empty")
        self._clock = clock

    def execute(self, dataset: RagEvalDataset) -> RagEvalObservationBundle:
        from backend.env import load_env

        load_env()
        from backend.agent.models import model_registry
        from backend.runs.request_context import RunRequestContext
        from backend.providers.core import ProviderError
        from backend.providers.runtime import provider_runtime
        from backend.rag.pipeline import resume_rag_from_hitl, run_rag_graph

        if provider_runtime.readiness().running:
            raise RagEvalExecutionError(
                "live RAG evaluation requires a dedicated process"
            )

        observations: list[RagEvalObservation] = []
        observed_index_id = self.expected_index_id
        try:
            provider_runtime.start_sync()
        except BaseExceptionGroup as exc:
            raise RagEvalExecutionError(
                "live RAG evaluation could not start Provider Runtime"
            ) from exc
        except Exception as exc:
            raise RagEvalExecutionError(
                "live RAG evaluation could not start Provider Runtime"
            ) from exc
        primary_error: BaseException | None = None
        try:
            for case in dataset.cases:
                started_at = self._clock()
                ctx = RunRequestContext.for_sync(
                    user_id=self.user_id,
                    thread_id=f"rag_eval_{case.id}",
                    model_snapshot=model_registry.environment_snapshot(),
                )
                ctx.configure_provider_runtime(
                    deadline_at=started_at + self.timeout_seconds,
                )
                try:
                    initial = run_rag_graph(case.question, ctx)
                    final = initial
                    for answer in case.hitl_answers:
                        resume_state = final.get("hitl_resume_state")
                        if not isinstance(resume_state, dict):
                            break
                        final = resume_rag_from_hitl(
                            resume_state,
                            answer,
                            ctx,
                        )
                    case_index_ids = _retrieval_index_ids(initial, final)
                    if len(case_index_ids) > 1:
                        raise RagEvalExecutionError(
                            f"live RAG evaluation mixed document indexes in case {case.id}"
                        )
                    if case_index_ids:
                        case_index_id = next(iter(case_index_ids))
                        if observed_index_id is None:
                            observed_index_id = case_index_id
                        elif case_index_id != observed_index_id:
                            raise RagEvalExecutionError(
                                "live RAG evaluation document index changed during execution"
                            )
                    observations.append(
                        observation_from_rag_results(
                            case,
                            initial=initial,
                            final=final,
                            duration_ms=(self._clock() - started_at) * 1000,
                        )
                    )
                except ProviderError as exc:
                    observations.append(
                        RagEvalObservation(
                            case_id=case.id,
                            route=RagRoute.PROVIDER_FAILED,
                            outcome=RagOutcome.INSUFFICIENT_EVIDENCE,
                            provider_error_code=exc.code.value,
                            provider_error_stage=RagProviderErrorStage.RETRIEVAL,
                            duration_ms=max(
                                (self._clock() - started_at) * 1000,
                                0.0,
                            ),
                        )
                    )
                except RagEvalExecutionError:
                    raise
                except Exception as exc:
                    raise RagEvalExecutionError(
                        f"live RAG evaluation failed for case {case.id}"
                    ) from exc
                finally:
                    ctx.close()
        except BaseException as exc:
            primary_error = exc
            raise
        finally:
            try:
                provider_runtime.close_sync()
            except BaseExceptionGroup as exc:
                if primary_error is not None:
                    primary_error.add_note(
                        "Provider Runtime also failed while closing live evaluation"
                    )
                else:
                    raise RagEvalExecutionError(
                        "live RAG evaluation could not close Provider Runtime"
                    ) from exc
            except Exception as exc:
                if primary_error is not None:
                    primary_error.add_note(
                        "Provider Runtime also failed while closing live evaluation"
                    )
                else:
                    raise RagEvalExecutionError(
                        "live RAG evaluation could not close Provider Runtime"
                    ) from exc

        return RagEvalObservationBundle(
            dataset_fingerprint=dataset_fingerprint(dataset),
            observations=tuple(observations),
        )


def observation_from_rag_results(
    case: RagEvalCase,
    *,
    initial: Mapping[str, Any],
    final: Mapping[str, Any],
    duration_ms: float,
) -> RagEvalObservation:
    """Project raw graph results onto the stable, sanitized Observation Interface."""

    initial_trace = _trace(initial)
    final_trace = _trace(final)
    initial_route = _route(initial, initial_trace)
    initial_hitl = _hitl_kind(initial_route)
    provider_error_code = _first_text(
        final_trace.get("provider_error_code"),
        initial_trace.get("provider_error_code"),
        *(trace.get("provider_error_code") for trace in _sub_traces(final_trace)),
    )
    final_chunks = _chunks_from_trace(final, final_trace, "retrieved_chunks")
    final_outcome = _outcome(final, final_trace)
    resolved = None
    if initial_hitl is not RagHitlKind.NONE:
        final_route = _route(final, final_trace)
        expected_final_outcome = (
            case.expected.hitl_final_outcome or RagOutcome.ANSWERABLE
        )
        resolved = (
            final_route not in {RagRoute.CLARIFY, RagRoute.SCOPE_SELECT}
            and not isinstance(final.get("hitl_resume_state"), dict)
            and provider_error_code is None
            and final_outcome is expected_final_outcome
            and (final_outcome is not RagOutcome.ANSWERABLE or bool(final_chunks))
        )
    return RagEvalObservation(
        case_id=case.id,
        complexity=_complexity(initial, initial_trace),
        route=initial_route,
        outcome=_outcome(initial, initial_trace),
        hitl=initial_hitl,
        hitl_resolution_success=resolved,
        hitl_final_outcome=(
            final_outcome if initial_hitl is not RagHitlKind.NONE else None
        ),
        rewrite_performed=_rewrite_performed(initial_trace, final_trace),
        provider_error_code=provider_error_code,
        provider_error_stage=(
            RagProviderErrorStage.RETRIEVAL if provider_error_code is not None else None
        ),
        duration_ms=max(float(duration_ms), 0.0),
        retrieved_chunks=final_chunks,
        initial_retrieved_chunks=_chunks_from_trace(
            initial,
            initial_trace,
            "initial_retrieved_chunks",
        ),
        rewrite_retrieved_chunks=_chunks_from_trace(
            final,
            final_trace,
            "rewrite_retrieved_chunks",
        ),
    )


def rag_source_fingerprint(root: str | Path) -> str:
    """Hash the implementation files that can materially change RAG observations."""

    root_path = Path(root)
    relative_paths = (
        "backend/core/settings.py",
        "backend/runs/request_context.py",
        "backend/documents/catalog.py",
        "backend/documents/publication.py",
        "backend/documents/retrieval.py",
        "backend/indexing/document_loader.py",
        "backend/indexing/embedding.py",
        "backend/indexing/html_processor.py",
        "backend/indexing/milvus_client.py",
        "backend/indexing/milvus_writer.py",
        "backend/indexing/parent_chunk_store.py",
        "backend/providers/core.py",
        "backend/providers/embedding.py",
        "backend/providers/rerank.py",
        "backend/providers/runtime.py",
        "backend/rag/evidence.py",
        "backend/rag/outcomes.py",
        "backend/rag/pipeline.py",
        "backend/rag/reranking.py",
        "backend/rag/runtime_context.py",
        "backend/rag/utils.py",
        "backend/schemas/rag.py",
        "backend/security/milvus_filters.py",
        "pyproject.toml",
        "uv.lock",
    )
    digest = hashlib.sha256()
    for relative in relative_paths:
        path = root_path / relative
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def artifact_tree_fingerprint(path: str | Path) -> str:
    """Hash a controlled corpus using relative paths and exact file bytes."""

    root = Path(path)
    if root.is_file():
        files = (root,)
        relative_root = root.parent
    elif root.is_dir():
        files = tuple(sorted(item for item in root.rglob("*") if item.is_file()))
        relative_root = root
    else:
        raise FileNotFoundError(f"evaluation artifact path does not exist: {root}")
    if not files:
        raise ValueError(f"evaluation artifact path is empty: {root}")

    digest = hashlib.sha256()
    for file_path in files:
        relative = file_path.relative_to(relative_root).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(file_path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def profile_fingerprint(profile: Mapping[str, Any]) -> str:
    payload = json.dumps(
        dict(profile),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def live_rag_profile_snapshot(*, profile_id: str, index_id: str) -> dict[str, Any]:
    """Capture non-secret effective settings that can change live RAG quality."""

    from backend.core.settings import get_settings
    from backend.indexing.milvus_client import MilvusSettings

    settings = get_settings()
    milvus = MilvusSettings.from_env()
    utils = importlib.import_module("backend.rag.utils")
    from backend.agent.models import model_registry

    try:
        snapshot = utils.resolve_retrieval_snapshot()
    except Exception as exc:
        raise RagEvalExecutionError(
            "live RAG profile could not resolve the effective document index"
        ) from exc
    effective_index_id = snapshot.index_id
    if index_id != effective_index_id:
        raise RagEvalExecutionError(
            "explicit index-id does not match the effective RAG index identity"
        )
    return {
        "profile_id": profile_id,
        "index_id": index_id,
        "models": {
            role.value: {
                "model_name": spec.model_name,
                "profile_version": spec.profile_version,
                "provider_endpoint_sha256": _identity_hash(spec.base_url),
                "timeout_seconds": spec.timeout_seconds,
            }
            for role, spec in model_registry.environment_snapshot().assignments.items()
        },
        "rag": settings.rag.model_dump(mode="json"),
        "embedding": {
            "model": settings.embedding.model,
            "revision": settings.embedding.revision,
            "device": settings.embedding.device,
            "dimension": settings.embedding.dimension,
            "cache_namespace": settings.embedding.cache_namespace,
        },
        "retrieval": {
            "collection_name": milvus.collection_name,
            "collection_names": sorted(
                {target.collection_name for target in snapshot.targets}
            ),
            "target_count": len(snapshot.targets),
            "milvus_uri_sha256": _identity_hash(milvus.uri),
            "embedding_scope_index_id": effective_index_id,
            "top_k": utils.RETRIEVAL_TOP_K,
            "candidate_multiplier": utils.RETRIEVAL_CANDIDATE_MULTIPLIER,
            "candidate_k_raw": utils._RETRIEVAL_CANDIDATE_K_RAW,  # noqa: SLF001
            "leaf_level": utils.LEAF_RETRIEVE_LEVEL,
            "auto_merge_enabled": utils.AUTO_MERGE_ENABLED,
            "auto_merge_threshold": utils.AUTO_MERGE_THRESHOLD,
        },
        "rerank": {
            "enabled": settings.rerank.enabled,
            "model": settings.rerank.model,
            "provider_endpoint_sha256": _identity_hash(settings.rerank.binding_host),
            "timeout_seconds": settings.rerank.timeout_seconds,
            "min_score": settings.rerank.min_score,
            "candidate_limit": settings.rerank.candidate_limit,
            "max_document_characters": settings.rerank.max_document_characters,
            "max_total_characters": settings.rerank.max_total_characters,
        },
    }


def _identity_hash(value: str) -> str | None:
    normalized = str(value or "").strip()
    if not normalized:
        return None
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _trace(result: Mapping[str, Any]) -> dict[str, Any]:
    value = result.get("rag_trace")
    return dict(value) if isinstance(value, Mapping) else {}


def _sub_traces(trace: Mapping[str, Any]) -> tuple[dict[str, Any], ...]:
    value = trace.get("sub_traces")
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return ()
    return tuple(dict(item) for item in value if isinstance(item, Mapping))


def _retrieval_index_ids(*results: Mapping[str, Any]) -> set[str]:
    identities: set[str] = set()

    def _collect(trace: Mapping[str, Any]) -> None:
        index_id = _optional_text(trace.get("retrieval_index_id"))
        if index_id is not None:
            identities.add(index_id)
        for sub_trace in _sub_traces(trace):
            _collect(sub_trace)

    for result in results:
        result_index_id = _optional_text(result.get("retrieval_index_id"))
        if result_index_id is not None:
            identities.add(result_index_id)
        _collect(_trace(result))
    return identities


def _route(
    result: Mapping[str, Any],
    trace: Mapping[str, Any],
) -> RagRoute | None:
    retrieval_status = _first_text(
        result.get("retrieval_status"),
        trace.get("retrieval_status"),
    )
    hitl_status_routes = {
        "needs_clarification": RagRoute.CLARIFY,
        "needs_scope_selection": RagRoute.SCOPE_SELECT,
    }
    if retrieval_status in hitl_status_routes:
        return hitl_status_routes[retrieval_status]

    if retrieval_status is None:
        ambiguity = _first_text(
            result.get("evidence_ambiguity"),
            trace.get("evidence_ambiguity"),
        )
        if ambiguity == "missing_slot":
            return RagRoute.CLARIFY
        if ambiguity == "multiple_candidates":
            return RagRoute.SCOPE_SELECT

    value = _first_text(result.get("route"), trace.get("route"))
    if value is None:
        options = result.get("hitl_options")
        if not isinstance(options, Sequence) or isinstance(options, (str, bytes)):
            options = trace.get("hitl_options")
        selectable_options = {
            str(option).strip() for option in options or () if str(option).strip()
        }
        if len(selectable_options) >= 2:
            return RagRoute.SCOPE_SELECT
    if value is None:
        return None
    try:
        return RagRoute(value)
    except ValueError:
        return None


def _complexity(
    result: Mapping[str, Any],
    trace: Mapping[str, Any],
):
    from backend.evaluation.rag import RagComplexity

    value = _first_text(result.get("complexity"), trace.get("complexity"))
    if value is None:
        return None
    try:
        return RagComplexity(value)
    except ValueError:
        return None


def _outcome(
    result: Mapping[str, Any],
    trace: Mapping[str, Any],
) -> RagOutcome | None:
    value = _first_text(
        result.get("retrieval_outcome"),
        trace.get("retrieval_outcome"),
    )
    if value is None:
        return None
    try:
        return RagOutcome(value)
    except ValueError:
        return None


def _hitl_kind(route: RagRoute | None) -> RagHitlKind:
    if route is RagRoute.CLARIFY:
        return RagHitlKind.CLARIFY
    if route is RagRoute.SCOPE_SELECT:
        return RagHitlKind.SCOPE_SELECT
    return RagHitlKind.NONE


def _rewrite_performed(*traces: Mapping[str, Any]) -> bool:
    for trace in traces:
        candidates = (trace, *_sub_traces(trace))
        if any(
            candidate.get("rewrite_method")
            or candidate.get("rewritten_query")
            or candidate.get("rewrite_retrieved_chunks")
            for candidate in candidates
        ):
            return True
    return False


def _chunks_from_trace(
    result: Mapping[str, Any],
    trace: Mapping[str, Any],
    key: str,
) -> tuple[RagRetrievedChunk, ...]:
    raw = trace.get(key)
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        raw = result.get("docs") if key == "retrieved_chunks" else None
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        merged: list[Any] = []
        for sub_trace in _sub_traces(trace):
            value = sub_trace.get(key)
            if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
                merged.extend(value)
        raw = merged

    chunks: list[RagRetrievedChunk] = []
    seen: set[tuple[str | None, str | None]] = set()
    for item in raw or ():
        if not isinstance(item, Mapping):
            continue
        chunk_id = _optional_text(item.get("chunk_id"))
        text = _optional_text(item.get("text"))
        document_version_id = _optional_text(item.get("document_version_id"))
        content_sha256 = _manifest_content_hash(item.get("content_hash"))
        if document_version_id is not None and content_sha256 is None:
            raise RagEvalExecutionError(
                "versioned retrieved chunk is missing manifest content_hash"
            )
        if document_version_id is None and content_sha256 is None and text is not None:
            content_sha256 = _content_hash(text)
        identity = chunk_id, content_sha256
        if identity == (None, None) or identity in seen:
            continue
        seen.add(identity)
        chunks.append(
            RagRetrievedChunk(
                rank=len(chunks) + 1,
                chunk_id=chunk_id,
                content_sha256=content_sha256,
                document_id=_first_text(
                    item.get("document_id"),
                    item.get("doc_id"),
                ),
                canonical_name=_first_text(
                    item.get("canonical_name"),
                    item.get("filename"),
                ),
                merged_from_children=bool(item.get("merged_from_children")),
            )
        )
    return tuple(chunks)


def _content_hash(value: str) -> str:
    normalized = unicodedata.normalize("NFC", " ".join(value.split()))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _manifest_content_hash(value: Any) -> str | None:
    normalized = _optional_text(value)
    if normalized is None or re.fullmatch(r"[0-9a-fA-F]{64}", normalized) is None:
        return None
    return normalized.lower()


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _first_text(*values: Any) -> str | None:
    for value in values:
        text = _optional_text(value)
        if text is not None:
            return text
    return None


__all__ = [
    "artifact_tree_fingerprint",
    "LiveRagEvalAdapter",
    "PredictionFileAdapter",
    "RagEvalExecutionError",
    "RagEvalExecutor",
    "observation_from_rag_results",
    "live_rag_profile_snapshot",
    "profile_fingerprint",
    "rag_source_fingerprint",
]
