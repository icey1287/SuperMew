from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable, Sequence

import pytest

from backend.providers.core import (
    ProviderCallContext,
    ProviderCode,
    ProviderError,
    ProviderOperation,
)
from backend.providers.loop_bridge import ProviderLoopBridge
from backend.providers.rerank import (
    DisabledRerankerAdapter,
    RerankItem,
    RerankResult,
)
from backend.rag.reranking import RerankStage


class _FakeReranker:
    enabled = True
    model = "fake-reranker"
    timeout_seconds = 2.5

    def __init__(
        self,
        outcome: RerankResult | BaseException | Callable[..., RerankResult],
    ) -> None:
        self.outcome = outcome
        self.calls: list[dict] = []
        self.thread_ids: list[int] = []

    async def rerank(
        self,
        *,
        query: str,
        documents: Sequence[str],
        top_n: int,
        deadline: float | None = None,
        cancellation=None,
    ) -> RerankResult:
        self.thread_ids.append(threading.get_ident())
        self.calls.append(
            {
                "query": query,
                "documents": list(documents),
                "top_n": top_n,
                "deadline": deadline,
                "cancellation": cancellation,
            }
        )
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        if callable(self.outcome):
            return self.outcome(
                query=query,
                documents=documents,
                top_n=top_n,
            )
        return self.outcome

    async def aclose(self) -> None:
        return None


def _provider_error(
    code: ProviderCode,
    *,
    attempts: int = 1,
    max_attempts: int = 1,
) -> ProviderError:
    return ProviderError(
        code,
        context=ProviderCallContext(
            provider="fake-reranker",
            operation=ProviderOperation.RERANK,
        ),
        attempts=attempts,
        max_attempts=max_attempts,
    )


@pytest.mark.asyncio
async def test_stage_bounds_candidates_and_characters_then_applies_threshold():
    provider = _FakeReranker(
        RerankResult(
            items=(
                RerankItem(index=1, score=0.8),
                RerankItem(index=0, score=0.4),
            ),
            attempts=1,
        )
    )
    stage = RerankStage(
        provider,
        candidate_limit=3,
        max_document_characters=4,
        max_total_characters=7,
        min_score=0.5,
    )

    documents, meta = await stage.run_async(
        "question",
        [
            {"chunk_id": "c1", "text": "abcdef", "score": 0.99},
            {"chunk_id": "c2", "text": "12345", "score": 0.01},
            {"chunk_id": "c3", "text": "tail", "score": 0.5},
            {"chunk_id": "c4", "text": "outside", "score": 0.8},
        ],
        2,
    )

    assert provider.calls[0]["documents"] == ["abcd", "123"]
    assert provider.calls[0]["top_n"] == 2
    assert [item["chunk_id"] for item in documents] == ["c2"]
    assert documents[0]["rrf_rank"] == 2
    assert documents[0]["rerank_score"] == 0.8
    assert meta["rerank_applied"] is True
    assert meta["rerank_threshold_applied"] is True
    assert meta["post_rerank_count"] == 2
    assert meta["post_threshold_count"] == 1
    assert meta["rerank_candidate_count"] == 2
    assert meta["rerank_payload_characters"] == 7
    assert meta["rerank_truncated_document_count"] == 2
    assert meta["rerank_candidate_limit_applied"] is True


@pytest.mark.asyncio
async def test_provider_failure_preserves_recall_order_and_skips_threshold():
    provider = _FakeReranker(
        _provider_error(
            ProviderCode.RERANK_TIMEOUT,
            attempts=2,
            max_attempts=2,
        )
    )
    stage = RerankStage(
        provider,
        candidate_limit=1,
        min_score=0.95,
    )
    source = [
        {"chunk_id": "first", "text": "a", "score": 0.1},
        {"chunk_id": "second", "text": "b", "score": 0.99},
        {"chunk_id": "third", "text": "c", "score": 0.8},
    ]

    documents, meta = await stage.run_async("question", source, 2)

    assert [item["chunk_id"] for item in documents] == ["first", "second"]
    assert [item["rrf_rank"] for item in documents] == [1, 2]
    assert meta["rerank_applied"] is False
    assert meta["rerank_fallback_applied"] is True
    assert meta["rerank_threshold_applied"] is False
    assert meta["rerank_error_code"] == "RERANK_TIMEOUT"
    assert meta["rerank_retryable"] is True
    assert meta["rerank_attempts"] == 2
    assert meta["post_threshold_count"] == 2


@pytest.mark.asyncio
async def test_invalid_provider_result_uses_typed_recall_fallback():
    provider = _FakeReranker(RerankResult(items=(), attempts=1))
    stage = RerankStage(provider, min_score=1.0)

    documents, meta = await stage.run_async(
        "question",
        [{"chunk_id": "c1", "text": "evidence", "score": 0.1}],
        1,
    )

    assert [item["chunk_id"] for item in documents] == ["c1"]
    assert meta["rerank_error_code"] == "RERANK_INVALID_RESPONSE"
    assert meta["rerank_fallback_applied"] is True
    assert meta["rerank_threshold_applied"] is False


@pytest.mark.asyncio
async def test_disabled_provider_is_an_explicit_skip_not_a_fallback():
    stage = RerankStage(DisabledRerankerAdapter(), min_score=0.99)

    documents, meta = await stage.run_async(
        "question",
        [
            {"chunk_id": "c1", "text": "a", "score": 0.01},
            {"chunk_id": "c2", "text": "b", "score": 0.02},
        ],
        1,
    )

    assert [item["chunk_id"] for item in documents] == ["c1"]
    assert meta["rerank_enabled"] is False
    assert meta["rerank_skip_reason"] == "disabled"
    assert meta["rerank_fallback_applied"] is False
    assert meta["rerank_threshold_applied"] is False


def test_sync_compatibility_uses_injected_long_lived_loop_bridge():
    provider = _FakeReranker(
        RerankResult(items=(RerankItem(index=0, score=0.8),), attempts=1)
    )
    bridge = ProviderLoopBridge(thread_name="rerank-stage-test-loop")
    stage = RerankStage(provider, loop_bridge=bridge)
    caller_thread = threading.get_ident()
    try:
        documents, meta = stage.run(
            "question",
            [{"chunk_id": "c1", "text": "evidence"}],
            1,
        )

        assert [item["chunk_id"] for item in documents] == ["c1"]
        assert meta["rerank_applied"] is True
        assert provider.thread_ids[0] != caller_thread
        assert bridge.running is True
    finally:
        bridge.close()


@pytest.mark.asyncio
async def test_cancelled_error_propagates_instead_of_falling_back():
    provider = _FakeReranker(asyncio.CancelledError("cancelled"))
    stage = RerankStage(provider)

    with pytest.raises(asyncio.CancelledError):
        await stage.run_async(
            "question",
            [{"chunk_id": "c1", "text": "evidence"}],
            1,
        )


@pytest.mark.asyncio
async def test_pre_cancelled_skip_path_still_propagates_cancellation():
    stage = RerankStage(DisabledRerankerAdapter())

    with pytest.raises(asyncio.CancelledError):
        await stage.run_async(
            "question",
            [{"chunk_id": "c1", "text": "evidence"}],
            1,
            cancellation=lambda: True,
        )
