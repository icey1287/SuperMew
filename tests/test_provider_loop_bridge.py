import asyncio
import threading
import time
import unittest

from backend.providers.loop_bridge import ProviderLoopBridge


class ProviderLoopBridgeTests(unittest.TestCase):
    def setUp(self):
        self.bridge = ProviderLoopBridge(cancellation_poll_seconds=0.005)

    def tearDown(self):
        self.bridge.close()

    def test_sync_calls_reuse_one_background_loop_thread(self):
        async def identify():
            return id(asyncio.get_running_loop()), threading.get_ident()

        first = self.bridge.call_sync(identify)
        second = self.bridge.call_sync(identify)

        self.assertEqual(first, second)
        self.assertNotEqual(threading.get_ident(), first[1])
        self.assertEqual(self.bridge.thread_ident, first[1])

    def test_sync_deadline_cancels_the_concurrent_future(self):
        cleaned_up = threading.Event()

        async def slow_call():
            try:
                await asyncio.sleep(10)
            finally:
                cleaned_up.set()

        with self.assertRaises(TimeoutError):
            self.bridge.call_sync(
                slow_call,
                deadline=time.monotonic() + 0.03,
            )

        self.assertTrue(cleaned_up.wait(1))

    def test_sync_cancellation_probe_cancels_the_concurrent_future(self):
        started = threading.Event()
        cancelled = threading.Event()
        cleaned_up = threading.Event()

        async def slow_call():
            started.set()
            try:
                await asyncio.sleep(10)
            finally:
                cleaned_up.set()

        def request_cancellation():
            self.assertTrue(started.wait(1))
            cancelled.set()

        trigger = threading.Thread(target=request_cancellation)
        trigger.start()
        with self.assertRaises(asyncio.CancelledError):
            self.bridge.call_sync(slow_call, cancellation=cancelled.is_set)
        trigger.join(1)

        self.assertTrue(cleaned_up.wait(1))

    def test_close_is_idempotent_and_rejects_new_calls(self):
        self.bridge.start()
        self.bridge.close()
        self.bridge.close()

        self.assertTrue(self.bridge.closed)
        with self.assertRaisesRegex(RuntimeError, "closed"):
            self.bridge.call_sync(lambda: asyncio.sleep(0))

    def test_factory_must_return_an_awaitable(self):
        with self.assertRaisesRegex(TypeError, "awaitable"):
            self.bridge.call_sync(lambda: "not-awaitable")


class ProviderLoopBridgeAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bridge = ProviderLoopBridge(cancellation_poll_seconds=0.005)

    async def asyncTearDown(self):
        self.bridge.close()

    async def test_call_async_does_not_block_the_caller_loop(self):
        caller_thread = threading.get_ident()
        ticks = 0

        async def bridge_work():
            await asyncio.sleep(0.04)
            return threading.get_ident()

        task = asyncio.create_task(self.bridge.call_async(bridge_work))
        while not task.done():
            ticks += 1
            await asyncio.sleep(0.003)

        self.assertGreater(ticks, 3)
        self.assertNotEqual(caller_thread, await task)

    async def test_cold_call_async_does_not_block_during_slow_loop_start(self):
        class SlowStartBridge(ProviderLoopBridge):
            def _run_loop(self):
                time.sleep(0.1)
                super()._run_loop()

        bridge = SlowStartBridge()
        stop_ticker = False
        ticks = 0

        async def ticker():
            nonlocal ticks
            while not stop_ticker:
                ticks += 1
                await asyncio.sleep(0.005)

        ticker_task = asyncio.create_task(ticker())
        try:
            await bridge.call_async(lambda: asyncio.sleep(0))
        finally:
            stop_ticker = True
            await ticker_task
            bridge.close()

        self.assertGreaterEqual(ticks, 5)

    async def test_call_sync_is_rejected_inside_a_running_loop(self):
        called = False

        async def work():
            nonlocal called
            called = True

        with self.assertRaisesRegex(RuntimeError, "call_async"):
            self.bridge.call_sync(work)

        self.assertFalse(called)
        self.assertFalse(self.bridge.running)

    async def test_cancelling_async_caller_cancels_bridge_work(self):
        cleaned_up = threading.Event()

        async def slow_call():
            try:
                await asyncio.sleep(10)
            finally:
                cleaned_up.set()

        task = asyncio.create_task(self.bridge.call_async(slow_call))
        await asyncio.sleep(0.02)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

        self.assertTrue(await asyncio.to_thread(cleaned_up.wait, 1))


if __name__ == "__main__":
    unittest.main()
