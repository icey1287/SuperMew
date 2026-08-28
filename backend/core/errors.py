from __future__ import annotations

import json
import logging
import math
import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Mapping

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from backend.security.headers import browser_hardening_headers


logger = logging.getLogger(__name__)


class ErrorCode(StrEnum):
    INVALID_REQUEST = "INVALID_REQUEST"
    AUTHENTICATION_REQUIRED = "AUTHENTICATION_REQUIRED"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    POLICY_DENIED = "POLICY_DENIED"
    NOT_FOUND = "NOT_FOUND"
    CONFLICT = "CONFLICT"
    RATE_LIMITED = "RATE_LIMITED"
    RATE_LIMIT_UNAVAILABLE = "RATE_LIMIT_UNAVAILABLE"
    MODEL_RATE_LIMITED = "MODEL_RATE_LIMITED"
    MODEL_TIMEOUT = "MODEL_TIMEOUT"
    MODEL_UNAVAILABLE = "MODEL_UNAVAILABLE"
    MODEL_CALL_LIMIT_EXCEEDED = "MODEL_CALL_LIMIT_EXCEEDED"
    EMBEDDING_TIMEOUT = "EMBEDDING_TIMEOUT"
    EMBEDDING_UNAVAILABLE = "EMBEDDING_UNAVAILABLE"
    VECTOR_STORE_TIMEOUT = "VECTOR_STORE_TIMEOUT"
    VECTOR_STORE_UNAVAILABLE = "VECTOR_STORE_UNAVAILABLE"
    RERANK_TIMEOUT = "RERANK_TIMEOUT"
    RERANK_UNAVAILABLE = "RERANK_UNAVAILABLE"
    TOOL_TIMEOUT = "TOOL_TIMEOUT"
    TOOL_UNAVAILABLE = "TOOL_UNAVAILABLE"
    WEB_TOOL_RESULT_CONTEXT_BUDGET_EXCEEDED = "WEB_TOOL_RESULT_CONTEXT_BUDGET_EXCEEDED"
    PROVIDER_TIMEOUT = "PROVIDER_TIMEOUT"
    UPLOAD_INVALID = "UPLOAD_INVALID"
    UPLOAD_TOO_LARGE = "UPLOAD_TOO_LARGE"
    DOCUMENT_PARSE_FAILED = "DOCUMENT_PARSE_FAILED"
    STORAGE_UNAVAILABLE = "STORAGE_UNAVAILABLE"
    RUN_ACTIVE = "RUN_ACTIVE"
    RUN_NOT_FOUND = "RUN_NOT_FOUND"
    RUN_STATE_CONFLICT = "RUN_STATE_CONFLICT"
    RUN_CANCELLED = "RUN_CANCELLED"
    RUN_INTERRUPTED = "RUN_INTERRUPTED"
    RUN_EXECUTION_FAILED = "RUN_EXECUTION_FAILED"
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
    THREAD_VERSION_CONFLICT = "THREAD_VERSION_CONFLICT"
    INTERNAL_ERROR = "INTERNAL_ERROR"


_CODE_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
_LABEL_PATTERN = re.compile(r"^[A-Za-z0-9_.:-]{1,80}$")
_PUBLIC_ERROR_FORMAT = "public_error_v1"


def _code_value(code: ErrorCode | str) -> str:
    value = code.value if isinstance(code, ErrorCode) else str(code)
    value = value.strip().upper()
    return value if _CODE_PATTERN.fullmatch(value) else ErrorCode.INTERNAL_ERROR.value


def _safe_label(value: Any) -> str | None:
    if value is None:
        return None
    label = str(value).strip()
    return label if _LABEL_PATTERN.fullmatch(label) else None


def _safe_retry_after(value: Any) -> float | None:
    if value is None:
        return None
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(seconds) or seconds < 0:
        return None
    return seconds


