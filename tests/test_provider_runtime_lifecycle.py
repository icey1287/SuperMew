from __future__ import annotations

import asyncio
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from backend.core.settings import get_settings
from backend.providers.loop_bridge import ProviderLoopBridge
from backend.providers.runtime import ProviderRuntime


class _CapabilityRuntime:
    def __init__(self, settings, *, start, close) -> None:
        self.settings = settings
        self.factory = object()
        self.catalog = object()
        self.tools = object()
        self._start = start
        self._close = close

    def start(self):
        return self._start()

    def close(self):
        return self._close()


class _CapabilityControl:
    def __init__(self, runtime: _CapabilityRuntime) -> None:
        self.runtime = runtime
        self.active = False

    def ensure_defaults(self):
        return None

    def build_runtime(self):
        return self.runtime

    def apply_runtime(self, runtime):
        runtime.start()
        self.active = True
        return runtime

    def close_runtime(self):
        if self.active:
            self.runtime.close()
            self.active = False


class _FakeReranker:
    enabled = True
    model = "fake-reranker"
    timeout_seconds = 1.0

    def __init__(
        self,
        *,
        close_delay: float = 0,
        close_error: BaseException | None = None,
    ) -> None:
        self.close_calls = 0
        self.close_thread: int | None = None
        self.close_delay = close_delay
        self.close_error = close_error

    async def rerank(self, **kwargs):
        raise AssertionError(f"unexpected rerank call: {kwargs}")

    async def aclose(self) -> None:
        self.close_calls += 1
        self.close_thread = threading.get_ident()
        if self.close_delay:
            await asyncio.sleep(self.close_delay)
        if self.close_error is not None:
            raise self.close_error


class ProviderRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_resources_are_created_and_closed_on_the_bridge_owner_loop(self):
        settings = get_settings().model_copy(deep=True)
        settings.embedding.warmup_on_start = False
        settings.rerank.model = "fake-reranker"
        settings.rerank.binding_host = "https://rerank.example.test"
        settings.rerank.api_key = settings.models.api_key
        bridge = ProviderLoopBridge(thread_name="provider-runtime-test")
        created: list[tuple[int, _FakeReranker]] = []

        def factory(_):
            reranker = _FakeReranker()
            created.append((threading.get_ident(), reranker))
            return reranker

        runtime = ProviderRuntime(
            settings=settings,
            bridge=bridge,
            reranker_factory=factory,
        )
        self.assertFalse(runtime.embedding_runtime.readiness().model_loaded)
        self.assertFalse(bridge.running)

        await runtime.start()
        await runtime.start()

        self.assertEqual(1, len(created))
        self.assertEqual(bridge.thread_ident, created[0][0])
        self.assertTrue(runtime.readiness().running)
        self.assertFalse(runtime.embedding_runtime.readiness().model_loaded)

        await runtime.aclose()
        await runtime.aclose()

        self.assertEqual(1, created[0][1].close_calls)
        self.assertEqual(created[0][0], created[0][1].close_thread)
        self.assertTrue(bridge.closed)

    async def test_concurrent_close_calls_cleanup_resources_once(self):
        settings = get_settings().model_copy(deep=True)
        settings.embedding.warmup_on_start = False
        bridge = ProviderLoopBridge(thread_name="provider-runtime-concurrent-close")
        reranker = _FakeReranker(close_delay=0.02)
        runtime = ProviderRuntime(
            settings=settings,
            bridge=bridge,
            reranker_factory=lambda _: reranker,
        )
        await runtime.start()

        await asyncio.gather(runtime.aclose(), runtime.aclose())

        self.assertEqual(1, reranker.close_calls)
        self.assertTrue(runtime.embedding_runtime.readiness().closed)
        self.assertTrue(bridge.closed)

    async def test_embedding_is_closed_even_when_reranker_close_fails(self):
        settings = get_settings().model_copy(deep=True)
        settings.embedding.warmup_on_start = False
        bridge = ProviderLoopBridge(thread_name="provider-runtime-failed-close")
        reranker = _FakeReranker(close_error=RuntimeError("reranker close failed"))
        runtime = ProviderRuntime(
            settings=settings,
            bridge=bridge,
            reranker_factory=lambda _: reranker,
        )
        await runtime.start()

        with self.assertRaisesRegex(RuntimeError, "reranker close failed"):
            await runtime.aclose()

        self.assertEqual(1, reranker.close_calls)
        self.assertTrue(runtime.embedding_runtime.readiness().closed)
        self.assertTrue(bridge.closed)

    async def test_cancelled_close_caller_waits_for_runtime_cleanup(self):
        settings = get_settings().model_copy(deep=True)
        settings.embedding.warmup_on_start = False
        bridge = ProviderLoopBridge(thread_name="provider-runtime-cancelled-close")
        reranker = _FakeReranker(close_delay=0.04)
        runtime = ProviderRuntime(
            settings=settings,
            bridge=bridge,
            reranker_factory=lambda _: reranker,
        )
        await runtime.start()
        close = asyncio.create_task(runtime.aclose())
        await asyncio.sleep(0.01)
        close.cancel()

        with self.assertRaises(asyncio.CancelledError):
            await close
        await runtime.aclose()

        self.assertEqual(1, reranker.close_calls)
        self.assertTrue(runtime.embedding_runtime.readiness().closed)
        self.assertTrue(bridge.closed)


class AppProviderLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_lifespan_starts_provider_before_runs_and_closes_it_after_runs(self):
        import backend.app as app_module

        events: list[str] = []

        class Provider:
            async def start(self):
                events.append("provider.start")

            async def aclose(self):
                events.append("provider.close")

        class Executor:
            async def start(self):
                events.append("runs.start")

            async def close(self):
                events.append("runs.close")

        class SandboxRuntime:
            def start(self):
                events.append("sandbox.start")

            def close(self):
                events.append("sandbox.close")

        class Publisher:
            async def run(self, stop_event):
                events.append("publisher.run")
                await stop_event.wait()

            async def close(self):
                events.append("publisher.close")

        class Cancellation:
            async def listen(self, stop_event):
                events.append("cancellation.listen")
                await stop_event.wait()

            async def close(self):
                events.append("cancellation.close")

        settings = SimpleNamespace(
            security=SimpleNamespace(
                cors_origins=["http://localhost:5173"],
                cors_allow_credentials=True,
            ),
            sql_assistant=SimpleNamespace(enabled=True),
            web_research=SimpleNamespace(enabled=True),
            sandbox=SimpleNamespace(enabled=True),
            validate_startup=lambda: events.append("settings.validate"),
        )

        def init_db():
            events.append("db.init")

        runtime = _CapabilityRuntime(
            settings,
            start=lambda: events.extend(["sql.start", "web.start", "web.install"]),
            close=lambda: events.extend(["web.clear", "web.close", "sql.close"]),
        )

        with (
            patch.object(app_module, "get_settings", return_value=settings),
            patch.object(app_module, "init_db", side_effect=init_db),
            patch.object(
                app_module,
                "model_control_service",
                SimpleNamespace(ensure_environment_defaults=lambda: None),
            ),
            patch.object(
                app_module,
                "capability_control_service",
                _CapabilityControl(runtime),
            ),
            patch.object(app_module, "provider_runtime", Provider()),
            patch.object(
                app_module,
                "build_sandbox_runtime",
                return_value=SandboxRuntime(),
            ),
            patch.object(
                app_module,
                "install_sandbox_runtime",
                side_effect=lambda _runtime: events.append("sandbox.install"),
            ),
            patch.object(
                app_module,
                "clear_sandbox_runtime",
                side_effect=lambda _runtime: events.append("sandbox.clear"),
            ),
            patch.object(app_module, "run_agent_executor", Executor()),
            patch.object(app_module, "default_publisher", Publisher()),
            patch.object(app_module, "cancellation_registry", Cancellation()),
        ):
            app = app_module.create_app()
            async with app.router.lifespan_context(app):
                await asyncio.sleep(0)
                self.assertLess(
                    events.index("provider.start"),
                    events.index("sql.start"),
                )
                self.assertLess(
                    events.index("sql.start"),
                    events.index("web.start"),
                )
                self.assertLess(
                    events.index("web.start"),
                    events.index("web.install"),
                )
                self.assertLess(
                    events.index("web.install"),
                    events.index("sandbox.start"),
                )
                self.assertLess(
                    events.index("sandbox.start"),
                    events.index("sandbox.install"),
                )
                self.assertLess(
                    events.index("sandbox.install"),
                    events.index("runs.start"),
                )

        self.assertLess(events.index("runs.close"), events.index("sandbox.clear"))
        self.assertLess(events.index("sandbox.clear"), events.index("sandbox.close"))
        self.assertLess(events.index("sandbox.close"), events.index("web.clear"))
        self.assertLess(events.index("web.clear"), events.index("web.close"))
        self.assertLess(events.index("web.close"), events.index("sql.close"))
        self.assertLess(events.index("sql.close"), events.index("provider.close"))
        self.assertLess(events.index("publisher.close"), events.index("provider.close"))
        self.assertLess(
            events.index("cancellation.close"),
            events.index("provider.close"),
        )

    async def test_partial_run_executor_start_is_closed_before_provider(self):
        import backend.app as app_module

        events: list[str] = []

        class Provider:
            async def start(self):
                events.append("provider.start")

            async def aclose(self):
                events.append("provider.close")

        class FailingExecutor:
            async def start(self):
                events.append("runs.start")
                raise RuntimeError("recovery failed")

            async def close(self):
                events.append("runs.close")

        class SandboxRuntime:
            def start(self):
                events.append("sandbox.start")

            def close(self):
                events.append("sandbox.close")

        settings = SimpleNamespace(
            security=SimpleNamespace(
                cors_origins=["http://localhost:5173"],
                cors_allow_credentials=True,
            ),
            sandbox=SimpleNamespace(enabled=True),
            validate_startup=lambda: None,
        )
        runtime = _CapabilityRuntime(settings, start=lambda: None, close=lambda: None)

        with (
            patch.object(app_module, "get_settings", return_value=settings),
            patch.object(app_module, "init_db"),
            patch.object(
                app_module,
                "model_control_service",
                SimpleNamespace(ensure_environment_defaults=lambda: None),
            ),
            patch.object(
                app_module,
                "capability_control_service",
                _CapabilityControl(runtime),
            ),
            patch.object(app_module, "provider_runtime", Provider()),
            patch.object(
                app_module,
                "build_sandbox_runtime",
                return_value=SandboxRuntime(),
            ),
            patch.object(
                app_module,
                "install_sandbox_runtime",
                side_effect=lambda _runtime: events.append("sandbox.install"),
            ),
            patch.object(
                app_module,
                "clear_sandbox_runtime",
                side_effect=lambda _runtime: events.append("sandbox.clear"),
            ),
            patch.object(app_module, "run_agent_executor", FailingExecutor()),
        ):
            app = app_module.create_app()
            with self.assertRaisesRegex(RuntimeError, "recovery failed"):
                async with app.router.lifespan_context(app):
                    self.fail("lifespan should not yield after failed recovery")

        self.assertEqual(
            [
                "provider.start",
                "sandbox.start",
                "sandbox.install",
                "runs.start",
                "runs.close",
                "sandbox.clear",
                "sandbox.close",
                "provider.close",
            ],
            events,
        )

    async def test_partial_sql_start_is_closed_before_provider(self):
        import backend.app as app_module

        events: list[str] = []

        class Provider:
            async def start(self):
                events.append("provider.start")

            async def aclose(self):
                events.append("provider.close")

        settings = SimpleNamespace(
            security=SimpleNamespace(
                cors_origins=["http://localhost:5173"],
                cors_allow_credentials=True,
            ),
            sql_assistant=SimpleNamespace(enabled=True),
            validate_startup=lambda: None,
        )

        def start_runtime():
            events.append("sql.start")
            raise RuntimeError("sql startup failed")

        runtime = _CapabilityRuntime(
            settings,
            start=start_runtime,
            close=lambda: events.append("sql.close"),
        )

        with (
            patch.object(app_module, "get_settings", return_value=settings),
            patch.object(app_module, "init_db"),
            patch.object(
                app_module,
                "model_control_service",
                SimpleNamespace(ensure_environment_defaults=lambda: None),
            ),
            patch.object(
                app_module,
                "capability_control_service",
                _CapabilityControl(runtime),
            ),
            patch.object(app_module, "provider_runtime", Provider()),
        ):
            app = app_module.create_app()
            with self.assertRaisesRegex(RuntimeError, "sql startup failed"):
                async with app.router.lifespan_context(app):
                    self.fail("lifespan should not yield after failed SQL startup")

        self.assertEqual(
            ["provider.start", "sql.start", "sql.close", "provider.close"],
            events,
        )

    async def test_partial_web_start_is_closed_without_installing_runtime(self):
        import backend.app as app_module

        events: list[str] = []

        class Provider:
            async def start(self):
                events.append("provider.start")

            async def aclose(self):
                events.append("provider.close")

        settings = SimpleNamespace(
            security=SimpleNamespace(
                cors_origins=["http://localhost:5173"],
                cors_allow_credentials=True,
            ),
            web_research=SimpleNamespace(enabled=True),
            validate_startup=lambda: None,
        )

        def start_runtime():
            events.append("web.start")
            raise RuntimeError("web startup failed")

        runtime = _CapabilityRuntime(
            settings,
            start=start_runtime,
            close=lambda: events.append("web.close"),
        )

        with (
            patch.object(app_module, "get_settings", return_value=settings),
            patch.object(app_module, "init_db"),
            patch.object(
                app_module,
                "model_control_service",
                SimpleNamespace(ensure_environment_defaults=lambda: None),
            ),
            patch.object(
                app_module,
                "capability_control_service",
                _CapabilityControl(runtime),
            ),
            patch.object(app_module, "provider_runtime", Provider()),
        ):
            app = app_module.create_app()
            with self.assertRaisesRegex(RuntimeError, "web startup failed"):
                async with app.router.lifespan_context(app):
                    self.fail("lifespan should not yield after failed web startup")

        self.assertEqual(
            ["provider.start", "web.start", "web.close", "provider.close"],
            events,
        )

    async def test_partial_sandbox_start_closes_all_started_runtimes_in_reverse_order(
        self,
    ):
        import backend.app as app_module

        events: list[str] = []

        class Provider:
            async def start(self):
                events.append("provider.start")

            async def aclose(self):
                events.append("provider.close")

        class FailingSandboxRuntime:
            def start(self):
                events.append("sandbox.start")
                raise RuntimeError("sandbox startup failed")

            def close(self):
                events.append("sandbox.close")

        settings = SimpleNamespace(
            security=SimpleNamespace(
                cors_origins=["http://localhost:5173"],
                cors_allow_credentials=True,
            ),
            sql_assistant=SimpleNamespace(enabled=True),
            web_research=SimpleNamespace(enabled=True),
            sandbox=SimpleNamespace(enabled=True),
            validate_startup=lambda: None,
        )
        runtime = _CapabilityRuntime(
            settings,
            start=lambda: events.extend(["sql.start", "web.start", "web.install"]),
            close=lambda: events.extend(["web.clear", "web.close", "sql.close"]),
        )

        with (
            patch.object(app_module, "get_settings", return_value=settings),
            patch.object(app_module, "init_db"),
            patch.object(
                app_module,
                "model_control_service",
                SimpleNamespace(ensure_environment_defaults=lambda: None),
            ),
            patch.object(
                app_module,
                "capability_control_service",
                _CapabilityControl(runtime),
            ),
            patch.object(app_module, "provider_runtime", Provider()),
            patch.object(
                app_module,
                "build_sandbox_runtime",
                return_value=FailingSandboxRuntime(),
            ),
            patch.object(
                app_module,
                "install_sandbox_runtime",
                side_effect=lambda _runtime: events.append("sandbox.install"),
            ),
        ):
            app = app_module.create_app()
            with self.assertRaisesRegex(RuntimeError, "sandbox startup failed"):
                async with app.router.lifespan_context(app):
                    self.fail("lifespan should not yield after failed Sandbox startup")

        self.assertEqual(
            [
                "provider.start",
                "sql.start",
                "web.start",
                "web.install",
                "sandbox.start",
                "sandbox.close",
                "web.clear",
                "web.close",
                "sql.close",
                "provider.close",
            ],
            events,
        )

    async def test_shutdown_continues_after_one_adapter_close_fails(self):
        import backend.app as app_module

        events: list[str] = []

        class Provider:
            async def start(self):
                events.append("provider.start")

            async def aclose(self):
                events.append("provider.close")

        class Executor:
            async def start(self):
                events.append("runs.start")

            async def close(self):
                events.append("runs.close")

        class Publisher:
            async def run(self, stop_event):
                await stop_event.wait()

            async def close(self):
                events.append("publisher.close")
                raise RuntimeError("publisher close failed")

        class Cancellation:
            async def listen(self, stop_event):
                await stop_event.wait()

            async def close(self):
                events.append("cancellation.close")

        settings = SimpleNamespace(
            security=SimpleNamespace(
                cors_origins=["http://localhost:5173"],
                cors_allow_credentials=True,
            ),
            validate_startup=lambda: None,
        )
        runtime = _CapabilityRuntime(settings, start=lambda: None, close=lambda: None)

        with (
            patch.object(app_module, "get_settings", return_value=settings),
            patch.object(app_module, "init_db"),
            patch.object(
                app_module,
                "model_control_service",
                SimpleNamespace(ensure_environment_defaults=lambda: None),
            ),
            patch.object(
                app_module,
                "capability_control_service",
                _CapabilityControl(runtime),
            ),
            patch.object(app_module, "provider_runtime", Provider()),
            patch.object(app_module, "run_agent_executor", Executor()),
            patch.object(app_module, "default_publisher", Publisher()),
            patch.object(app_module, "cancellation_registry", Cancellation()),
        ):
            app = app_module.create_app()
            with self.assertRaisesRegex(RuntimeError, "publisher close failed"):
                async with app.router.lifespan_context(app):
                    await asyncio.sleep(0)

        self.assertIn("cancellation.close", events)
        self.assertIn("provider.close", events)


if __name__ == "__main__":
    unittest.main()
