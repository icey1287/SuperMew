from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import unquote, urlsplit

from pydantic import BaseModel, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from backend.sandbox.contracts import validate_image_digest
from backend.security.origins import canonical_http_origin


PROJECT_ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = PROJECT_ROOT / ".env"


class _EnvSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ENV_FILE,
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )


class ApplicationSettings(_EnvSettings):
    config_version: int = Field(default=1, validation_alias="CONFIG_VERSION")
    environment: Literal["development", "test", "production"] = Field(
        default="development",
        validation_alias="APP_ENV",
    )
    host: str = Field(default="0.0.0.0", validation_alias="HOST")
    port: int = Field(default=8000, validation_alias="PORT")
    default_tenant_id: str = Field(
        default="default",
        pattern=r"^[a-z][a-z0-9_.:-]{0,63}$",
        validation_alias="DEFAULT_TENANT_ID",
    )


class ModelSettings(_EnvSettings):
    api_key: SecretStr = Field(default=SecretStr(""), validation_alias="ARK_API_KEY")
    base_url: str = Field(default="", validation_alias="BASE_URL")
    answer_model: str = Field(default="", validation_alias="MODEL")
    fast_model: str = Field(default="", validation_alias="FAST_MODEL")
    grade_model: str = Field(default="", validation_alias="GRADE_MODEL")
    evaluation_model: str = Field(
        default="",
        validation_alias="EVALUATION_MODEL",
    )
    timeout_seconds: float = Field(
        default=30.0,
        gt=0,
        le=600,
        validation_alias="MODEL_TIMEOUT_SECONDS",
    )


class RagSettings(_EnvSettings):
    retrieval_top_k: int = Field(
        default=8, ge=1, le=100, validation_alias="RETRIEVAL_TOP_K"
    )
    retrieval_candidate_k: int = Field(
        default=30,
        ge=1,
        le=500,
        validation_alias="RETRIEVAL_CANDIDATE_K",
    )
    max_subqueries: int = Field(
        default=4, ge=1, le=8, validation_alias="RAG_MAX_SUBQUERIES"
    )
    max_concurrent_subqueries: int = Field(
        default=2,
        ge=1,
        le=8,
        validation_alias="RAG_MAX_CONCURRENT_SUBQUERIES",
    )
    max_context_tokens: int = Field(
        default=12000,
        ge=512,
        validation_alias="RAG_MAX_CONTEXT_TOKENS",
    )


class EmbeddingSettings(_EnvSettings):
    model: str = Field(default="BAAI/bge-m3", validation_alias="EMBEDDING_MODEL")
    revision: str = Field(
        default="5617a9f61b028005a4858fdac845db406aefb181",
        validation_alias="EMBEDDING_MODEL_REVISION",
    )
    device: str = Field(default="cpu", validation_alias="EMBEDDING_DEVICE")
    dimension: int = Field(
        default=1024,
        ge=1,
        validation_alias="DENSE_EMBEDDING_DIM",
    )
    timeout_seconds: float = Field(
        default=15.0,
        gt=0,
        le=600,
        validation_alias="EMBEDDING_TIMEOUT_SECONDS",
    )
    executor_workers: int = Field(
        default=1,
        ge=1,
        le=8,
        validation_alias="EMBEDDING_EXECUTOR_WORKERS",
    )
    max_concurrency: int = Field(
        default=1,
        ge=1,
        le=32,
        validation_alias="EMBEDDING_MAX_CONCURRENCY",
    )
    query_microbatch_ms: float = Field(
        default=20.0,
        ge=0,
        le=1000,
        validation_alias="EMBEDDING_QUERY_MICROBATCH_MS",
    )
    query_max_batch_size: int = Field(
        default=16,
        ge=1,
        le=256,
        validation_alias="EMBEDDING_QUERY_MAX_BATCH_SIZE",
    )
    query_queue_size: int = Field(
        default=128,
        ge=1,
        le=10000,
        validation_alias="EMBEDDING_QUERY_QUEUE_SIZE",
    )
    query_cache_size: int = Field(
        default=1024,
        ge=0,
        le=100000,
        validation_alias="EMBEDDING_QUERY_CACHE_SIZE",
    )
    cache_namespace: str = Field(
        default="default",
        min_length=1,
        max_length=128,
        validation_alias="EMBEDDING_CACHE_NAMESPACE",
    )
    warmup_on_start: bool = Field(
        default=True,
        validation_alias="EMBEDDING_WARMUP_ON_START",
    )


def _is_placeholder(value: str) -> bool:
    normalized = str(value or "").strip().lower()
    return (
        not normalized
        or normalized.startswith(("your_", "your-", "replace-with"))
        or any(marker in normalized for marker in ("your-rerank", "your_rerank"))
    )


class RerankSettings(_EnvSettings):
    model: str = Field(default="", validation_alias="RERANK_MODEL")
    binding_host: str = Field(default="", validation_alias="RERANK_BINDING_HOST")
    api_key: SecretStr = Field(
        default=SecretStr(""),
        validation_alias="RERANK_API_KEY",
    )
    timeout_seconds: float = Field(
        default=5.0,
        gt=0,
        le=120,
        validation_alias="RERANK_TIMEOUT_SECONDS",
    )
    min_score: float = Field(
        default=0.0,
        allow_inf_nan=False,
        validation_alias="RERANK_MIN_SCORE",
    )
    max_concurrency: int = Field(
        default=4,
        ge=1,
        le=128,
        validation_alias="RERANK_MAX_CONCURRENCY",
    )
    max_connections: int = Field(
        default=20,
        ge=1,
        le=1000,
        validation_alias="RERANK_MAX_CONNECTIONS",
    )
    max_keepalive_connections: int = Field(
        default=10,
        ge=0,
        le=1000,
        validation_alias="RERANK_MAX_KEEPALIVE_CONNECTIONS",
    )
    candidate_limit: int = Field(
        default=30,
        ge=1,
        le=500,
        validation_alias="RERANK_CANDIDATE_LIMIT",
    )
    max_document_characters: int = Field(
        default=8000,
        ge=1,
        validation_alias="RERANK_MAX_DOCUMENT_CHARACTERS",
    )
    max_total_characters: int = Field(
        default=60000,
        ge=1,
        validation_alias="RERANK_MAX_TOTAL_CHARACTERS",
    )
    circuit_failure_threshold: int = Field(
        default=5,
        ge=1,
        le=100,
        validation_alias="RERANK_CIRCUIT_FAILURE_THRESHOLD",
    )
    circuit_reset_seconds: float = Field(
        default=30.0,
        gt=0,
        le=3600,
        validation_alias="RERANK_CIRCUIT_RESET_SECONDS",
    )

    @property
    def enabled(self) -> bool:
        return not any(
            _is_placeholder(value)
            for value in (
                self.model,
                self.binding_host,
                self.api_key.get_secret_value(),
            )
        )

    @property
    def endpoint(self) -> str:
        if not self.enabled:
            return ""
        host = self.binding_host.strip().rstrip("/")
        return host if host.endswith("/v1/rerank") else f"{host}/v1/rerank"


