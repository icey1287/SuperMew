import asyncio
import math
import threading
import time
import unittest

from backend.providers.core import ProviderCode, ProviderError, ProviderPolicy
from backend.providers.embedding import (
    EmbeddingRuntime,
    EmbeddingScope,
    EmbeddingService,
)
from backend.providers.loop_bridge import ProviderLoopBridge


class RecordingModel:
    def __init__(self, *, delay=0.0):
        self.delay = delay
        self.query_calls = []
        self.document_calls = []
        self.active = 0
        self.max_active = 0
        self._lock = threading.Lock()

    def encode_query(self, texts, **kwargs):
        self.query_calls.append((list(texts), dict(kwargs)))
        return self._encode(texts)

    def encode_document(self, texts, **kwargs):
        self.document_calls.append((list(texts), dict(kwargs)))
        return self._encode(texts)

    def _encode(self, texts):
        with self._lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            if self.delay:
                time.sleep(self.delay)
            return [self._vector(text) for text in texts]
        finally:
            with self._lock:
                self.active -= 1

    @staticmethod
    def _vector(text):
        seed = float(sum(ord(character) for character in text) % 17)
        return [seed, seed + 1.0, seed + 2.0]


class InvalidVectorModel(RecordingModel):
    def encode_query(self, texts, **kwargs):
        return [[math.nan] for _ in texts]


class EmbeddingRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_lazy_query_encoding_normalizes_text_and_keeps_loop_responsive(self):
        model = RecordingModel(delay=0.05)
        factory_threads = []

        def model_factory():
            factory_threads.append(threading.get_ident())
            return model

        runtime = EmbeddingRuntime(
            model_factory=model_factory,
            policy=ProviderPolicy(max_attempts=1),
            microbatch_window_seconds=0,
        )
        self.assertFalse(runtime.readiness().ready)
        self.assertFalse(runtime.readiness().model_loaded)

        task = asyncio.create_task(runtime.embed_query("  Ａ\t B  "))
        ticks = 0
        while not task.done():
            ticks += 1
            await asyncio.sleep(0.003)
        vector = await task

        self.assertGreater(ticks, 3)
        self.assertEqual(RecordingModel._vector("A B"), vector)
        self.assertEqual(["A B"], model.query_calls[0][0])
        self.assertTrue(model.query_calls[0][1]["normalize_embeddings"])
        self.assertNotEqual(threading.get_ident(), factory_threads[0])
        self.assertTrue(runtime.readiness().ready)
        self.assertEqual(3, runtime.readiness().dimension)
        await runtime.close()

    async def test_microbatch_inflight_dedup_lru_and_cache_scope(self):
        model = RecordingModel(delay=0.01)
        runtime = EmbeddingRuntime(
            model_factory=lambda: model,
            policy=ProviderPolicy(max_attempts=1),
            microbatch_window_seconds=0.02,
            max_batch_size=8,
            cache_size=2,
        )
        scope = EmbeddingScope(namespace="docs", tenant_id="tenant-a", index_id="v1")

        first, duplicate, second = await asyncio.gather(
            runtime.embed_query("hello   world", scope=scope),
            runtime.embed_query("hello world", scope=scope),
            runtime.embed_query("second", scope=scope),
        )

        self.assertEqual(first, duplicate)
        self.assertNotEqual(first, second)
        self.assertEqual(1, len(model.query_calls))
        self.assertCountEqual(
            ["hello world", "second"],
            model.query_calls[0][0],
        )

        self.assertEqual(first, await runtime.embed_query("hello world", scope=scope))
        self.assertEqual(1, len(model.query_calls))

        other_scope = EmbeddingScope(
            namespace="docs", tenant_id="tenant-b", index_id="v1"
        )
        await runtime.embed_query("hello world", scope=other_scope)
        self.assertEqual(2, len(model.query_calls))
        self.assertGreaterEqual(runtime.stats().cache_hits, 1)
        self.assertGreaterEqual(runtime.stats().inflight_joins, 1)
        await runtime.close()

    async def test_document_batches_are_bounded_and_use_document_semantics(self):
        model = RecordingModel()
        runtime = EmbeddingRuntime(
            model_factory=lambda: model,
            policy=ProviderPolicy(max_attempts=1),
            max_batch_size=2,
            microbatch_window_seconds=0.01,
        )

        vectors = await runtime.embed_documents(["one", "two", "three", "four", "five"])

        self.assertEqual(5, len(vectors))
        self.assertFalse(model.query_calls)
        self.assertTrue(all(len(call[0]) <= 2 for call in model.document_calls))
        self.assertCountEqual(
            ["one", "two", "three", "four", "five"],
            [text for call, _ in model.document_calls for text in call],
        )
        await runtime.close()

    async def test_document_indexing_does_not_evict_query_lru_entries(self):
        model = RecordingModel()
        runtime = EmbeddingRuntime(
            model_factory=lambda: model,
            policy=ProviderPolicy(max_attempts=1),
            cache_size=1,
            microbatch_window_seconds=0,
        )

        first = await runtime.embed_query("cached query")
        await runtime.embed_documents(["document chunk"])
        second = await runtime.embed_query("cached query")

        self.assertEqual(first, second)
        self.assertEqual(1, len(model.query_calls))
        self.assertEqual(1, len(model.document_calls))
        await runtime.close()

    async def test_query_and_document_workers_share_encoder_concurrency_gate(self):
        model = RecordingModel(delay=0.04)
        runtime = EmbeddingRuntime(
            model_factory=lambda: model,
            policy=ProviderPolicy(max_attempts=1),
            encoder_concurrency=1,
            executor_workers=2,
            microbatch_window_seconds=0,
        )

        await asyncio.gather(
            runtime.embed_query("query"),
            runtime.embed_documents(["document"]),
        )

        self.assertEqual(1, model.max_active)
        await runtime.close()

    async def test_invalid_vectors_become_typed_provider_failures(self):
        runtime = EmbeddingRuntime(
            model_factory=InvalidVectorModel,
            policy=ProviderPolicy(max_attempts=1),
            microbatch_window_seconds=0,
        )

        with self.assertRaises(ProviderError) as raised:
            await runtime.embed_query("bad vector")

        self.assertEqual(ProviderCode.EMBEDDING_UNAVAILABLE, raised.exception.code)
        self.assertNotIn("nan", raised.exception.message.lower())
        await runtime.close()

    async def test_absolute_deadline_actively_stops_waiting_for_encoder(self):
        runtime = EmbeddingRuntime(
            model_factory=lambda: RecordingModel(delay=0.15),
            policy=ProviderPolicy(max_attempts=1),
            microbatch_window_seconds=0,
        )
        started_at = time.monotonic()

        with self.assertRaises(ProviderError) as raised:
            await runtime.embed_query(
                "slow",
                deadline=time.monotonic() + 0.02,
            )

        self.assertEqual(ProviderCode.PROVIDER_DEADLINE_EXCEEDED, raised.exception.code)
        self.assertLess(time.monotonic() - started_at, 0.1)
        await runtime.close()

    async def test_cancellation_probe_stops_waiting_without_blocking_encoder_thread(
        self,
    ):
        cancelled = False
        runtime = EmbeddingRuntime(
            model_factory=lambda: RecordingModel(delay=0.1),
            policy=ProviderPolicy(max_attempts=1),
            microbatch_window_seconds=0,
            cancellation_poll_seconds=0.005,
        )

        async def request_cancellation():
            nonlocal cancelled
            await asyncio.sleep(0.02)
            cancelled = True

        trigger = asyncio.create_task(request_cancellation())
        with self.assertRaises(asyncio.CancelledError):
            await runtime.embed_query("cancel", cancellation=lambda: cancelled)
        await trigger
        await runtime.close()

    async def test_bounded_queue_wait_respects_provider_deadline(self):
        runtime = EmbeddingRuntime(
            model_factory=lambda: RecordingModel(delay=0.08),
            policy=ProviderPolicy(max_attempts=1),
            max_queue_size=1,
            microbatch_window_seconds=0,
        )

        first = asyncio.create_task(runtime.embed_query("first"))
        await asyncio.sleep(0.01)
        with self.assertRaises(ProviderError) as raised:
            await runtime.embed_query(
                "second",
                deadline=time.monotonic() + 0.015,
            )
        self.assertEqual(ProviderCode.PROVIDER_DEADLINE_EXCEEDED, raised.exception.code)
        await first
        await runtime.close()

    async def test_warmup_reports_readiness_and_close_rejects_later_calls(self):
        runtime = EmbeddingRuntime(
            model_factory=RecordingModel,
            policy=ProviderPolicy(max_attempts=1),
            microbatch_window_seconds=0,
        )

        readiness = await runtime.warmup()
        self.assertTrue(readiness.ready)
        self.assertTrue(readiness.model_loaded)
        await runtime.close()
        self.assertTrue(runtime.readiness().closed)

        with self.assertRaisesRegex(RuntimeError, "closed"):
            await runtime.embed_query("after close")

    async def test_close_releases_capacity_waiters_without_requeueing_work(self):
        runtime = EmbeddingRuntime(
            model_factory=lambda: RecordingModel(delay=0.08),
            policy=ProviderPolicy(max_attempts=1),
            max_queue_size=1,
            microbatch_window_seconds=0,
        )
        tasks = [
            asyncio.create_task(runtime.embed_query(f"query-{index}"))
            for index in range(4)
        ]
        await asyncio.sleep(0.01)

        await runtime.close()
        results = await asyncio.wait_for(
            asyncio.gather(*tasks, return_exceptions=True),
            timeout=1,
        )

        self.assertTrue(all(isinstance(result, BaseException) for result in results))
        readiness = runtime.readiness()
        self.assertTrue(readiness.closed)
        self.assertEqual(0, readiness.queue_depth)
        self.assertEqual(0, readiness.inflight)

    async def test_cancelled_close_caller_still_finishes_runtime_cleanup(self):
        runtime = EmbeddingRuntime(
            model_factory=lambda: RecordingModel(delay=0.08),
            policy=ProviderPolicy(max_attempts=1),
            microbatch_window_seconds=0,
        )
        query = asyncio.create_task(runtime.embed_query("slow close"))
        await asyncio.sleep(0.01)
        close = asyncio.create_task(runtime.close())
        await asyncio.sleep(0.01)
        close.cancel()

        with self.assertRaises(asyncio.CancelledError):
            await close
        await asyncio.gather(query, return_exceptions=True)
        await runtime.close()

        self.assertTrue(runtime.readiness().closed)
        self.assertEqual(0, runtime.readiness().inflight)


class EmbeddingServiceTests(unittest.TestCase):
    def test_sync_facade_uses_bridge_without_creating_per_call_event_loops(self):
        model = RecordingModel()
        runtime = EmbeddingRuntime(
            model_factory=lambda: model,
            policy=ProviderPolicy(max_attempts=1),
            microbatch_window_seconds=0,
        )
        bridge = ProviderLoopBridge()
        service = EmbeddingService(runtime=runtime, bridge=bridge)
        try:
            first = service.get_embeddings(["document"])
            loop_thread = bridge.thread_ident
            second = service.embed_query("query")

            self.assertEqual([RecordingModel._vector("document")], first)
            self.assertEqual(RecordingModel._vector("query"), second)
            self.assertEqual(loop_thread, bridge.thread_ident)
            self.assertTrue(service.readiness().ready)
        finally:
            service.close(close_bridge=True)


if __name__ == "__main__":
    unittest.main()
