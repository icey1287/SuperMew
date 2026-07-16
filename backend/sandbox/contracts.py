"""Pure contracts for isolated Sandbox execution.

Raw source text and runtime identities are intentionally excluded from reprs and
safe readiness metadata.  The Docker implementation is replaceable at the
``SandboxAdapter`` seam, while callers depend only on these bounded contracts.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Final


_SHA256_RE: Final = re.compile(r"[0-9a-f]{64}\Z")
_INVOCATION_ID_RE: Final = re.compile(r"sbx_[0-9a-f]{32}\Z")
_STABLE_ID_RE: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:@+-]{0,255}\Z")
_IMAGE_DIGEST_RE: Final = re.compile(
    r"(?:"
    r"sha256:[0-9a-f]{64}"
    r"|"
    r"[a-z0-9]+(?:[._-][a-z0-9]+)*(?::[0-9]{1,5})?"
    r"(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)*"
    r"@sha256:[0-9a-f]{64}"
    r")\Z"
)
_SAFE_DETAIL_KEYS: Final = frozenset(
    {"attempts", "exit_code", "files_created", "output_bytes", "stage", "truncated"}
)
_SAFE_DETAIL_TEXT_RE: Final = re.compile(r"[a-z][a-z0-9_.:-]{0,63}\Z")


def _canonical_hash(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def _positive_int(
    value: int,
    *,
    field_name: str,
    minimum: int = 1,
    maximum: int | None = None,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{field_name} must be an integer")
    if value < minimum or (maximum is not None and value > maximum):
        upper = "" if maximum is None else f" and at most {maximum}"
        raise ValueError(f"{field_name} must be at least {minimum}{upper}")
    return value


def _positive_float(
    value: float,
    *,
    field_name: str,
    minimum: float,
    maximum: float,
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{field_name} must be a number")
    normalized = float(value)
    if not math.isfinite(normalized) or not minimum <= normalized <= maximum:
        raise ValueError(
            f"{field_name} must be finite and between {minimum} and {maximum}"
        )
    return normalized


def _bounded_context_text(value: str, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    normalized = value.strip()
    if (
        not normalized
        or len(normalized.encode("utf-8", errors="replace")) > 512
        or any(
            ord(character) < 0x20 or ord(character) == 0x7F for character in normalized
        )
    ):
        raise ValueError(f"{field_name} must be bounded context text")
    return normalized


def validate_image_digest(value: str) -> str:
    """Validate an immutable local image reference.

    Tags are deliberately rejected.  Enabling the Sandbox must never trigger a
    network pull or silently move to a different image.
    """

    if not isinstance(value, str):
        raise TypeError("image must be a string")
    normalized = value.strip()
    if _IMAGE_DIGEST_RE.fullmatch(normalized) is None:
        raise ValueError("image must be an immutable digest reference")
    return normalized


class SandboxLanguage(StrEnum):
    PYTHON = "python"
    SH = "sh"


class SandboxErrorCode(StrEnum):
    DISABLED = "SANDBOX_DISABLED"
    NOT_CONFIGURED = "SANDBOX_NOT_CONFIGURED"
    NOT_READY = "SANDBOX_NOT_READY"
    CLOSED = "SANDBOX_CLOSED"
    INVALID_REQUEST = "SANDBOX_INVALID_REQUEST"
    BUSY = "SANDBOX_BUSY"
    CANCELLED = "SANDBOX_CANCELLED"
    TIMEOUT = "SANDBOX_TIMEOUT"
    OUTPUT_LIMIT = "SANDBOX_OUTPUT_LIMIT"
    MEMORY_LIMIT = "SANDBOX_MEMORY_LIMIT"
    PROCESS_LIMIT = "SANDBOX_PROCESS_LIMIT"
    DISK_LIMIT = "SANDBOX_DISK_LIMIT"
    FILE_LIMIT = "SANDBOX_FILE_LIMIT"
    UNSAFE_FILE = "SANDBOX_UNSAFE_FILE"
    ADAPTER_UNAVAILABLE = "SANDBOX_ADAPTER_UNAVAILABLE"
    IMAGE_UNAVAILABLE = "SANDBOX_IMAGE_UNAVAILABLE"
    PROTOCOL_ERROR = "SANDBOX_PROTOCOL_ERROR"
    EXECUTION_FAILED = "SANDBOX_EXECUTION_FAILED"
    CLEANUP_FAILED = "SANDBOX_CLEANUP_FAILED"


class SandboxError(RuntimeError):
    """Stable failure that never embeds source, paths, or Docker diagnostics."""

    def __init__(
        self,
        code: SandboxErrorCode | str,
        *,
        retryable: bool = False,
        safe_details: Mapping[str, str | int | bool] | None = None,
    ) -> None:
        self.code = SandboxErrorCode(code).value
        self.retryable = bool(retryable)
        details = dict(safe_details or {})
        if set(details).difference(_SAFE_DETAIL_KEYS):
            raise ValueError("safe_details contains an unknown key")
        if any(
            not isinstance(key, str)
            or not isinstance(value, (str, int, bool))
            or len(key) > 64
            or (
                isinstance(value, str) and _SAFE_DETAIL_TEXT_RE.fullmatch(value) is None
            )
            or (
                isinstance(value, int)
                and not isinstance(value, bool)
                and not -(2**31) <= value < 2**31
            )
            for key, value in details.items()
        ):
            raise ValueError("safe_details must contain bounded scalar metadata")
        self.safe_details = MappingProxyType(details)
        super().__init__(self.code)


@dataclass(frozen=True, slots=True)
class SandboxLimits:
    timeout_seconds: float = 15.0
    cpu_count: float = 0.5
    memory_bytes: int = 256 * 1024 * 1024
    pids_limit: int = 32
    workspace_bytes: int = 64 * 1024 * 1024
    max_source_bytes: int = 64 * 1024
    max_output_bytes: int = 64 * 1024
    max_files: int = 32
    max_file_bytes: int = 8 * 1024 * 1024
    max_total_file_bytes: int = 32 * 1024 * 1024
    max_path_bytes: int = 240
    max_path_depth: int = 8
    cleanup_timeout_seconds: float = 3.0

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "timeout_seconds",
            _positive_float(
                self.timeout_seconds,
                field_name="timeout_seconds",
                minimum=0.1,
                maximum=600.0,
            ),
        )
        object.__setattr__(
            self,
            "cpu_count",
            _positive_float(
                self.cpu_count,
                field_name="cpu_count",
                minimum=0.05,
                maximum=8.0,
            ),
        )
        object.__setattr__(
            self,
            "cleanup_timeout_seconds",
            _positive_float(
                self.cleanup_timeout_seconds,
                field_name="cleanup_timeout_seconds",
                minimum=0.1,
                maximum=30.0,
            ),
        )
        for field_name, minimum, maximum in (
            ("memory_bytes", 32 * 1024 * 1024, 8 * 1024 * 1024 * 1024),
            ("pids_limit", 4, 512),
            ("workspace_bytes", 1024 * 1024, 1024 * 1024 * 1024),
            ("max_source_bytes", 1, 4 * 1024 * 1024),
            ("max_output_bytes", 1, 16 * 1024 * 1024),
            ("max_files", 1, 4096),
            ("max_file_bytes", 1, 512 * 1024 * 1024),
            ("max_total_file_bytes", 1, 1024 * 1024 * 1024),
            ("max_path_bytes", 16, 4096),
            ("max_path_depth", 1, 64),
        ):
            _positive_int(
                getattr(self, field_name),
                field_name=field_name,
                minimum=minimum,
                maximum=maximum,
            )
        if self.max_file_bytes > self.max_total_file_bytes:
            raise ValueError("max_file_bytes cannot exceed max_total_file_bytes")
        if self.max_total_file_bytes > self.workspace_bytes:
            raise ValueError("max_total_file_bytes cannot exceed workspace_bytes")
        if self.max_source_bytes >= self.workspace_bytes:
            raise ValueError("max_source_bytes must be smaller than workspace_bytes")

    @property
    def protocol_output_bytes(self) -> int:
        # Base64 plus a bounded JSON envelope and Docker CLI diagnostics.
        return ((self.max_output_bytes + 2) // 3) * 4 + 64 * 1024

    def runner_payload(self) -> dict[str, int | float]:
        return {
            "timeout_seconds": self.timeout_seconds,
            "max_output_bytes": self.max_output_bytes,
            "max_files": self.max_files,
            "max_file_bytes": self.max_file_bytes,
            "max_total_file_bytes": self.max_total_file_bytes,
            "max_path_bytes": self.max_path_bytes,
            "max_path_depth": self.max_path_depth,
        }


@dataclass(frozen=True, slots=True, repr=False)
class SandboxIdentity:
    user_id: str
    tenant_id: str
    thread_id: str
    run_id: str

    def __post_init__(self) -> None:
        for field_name in ("user_id", "tenant_id", "thread_id", "run_id"):
            object.__setattr__(
                self,
                field_name,
                _bounded_context_text(getattr(self, field_name), field_name=field_name),
            )

    @property
    def binding_hash(self) -> str:
        return _canonical_hash(
            {
                "schema_version": 1,
                "user_id": self.user_id,
                "tenant_id": self.tenant_id,
                "thread_id": self.thread_id,
                "run_id": self.run_id,
            }
        )

    def __repr__(self) -> str:
        return "SandboxIdentity(bound=True)"


@dataclass(frozen=True, slots=True, repr=False)
class SandboxExecutionRequest:
    identity: SandboxIdentity
    language: SandboxLanguage | str
    source: str = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.identity, SandboxIdentity):
            raise TypeError("identity must be SandboxIdentity")
        object.__setattr__(self, "language", SandboxLanguage(self.language))
        if not isinstance(self.source, str):
            raise TypeError("source must be a string")
        if not self.source or "\x00" in self.source:
            raise ValueError("source must be non-empty text without NUL")
        try:
            self.source.encode("utf-8")
        except UnicodeEncodeError:
            raise ValueError("source must be valid UTF-8 text") from None

    def __repr__(self) -> str:
        return (
            "SandboxExecutionRequest("
            f"language={self.language.value!r}, source_redacted=True)"
        )


@dataclass(frozen=True, slots=True, repr=False)
class SandboxExecutionSpec:
    invocation_id: str
    identity_binding: str = field(repr=False)
    language: SandboxLanguage | str
    source: str = field(repr=False)
    image: str
    limits: SandboxLimits

    def __post_init__(self) -> None:
        if _INVOCATION_ID_RE.fullmatch(self.invocation_id) is None:
            raise ValueError("invocation_id must be an opaque Sandbox ID")
        if _SHA256_RE.fullmatch(self.identity_binding) is None:
            raise ValueError("identity_binding must be a SHA-256 digest")
        object.__setattr__(self, "language", SandboxLanguage(self.language))
        if not isinstance(self.source, str) or not self.source:
            raise ValueError("source must be non-empty text")
        object.__setattr__(self, "image", validate_image_digest(self.image))
        if not isinstance(self.limits, SandboxLimits):
            raise TypeError("limits must be SandboxLimits")

    def __repr__(self) -> str:
        return (
            "SandboxExecutionSpec("
            f"invocation_id={self.invocation_id!r}, "
            f"language={self.language.value!r}, source_redacted=True)"
        )


@dataclass(frozen=True, slots=True, repr=False)
class SandboxExecutionResult:
    exit_code: int
    stdout: str = field(repr=False)
    stderr: str = field(repr=False)
    duration_ms: int
    files_created: int = 0
    stdout_truncated: bool = False
    stderr_truncated: bool = False

    def __post_init__(self) -> None:
        if isinstance(self.exit_code, bool) or not isinstance(self.exit_code, int):
            raise TypeError("exit_code must be an integer")
        if not -255 <= self.exit_code <= 255:
            raise ValueError("exit_code must be a bounded process status")
        if not isinstance(self.stdout, str) or not isinstance(self.stderr, str):
            raise TypeError("stdout and stderr must be strings")
        _positive_int(
            self.duration_ms,
            field_name="duration_ms",
            minimum=0,
            maximum=3_600_000,
        )
        _positive_int(
            self.files_created,
            field_name="files_created",
            minimum=0,
            maximum=4_096,
        )
        if not isinstance(self.stdout_truncated, bool) or not isinstance(
            self.stderr_truncated, bool
        ):
            raise TypeError("truncation fields must be booleans")
        try:
            stdout_bytes = self.stdout.encode("utf-8")
            stderr_bytes = self.stderr.encode("utf-8")
        except UnicodeEncodeError:
            raise ValueError("Sandbox output must be valid UTF-8") from None
        if len(stdout_bytes) > 16 * 1024 * 1024 or len(stderr_bytes) > 16 * 1024 * 1024:
            raise ValueError("Sandbox output must be bounded")

    @property
    def output_bytes(self) -> int:
        return len(self.stdout.encode("utf-8")) + len(self.stderr.encode("utf-8"))

    def to_public_dict(self) -> dict[str, object]:
        return {
            "exit_code": self.exit_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "files_created": self.files_created,
            "stdout_truncated": self.stdout_truncated,
            "stderr_truncated": self.stderr_truncated,
        }

    def observability_metadata(self) -> dict[str, object]:
        return {
            "exit_code": self.exit_code,
            "output_bytes": self.output_bytes,
            "files_created": self.files_created,
            "truncated": self.stdout_truncated or self.stderr_truncated,
        }

    def __repr__(self) -> str:
        return (
            "SandboxExecutionResult("
            f"exit_code={self.exit_code!r}, duration_ms={self.duration_ms!r}, "
            f"files_created={self.files_created!r}, output_redacted=True)"
        )


@dataclass(frozen=True, slots=True)
class SandboxReadiness:
    enabled: bool
    started: bool
    closed: bool
    ready: bool
    adapter: str
    daemon_reachable: bool
    image_available: bool
    active_executions: int

    def __post_init__(self) -> None:
        for field_name in (
            "enabled",
            "started",
            "closed",
            "ready",
            "daemon_reachable",
            "image_available",
        ):
            if not isinstance(getattr(self, field_name), bool):
                raise TypeError(f"{field_name} must be a bool")
        if (
            not isinstance(self.adapter, str)
            or _STABLE_ID_RE.fullmatch(self.adapter) is None
        ):
            raise ValueError("adapter must be a stable identifier")
        _positive_int(
            self.active_executions,
            field_name="active_executions",
            minimum=0,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "enabled": self.enabled,
            "started": self.started,
            "closed": self.closed,
            "ready": self.ready,
            "adapter": self.adapter,
            "daemon_reachable": self.daemon_reachable,
            "image_available": self.image_available,
            "active_executions": self.active_executions,
        }


__all__ = [
    "SandboxError",
    "SandboxErrorCode",
    "SandboxExecutionRequest",
    "SandboxExecutionResult",
    "SandboxExecutionSpec",
    "SandboxIdentity",
    "SandboxLanguage",
    "SandboxLimits",
    "SandboxReadiness",
    "validate_image_digest",
]
