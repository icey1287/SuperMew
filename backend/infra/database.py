from __future__ import annotations

import re
import unicodedata
from pathlib import Path

from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, event
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


_CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_INVISIBLE_CHAR_RE = re.compile(r"[\u200b-\u200d\ufeff\u200f\u202a-\u202e]")


def _clean_nul_chars(value):
    if isinstance(value, str):
        value = unicodedata.normalize("NFC", value)
        value = _INVISIBLE_CHAR_RE.sub("", value)
        value = _CONTROL_CHAR_RE.sub("", value)
        value = re.sub(r"[\ue000-\uf8ff]", "", value)
        try:
            return value.encode("utf-8", "ignore").decode("utf-8", "ignore")
        except Exception:
            return "".join(
                character
                for character in value
                if not 0xD800 <= ord(character) <= 0xDFFF
            )
    if isinstance(value, dict):
        return {key: _clean_nul_chars(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_clean_nul_chars(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_clean_nul_chars(item) for item in value)
    return value


@event.listens_for(engine, "before_cursor_execute", retval=True)
def before_cursor_execute(conn, cursor, statement, parameters, context, executemany):
    if parameters is not None:
        if isinstance(parameters, dict):
            for key, value in list(parameters.items()):
                parameters[key] = _clean_nul_chars(value)
        elif isinstance(parameters, list):
            for index, value in enumerate(parameters):
                parameters[index] = _clean_nul_chars(value)
        elif isinstance(parameters, tuple):
            parameters = tuple(_clean_nul_chars(value) for value in parameters)
    return statement, parameters


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
