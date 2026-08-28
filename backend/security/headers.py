"""Browser security response headers with a frontend-only CSP."""

from __future__ import annotations

from typing import Final

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send


CONTENT_SECURITY_POLICY: Final = "; ".join(
    (
        "default-src 'self'",
        "base-uri 'self'",
        "object-src 'none'",
        "frame-ancestors 'none'",
        "form-action 'self'",
        "script-src 'self'",
        "style-src 'self' 'unsafe-inline'",
        "img-src 'self' data: blob:",
        "font-src 'self' data:",
        "connect-src 'self'",
    )
)
PERMISSIONS_POLICY: Final = (
    "camera=(), geolocation=(), microphone=(), payment=(), usb=()"
)
_DOCS_PREFIXES: Final = ("/docs", "/redoc")


def browser_hardening_headers() -> dict[str, str]:
    return {
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
        "Permissions-Policy": PERMISSIONS_POLICY,
    }


def _is_frontend_html(*, path: str, headers: MutableHeaders) -> bool:
    content_type = headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type != "text/html":
        return False
    return not any(
        path == prefix or path.startswith(f"{prefix}/") for prefix in _DOCS_PREFIXES
    )


class SecurityHeadersMiddleware:
    """Attach stable browser hardening without breaking FastAPI's CDN docs."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path_value = scope.get("path", "/")
        path = path_value if isinstance(path_value, str) else "/"

        async def send_with_security_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in browser_hardening_headers().items():
                    headers[name] = value
                if _is_frontend_html(path=path, headers=headers):
                    headers["Content-Security-Policy"] = CONTENT_SECURITY_POLICY
            await send(message)

        await self.app(scope, receive, send_with_security_headers)


__all__ = [
    "CONTENT_SECURITY_POLICY",
    "PERMISSIONS_POLICY",
    "SecurityHeadersMiddleware",
    "browser_hardening_headers",
]
