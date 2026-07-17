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
    RateLimitSettings,
    RerankSettings,
    RunSettings,
    SandboxSettings,
    SecuritySettings,
    SkillSettings,
    SqlAssistantSettings,
    StorageSettings,
    WebResearchSettings,
    WorkerSettings,
)


def make_settings(
    *,
    secret: str,
    environment: str = "development",
    cors: str | None = None,
):
    if cors is None:
        cors = (
            "https://frontend.example.test"
            if environment == "production"
            else "http://localhost:3000"
        )
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
            ADMIN_INVITE_CODE="",
            CORS_ORIGINS=cors,
        ),
        rate_limits=RateLimitSettings(
            _env_file=None,
            RATE_LIMIT_BACKEND=("redis" if environment == "production" else "memory"),
            RATE_LIMIT_HMAC_KEY="r" * 40,
        ),
        storage=StorageSettings(
            _env_file=None,
            DATABASE_URL="postgresql+psycopg2://app:strong@db/supermew",
        ),
        worker=WorkerSettings(_env_file=None),
        observability=ObservabilitySettings(_env_file=None),
        skills=SkillSettings(_env_file=None),
        sql_assistant=SqlAssistantSettings(_env_file=None),
        web_research=WebResearchSettings(_env_file=None),
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

    def test_empty_cors_allowlist_supports_same_origin_only_deployments(self):
        settings = make_settings(secret="x" * 40, cors="")

        settings.validate_startup()
        self.assertEqual([], settings.security.cors_origins)

    def test_cors_origins_must_be_canonical_http_origins(self):
        invalid_values = (
            "ftp://frontend.example.test",
            "https://frontend.example.test/path",
            "https://frontend.example.test?debug=1",
            "https://user@frontend.example.test",
            "https://frontend.example.test:invalid",
        )

        for value in invalid_values:
            with self.subTest(value=value):
                settings = make_settings(secret="x" * 40, cors=value)
                with self.assertRaisesRegex(ValueError, "CORS_ORIGINS"):
                    settings.validate_startup()

    def test_production_cross_origin_allowlist_requires_https(self):
        for value in ("http://localhost:3000", "HTTP://LOCALHOST:3000"):
            with self.subTest(value=value):
                settings = make_settings(
                    secret="x" * 40,
                    environment="production",
                    cors=value,
                )
                settings.security.refresh_cookie_secure = True

                with self.assertRaisesRegex(ValueError, "CORS_ORIGINS.*HTTPS"):
                    settings.validate_startup()

        settings = make_settings(
            secret="x" * 40,
            environment="production",
            cors="",
        )
        settings.security.refresh_cookie_secure = True
        settings.validate_startup()

    def test_production_credentialed_cors_allows_only_one_browser_origin(self):
        settings = make_settings(
            secret="x" * 40,
            environment="production",
            cors="https://app.example.test,https://admin.example.test",
        )
        settings.security.refresh_cookie_secure = True

        with self.assertRaisesRegex(ValueError, "最多允许一个浏览器 Origin"):
            settings.validate_startup()

    def test_production_requires_secure_refresh_cookie(self):
        settings = make_settings(secret="x" * 40, environment="production")

        with self.assertRaisesRegex(ValueError, "refresh Cookie.*Secure"):
            settings.validate_startup()

        settings.security.refresh_cookie_secure = True
        settings.validate_startup()

    def test_samesite_none_requires_secure_refresh_cookie(self):
        settings = make_settings(secret="x" * 40)
        settings.security.refresh_cookie_samesite = "none"

        with self.assertRaisesRegex(ValueError, "SameSite=None.*Secure"):
            settings.validate_startup()

        settings.security.refresh_cookie_secure = True
        settings.validate_startup()

    def test_admin_invite_is_disabled_when_empty_and_rejects_public_placeholders(self):
        settings = make_settings(secret="x" * 40)
        settings.security.admin_invite_code = SecretStr("")
        settings.validate_startup()

        settings.security.admin_invite_code = SecretStr(
            "replace-with-private-admin-invite-code"
        )
        with self.assertRaisesRegex(ValueError, "ADMIN_INVITE_CODE"):
            settings.validate_startup()

        settings.security.admin_invite_code = SecretStr("a" * 40)
        settings.validate_startup()

        settings.security.admin_invite_code = SecretStr("x" * 40)
        with self.assertRaisesRegex(ValueError, "不得与 JWT"):
            settings.validate_startup()

    def test_redis_rate_limit_requires_an_independent_hmac_key(self):
        settings = make_settings(secret="x" * 40)
        settings.rate_limits.backend = "redis"
        settings.rate_limits.identity_hmac_key = SecretStr("")

        with self.assertRaisesRegex(ValueError, "RATE_LIMIT_HMAC_KEY"):
            settings.validate_startup()

        settings.rate_limits.identity_hmac_key = SecretStr("r" * 40)
        settings.validate_startup()

    def test_production_requires_redis_rate_limit_and_distinct_secret(self):
        settings = make_settings(secret="x" * 40, environment="production")
        settings.security.refresh_cookie_secure = True
        settings.rate_limits.enabled = False

        with self.assertRaisesRegex(ValueError, "RATE_LIMIT_ENABLED"):
            settings.validate_startup()

        settings.rate_limits.enabled = True
        settings.rate_limits.backend = "memory"
        with self.assertRaisesRegex(ValueError, "RATE_LIMIT_BACKEND"):
            settings.validate_startup()

        settings.rate_limits.backend = "redis"
        settings.rate_limits.identity_hmac_key = SecretStr("x" * 40)
        with self.assertRaisesRegex(ValueError, "不得与 JWT_SECRET_KEY 相同"):
            settings.validate_startup()

        settings.rate_limits.identity_hmac_key = SecretStr("r" * 40)
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
        settings.web_research.brave_search_api_key = SecretStr("brave-secret")
        dumped = str(settings.redacted_dict())
        self.assertNotIn("x" * 40, dumped)
        self.assertNotIn("app:strong", dumped)
        self.assertNotIn("rerank-secret", dumped)
        self.assertNotIn("brave-secret", dumped)
        self.assertNotIn("r" * 40, dumped)

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

    def test_sql_assistant_is_disabled_and_secretless_by_default(self):
        sql = SqlAssistantSettings(_env_file=None)

        self.assertFalse(sql.enabled)
        self.assertEqual("", sql.dsn.get_secret_value())
        self.assertEqual(("public",), sql.allowed_schemas)
        self.assertEqual((), sql.allowed_tables)

    def test_enabled_sql_assistant_requires_fail_closed_connection_policy(self):
        settings = make_settings(secret="x" * 40)
        settings.sql_assistant.enabled = True

        with self.assertRaisesRegex(ValueError, "SQL_ASSISTANT_DSN"):
            settings.validate_startup()

        settings.sql_assistant.dsn = SecretStr("mysql://reader:secret@db/analytics")
        settings.sql_assistant.expected_role = "analytics_reader"
        settings.sql_assistant.allowed_tables_raw = "public.orders"
        with self.assertRaisesRegex(ValueError, "PostgreSQL DSN"):
            settings.validate_startup()

    def test_enabled_sql_assistant_accepts_explicit_read_only_scope(self):
        settings = make_settings(secret="x" * 40)
        settings.sql_assistant = SqlAssistantSettings(
            _env_file=None,
            SQL_ASSISTANT_ENABLED=True,
            SQL_ASSISTANT_DSN=("postgresql://analytics_reader:secret@db/analytics"),
            SQL_ASSISTANT_EXPECTED_ROLE="analytics_reader",
            SQL_ASSISTANT_ALLOWED_SCHEMAS="analytics",
            SQL_ASSISTANT_ALLOWED_TABLES="analytics.orders,analytics.customers",
            SQL_ASSISTANT_SENSITIVE_COLUMNS="analytics.customers.email",
        )

        settings.validate_startup()

        self.assertEqual(("analytics",), settings.sql_assistant.allowed_schemas)
        self.assertEqual(
            ("analytics.orders", "analytics.customers"),
            settings.sql_assistant.allowed_tables,
        )

        settings.sql_assistant.dsn = SecretStr(
            "postgresql://different_reader:secret@db/analytics"
        )
        with self.assertRaisesRegex(ValueError, "EXPECTED_ROLE 一致"):
            settings.validate_startup()

    def test_sql_assistant_rejects_unsafe_role_and_scope_relationships(self):
        settings = make_settings(secret="x" * 40)
        settings.sql_assistant = SqlAssistantSettings(
            _env_file=None,
            SQL_ASSISTANT_ENABLED=True,
            SQL_ASSISTANT_DSN=("postgresql://analytics_reader:secret@db/analytics"),
            SQL_ASSISTANT_EXPECTED_ROLE="postgres",
            SQL_ASSISTANT_ALLOWED_SCHEMAS="analytics",
            SQL_ASSISTANT_ALLOWED_TABLES="private.orders",
        )

        with self.assertRaisesRegex(ValueError, "高权限角色"):
            settings.validate_startup()

        settings.sql_assistant.expected_role = "analytics_reader"
        with self.assertRaisesRegex(ValueError, "allowlist 内的 schema"):
            settings.validate_startup()

    def test_sql_assistant_budget_and_pool_relationships_are_validated(self):
        settings = make_settings(secret="x" * 40)
        settings.sql_assistant.lock_timeout_seconds = 10
        settings.sql_assistant.statement_timeout_seconds = 10
        with self.assertRaisesRegex(ValueError, "LOCK_TIMEOUT"):
            settings.validate_startup()

        settings = make_settings(secret="x" * 40)
        settings.sql_assistant.pool_min_size = 5
        settings.sql_assistant.pool_max_size = 4
        with self.assertRaisesRegex(ValueError, "POOL_MIN_SIZE"):
            settings.validate_startup()

        settings = make_settings(secret="x" * 40)
        settings.sql_assistant.max_cell_bytes = (
            settings.sql_assistant.max_result_bytes + 1
        )
        with self.assertRaisesRegex(ValueError, "MAX_CELL_BYTES"):
            settings.validate_startup()

    def test_sql_assistant_requires_a_distinct_database_identity(self):
        settings = make_settings(secret="x" * 40)
        settings.sql_assistant = SqlAssistantSettings(
            _env_file=None,
            SQL_ASSISTANT_ENABLED=True,
            SQL_ASSISTANT_DSN="postgresql://app:other@analytics/warehouse",
            SQL_ASSISTANT_EXPECTED_ROLE="analytics_reader",
            SQL_ASSISTANT_ALLOWED_SCHEMAS="analytics",
            SQL_ASSISTANT_ALLOWED_TABLES="analytics.orders",
        )

        with self.assertRaisesRegex(ValueError, "不同 username"):
            settings.validate_startup()

    def test_enabled_sql_assistant_requires_strict_privilege_checks(self):
        settings = make_settings(secret="x" * 40)
        settings.sql_assistant = SqlAssistantSettings(
            _env_file=None,
            SQL_ASSISTANT_ENABLED=True,
            SQL_ASSISTANT_DSN=(
                "postgresql://analytics_reader:secret@analytics/warehouse"
            ),
            SQL_ASSISTANT_EXPECTED_ROLE="analytics_reader",
            SQL_ASSISTANT_ALLOWED_SCHEMAS="analytics",
            SQL_ASSISTANT_ALLOWED_TABLES="analytics.orders",
            SQL_ASSISTANT_STRICT_PRIVILEGE_CHECK=False,
        )

        with self.assertRaisesRegex(ValueError, "STRICT_PRIVILEGE_CHECK"):
            settings.validate_startup()

    def test_sql_assistant_allowlists_reject_duplicates_and_unsafe_identifiers(self):
        with self.assertRaises(ValidationError):
            SqlAssistantSettings(
                _env_file=None,
                SQL_ASSISTANT_ALLOWED_TABLES="PUBLIC.Orders,public.orders",
            )

        with self.assertRaises(ValidationError):
            SqlAssistantSettings(
                _env_file=None,
                SQL_ASSISTANT_SENSITIVE_COLUMNS="public.users.email;drop table x",
            )

    def test_sql_assistant_dsn_is_redacted_from_settings_dump(self):
        settings = make_settings(secret="x" * 40)
        settings.sql_assistant.dsn = SecretStr(
            "postgresql://sql_reader:sql-password@db/analytics"
        )

        dumped = str(settings.redacted_dict())

        self.assertNotIn("sql-password", dumped)
        self.assertIn("sql_reader:***@db", dumped)

    def test_web_research_is_disabled_and_secretless_by_default(self):
        web = WebResearchSettings(_env_file=None)

        self.assertFalse(web.enabled)
        self.assertFalse(web.search_configured)
        self.assertEqual("", web.brave_search_api_key.get_secret_value())
        self.assertEqual(8, web.max_dns_addresses)
        self.assertEqual(3_072, web.max_content_bytes)
        self.assertEqual(4_096, web.max_total_evidence_bytes)

    def test_enabled_web_research_requires_real_brave_key_in_every_environment(self):
        settings = make_settings(secret="x" * 40)
        settings.web_research.enabled = True

        with self.assertRaisesRegex(ValueError, "BRAVE_SEARCH_API_KEY"):
            settings.validate_startup()

        settings.web_research.brave_search_api_key = SecretStr(
            "your_brave_search_api_key"
        )
        with self.assertRaisesRegex(ValueError, "BRAVE_SEARCH_API_KEY"):
            settings.validate_startup()

        settings.web_research.brave_search_api_key = SecretStr("production-key")
        settings.validate_startup()

    def test_web_research_budget_relationships_are_validated(self):
        settings = make_settings(secret="x" * 40)
        settings.web_research.default_search_results = 13
        settings.web_research.max_search_results = 12
        with self.assertRaisesRegex(ValueError, "DEFAULT_SEARCH_RESULTS"):
            settings.validate_startup()

        settings = make_settings(secret="x" * 40)
        settings.web_research.dns_timeout_seconds = 11
        settings.web_research.request_timeout_seconds = 10
        with self.assertRaisesRegex(ValueError, "DNS_TIMEOUT_SECONDS"):
            settings.validate_startup()

        settings = make_settings(secret="x" * 40)
        settings.web_research.max_content_bytes = 600_000
        settings.web_research.max_total_evidence_bytes = 500_000
        with self.assertRaisesRegex(ValueError, "MAX_CONTENT_BYTES"):
            settings.validate_startup()

        settings = make_settings(secret="x" * 40)
        settings.web_research.enabled = True
        settings.web_research.brave_search_api_key = SecretStr("production-key")
        settings.web_research.max_content_bytes = 10_000
        settings.web_research.max_total_evidence_bytes = 20_000
        with self.assertRaisesRegex(ValueError, "Agent 输入 token 预算"):
            settings.validate_startup()

        settings.agent.max_context_tokens = 50_000
        settings.validate_startup()

    def test_web_research_hard_dns_cap_and_header_safety_are_validated(self):
        with self.assertRaises(ValidationError):
            WebResearchSettings(
                _env_file=None,
                WEB_RESEARCH_MAX_DNS_ADDRESSES=33,
            )

        with self.assertRaises(ValidationError):
            WebResearchSettings(
                _env_file=None,
                WEB_RESEARCH_USER_AGENT="safe\r\nX-Leak: value",
            )

    def test_sandbox_is_disabled_without_touching_docker_by_default(self):
        sandbox = SandboxSettings(_env_file=None)

        self.assertFalse(sandbox.enabled)
        self.assertEqual("", sandbox.docker_image)
        self.assertEqual("docker", sandbox.adapter)
        make_settings(secret="x" * 40).validate_startup()

    def test_enabled_sandbox_requires_an_immutable_image(self):
        settings = make_settings(secret="x" * 40)
        settings.sandbox.enabled = True

        with self.assertRaisesRegex(ValueError, "SANDBOX_DOCKER_IMAGE"):
            settings.validate_startup()

        with self.assertRaises(ValidationError):
            SandboxSettings(
                _env_file=None,
                SANDBOX_ENABLED=True,
                SANDBOX_DOCKER_IMAGE="python:3.12",
            )

        settings.sandbox.docker_image = "sha256:" + ("a" * 64)
        settings.validate_startup()

    def test_sandbox_budget_relationships_are_validated(self):
        settings = make_settings(secret="x" * 40)
        settings.sandbox.max_file_bytes = 20
        settings.sandbox.max_total_file_bytes = 10
        with self.assertRaisesRegex(ValueError, "MAX_FILE_BYTES"):
            settings.validate_startup()

        settings = make_settings(secret="x" * 40)
        settings.sandbox.max_total_file_bytes = settings.sandbox.workspace_bytes + 1
        with self.assertRaisesRegex(ValueError, "MAX_TOTAL_FILE_BYTES"):
            settings.validate_startup()

    def test_production_sandbox_requires_rootless_daemon(self):
        settings = make_settings(secret="x" * 40, environment="production")
        settings.sandbox.enabled = True
        settings.sandbox.docker_image = "sha256:" + ("b" * 64)
        settings.sandbox.require_rootless = False

        with self.assertRaisesRegex(ValueError, "rootless"):
            settings.validate_startup()


if __name__ == "__main__":
    unittest.main()
