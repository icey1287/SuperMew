"""Request-owned Tool Adapter for the isolated Sandbox runtime."""

from __future__ import annotations

from typing import Protocol

from langchain_core.tools import BaseTool, tool

from backend.sandbox import (
    SandboxError,
    SandboxExecutionRequest,
    SandboxExecutionResult,
    SandboxIdentity,
    get_sandbox_runtime,
)
from backend.tools.contracts import ToolResultV1, new_tool_failure, new_tool_success


SANDBOX_METADATA_KEYS = frozenset(
    {"exit_code", "files_created", "output_bytes", "truncated"}
)


class SandboxRuntimeContext(Protocol):
    user_id: str
    tenant_id: str
    thread_id: str
    run_id: str | None
    request_context: object

    def check_deadline(self) -> None: ...


def _result(result: SandboxExecutionResult) -> ToolResultV1:
    return new_tool_success(
        data=result.to_public_dict(),
        duration_ms=result.duration_ms,
        observability_metadata={
            key: value
            for key, value in result.observability_metadata().items()
            if key in SANDBOX_METADATA_KEYS
        },
    )


def make_sandbox_execute(context: SandboxRuntimeContext) -> BaseTool:
    """Build a Run-owned Adapter without exposing identity or runtime controls."""

    @tool("sandbox_execute")
    def sandbox_execute(language: str, source: str) -> ToolResultV1:
        """Execute bounded Python or shell source in an approved isolated Sandbox."""

        context.check_deadline()
        run_id = context.run_id
        if not isinstance(run_id, str) or not run_id:
            return new_tool_failure(
                error_code="SANDBOX_INVALID_REQUEST",
                retryable=False,
            )
        provider_runtime = getattr(context.request_context, "provider_runtime", None)
        deadline_at, cancellation_probe = (
            provider_runtime() if callable(provider_runtime) else (None, None)
        )
        try:
            request = SandboxExecutionRequest(
                identity=SandboxIdentity(
                    user_id=context.user_id,
                    tenant_id=context.tenant_id,
                    thread_id=context.thread_id,
                    run_id=run_id,
                ),
                language=language,
                source=source,
            )
            return _result(
                get_sandbox_runtime().execute(
                    request,
                    deadline_at=deadline_at,
                    cancellation_probe=cancellation_probe,
                )
            )
        except SandboxError as exc:
            return new_tool_failure(
                error_code=exc.code,
                retryable=exc.retryable,
                observability_metadata={
                    key: value
                    for key, value in exc.safe_details.items()
                    if key in SANDBOX_METADATA_KEYS
                },
            )
        except (TypeError, ValueError):
            return new_tool_failure(
                error_code="SANDBOX_INVALID_REQUEST",
                retryable=False,
            )

    return sandbox_execute


__all__ = ["SANDBOX_METADATA_KEYS", "make_sandbox_execute"]