class RunSettings(_EnvSettings):
    default_deadline_seconds: float = Field(
        default=120.0,
        ge=1.0,
        validation_alias="RUN_DEADLINE_SECONDS",
    )
    event_queue_size: int = Field(
        default=256, ge=16, validation_alias="RUN_EVENT_QUEUE_SIZE"
    )
    heartbeat_seconds: float = Field(
        default=15.0,
        ge=1.0,
        validation_alias="RUN_HEARTBEAT_SECONDS",
    )
    disconnect_policy: Literal["cancel", "continue"] = Field(
        default="continue",
        validation_alias="RUN_ON_DISCONNECT",
    )
    multitask_strategy: Literal["reject", "enqueue", "cancel_previous"] = Field(
        default="reject",
        validation_alias="RUN_MULTITASK_STRATEGY",
    )
    event_poll_interval_seconds: float = Field(
        default=0.25,
        ge=0.05,
        validation_alias="RUN_EVENT_POLL_INTERVAL_SECONDS",
    )
    redis_stream_maxlen: int = Field(
        default=10000,
        ge=100,
        validation_alias="RUN_EVENT_STREAM_MAXLEN",
    )
    outbox_batch_size: int = Field(
        default=100,
        ge=1,
        le=1000,
        validation_alias="OUTBOX_BATCH_SIZE",
    )
    cancellation_ttl_seconds: int = Field(
        default=3600,
        ge=60,
        validation_alias="RUN_CANCELLATION_TTL_SECONDS",
    )
    cancellation_wait_seconds: float = Field(
        default=0.2,
        ge=0.01,
        le=5.0,
        validation_alias="RUN_CANCELLATION_WAIT_SECONDS",
    )


class AgentSettings(_EnvSettings):
    recursion_limit: int = Field(
        default=32,
        ge=2,
        le=100,
        validation_alias="AGENT_RECURSION_LIMIT",
    )
    max_model_calls: int = Field(
        default=4,
        ge=1,
        le=100,
        validation_alias="AGENT_MAX_MODEL_CALLS",
    )
    max_tool_calls: int = Field(
        default=6,
        ge=1,
        le=100,
        validation_alias="AGENT_MAX_TOOL_CALLS",
    )
    max_repeated_tool_calls: int = Field(
        default=2,
        ge=1,
        le=10,
        validation_alias="AGENT_MAX_REPEATED_TOOL_CALLS",
    )
    max_context_tokens: int = Field(
        default=12000,
        ge=1024,
        validation_alias="AGENT_MAX_CONTEXT_TOKENS",
    )
    response_reserve_tokens: int = Field(
        default=2048,
        ge=256,
        validation_alias="AGENT_RESPONSE_RESERVE_TOKENS",
    )
    memory_message_threshold: int = Field(
        default=6,
        ge=2,
        le=100,
        validation_alias="AGENT_MEMORY_MESSAGE_THRESHOLD",
    )

    @property
    def minimum_recursion_limit(self) -> int:
        tool_rounds = min(self.max_tool_calls, max(self.max_model_calls - 1, 0))
        return 8 + (tool_rounds * 5)

    @property
    def input_token_budget(self) -> int:
        return self.max_context_tokens - self.response_reserve_tokens


class SecuritySettings(_EnvSettings):
    jwt_secret_key: SecretStr = Field(
        default=SecretStr(""),
        validation_alias="JWT_SECRET_KEY",
    )
    jwt_algorithm: str = Field(default="HS256", validation_alias="JWT_ALGORITHM")
    access_token_expire_minutes: int = Field(
        default=30,
        ge=1,
        validation_alias="JWT_EXPIRE_MINUTES",
    )
    refresh_token_expire_days: int = Field(
        default=14,
        ge=1,
        validation_alias="JWT_REFRESH_EXPIRE_DAYS",
    )
    refresh_token_retention_days: int = Field(
        default=30,
        ge=1,
        le=3650,
        validation_alias="AUTH_REFRESH_LEDGER_RETENTION_DAYS",
    )
    refresh_cookie_name: str = Field(
        default="supermew_refresh",
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$",
        validation_alias="AUTH_REFRESH_COOKIE_NAME",
    )
    refresh_cookie_secure: bool = Field(
        default=False,
        validation_alias="AUTH_REFRESH_COOKIE_SECURE",
    )
    refresh_cookie_samesite: Literal["lax", "strict", "none"] = Field(
        default="lax",
        validation_alias="AUTH_REFRESH_COOKIE_SAMESITE",
    )
    admin_invite_code: SecretStr = Field(
        default=SecretStr(""),
        validation_alias="ADMIN_INVITE_CODE",
    )
    password_pbkdf2_rounds: int = Field(
        default=310000,
        ge=200000,
        validation_alias="PASSWORD_PBKDF2_ROUNDS",
    )
    cors_origins_raw: str = Field(
        default="http://localhost:3000,http://127.0.0.1:3000",
        validation_alias="CORS_ORIGINS",
    )
    cors_allow_credentials: bool = Field(
        default=True,
        validation_alias="CORS_ALLOW_CREDENTIALS",
    )

    @property
    def cors_origins(self) -> list[str]:
        return list(
            dict.fromkeys(
                canonical_http_origin(origin) or origin.strip()
                for origin in self.cors_origins_raw.split(",")
                if origin.strip()
            )
        )


class RateLimitSettings(_EnvSettings):
    enabled: bool = Field(default=True, validation_alias="RATE_LIMIT_ENABLED")
    backend: Literal["memory", "redis"] = Field(
        default="memory",
        validation_alias="RATE_LIMIT_BACKEND",
    )
    identity_hmac_key: SecretStr = Field(
        default=SecretStr(""),
        validation_alias="RATE_LIMIT_HMAC_KEY",
    )
    key_prefix: str = Field(
        default="supermew",
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$",
        validation_alias="RATE_LIMIT_KEY_PREFIX",
    )