def _status_for_code(code: ErrorCode | str) -> int:
    value = _code_value(code)
    if value in {
        ErrorCode.RATE_LIMITED.value,
        ErrorCode.MODEL_RATE_LIMITED.value,
        "RERANK_RATE_LIMITED",
    }:
        return 429
    if value in {
        ErrorCode.MODEL_TIMEOUT.value,
        ErrorCode.EMBEDDING_TIMEOUT.value,
        ErrorCode.VECTOR_STORE_TIMEOUT.value,
        ErrorCode.RERANK_TIMEOUT.value,
        ErrorCode.TOOL_TIMEOUT.value,
        ErrorCode.PROVIDER_TIMEOUT.value,
        "PROVIDER_DEADLINE_EXCEEDED",
    }:
        return 504
    if value in {
        ErrorCode.MODEL_UNAVAILABLE.value,
        ErrorCode.EMBEDDING_UNAVAILABLE.value,
        ErrorCode.VECTOR_STORE_UNAVAILABLE.value,
        ErrorCode.RERANK_UNAVAILABLE.value,
        ErrorCode.TOOL_UNAVAILABLE.value,
        ErrorCode.STORAGE_UNAVAILABLE.value,
        ErrorCode.RATE_LIMIT_UNAVAILABLE.value,
        "PROVIDER_AUTHENTICATION_FAILED",
    }:
        return 503
    if value == "PROVIDER_REQUEST_INVALID":
        return 502
    if value == ErrorCode.WEB_TOOL_RESULT_CONTEXT_BUDGET_EXCEEDED.value:
        return 422
    if value == ErrorCode.MODEL_CALL_LIMIT_EXCEEDED.value:
        return 422
    if value in {ErrorCode.PERMISSION_DENIED.value, ErrorCode.POLICY_DENIED.value}:
        return 403
    return 500


@dataclass(frozen=True)
class PublicError:
    """Safe error contract shared by HTTP, Run persistence, and Provider adapters."""

    code: ErrorCode | str
    message: str
    status_code: int = 500
    retryable: bool = False
    category: str | None = None
    stage: str | None = None
    provider: str | None = None
    retry_after: float | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "code", _code_value(self.code))
        message = str(self.message or "").strip()
        object.__setattr__(
            self,
            "message",
            message or "服务暂时不可用，请稍后重试",
        )
        status = int(self.status_code or _status_for_code(self.code))
        object.__setattr__(self, "status_code", status if 400 <= status <= 599 else 500)
        object.__setattr__(self, "category", _safe_label(self.category))
        object.__setattr__(self, "stage", _safe_label(self.stage))
        object.__setattr__(self, "provider", _safe_label(self.provider))
        object.__setattr__(self, "retry_after", _safe_retry_after(self.retry_after))
        object.__setattr__(self, "details", dict(self.details or {}))

    @property
    def safe_details(self) -> dict[str, Any]:
        return self.details

    def contract(self) -> dict[str, Any]:
        return {
            "code": str(self.code),
            "message": self.message,
            "retryable": self.retryable,
            "category": self.category,
            "stage": self.stage,
            "provider": self.provider,
            "retry_after": self.retry_after,
        }


