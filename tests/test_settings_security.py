import unittest

from pydantic import SecretStr

from backend.core.settings import (
    AgentSettings,
    AppSettings,
    ApplicationSettings,
    ModelSettings,
    ObservabilitySettings,
    RagSettings,
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

    def test_redacted_dict_does_not_expose_secrets(self):
        settings = make_settings(secret="x" * 40)
        dumped = str(settings.redacted_dict())
        self.assertNotIn("x" * 40, dumped)
        self.assertNotIn("app:strong", dumped)

    def test_agent_budget_relationships_are_validated_at_startup(self):
        settings = make_settings(secret="x" * 40)
        settings.agent.response_reserve_tokens = settings.agent.max_context_tokens
        with self.assertRaisesRegex(ValueError, "RESPONSE_RESERVE"):
            settings.validate_startup()

        settings = make_settings(secret="x" * 40)
        settings.agent.recursion_limit = 8
        with self.assertRaisesRegex(ValueError, "RECURSION_LIMIT"):
            settings.validate_startup()


if __name__ == "__main__":
    unittest.main()