class StorageSettings(_EnvSettings):
    database_url: SecretStr = Field(
        default=SecretStr(
            "postgresql+psycopg2://postgres:postgres@localhost:5432/langchain_app"
        ),
        validation_alias="DATABASE_URL",
    )
    redis_url: SecretStr = Field(
        default=SecretStr("redis://localhost:6379/0"),
        validation_alias="REDIS_URL",
    )
    redis_key_prefix: str = Field(
        default="supermew", validation_alias="REDIS_KEY_PREFIX"
    )
    upload_dir: Path = Field(
        default=PROJECT_ROOT / "data" / "documents",
        validation_alias="UPLOAD_DIR",
    )
    max_upload_bytes: int = Field(
        default=50 * 1024 * 1024,
        ge=1024,
        validation_alias="MAX_UPLOAD_BYTES",
    )
    max_document_pages: int = Field(
        default=2000,
        ge=1,
        validation_alias="MAX_DOCUMENT_PAGES",
    )
    max_page_characters: int = Field(
        default=200000,
        ge=1000,
        validation_alias="MAX_PAGE_CHARACTERS",
    )
    parser_timeout_seconds: float = Field(
        default=120.0,
        ge=1.0,
        validation_alias="PARSER_TIMEOUT_SECONDS",
    )
    max_archive_entries: int = Field(
        default=5000,
        ge=1,
        validation_alias="MAX_ARCHIVE_ENTRIES",
    )
    max_uncompressed_bytes: int = Field(
        default=250 * 1024 * 1024,
        ge=1024,
        validation_alias="MAX_UNCOMPRESSED_BYTES",
    )
    max_compression_ratio: float = Field(
        default=100.0,
        ge=1.0,
        validation_alias="MAX_COMPRESSION_RATIO",
    )

    @field_validator("upload_dir", mode="after")
    @classmethod
    def resolve_upload_dir(cls, value: Path) -> Path:
        path = value.expanduser()
        return path.resolve() if path.is_absolute() else (PROJECT_ROOT / path).resolve()


class WorkerSettings(_EnvSettings):
    worker_id: str = Field(default="", validation_alias="WORKER_ID")
    indexing_worker_id: str = Field(
        default="",
        validation_alias="INDEX_WORKER_ID",
    )
    max_concurrent_runs: int = Field(
        default=8,
        ge=1,
        le=128,
        validation_alias="RUN_WORKER_MAX_CONCURRENCY",
    )
    lease_seconds: int = Field(
        default=60, ge=10, validation_alias="WORKER_LEASE_SECONDS"
    )
    heartbeat_seconds: int = Field(
        default=15,
        ge=1,
        validation_alias="WORKER_HEARTBEAT_SECONDS",
    )
    max_attempts: int = Field(default=3, ge=1, validation_alias="WORKER_MAX_ATTEMPTS")
    indexing_worker_required: bool = Field(
        default=True,
        validation_alias="INDEX_WORKER_REQUIRED",
    )
    indexing_poll_seconds: float = Field(
        default=1.0,
        ge=0.05,
        validation_alias="INDEX_WORKER_POLL_SECONDS",
    )
    indexing_lease_seconds: int = Field(
        default=90,
        ge=10,
        validation_alias="INDEX_WORKER_LEASE_SECONDS",
    )
    indexing_heartbeat_seconds: int = Field(
        default=15,
        ge=1,
        validation_alias="INDEX_WORKER_HEARTBEAT_SECONDS",
    )
    indexing_retry_base_seconds: float = Field(
        default=5.0,
        ge=0.1,
        validation_alias="INDEX_WORKER_RETRY_BASE_SECONDS",
    )
    indexing_retry_max_seconds: float = Field(
        default=300.0,
        ge=1.0,
        validation_alias="INDEX_WORKER_RETRY_MAX_SECONDS",
    )
    indexing_retry_jitter_ratio: float = Field(
        default=0.2,
        ge=0.0,
        le=1.0,
        validation_alias="INDEX_WORKER_RETRY_JITTER_RATIO",
    )
    indexing_readiness_ttl_seconds: int = Field(
        default=45,
        ge=5,
        validation_alias="INDEX_WORKER_READINESS_TTL_SECONDS",
    )
    evaluation_worker_id: str = Field(
        default="",
        validation_alias="EVALUATION_WORKER_ID",
    )
    evaluation_poll_seconds: float = Field(
        default=1.0,
        ge=0.05,
        validation_alias="EVALUATION_WORKER_POLL_SECONDS",
    )
    evaluation_lease_seconds: int = Field(
        default=180,
        ge=30,
        validation_alias="EVALUATION_WORKER_LEASE_SECONDS",
    )
    evaluation_heartbeat_seconds: int = Field(
        default=15,
        ge=1,
        validation_alias="EVALUATION_WORKER_HEARTBEAT_SECONDS",
    )
    evaluation_case_timeout_seconds: float = Field(
        default=120.0,
        ge=5.0,
        le=1800.0,
        validation_alias="EVALUATION_CASE_TIMEOUT_SECONDS",
    )
    evaluation_max_attempts: int = Field(
        default=3,
        ge=1,
        le=20,
        validation_alias="EVALUATION_WORKER_MAX_ATTEMPTS",
    )


class ObservabilitySettings(_EnvSettings):
    log_level: str = Field(default="INFO", validation_alias="LOG_LEVEL")
    metrics_enabled: bool = Field(default=True, validation_alias="METRICS_ENABLED")
    trace_retention_days: int = Field(
        default=30,
        ge=1,
        validation_alias="TRACE_RETENTION_DAYS",
    )


class SkillSettings(_EnvSettings):
    skill_dir: Path = Field(
        default=PROJECT_ROOT / "skills",
        validation_alias="SKILL_DIR",
    )
    manifest_name: str = Field(
        default="skill.yaml",
        pattern=r"^[a-zA-Z0-9._-]{1,64}$",
        validation_alias="SKILL_MANIFEST_NAME",
    )
    max_content_bytes: int = Field(
        default=262_144,
        ge=1_024,
        le=4_194_304,
        validation_alias="SKILL_MAX_CONTENT_BYTES",
    )


