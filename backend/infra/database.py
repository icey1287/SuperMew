from __future__ import annotations

from pathlib import Path

from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

from backend.core.settings import PROJECT_ROOT, get_settings


DATABASE_URL = get_settings().storage.database_url.get_secret_value()

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(
    bind=engine,
    autoflush=False,
    autocommit=False,
    expire_on_commit=False,
)
Base = declarative_base()


def alembic_config(database_url: str | None = None) -> Config:
    config = Config(str(Path(PROJECT_ROOT) / "alembic.ini"))
    if database_url:
        config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    return config


def schema_revisions(connection=None) -> tuple[str | None, str]:
    config = alembic_config()
    scripts = ScriptDirectory.from_config(config)
    expected = scripts.get_current_head()
    if connection is not None:
        current = MigrationContext.configure(connection).get_current_revision()
        return current, expected
    with engine.connect() as current_connection:
        current = MigrationContext.configure(current_connection).get_current_revision()
        return current, expected


def assert_schema_current() -> None:
    current, expected = schema_revisions()
    if current != expected:
        raise RuntimeError(
            "数据库 schema 版本不匹配："
            f"current={current or 'none'} expected={expected}；"
            "请先执行 `uv run alembic upgrade head`"
        )


def init_db() -> None:
    """启动时校验数据库迁移版本。"""
    assert_schema_current()
