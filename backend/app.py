import asyncio
from contextlib import asynccontextmanager
from uuid import uuid4

# Application imports read settings at module load, so load the project environment first.
# ruff: noqa: E402
from backend.env import PROJECT_ROOT, load_env

load_env()

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from backend.api.router import router
from backend.auth.access import resolve_access_token_subject
from backend.auth.origin import (
    AuthBodyLimitMiddleware,
    AuthRequestGuardMiddleware,
)
from backend.capabilities.control_service import capability_control_service
from backend.core.errors import install_exception_handlers
from backend.core.settings import get_settings
from backend.events.outbox import default_publisher
from backend.infra.database import init_db
from backend.model_control import model_control_service
from backend.providers.runtime import provider_runtime
from backend.rate_limits.http import RateLimitMiddleware
from backend.rate_limits.runtime import build_rate_limiter
from backend.runs.agent_executor import run_agent_executor
from backend.runs.cancellation import cancellation_registry
from backend.sandbox import (
    build_sandbox_runtime,
    clear_sandbox_runtime,
    install_sandbox_runtime,
)
from backend.security.headers import SecurityHeadersMiddleware

FRONTEND_DIR = PROJECT_ROOT / "frontend" / "dist"


def create_app() -> FastAPI:
    settings = get_settings()
    rate_limiter = build_rate_limiter(settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        provider_started = False
        capability_runtime = None
        capability_applied = False
        sandbox_start_attempted = False
        sandbox_runtime = None
        sandbox_installed = False
        executor_start_attempted = False
        stop_event: asyncio.Event | None = None
        publisher_task: asyncio.Task | None = None
        cancellation_task: asyncio.Task | None = None
        try:
            settings.validate_startup()
            await asyncio.to_thread(init_db)
            await asyncio.to_thread(model_control_service.ensure_environment_defaults)
            await asyncio.to_thread(capability_control_service.ensure_defaults)
            capability_runtime = await asyncio.to_thread(
                capability_control_service.build_runtime
            )
            await provider_runtime.start()
            provider_started = True
            await asyncio.to_thread(
                capability_control_service.apply_runtime,
                capability_runtime,
            )
            capability_applied = True
            sandbox_runtime = build_sandbox_runtime(settings)
            sandbox_start_attempted = True
            await asyncio.to_thread(sandbox_runtime.start)
            install_sandbox_runtime(sandbox_runtime)
            sandbox_installed = True
            executor_start_attempted = True
            await run_agent_executor.start()
            stop_event = asyncio.Event()
            publisher_task = asyncio.create_task(default_publisher.run(stop_event))
            cancellation_task = asyncio.create_task(
                cancellation_registry.listen(stop_event)
            )
            yield
        finally:
            cleanup_errors: list[BaseException] = []
            if executor_start_attempted:
                try:
                    await run_agent_executor.close()
                except BaseException as exc:
                    cleanup_errors.append(exc)
            if stop_event is not None:
                stop_event.set()
            background_tasks = [
                task for task in (publisher_task, cancellation_task) if task is not None
            ]
            if background_tasks:
                try:
                    await asyncio.wait_for(
                        asyncio.gather(
                            *background_tasks,
                            return_exceptions=True,
                        ),
                        timeout=3,
                    )
                except BaseException as exc:
                    cleanup_errors.append(exc)
                    for task in background_tasks:
                        task.cancel()
                    await asyncio.gather(
                        *background_tasks,
                        return_exceptions=True,
                    )
            if stop_event is not None:
                for closer in (
                    default_publisher.close,
                    cancellation_registry.close,
                ):
                    try:
                        await closer()
                    except BaseException as exc:
                        cleanup_errors.append(exc)
            if sandbox_installed:
                try:
                    clear_sandbox_runtime(sandbox_runtime)
                except BaseException as exc:
                    cleanup_errors.append(exc)
            if sandbox_start_attempted and sandbox_runtime is not None:
                try:
                    await asyncio.to_thread(sandbox_runtime.close)
                except BaseException as exc:
                    cleanup_errors.append(exc)
            if capability_applied:
                try:
                    await asyncio.to_thread(capability_control_service.close_runtime)
                except BaseException as exc:
                    cleanup_errors.append(exc)
            elif capability_runtime is not None:
                try:
                    await asyncio.to_thread(capability_runtime.close)
                except BaseException as exc:
                    cleanup_errors.append(exc)
            if provider_started:
                try:
                    await provider_runtime.aclose()
                except BaseException as exc:
                    cleanup_errors.append(exc)
            if rate_limiter is not None:
                try:
                    await rate_limiter.close()
                except BaseException as exc:
                    cleanup_errors.append(exc)
            if len(cleanup_errors) == 1:
                raise cleanup_errors[0]
            if cleanup_errors:
                raise BaseExceptionGroup("application shutdown failed", cleanup_errors)

    app = FastAPI(title="SuperMew API", version="1.0.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.rate_limiter = rate_limiter
    install_exception_handlers(app)

    # Streamed auth bodies are bounded after Rate Limit has charged host quota.
    app.add_middleware(AuthBodyLimitMiddleware)

    if rate_limiter is not None:
        app.add_middleware(
            RateLimitMiddleware,
            limiter=rate_limiter,
            bearer_subject_resolver=resolve_access_token_subject,
        )

    # Added after Rate Limit so Starlette places this metadata guard outside it:
    # cross-site, non-JSON and declared-oversized requests consume no quota.
    app.add_middleware(
        AuthRequestGuardMiddleware,
        settings=settings.security,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.security.cors_origins,
        allow_credentials=settings.security.cors_allow_credentials,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=[
            "X-Request-ID",
            "X-Run-ID",
            "X-Thread-Version",
        ],
    )

    @app.middleware("http")
    async def _request_id(request, call_next):
        request_id = request.headers.get("X-Request-ID") or uuid4().hex
        request.state.request_id = request_id
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        return response

    @app.middleware("http")
    async def _no_cache(request, call_next):
        response = await call_next(request)
        path = request.url.path or ""
        if path == "/auth" or path.startswith("/auth/"):
            response.headers["Cache-Control"] = "no-store"
            response.headers["Pragma"] = "no-cache"
        elif path == "/" or path.endswith((".html", ".js", ".css")):
            response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
        return response

    app.add_middleware(SecurityHeadersMiddleware)

    app.include_router(router)

    if FRONTEND_DIR.exists():
        app.mount(
            "/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="static"
        )

    return app


app = create_app()