class SandboxSettings(_EnvSettings):
    """Fail-closed configuration for the isolated Sandbox Module."""

    enabled: bool = Field(default=False, validation_alias="SANDBOX_ENABLED")
    adapter: Literal["docker", "disabled"] = Field(
        default="docker",
        validation_alias="SANDBOX_ADAPTER",
    )
    docker_image: str = Field(default="", validation_alias="SANDBOX_DOCKER_IMAGE")
    docker_binary: str = Field(
        default="docker",
        pattern=r"^[^\x00-\x20\x7f]{1,1024}$",
        validation_alias="SANDBOX_DOCKER_BINARY",
    )
    docker_host: str | None = Field(
        default=None,
        validation_alias="SANDBOX_DOCKER_HOST",
    )
    require_rootless: bool = Field(
        default=False,
        validation_alias="SANDBOX_REQUIRE_ROOTLESS",
    )
    max_concurrency: int = Field(
        default=2,
        ge=1,
        le=32,
        validation_alias="SANDBOX_MAX_CONCURRENCY",
    )
    timeout_seconds: float = Field(
        default=15.0,
        ge=0.1,
        le=600,
        allow_inf_nan=False,
        validation_alias="SANDBOX_TIMEOUT_SECONDS",
    )
    cpu_limit: float = Field(
        default=0.5,
        ge=0.05,
        le=8,
        allow_inf_nan=False,
        validation_alias="SANDBOX_CPU_LIMIT",
    )
    memory_bytes: int = Field(
        default=268_435_456,
        ge=33_554_432,
        le=8_589_934_592,
        validation_alias="SANDBOX_MEMORY_BYTES",
    )
    pids_limit: int = Field(
        default=32,
        ge=4,
        le=512,
        validation_alias="SANDBOX_PIDS_LIMIT",
    )
    workspace_bytes: int = Field(
        default=67_108_864,
        ge=1_048_576,
        le=1_073_741_824,
        validation_alias="SANDBOX_WORKSPACE_BYTES",
    )
    max_source_bytes: int = Field(
        default=65_536,
        ge=1,
        le=4_194_304,
        validation_alias="SANDBOX_MAX_SOURCE_BYTES",
    )
    max_output_bytes: int = Field(
        default=65_536,
        ge=1,
        le=16_777_216,
        validation_alias="SANDBOX_MAX_OUTPUT_BYTES",
    )
    max_files: int = Field(
        default=32,
        ge=1,
        le=4_096,
        validation_alias="SANDBOX_MAX_FILES",
    )
    max_file_bytes: int = Field(
        default=8_388_608,
        ge=1,
        le=536_870_912,
        validation_alias="SANDBOX_MAX_FILE_BYTES",
    )
    max_total_file_bytes: int = Field(
        default=33_554_432,
        ge=1,
        le=1_073_741_824,
        validation_alias="SANDBOX_MAX_TOTAL_FILE_BYTES",
    )
    max_path_bytes: int = Field(
        default=240,
        ge=16,
        le=4_096,
        validation_alias="SANDBOX_MAX_PATH_BYTES",
    )
    max_path_depth: int = Field(
        default=8,
        ge=1,
        le=64,
        validation_alias="SANDBOX_MAX_PATH_DEPTH",
    )
    cleanup_timeout_seconds: float = Field(
        default=3.0,
        ge=0.1,
        le=30,
        allow_inf_nan=False,
        validation_alias="SANDBOX_CLEANUP_TIMEOUT_SECONDS",
    )

    @field_validator("docker_image")
    @classmethod
    def validate_docker_image(cls, value: str) -> str:
        normalized = value.strip()
        return validate_image_digest(normalized) if normalized else ""

    @field_validator("docker_host")
    @classmethod
    def validate_docker_host(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        normalized = value.strip()
        if not normalized.startswith("unix://") or len(normalized) > 2_048:
            raise ValueError("SANDBOX_DOCKER_HOST 只能使用本地 Unix endpoint")
        return normalized


_POSTGRES_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]{0,62}$")
_QUALIFIED_TABLE = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_$]{0,62}\."
    r"(?:[A-Za-z_][A-Za-z0-9_$]{0,62}|\*)$"
)
_QUALIFIED_COLUMN = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_$]{0,62}\."
    r"[A-Za-z_][A-Za-z0-9_$]{0,62}\."
    r"[A-Za-z_][A-Za-z0-9_$]{0,62}$"
)


def _normalize_allowlist(
    value: str,
    *,
    label: str,
    pattern: re.Pattern[str],
) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{label} 必须是逗号分隔的字符串")
    entries = [entry.strip().casefold() for entry in value.split(",") if entry.strip()]
    if len(entries) > 512:
        raise ValueError(f"{label} 最多允许 512 项")
    if len(set(entries)) != len(entries):
        raise ValueError(f"{label} 不能包含重复项")
    invalid = [entry for entry in entries if pattern.fullmatch(entry) is None]
    if invalid:
        raise ValueError(f"{label} 包含非法标识符")
    return ",".join(entries)


