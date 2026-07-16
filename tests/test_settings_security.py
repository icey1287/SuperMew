import unittest

from pydantic import SecretStr, ValidationError

from backend.core.settings import (
    AgentSettings,
    AppSettings,
    ApplicationSettings,
    EmbeddingSettings,
    ModelSettings,
    ObservabilitySettings,
    PROJECT_ROOT,
    RagSettings,
    RerankSettings,
    RunSettings,
    SecuritySettings,
    SkillSettings,
    StorageSettings,
    WorkerSettings,
)


def make_settings(
    *,
    secret: str,
    environment: str = "development",
    cors: str = "http://localhost:5173",
):
    return AppSettings(
        app=ApplicationSettings(_env_file=None, APP_ENV=environment),
        models=ModelSettings(_env_file=None, ARK_API_KEY="test", MODEL="answer"),
        rag=RagSettings(_env_file=None),
        embedding=EmbeddingSettings(_env_file=None),
        rerank=RerankSettings(_env_file=None),
        runs=RunSettings(_env_file=None),
        agent=AgentSettings(_env_file=None),
        security=SecuritySettings(
            _env_file=None,
            JWT_SECRET_KEY=secret,
            CORS_ORIGINS=cors,
        ),
        storage=StorageSettings(
            _env_file=None,
            DATABASE_URL="postgresql+psycopg2://app:strong@db/supermew",
        ),
        worker=WorkerSettings(_env_file=None),
        observability=ObservabilitySettings(_env_file=None),
        skills=SkillSettings(_env_file=None),
    )


class SettingsSecurityTests(unittest.TestCase):
    def test_weak_jwt_secret_is_rejected(self):
        settings = make_settings(secret="change-this-secret")
        with self.assertRaisesRegex(ValueError, "JWT_SECRET_KEY"):
            settings.validate_startup()

    def test_wildcard_cors_is_rejected(self):
        settings = make_settings(secret="x" * 40, cors="*")
        with self.assertRaisesRegex(ValueError, "CORS_ORIGINS"):
            settings.validate_startup()

    def test_production_default_database_credentials_are_rejected(self):
        settings = make_settings(secret="x" * 40, environment="production")
        settings.storage.database_url = SecretStr(
            "postgresql+psycopg2://postgres:postgres@db/langchain_app"
        )
        with self.assertRaisesRegex(ValueError, "默认数据库凭据"):
            settings.validate_startup()

    def test_production_requires_postgresql_for_worker_claim_fencing(self):
        settings = make_settings(secret="x" * 40, environment="production")
        settings.storage.database_url = SecretStr("sqlite:///supermew.db")

        with self.assertRaisesRegex(ValueError, "必须使用 PostgreSQL"):
            settings.validate_startup()

    def test_redacted_dict_does_not_expose_secrets(self):
        settings = make_settings(secret="x" * 40)
        settings.rerank.api_key = SecretStr("rerank-secret")
        dumped = str(settings.redacted_dict())
        self.assertNotIn("x" * 40, dumped)
        self.assertNotIn("app:strong", dumped)
        self.assertNotIn("rerank-secret", dumped)

    def test_agent_budget_relationships_are_validated_at_startup(self):
        settings = make_settings(secret="x" * 40)
        settings.agent.response_reserve_tokens = settings.agent.max_context_tokens
        with self.assertRaisesRegex(ValueError, "RESPONSE_RESERVE"):
            settings.validate_startup()

        settings = make_settings(secret="x" * 40)
        settings.agent.recursion_limit = 8
        with self.assertRaisesRegex(ValueError, "RECURSION_LIMIT"):
            settings.validate_startup()

    def test_rerank_connection_pool_relationship_is_validated(self):
        settings = make_settings(secret="x" * 40)
        settings.rerank.max_connections = 2
        settings.rerank.max_keepalive_connections = 3

        with self.assertRaisesRegex(ValueError, "KEEPALIVE"):
            settings.validate_startup()

    def test_placeholder_rerank_configuration_is_disabled(self):
        settings = make_settings(secret="x" * 40)
        settings.rerank.model = "your_rerank_model"
        settings.rerank.binding_host = "https://your-rerank-host"
        settings.rerank.api_key = SecretStr("your_rerank_api_key")

        self.assertFalse(settings.rerank.enabled)
        self.assertEqual("", settings.rerank.endpoint)

    def test_rerank_min_score_rejects_non_finite_values(self):
        with self.assertRaises(ValidationError):
            RerankSettings(_env_file=None, RERANK_MIN_SCORE="nan")

    def test_production_requires_embedding_warmup(self):
        settings = make_settings(secret="x" * 40, environment="production")
        settings.embedding.warmup_on_start = False

        with self.assertRaisesRegex(ValueError, "EMBEDDING_WARMUP"):
            settings.validate_startup()

    def test_indexing_worker_lease_and_backoff_relationships_are_validated(self):
        settings = make_settings(secret="x" * 40)
        settings.worker.indexing_heartbeat_seconds = (
            settings.worker.indexing_lease_seconds
        )
        with self.assertRaisesRegex(ValueError, "INDEX_WORKER_HEARTBEAT_SECONDS"):
            settings.validate_startup()

        settings = make_settings(secret="x" * 40)
        settings.worker.indexing_retry_base_seconds = 10
        settings.worker.indexing_retry_max_seconds = 5
        with self.assertRaisesRegex(ValueError, "INDEX_WORKER_RETRY_BASE_SECONDS"):
            settings.validate_startup()

        settings = make_settings(secret="x" * 40)
        settings.worker.indexing_poll_seconds = 60
        settings.worker.indexing_readiness_ttl_seconds = 45
        with self.assertRaisesRegex(ValueError, "INDEX_WORKER_READINESS_TTL_SECONDS"):
            settings.validate_startup()

    def test_production_cannot_disable_durable_indexing_worker_gate(self):
        settings = make_settings(secret="x" * 40, environment="production")
        settings.worker.indexing_worker_required = False
        with self.assertRaisesRegex(ValueError, "INDEX_WORKER_REQUIRED"):
            settings.validate_startup()

    def test_relative_upload_dir_is_anchored_to_project_root(self):
        storage = StorageSettings(_env_file=None, UPLOAD_DIR="shared/documents")

        self.assertEqual(
            (PROJECT_ROOT / "shared/documents").resolve(),
            storage.upload_dir,
        )


if __name__ == "__main__":
    unittest.main()
