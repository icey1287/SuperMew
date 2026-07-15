from __future__ import annotations

import asyncio
import math
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from backend.providers.core import (
    ProviderCallContext,
    ProviderCode,
    ProviderError,
    ProviderOperation,
)
from backend.providers.loop_bridge import ProviderLoopBridge
from backend.providers.rerank import RerankResult, RerankerProvider


CancellationProbe = Callable[[], bool]
RerankStageOutput = tuple[list[dict[str, Any]], dict[str, Any]]


class RerankStage:
    """RAG Module that owns payload bounds, fallback, threshold, and trace metadata."""

    def __init__(
        self,
        provider: RerankerProvider,
        *,
        loop_bridge: ProviderLoopBridge | None = None,
        candidate_limit: int = 30,
        max_document_characters: int = 8000,
        max_total_characters: int = 60000,
        min_score: float = 0.0,
    ) -> None:
        self._validate_positive_int("candidate_limit", candidate_limit)
        self._validate_positive_int("max_document_characters", max_document_characters)
        self._validate_positive_int("max_total_characters", max_total_characters)
        if not math.isfinite(float(min_score)):
            raise ValueError("min_score must be finite")
        self._provider = provider
        self._loop_bridge = loop_bridge
        self._candidate_limit = candidate_limit
        self._max_document_characters = max_document_characters
        self._max_total_characters = max_total_characters
        self._min_score = float(min_score)

    def run(
        self,
        query: str,
        documents: Sequence[Mapping[str, Any]],
        top_k: int,
        *,
        deadline: float | None = None,
        cancellation: CancellationProbe | None = None,
    ) -> RerankStageOutput:
        """Compatibility Seam for synchronous graph nodes."""

        if self._loop_bridge is None:
            raise RuntimeError("sync rerank requires an injected ProviderLoopBridge")
        return self._loop_bridge.call_sync(
            lambda: self.run_async(
                query,
                documents,
                top_k,
                deadline=deadline,
                cancellation=cancellation,
            ),
            cancellation=cancellation,
        )

    async def run_async(
        self,
        query: str,
        documents: Sequence[Mapping[str, Any]],
        top_k: int,
        *,
        deadline: float | None = None,
        cancellation: CancellationProbe | None = None,
    ) -> RerankStageOutput:
        if cancellation is not None and cancellation():
            raise asyncio.CancelledError("rerank stage cancelled")
        if not isinstance(query, str):
            raise TypeError("query must be a string")
        self._validate_positive_int("top_k", top_k)
        ranked_documents = self._with_recall_rank(documents)
        meta = self._base_meta(candidate_count=len(ranked_documents))

        if not ranked_documents:
            meta.update(
                {
                    "rerank_skip_reason": "no_candidates",
                    "post_rerank_count": 0,
                    "post_threshold_count": 0,
                }
            )
            return [], meta

        if not self._provider.enabled:
            fallback = ranked_documents[:top_k]
            meta.update(
                {
                    "rerank_skip_reason": "disabled",
                    "post_rerank_count": len(fallback),
                    "post_threshold_count": len(fallback),
                }
            )
            return fallback, meta

        candidates, payload_documents, bounds_meta = self._bounded_candidates(
            ranked_documents
        )
        meta.update(bounds_meta)
        provider_top_n = min(top_k, len(candidates))
        try:
            result = await self._provider.rerank(
                query=query,
                documents=payload_documents,
                top_n=provider_top_n,
                deadline=deadline,
                cancellation=cancellation,
            )
            reranked = self._materialize_result(
                result, candidates, deadline, cancellation
            )
        except ProviderError as exc:
            fallback = ranked_documents[:top_k]
            meta.update(
                {
                    "rerank_error_code": self._provider_code(exc),
                    "rerank_retryable": exc.retryable,
                    "rerank_attempts": exc.attempts,
                    "rerank_fallback_applied": True,
                    "post_rerank_count": len(fallback),
                    "post_threshold_count": len(fallback),
                }
            )
            return fallback, meta

        post_rerank_count = len(reranked)
        thresholded = [
            document
            for document in reranked
            if float(document["rerank_score"]) >= self._min_score
        ]
        meta.update(
            {
                "rerank_applied": True,
                "rerank_attempts": result.attempts,
                "rerank_threshold_applied": True,
                "post_rerank_count": post_rerank_count,
                "post_threshold_count": len(thresholded),
            }
        )
        return thresholded, meta

    def _bounded_candidates(
        self, documents: list[dict[str, Any]]
    ) -> tuple[list[dict[str, Any]], list[str], dict[str, Any]]:
        candidates: list[dict[str, Any]] = []
        payload_documents: list[str] = []
        total_characters = 0
        truncated_documents = 0

        for document in documents[: self._candidate_limit]:
            if total_characters >= self._max_total_characters:
                break
            raw_text = document.get("text", "")
            text = "" if raw_text is None else str(raw_text)
            bounded = text[: self._max_document_characters]
            remaining = self._max_total_characters - total_characters
            bounded = bounded[:remaining]
            if bounded != text:
                truncated_documents += 1
            candidates.append(document)
            payload_documents.append(bounded)
            total_characters += len(bounded)

        return (
            candidates,
            payload_documents,
            {
                "rerank_candidate_count": len(candidates),
                "rerank_candidate_limit": self._candidate_limit,
                "rerank_candidate_limit_applied": len(candidates) < len(documents),
                "rerank_payload_characters": total_characters,
                "rerank_document_character_limit": self._max_document_characters,
                "rerank_total_character_limit": self._max_total_characters,
                "rerank_truncated_document_count": truncated_documents,
            },
        )

    def _materialize_result(
        self,
        result: RerankResult,
        candidates: list[dict[str, Any]],
        deadline: float | None,
        cancellation: CancellationProbe | None,
    ) -> list[dict[str, Any]]:
        if not result.items:
            raise self._invalid_result(deadline, cancellation, result.attempts)
        reranked: list[dict[str, Any]] = []
        seen_indices: set[int] = set()
        for item in result.items:
            if (
                isinstance(item.index, bool)
                or not isinstance(item.index, int)
                or not 0 <= item.index < len(candidates)
                or item.index in seen_indices
                or not math.isfinite(float(item.score))
            ):
                raise self._invalid_result(deadline, cancellation, result.attempts)
            seen_indices.add(item.index)
            document = dict(candidates[item.index])
            document["rerank_score"] = float(item.score)
            reranked.append(document)
        return reranked

    def _invalid_result(
        self,
        deadline: float | None,
        cancellation: CancellationProbe | None,
        attempts: int,
    ) -> ProviderError:
        return ProviderError(
            ProviderCode.RERANK_INVALID_RESPONSE,
            context=ProviderCallContext(
                provider=self._provider.model or "reranker",
                operation=ProviderOperation.RERANK,
                deadline=deadline,
                cancellation=cancellation,
            ),
            attempts=max(attempts, 1),
            max_attempts=max(attempts, 1),
        )

    def _base_meta(self, *, candidate_count: int) -> dict[str, Any]:
        return {
            "rerank_enabled": self._provider.enabled,
            "rerank_applied": False,
            "rerank_model": self._provider.model or None,
            "rerank_error_code": None,
            "rerank_retryable": None,
            "rerank_attempts": 0,
            "rerank_fallback_applied": False,
            "rerank_timeout_seconds": self._provider.timeout_seconds,
            "rerank_min_score": self._min_score,
            "rerank_threshold_applied": False,
            "rerank_skip_reason": None,
            "candidate_count": candidate_count,
        }

    @staticmethod
    def _with_recall_rank(
        documents: Sequence[Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        if isinstance(documents, (str, bytes)) or not isinstance(documents, Sequence):
            raise TypeError("documents must be a sequence of mappings")
        ranked: list[dict[str, Any]] = []
        for index, document in enumerate(documents, 1):
            if not isinstance(document, Mapping):
                raise TypeError("documents must contain only mappings")
            ranked.append({**document, "rrf_rank": index})
        return ranked

    @staticmethod
    def _provider_code(error: ProviderError) -> str:
        return getattr(error.code, "value", str(error.code))

    @staticmethod
    def _validate_positive_int(name: str, value: int) -> None:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f"{name} must be a positive int")


__all__ = ["RerankStage", "RerankStageOutput"]