class SqlAssistantSettings(_EnvSettings):
    """Fail-closed configuration for the read-only SQL Assistant Module."""

    enabled: bool = Field(
        default=False,
        validation_alias="SQL_ASSISTANT_ENABLED",
    )
    dsn: SecretStr = Field(
        default=SecretStr(""),
        validation_alias="SQL_ASSISTANT_DSN",
    )
    expected_role: str = Field(
        default="",
        max_length=63,
        validation_alias="SQL_ASSISTANT_EXPECTED_ROLE",
    )
    allowed_schemas_raw: str = Field(
        default="public",
        validation_alias="SQL_ASSISTANT_ALLOWED_SCHEMAS",
    )
    allowed_tables_raw: str = Field(
        default="",
        validation_alias="SQL_ASSISTANT_ALLOWED_TABLES",
    )
    sensitive_columns_raw: str = Field(
        default="",
        validation_alias="SQL_ASSISTANT_SENSITIVE_COLUMNS",
    )
    connect_timeout_seconds: float = Field(
        default=5.0,
        gt=0,
        le=60,
        allow_inf_nan=False,
        validation_alias="SQL_ASSISTANT_CONNECT_TIMEOUT_SECONDS",
    )
    schema_timeout_seconds: float = Field(
        default=5.0,
        gt=0,
        le=120,
        allow_inf_nan=False,
        validation_alias="SQL_ASSISTANT_SCHEMA_TIMEOUT_SECONDS",
    )
    statement_timeout_seconds: float = Field(
        default=10.0,
        gt=0,
        le=120,
        allow_inf_nan=False,
        validation_alias="SQL_ASSISTANT_STATEMENT_TIMEOUT_SECONDS",
    )
    lock_timeout_seconds: float = Field(
        default=1.0,
        gt=0,
        le=30,
        allow_inf_nan=False,
        validation_alias="SQL_ASSISTANT_LOCK_TIMEOUT_SECONDS",
    )
    max_rows: int = Field(
        default=200,
        ge=1,
        le=10_000,
        validation_alias="SQL_ASSISTANT_MAX_ROWS",
    )
    max_result_bytes: int = Field(
        default=262_144,
        ge=1_024,
        le=16_777_216,
        validation_alias="SQL_ASSISTANT_MAX_RESULT_BYTES",
    )
    max_cell_bytes: int = Field(
        default=65_536,
        ge=1,
        le=1_048_576,
        validation_alias="SQL_ASSISTANT_MAX_CELL_BYTES",
    )
    max_estimated_cost: float = Field(
        default=100_000.0,
        gt=0,
        le=1_000_000_000,
        allow_inf_nan=False,
        validation_alias="SQL_ASSISTANT_MAX_ESTIMATED_COST",
    )
    max_estimated_rows: int = Field(
        default=100_000,
        ge=1,
        le=1_000_000_000,
        validation_alias="SQL_ASSISTANT_MAX_ESTIMATED_ROWS",
    )
    max_estimated_bytes: int = Field(
        default=8_388_608,
        ge=1_024,
        le=1_073_741_824,
        validation_alias="SQL_ASSISTANT_MAX_ESTIMATED_BYTES",
    )
    fetch_size: int = Field(
        default=64,
        ge=1,
        le=10_000,
        validation_alias="SQL_ASSISTANT_FETCH_SIZE",
    )
    pool_min_size: int = Field(
        default=1,
        ge=1,
        le=32,
        validation_alias="SQL_ASSISTANT_POOL_MIN_SIZE",
    )
    pool_max_size: int = Field(
        default=4,
        ge=1,
        le=64,
        validation_alias="SQL_ASSISTANT_POOL_MAX_SIZE",
    )
    pool_timeout_seconds: float = Field(
        default=5.0,
        gt=0,
        le=120,
        allow_inf_nan=False,
        validation_alias="SQL_ASSISTANT_POOL_TIMEOUT_SECONDS",
    )
    pool_max_lifetime_seconds: float = Field(
        default=1_800.0,
        ge=60,
        le=86_400,
        allow_inf_nan=False,
        validation_alias="SQL_ASSISTANT_POOL_MAX_LIFETIME_SECONDS",
    )
    catalog_cache_ttl_seconds: float = Field(
        default=300.0,
        ge=1,
        le=3_600,
        allow_inf_nan=False,
        validation_alias="SQL_ASSISTANT_CATALOG_CACHE_TTL_SECONDS",
    )
    max_sql_characters: int = Field(
        default=20_000,
        ge=1,
        le=100_000,
        validation_alias="SQL_ASSISTANT_MAX_SQL_CHARACTERS",
    )
    max_tables_per_query: int = Field(
        default=8,
        ge=1,
        le=64,
        validation_alias="SQL_ASSISTANT_MAX_TABLES_PER_QUERY",
    )
    max_ast_nodes: int = Field(
        default=1_000,
        ge=32,
        le=10_000,
        validation_alias="SQL_ASSISTANT_MAX_AST_NODES",
    )
    strict_privilege_check: bool = Field(
        default=True,
        validation_alias="SQL_ASSISTANT_STRICT_PRIVILEGE_CHECK",
    )

    @field_validator("expected_role")
    @classmethod
    def validate_expected_role(cls, value: str) -> str:
        normalized = value.strip().casefold()
        if normalized and _POSTGRES_IDENTIFIER.fullmatch(normalized) is None:
            raise ValueError("SQL_ASSISTANT_EXPECTED_ROLE 必须是普通 PostgreSQL 标识符")
        return normalized

    @field_validator("allowed_schemas_raw")
    @classmethod
    def validate_allowed_schemas(cls, value: str) -> str:
        return _normalize_allowlist(
            value,
            label="SQL_ASSISTANT_ALLOWED_SCHEMAS",
            pattern=_POSTGRES_IDENTIFIER,
        )

    @field_validator("allowed_tables_raw")
    @classmethod
    def validate_allowed_tables(cls, value: str) -> str:
        return _normalize_allowlist(
            value,
            label="SQL_ASSISTANT_ALLOWED_TABLES",
            pattern=_QUALIFIED_TABLE,
        )

    @field_validator("sensitive_columns_raw")
    @classmethod
    def validate_sensitive_columns(cls, value: str) -> str:
        return _normalize_allowlist(
            value,
            label="SQL_ASSISTANT_SENSITIVE_COLUMNS",
            pattern=_QUALIFIED_COLUMN,
        )

    @property
    def allowed_schemas(self) -> tuple[str, ...]:
        return tuple(filter(None, self.allowed_schemas_raw.split(",")))

    @property
    def allowed_tables(self) -> tuple[str, ...]:
        return tuple(filter(None, self.allowed_tables_raw.split(",")))

    @property
    def sensitive_columns(self) -> tuple[str, ...]:
        return tuple(filter(None, self.sensitive_columns_raw.split(",")))


