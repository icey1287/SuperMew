from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.testclient import TestClient

from backend.core.errors import install_exception_handlers
from backend.security.headers import (
    CONTENT_SECURITY_POLICY,
    PERMISSIONS_POLICY,
    SecurityHeadersMiddleware,
)


def _app() -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None)
    install_exception_handlers(app)
    app.add_middleware(SecurityHeadersMiddleware)

    @app.get("/", response_class=HTMLResponse)
    def frontend() -> str:
        return "<html><body>frontend</body></html>"

    @app.get("/docs", response_class=HTMLResponse)
    def docs() -> str:
        return "<html><body>docs</body></html>"

    @app.get("/json")
    def json_response() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/boom")
    def boom() -> None:
        raise RuntimeError("internal-secret")

    return app


def test_browser_security_headers_apply_to_all_http_responses():
    with TestClient(_app()) as client:
        for path in ("/", "/docs", "/json"):
            response = client.get(path)
            assert response.headers["referrer-policy"] == "no-referrer"
            assert response.headers["x-content-type-options"] == "nosniff"
            assert response.headers["x-frame-options"] == "DENY"
            assert response.headers["permissions-policy"] == PERMISSIONS_POLICY


def test_csp_applies_only_to_frontend_html_and_keeps_docs_compatible():
    with TestClient(_app()) as client:
        frontend = client.get("/")
        docs = client.get("/docs")
        json_response = client.get("/json")

    assert frontend.headers["content-security-policy"] == CONTENT_SECURITY_POLICY
    assert "content-security-policy" not in docs.headers
    assert "content-security-policy" not in json_response.headers
    assert "script-src 'self'" in CONTENT_SECURITY_POLICY
    assert "style-src 'self' 'unsafe-inline'" in CONTENT_SECURITY_POLICY
    assert "connect-src 'self'" in CONTENT_SECURITY_POLICY


def test_unhandled_server_errors_keep_global_security_headers():
    with TestClient(_app(), raise_server_exceptions=False) as client:
        response = client.get("/boom")

    assert response.status_code == 500
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["permissions-policy"] == PERMISSIONS_POLICY
    assert "internal-secret" not in response.text
