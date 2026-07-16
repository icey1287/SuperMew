import sys
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

# 兼容直接执行 backend/app.py 时在导入 backend 包前修正 sys.path。
# ruff: noqa: E402

# 支持 `python backend/app.py` 与 `uvicorn backend.app:app` 两种启动方式
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from backend.env import PROJECT_ROOT, load_env

load_env()

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from backend.api.router import router
from backend.core.errors import install_exception_handlers
from backend.core.settings import get_settings
from backend.events.outbox import default_publisher
from backend.infra.database import init_db
from backend.providers.runtime import provider_runtime
from backend.runs.agent_executor import run_agent_executor
from backend.runs.cancellation import cancellation_registry
from backend.sql_assistant.runtime import get_sql_assistant_runtime
from backend.web_research.runtime import (
    build_web_research_runtime,
    clear_web_research_runtime,
    install_web_research_runtime,
)

FRONTEND_DIR = PROJECT_ROOT / "frontend" / "dist"


def create_app() -> FastAPI:
    settings = get_settings()

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        settings.validate_startup()
        provider_started = False
        sql_start_attempted = False
        sql_runtime = None
        web_start_attempted = False
        web_runtime = None
        web_installed = False
        executor_start_attempted = False
        stop_event: asyncio.Event | None = None
        publisher_task: asyncio.Task | None = None
        cancellation_task: asyncio.Task | None = None
        try:
            await asyncio.to_thread(init_db)
            await provider_runtime.start()
            provider_started = True
            if bool(
                getattr(getattr(settings, "sql_assistant", None), "enabled", False)
            ):
                sql_runtime = get_sql_assistant_runtime()
                sql_start_attempted = True
                await asyncio.to_thread(sql_runtime.start)
            if bool(getattr(getattr(settings, "web_research", None), "enabled", False)):
                web_runtime = build_web_research_runtime(settings)
                web_start_attempted = True
                await asyncio.to_thread(web_runtime.start)
                install_web_research_runtime(web_runtime)
                web_installed = True
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
            if web_installed:
                try:
                    clear_web_research_runtime(web_runtime)
                except BaseException as exc:
                    cleanup_errors.append(exc)
            if web_start_attempted and web_runtime is not None:
                try:
                    await asyncio.to_thread(web_runtime.close)
                except BaseException as exc:
                    cleanup_errors.append(exc)
            if sql_start_attempted and sql_runtime is not None:
                try:
                    await asyncio.to_thread(sql_runtime.close)
                except BaseException as exc:
                    cleanup_errors.append(exc)
            if provider_started:
                try:
                    await provider_runtime.aclose()
                except BaseException as exc:
                    cleanup_errors.append(exc)
            if len(cleanup_errors) == 1:
                raise cleanup_errors[0]
            if cleanup_errors:
                raise BaseExceptionGroup("application shutdown failed", cleanup_errors)

    app = FastAPI(title="SuperMew API", version="1.0.0", lifespan=lifespan)
    app.state.settings = settings
    install_exception_handlers(app)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.security.cors_origins,
        allow_credentials=settings.security.cors_allow_credentials,
        allow_methods=["*"],
        allow_headers=["*"],
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
        if path == "/" or path.endswith((".html", ".js", ".css")):
            response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
        return response

    app.include_router(router)

    if FRONTEND_DIR.exists():
        app.mount(
            "/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="static"
        )

    return app


app = create_app()

if __name__ == "__main__":
    import uvicorn

    settings = get_settings()
    uvicorn.run(app, host=settings.app.host, port=settings.app.port)