class WebResearchSettings(_EnvSettings):
    """Fail-closed configuration for bounded public Web Research."""

    enabled: bool = Field(
        default=False,
        validation_alias="WEB_RESEARCH_ENABLED",
    )
    brave_search_api_key: SecretStr = Field(
        default=SecretStr(""),
        validation_alias="BRAVE_SEARCH_API_KEY",
    )
    request_timeout_seconds: float = Field(
        default=10.0,
        gt=0,
        le=120,
        allow_inf_nan=False,
        validation_alias="WEB_RESEARCH_REQUEST_TIMEOUT_SECONDS",
    )
    dns_timeout_seconds: float = Field(
        default=2.0,
        gt=0,
        le=30,
        allow_inf_nan=False,
        validation_alias="WEB_RESEARCH_DNS_TIMEOUT_SECONDS",
    )
    dns_max_concurrency: int = Field(
        default=4,
        ge=1,
        le=32,
        validation_alias="WEB_RESEARCH_DNS_MAX_CONCURRENCY",
    )
    max_dns_addresses: int = Field(
        default=8,
        ge=1,
        le=32,
        validation_alias="WEB_RESEARCH_MAX_DNS_ADDRESSES",
    )
    max_query_bytes: int = Field(
        default=4_096,
        ge=1,
        le=16_384,
        validation_alias="WEB_RESEARCH_MAX_QUERY_BYTES",
    )
    max_url_bytes: int = Field(
        default=4_096,
        ge=1,
        le=16_384,
        validation_alias="WEB_RESEARCH_MAX_URL_BYTES",
    )
    max_title_bytes: int = Field(
        default=512,
        ge=1,
        le=4_096,
        validation_alias="WEB_RESEARCH_MAX_TITLE_BYTES",
    )
    max_snippet_bytes: int = Field(
        default=1_024,
        ge=1,
        le=32_768,
        validation_alias="WEB_RESEARCH_MAX_SNIPPET_BYTES",
    )
    max_content_bytes: int = Field(
        default=3_072,
        ge=1,
        le=2_097_152,
        validation_alias="WEB_RESEARCH_MAX_CONTENT_BYTES",
    )
    max_total_evidence_bytes: int = Field(
        default=4_096,
        ge=1,
        le=8_388_608,
        validation_alias="WEB_RESEARCH_MAX_TOTAL_EVIDENCE_BYTES",
    )
    max_response_bytes: int = Field(
        default=2_097_152,
        ge=1_024,
        le=8_388_608,
        validation_alias="WEB_RESEARCH_MAX_RESPONSE_BYTES",
    )
    max_compressed_bytes: int = Field(
        default=1_000_000,
        ge=1_024,
        le=8_388_608,
        validation_alias="WEB_RESEARCH_MAX_COMPRESSED_BYTES",
    )
    default_search_results: int = Field(
        default=5,
        ge=1,
        le=50,
        validation_alias="WEB_RESEARCH_DEFAULT_SEARCH_RESULTS",
    )
    max_search_results: int = Field(
        default=12,
        ge=1,
        le=50,
        validation_alias="WEB_RESEARCH_MAX_SEARCH_RESULTS",
    )
    max_citations: int = Field(
        default=32,
        ge=1,
        le=100,
        validation_alias="WEB_RESEARCH_MAX_CITATIONS",
    )
    max_redirects: int = Field(
        default=5,
        ge=0,
        le=10,
        validation_alias="WEB_RESEARCH_MAX_REDIRECTS",
    )
    max_concurrency: int = Field(
        default=4,
        ge=1,
        le=64,
        validation_alias="WEB_RESEARCH_MAX_CONCURRENCY",
    )
    user_agent: str = Field(
        default="SuperMew-WebResearch/1.0",
        min_length=1,
        max_length=256,
        validation_alias="WEB_RESEARCH_USER_AGENT",
    )

    @field_validator("user_agent")
    @classmethod
    def validate_user_agent(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized or any(
            marker in normalized for marker in ("\r", "\n", "\x00")
        ):
            raise ValueError("WEB_RESEARCH_USER_AGENT 必须是安全的单行字符串")
        return normalized

    @property
    def search_configured(self) -> bool:
        return self.enabled and not _is_placeholder(
            self.brave_search_api_key.get_secret_value()
        )


_WEAK_SECRETS = {
    "",
    "change-this-secret",
    "replace-with-strong-random-secret",
    "secret",
    "supermew",
}


class AppSettings(BaseModel):
    app: ApplicationSettings
    models: ModelSettings
    rag: RagSettings
    embedding: EmbeddingSettings
    rerank: RerankSettings
    runs: RunSettings
    agent: AgentSettings
    security: SecuritySettings
    rate_limits: RateLimitSettings = Field(
        default_factory=lambda: RateLimitSettings(_env_file=None)
    )
    storage: StorageSettings
    worker: WorkerSettings
    observability: ObservabilitySettings
    skills: SkillSettings
    sandbox: SandboxSettings = Field(
        default_factory=lambda: SandboxSettings(_env_file=None)
    )
    sql_assistant: SqlAssistantSettings
    web_research: WebResearchSettings

    def validate_startup(self) -> None:
        problems: list[str] = []
        secret = self.security.jwt_secret_key.get_secret_value().strip()
        if len(secret) < 32 or secret.lower() in _WEAK_SECRETS:
            problems.append("JWT_SECRET_KEY 必须是至少 32 字符的随机密钥")

        if self.security.jwt_algorithm not in {"HS256", "HS384", "HS512"}:
            problems.append("JWT_ALGORITHM 只能使用 HS256、HS384 或 HS512")

        if (
            self.security.refresh_cookie_samesite == "none"
            and not self.security.refresh_cookie_secure
        ):
            problems.append("SameSite=None 的 refresh Cookie 必须启用 Secure")

        rate_limit_key = self.rate_limits.identity_hmac_key.get_secret_value().strip()
        admin_invite = self.security.admin_invite_code.get_secret_value().strip()
        if admin_invite:
            if admin_invite in {secret, rate_limit_key}:
                problems.append(
                    "ADMIN_INVITE_CODE 不得与 JWT 或 Rate Limit Secret 相同"
                )
        if self.rate_limits.enabled and rate_limit_key and len(rate_limit_key) < 32:
            problems.append("RATE_LIMIT_HMAC_KEY 必须至少包含 32 个字符")
        if self.rate_limits.enabled and self.rate_limits.backend == "redis":
            if len(rate_limit_key) < 32 or rate_limit_key.lower() in _WEAK_SECRETS:
                problems.append(
                    "Redis Rate Limit 必须配置至少 32 字符的随机 RATE_LIMIT_HMAC_KEY"
                )

        if self.worker.indexing_heartbeat_seconds >= self.worker.indexing_lease_seconds:
            problems.append(
                "INDEX_WORKER_HEARTBEAT_SECONDS 必须小于 INDEX_WORKER_LEASE_SECONDS"
            )
        if (
            self.worker.indexing_retry_base_seconds
            > self.worker.indexing_retry_max_seconds
        ):
            problems.append(
                "INDEX_WORKER_RETRY_BASE_SECONDS 不能大于 INDEX_WORKER_RETRY_MAX_SECONDS"
            )
        readiness_interval = max(
            self.worker.indexing_poll_seconds,
            float(self.worker.indexing_heartbeat_seconds),
        )
        if self.worker.indexing_readiness_ttl_seconds <= readiness_interval * 2:
            problems.append(
                "INDEX_WORKER_READINESS_TTL_SECONDS 必须大于 worker 最大心跳间隔的两倍"
            )
        if (
            self.worker.evaluation_heartbeat_seconds
            >= self.worker.evaluation_lease_seconds
        ):
            problems.append(
                "EVALUATION_WORKER_HEARTBEAT_SECONDS 必须小于 "
                "EVALUATION_WORKER_LEASE_SECONDS"
            )

        origins = self.security.cors_origins
        invalid_origins = [
            origin
            for origin in origins
            if origin != "*" and canonical_http_origin(origin) is None
        ]
        if invalid_origins:
            problems.append(
                "CORS_ORIGINS 每一项都必须是无 path、query、userinfo 的 HTTP/HTTPS Origin"
            )
        if "*" in origins:
            problems.append("CORS_ORIGINS 禁止使用通配符 *")
        canonical_origins = [
            normalized
            for origin in origins
            if (normalized := canonical_http_origin(origin)) is not None
        ]
        if self.app.environment == "production" and any(
            origin.startswith("http://") for origin in canonical_origins
        ):
            problems.append("生产环境 CORS_ORIGINS 的跨源项必须使用 HTTPS")
        if (
            self.app.environment == "production"
            and self.security.cors_allow_credentials
            and len(canonical_origins) > 1
        ):
            problems.append(
                "生产环境 credentialed CORS 最多允许一个浏览器 Origin；"
                "Web Locks 无法跨 Origin 串行 Refresh Cookie 轮换"
            )

        if self.app.environment == "production":
            if not self.security.refresh_cookie_secure:
                problems.append("生产环境 refresh Cookie 必须启用 Secure")
            if not self.rate_limits.enabled:
                problems.append("生产环境必须启用 RATE_LIMIT_ENABLED")
            if self.rate_limits.backend != "redis":
                problems.append("生产环境 RATE_LIMIT_BACKEND 必须为 redis")
            if len(rate_limit_key) < 32 or rate_limit_key.lower() in _WEAK_SECRETS:
                problems.append(
                    "生产环境必须配置至少 32 字符的随机 RATE_LIMIT_HMAC_KEY"
                )
            if rate_limit_key and rate_limit_key == secret:
                problems.append("RATE_LIMIT_HMAC_KEY 不得与 JWT_SECRET_KEY 相同")
            if not self.worker.indexing_worker_required:
                problems.append("生产环境必须启用 INDEX_WORKER_REQUIRED")
            database_url = self.storage.database_url.get_secret_value()
            parsed = urlsplit(
                database_url.replace("postgresql+psycopg2", "postgresql", 1)
            )
            if parsed.scheme.split("+", 1)[0] not in {"postgres", "postgresql"}:
                problems.append(
                    "生产环境 DATABASE_URL 必须使用 PostgreSQL，"
                    "持久 worker 依赖 FOR UPDATE SKIP LOCKED"
                )
            if (parsed.username or "") == "postgres" and (
                parsed.password or ""
            ) == "postgres":
                problems.append("生产环境禁止使用 postgres/postgres 默认数据库凭据")
            if not self.models.api_key.get_secret_value().strip():
                problems.append("生产环境必须配置 ARK_API_KEY")
            if not self.embedding.warmup_on_start:
                problems.append("生产环境必须启用 EMBEDDING_WARMUP_ON_START")

        if self.web_research.enabled and not self.web_research.search_configured:
            problems.append("启用 Web Research 时必须配置 BRAVE_SEARCH_API_KEY")

        sandbox = self.sandbox
        if sandbox.enabled:
            if sandbox.adapter != "docker":
                problems.append("启用 Sandbox 时 SANDBOX_ADAPTER 必须为 docker")
            if not sandbox.docker_image:
                problems.append(
                    "启用 Sandbox 时必须配置 digest-pinned SANDBOX_DOCKER_IMAGE"
                )
            if self.app.environment == "production" and not sandbox.require_rootless:
                problems.append("生产环境 Sandbox 必须要求 rootless Docker daemon")
        if sandbox.max_file_bytes > sandbox.max_total_file_bytes:
            problems.append(
                "SANDBOX_MAX_FILE_BYTES 不能大于 SANDBOX_MAX_TOTAL_FILE_BYTES"
            )
        if sandbox.max_total_file_bytes > sandbox.workspace_bytes:
            problems.append(
                "SANDBOX_MAX_TOTAL_FILE_BYTES 不能大于 SANDBOX_WORKSPACE_BYTES"
            )
        if sandbox.max_source_bytes >= sandbox.workspace_bytes:
            problems.append("SANDBOX_MAX_SOURCE_BYTES 必须小于 SANDBOX_WORKSPACE_BYTES")

        if self.app.config_version != 1:
            problems.append(
                f"不支持的 CONFIG_VERSION={self.app.config_version}，当前仅支持 1"
            )

        if self.agent.response_reserve_tokens >= self.agent.max_context_tokens:
            problems.append(
                "AGENT_RESPONSE_RESERVE_TOKENS 必须小于 AGENT_MAX_CONTEXT_TOKENS"
            )
        if self.agent.recursion_limit < self.agent.minimum_recursion_limit:
            problems.append(
                "AGENT_RECURSION_LIMIT 与模型/工具调用预算不匹配，"
                f"当前至少需要 {self.agent.minimum_recursion_limit}"
            )
        if self.rerank.max_keepalive_connections > self.rerank.max_connections:
            problems.append(
                "RERANK_MAX_KEEPALIVE_CONNECTIONS 不能大于 RERANK_MAX_CONNECTIONS"
            )

        sql = self.sql_assistant
        if sql.enabled:
            dsn = sql.dsn.get_secret_value().strip()
            sql_username: str | None = None
            if not dsn:
                problems.append("启用 SQL Assistant 时必须配置 SQL_ASSISTANT_DSN")
            else:
                parsed = urlsplit(dsn)
                if parsed.scheme not in {"postgres", "postgresql"}:
                    problems.append("SQL_ASSISTANT_DSN 只能使用 PostgreSQL DSN")
                if not parsed.username or not parsed.path.strip("/"):
                    problems.append(
                        "SQL_ASSISTANT_DSN 必须显式包含独立 username 与 database"
                    )
                elif parsed.username:
                    sql_username = unquote(parsed.username).casefold()

                application_url = self.storage.database_url.get_secret_value()
                application_parsed = urlsplit(application_url)
                application_username = application_parsed.username
                if not application_username:
                    problems.append(
                        "启用 SQL Assistant 时必须能解析 DATABASE_URL username"
                    )
                elif (
                    parsed.username
                    and unquote(parsed.username).casefold()
                    == unquote(application_username).casefold()
                ):
                    problems.append(
                        "SQL_ASSISTANT_DSN 与 DATABASE_URL 必须使用不同 username"
                    )
            if not sql.expected_role:
                problems.append(
                    "启用 SQL Assistant 时必须配置 SQL_ASSISTANT_EXPECTED_ROLE"
                )
            elif sql.expected_role.casefold() in {
                "postgres",
                "rds_superuser",
                "azure_pg_admin",
                "cloudsqlsuperuser",
            }:
                problems.append("SQL_ASSISTANT_EXPECTED_ROLE 禁止使用高权限角色")
            elif sql_username and sql_username != sql.expected_role:
                problems.append(
                    "SQL_ASSISTANT_DSN username 必须与 SQL_ASSISTANT_EXPECTED_ROLE 一致"
                )
            if not sql.allowed_schemas:
                problems.append("SQL_ASSISTANT_ALLOWED_SCHEMAS 不能为空")
            if not sql.allowed_tables:
                problems.append("SQL_ASSISTANT_ALLOWED_TABLES 不能为空")
            if not sql.strict_privilege_check:
                problems.append(
                    "启用 SQL Assistant 时必须启用 SQL_ASSISTANT_STRICT_PRIVILEGE_CHECK"
                )

            allowed_schemas = set(sql.allowed_schemas)
            table_relations = set(sql.allowed_tables)
            for table in sql.allowed_tables:
                schema, _name = table.split(".", 1)
                if schema not in allowed_schemas:
                    problems.append(
                        "SQL_ASSISTANT_ALLOWED_TABLES 只能引用 allowlist 内的 schema"
                    )
                    break
            for column in sql.sensitive_columns:
                schema, table, _name = column.split(".", 2)
                if schema not in allowed_schemas or not (
                    f"{schema}.{table}" in table_relations
                    or f"{schema}.*" in table_relations
                ):
                    problems.append(
                        "SQL_ASSISTANT_SENSITIVE_COLUMNS 只能引用 allowlist 内的表"
                    )
                    break

        if sql.lock_timeout_seconds >= sql.statement_timeout_seconds:
            problems.append(
                "SQL_ASSISTANT_LOCK_TIMEOUT_SECONDS 必须小于 "
                "SQL_ASSISTANT_STATEMENT_TIMEOUT_SECONDS"
            )
        if sql.pool_min_size > sql.pool_max_size:
            problems.append(
                "SQL_ASSISTANT_POOL_MIN_SIZE 不能大于 SQL_ASSISTANT_POOL_MAX_SIZE"
            )
        if sql.fetch_size > sql.max_rows:
            problems.append("SQL_ASSISTANT_FETCH_SIZE 不能大于 SQL_ASSISTANT_MAX_ROWS")
        if sql.max_rows > sql.max_estimated_rows:
            problems.append(
                "SQL_ASSISTANT_MAX_ROWS 不能大于 SQL_ASSISTANT_MAX_ESTIMATED_ROWS"
            )
        if sql.max_cell_bytes > sql.max_result_bytes:
            problems.append(
                "SQL_ASSISTANT_MAX_CELL_BYTES 不能大于 SQL_ASSISTANT_MAX_RESULT_BYTES"
            )
        if sql.max_result_bytes > sql.max_estimated_bytes:
            problems.append(
                "SQL_ASSISTANT_MAX_RESULT_BYTES 不能大于 "
                "SQL_ASSISTANT_MAX_ESTIMATED_BYTES"
            )

        web = self.web_research
        if web.default_search_results > web.max_search_results:
            problems.append(
                "WEB_RESEARCH_DEFAULT_SEARCH_RESULTS 不能大于 "
                "WEB_RESEARCH_MAX_SEARCH_RESULTS"
            )
        if web.dns_timeout_seconds > web.request_timeout_seconds:
            problems.append(
                "WEB_RESEARCH_DNS_TIMEOUT_SECONDS 不能大于 "
                "WEB_RESEARCH_REQUEST_TIMEOUT_SECONDS"
            )
        if web.max_search_results > web.max_citations:
            problems.append(
                "WEB_RESEARCH_MAX_SEARCH_RESULTS 不能大于 WEB_RESEARCH_MAX_CITATIONS"
            )
        if web.max_title_bytes > web.max_content_bytes:
            problems.append(
                "WEB_RESEARCH_MAX_TITLE_BYTES 不能大于 WEB_RESEARCH_MAX_CONTENT_BYTES"
            )
        if web.max_snippet_bytes > web.max_content_bytes:
            problems.append(
                "WEB_RESEARCH_MAX_SNIPPET_BYTES 不能大于 WEB_RESEARCH_MAX_CONTENT_BYTES"
            )
        if web.max_content_bytes > web.max_total_evidence_bytes:
            problems.append(
                "WEB_RESEARCH_MAX_CONTENT_BYTES 不能大于 "
                "WEB_RESEARCH_MAX_TOTAL_EVIDENCE_BYTES"
            )
        if (
            web.enabled
            and web.max_total_evidence_bytes > self.agent.input_token_budget // 2
        ):
            problems.append(
                "WEB_RESEARCH_MAX_TOTAL_EVIDENCE_BYTES 不能大于 "
                "Agent 输入 token 预算的一半"
            )
        if problems:
            raise ValueError("；".join(problems))

    def redacted_dict(self) -> dict:
        payload = self.model_dump(mode="json")
        payload["models"]["api_key"] = "***"
        payload["rerank"]["api_key"] = "***"
        payload["security"]["jwt_secret_key"] = "***"
        payload["security"]["admin_invite_code"] = "***"
        payload["rate_limits"]["identity_hmac_key"] = "***"
        payload["storage"]["database_url"] = _redact_url(
            self.storage.database_url.get_secret_value()
        )
        payload["storage"]["redis_url"] = _redact_url(
            self.storage.redis_url.get_secret_value()
        )
        payload["sql_assistant"]["dsn"] = _redact_url(
            self.sql_assistant.dsn.get_secret_value()
        )
        payload["web_research"]["brave_search_api_key"] = "***"
        return payload


def _redact_url(value: str) -> str:
    parsed = urlsplit(value)
    if not parsed.password:
        return value
    username = parsed.username or ""
    host = parsed.hostname or ""
    port = f":{parsed.port}" if parsed.port else ""
    auth = f"{username}:***@" if username else "***@"
    return f"{parsed.scheme}://{auth}{host}{port}{parsed.path}"


@lru_cache(maxsize=1)
def get_settings() -> AppSettings:
    return AppSettings(
        app=ApplicationSettings(),
        models=ModelSettings(),
        rag=RagSettings(),
        embedding=EmbeddingSettings(),
        rerank=RerankSettings(),
        runs=RunSettings(),
        agent=AgentSettings(),
        security=SecuritySettings(),
        rate_limits=RateLimitSettings(),
        storage=StorageSettings(),
        worker=WorkerSettings(),
        observability=ObservabilitySettings(),
        skills=SkillSettings(),
        sandbox=SandboxSettings(),
        sql_assistant=SqlAssistantSettings(),
        web_research=WebResearchSettings(),
    )


def reset_settings_cache() -> None:
    get_settings.cache_clear()