class AppError(Exception):
    def __init__(
        self,
        code: ErrorCode | str,
        message: str,
        *,
        status_code: int = 400,
        retryable: bool = False,
        safe_details: dict[str, Any] | None = None,
        category: str | None = None,
        stage: str | None = None,
        provider: str | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.public_error = PublicError(
            code=code,
            message=message,
            status_code=status_code,
            retryable=retryable,
            category=category,
            stage=stage,
            provider=provider,
            retry_after=retry_after,
            details=safe_details or {},
        )
        self.code = code
        self.message = self.public_error.message
        self.status_code = self.public_error.status_code
        self.retryable = self.public_error.retryable
        self.safe_details = self.public_error.details
        self.category = self.public_error.category
        self.stage = self.public_error.stage
        self.provider = self.public_error.provider
        self.retry_after = self.public_error.retry_after


def serialize_public_error(error: PublicError | AppError) -> str:
    public = (
        public_error_from_exception(error) if isinstance(error, AppError) else error
    )
    return json.dumps(
        {
            "format": _PUBLIC_ERROR_FORMAT,
            **public.contract(),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def deserialize_public_error(value: str | None) -> PublicError | None:
    if not value:
        return None
    try:
        payload = json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get("format") != _PUBLIC_ERROR_FORMAT:
        return None
    code = payload.get("code")
    message = payload.get("message")
    if not isinstance(code, str) or not isinstance(message, str):
        return None
    return PublicError(
        code=code,
        message=message,
        status_code=_status_for_code(code),
        retryable=bool(payload.get("retryable")),
        category=payload.get("category"),
        stage=payload.get("stage"),
        provider=payload.get("provider"),
        retry_after=payload.get("retry_after"),
    )


def error_payload(
    error: PublicError | AppError,
    request_id: str | None = None,
) -> dict[str, Any]:
    public = (
        public_error_from_exception(error) if isinstance(error, AppError) else error
    )
    return {
        "error": {
            **public.contract(),
            "request_id": request_id,
            "details": public.details,
        }
    }


def _public_error_from_provider(exc: Exception) -> PublicError | None:
    candidate: Any = getattr(exc, "public_error", None)
    if callable(candidate):
        candidate = candidate()
    if candidate is None:
        factory = getattr(exc, "to_public_error", None)
        if callable(factory):
            candidate = factory()
    if isinstance(candidate, AppError):
        candidate = candidate.public_error
    if isinstance(candidate, PublicError):
        exc_type = type(exc)
        is_provider_error = exc_type.__name__ in {
            "ProviderError",
            "ProviderFailure",
        } or exc_type.__module__.startswith("backend.providers")
        if not is_provider_error:
            return candidate
        operation = getattr(exc, "operation", None)
        stage = getattr(operation, "value", operation)
        details = candidate.details
        return PublicError(
            code=candidate.code,
            message=candidate.message,
            status_code=candidate.status_code,
            retryable=candidate.retryable,
            category=candidate.category or "provider",
            stage=candidate.stage or stage or details.get("operation"),
            provider=candidate.provider
            or getattr(exc, "provider", None)
            or details.get("provider"),
            retry_after=candidate.retry_after
            or getattr(exc, "retry_after_seconds", None)
            or details.get("retry_after_seconds"),
            details=details,
        )
    if isinstance(candidate, Mapping):
        code = candidate.get("code")
        safe_message = candidate.get("message") or candidate.get("safe_message")
        if code and safe_message:
            return PublicError(
                code=code,
                message=str(safe_message),
                status_code=int(
                    candidate.get("status_code") or _status_for_code(str(code))
                ),
                retryable=bool(candidate.get("retryable")),
                category=candidate.get("category"),
                stage=candidate.get("stage"),
                provider=candidate.get("provider"),
                retry_after=candidate.get("retry_after")
                or candidate.get("retry_after_seconds"),
            )

    exc_type = type(exc)
    is_provider_error = exc_type.__name__ in {"ProviderError", "ProviderFailure"} or (
        exc_type.__module__.startswith("backend.providers")
    )
    if not is_provider_error:
        return None
    code = getattr(exc, "code", None)
    safe_message = getattr(exc, "safe_message", None)
    if not code or not safe_message:
        return None
    return PublicError(
        code=code,
        message=str(safe_message),
        status_code=int(getattr(exc, "status_code", 0) or _status_for_code(code)),
        retryable=bool(getattr(exc, "retryable", False)),
        category=getattr(exc, "category", None),
        stage=getattr(exc, "stage", None),
        provider=getattr(exc, "provider", None),
        retry_after=getattr(exc, "retry_after", None)
        or getattr(exc, "retry_after_seconds", None),
    )


def public_error_from_exception(
    exc: Exception,
    *,
    fallback: PublicError | None = None,
) -> PublicError:
    provider_error = _public_error_from_provider(exc)
    if provider_error is not None:
        return provider_error

    if isinstance(exc, AppError):
        return exc.public_error

    exc_type = type(exc)
    if (
        exc_type.__name__ == "ModelCallLimitExceededError"
        and exc_type.__module__ == "langchain.agents.middleware.model_call_limit"
    ):
        return PublicError(
            ErrorCode.MODEL_CALL_LIMIT_EXCEEDED,
            "模型调用次数达到本次运行上限，请缩小问题范围后重试。",
            status_code=422,
            retryable=False,
            category="run",
            stage="model_budget",
        )

    if isinstance(exc, HTTPException):
        code = _http_error_code(exc.status_code)
        detail = (
            exc.detail
            if isinstance(exc.detail, str) and exc.status_code < 500
            else "请求失败"
        )
        return PublicError(code, detail, status_code=exc.status_code)

    return fallback or PublicError(
        ErrorCode.INTERNAL_ERROR,
        "服务暂时不可用，请稍后重试",
        status_code=500,
        retryable=True,
        category="internal",
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


def _response_headers(
    public: PublicError,
    headers: Mapping[str, str] | None = None,
) -> dict[str, str] | None:
    merged = dict(headers or {})
    if public.retry_after is not None:
        value = public.retry_after
        merged["Retry-After"] = str(int(value) if value.is_integer() else value)
    return merged or None


def install_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error_handler(request: Request, exc: AppError) -> JSONResponse:
        public = public_error_from_exception(exc)
        return JSONResponse(
            status_code=public.status_code,
            content=error_payload(public, getattr(request.state, "request_id", None)),
            headers=_response_headers(public),
        )

    @app.exception_handler(HTTPException)
    async def _http_error_handler(request: Request, exc: HTTPException) -> JSONResponse:
        public = public_error_from_exception(exc)
        return JSONResponse(
            status_code=public.status_code,
            content=error_payload(public, getattr(request.state, "request_id", None)),
            headers=_response_headers(public, exc.headers),
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
            headers=_response_headers(public, browser_hardening_headers()),
        )
