from __future__ import annotations

import logging
import re
from enum import StrEnum
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse


logger = logging.getLogger(__name__)


class ErrorCode(StrEnum):
    INVALID_REQUEST = "INVALID_REQUEST"
    AUTHENTICATION_REQUIRED = "AUTHENTICATION_REQUIRED"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    NOT_FOUND = "NOT_FOUND"
    CONFLICT = "CONFLICT"
    RATE_LIMITED = "RATE_LIMITED"
    MODEL_RATE_LIMITED = "MODEL_RATE_LIMITED"
    MODEL_UNAVAILABLE = "MODEL_UNAVAILABLE"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class AppError(Exception):
    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        status_code: int = 400,
        retryable: bool = False,
        safe_details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.retryable = retryable
        self.safe_details = safe_details or {}


def error_payload(error: AppError, request_id: str | None = None) -> dict[str, Any]:
    return {
        "error": {
            "code": error.code.value,
            "message": error.message,
            "retryable": error.retryable,
            "request_id": request_id,
            "details": error.safe_details,
        }
    }


def public_error_from_exception(exc: Exception) -> AppError:
    if isinstance(exc, AppError):
        return exc
    if isinstance(exc, HTTPException):
        detail = exc.detail if isinstance(exc.detail, str) else "请求失败"
        code = _http_error_code(exc.status_code)
        return AppError(code, detail, status_code=exc.status_code)

    message = str(exc)
    status_match = re.search(
        r"(?:Error code|status(?:_code)?)[=: ]+(\d{3})", message, re.I
    )
    if status_match:
        upstream_status = int(status_match.group(1))
        if upstream_status == 429:
            return AppError(
                ErrorCode.MODEL_RATE_LIMITED,
                "上游模型服务当前繁忙，请稍后重试",
                status_code=429,
                retryable=True,
            )
        if upstream_status in {401, 403}:
            return AppError(
                ErrorCode.MODEL_UNAVAILABLE,
                "模型服务配置不可用，请联系管理员",
                status_code=503,
                retryable=False,
            )

    return AppError(
        ErrorCode.INTERNAL_ERROR,
        "服务暂时不可用，请稍后重试",
        status_code=500,
        retryable=True,
    )


def _http_error_code(status_code: int) -> ErrorCode:
    if status_code == 401:
        return ErrorCode.AUTHENTICATION_REQUIRED
    if status_code == 403:
        return ErrorCode.PERMISSION_DENIED
    if status_code == 404:
        return ErrorCode.NOT_FOUND
    if status_code == 409:
        return ErrorCode.CONFLICT
    if status_code == 429:
        return ErrorCode.RATE_LIMITED
    if 400 <= status_code < 500:
        return ErrorCode.INVALID_REQUEST
    return ErrorCode.INTERNAL_ERROR


def install_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error_handler(request: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=error_payload(exc, getattr(request.state, "request_id", None)),
        )

    @app.exception_handler(HTTPException)
    async def _http_error_handler(request: Request, exc: HTTPException) -> JSONResponse:
        public = public_error_from_exception(exc)
        return JSONResponse(
            status_code=public.status_code,
            content=error_payload(public, getattr(request.state, "request_id", None)),
            headers=exc.headers,
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error_handler(
        request: Request,
        exc: RequestValidationError,
    ) -> JSONResponse:
        error = AppError(
            ErrorCode.INVALID_REQUEST,
            "请求参数校验失败",
            status_code=422,
            safe_details={
                "fields": [
                    ".".join(str(part) for part in item["loc"]) for item in exc.errors()
                ]
            },
        )
        return JSONResponse(
            status_code=422,
            content=error_payload(error, getattr(request.state, "request_id", None)),
        )

    @app.exception_handler(Exception)
    async def _unhandled_error_handler(
        request: Request, exc: Exception
    ) -> JSONResponse:
        request_id = getattr(request.state, "request_id", None)
        logger.exception(
            "Unhandled request error request_id=%s", request_id, exc_info=exc
        )
        public = public_error_from_exception(exc)
        return JSONResponse(
            status_code=public.status_code,
            content=error_payload(public, request_id),
        )
