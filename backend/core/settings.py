from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


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


class ModelSettings(_EnvSettings):
    api_key: SecretStr = Field(default=SecretStr(""), validation_alias="ARK_API_KEY")
    base_url: str = Field(default="", validation_alias="BASE_URL")
    answer_model: str = Field(default="", validation_alias="MODEL")
    fast_model: str = Field(default="", validation_alias="FAST_MODEL")
    grade_model: str = Field(default="", validation_alias="GRADE_MODEL")
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
    revision: str = Field(default="main", validation_alias="EMBEDDING_MODEL_REVISION")
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
        default="http://localhost:5173,http://127.0.0.1:5173",
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
                origin.strip().rstrip("/")
                for origin in self.cors_origins_raw.split(",")
                if origin.strip()
            )
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


class WorkerSettings(_EnvSettings):
    worker_id: str = Field(default="", validation_alias="WORKER_ID")
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
    sandbox_enabled: bool = Field(default=False, validation_alias="SANDBOX_ENABLED")


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
    storage: StorageSettings
    worker: WorkerSettings
    observability: ObservabilitySettings
    skills: SkillSettings

    def validate_startup(self) -> None:
        problems: list[str] = []
        secret = self.security.jwt_secret_key.get_secret_value().strip()
        if len(secret) < 32 or secret.lower() in _WEAK_SECRETS:
            problems.append("JWT_SECRET_KEY 必须是至少 32 字符的随机密钥")

        if self.security.jwt_algorithm not in {"HS256", "HS384", "HS512"}:
            problems.append("JWT_ALGORITHM 只能使用 HS256、HS384 或 HS512")

        origins = self.security.cors_origins
        if not origins:
            problems.append("CORS_ORIGINS 不能为空")
        if "*" in origins:
            problems.append("CORS_ORIGINS 禁止使用通配符 *")

        if self.app.environment == "production":
            database_url = self.storage.database_url.get_secret_value()
            parsed = urlsplit(
                database_url.replace("postgresql+psycopg2", "postgresql", 1)
            )
            if (parsed.username or "") == "postgres" and (
                parsed.password or ""
            ) == "postgres":
                problems.append("生产环境禁止使用 postgres/postgres 默认数据库凭据")
            if not self.models.api_key.get_secret_value().strip():
                problems.append("生产环境必须配置 ARK_API_KEY")
            if not self.models.answer_model.strip():
                problems.append("生产环境必须配置 MODEL")
            if not self.embedding.warmup_on_start:
                problems.append("生产环境必须启用 EMBEDDING_WARMUP_ON_START")

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

        if problems:
            raise ValueError("；".join(problems))

    def redacted_dict(self) -> dict:
        payload = self.model_dump(mode="json")
        payload["models"]["api_key"] = "***"
        payload["rerank"]["api_key"] = "***"
        payload["security"]["jwt_secret_key"] = "***"
        payload["security"]["admin_invite_code"] = "***"
        payload["storage"]["database_url"] = _redact_url(
            self.storage.database_url.get_secret_value()
        )
        payload["storage"]["redis_url"] = _redact_url(
            self.storage.redis_url.get_secret_value()
        )
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
        storage=StorageSettings(),
        worker=WorkerSettings(),
        observability=ObservabilitySettings(),
        skills=SkillSettings(),
    )


def reset_settings_cache() -> None:
    get_settings.cache_clear()
