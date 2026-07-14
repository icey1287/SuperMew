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

FRONTEND_DIR = PROJECT_ROOT / "frontend" / "dist"


def create_app() -> FastAPI:
    settings = get_settings()

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        settings.validate_startup()
        init_db()
        stop_event = asyncio.Event()
        publisher_task = asyncio.create_task(default_publisher.run(stop_event))
        try:
            yield
        finally:
            stop_event.set()
            try:
                await asyncio.wait_for(publisher_task, timeout=3)
            except TimeoutError:
                publisher_task.cancel()
            await default_publisher.close()

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
