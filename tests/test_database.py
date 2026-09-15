import os
import subprocess
import sys


def test_application_engine_preserves_unicode_parameters():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            """
from sqlalchemy import text
from backend.infra.database import engine

values = ["👩‍💻", "e\\u0301", "字\\ue000符", "a\\u200bb", "行\\n列\\t值"]
try:
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE content (value TEXT)"))
        connection.execute(
            text("INSERT INTO content (value) VALUES (:value)"),
            [{"value": value} for value in values],
        )
        stored = connection.execute(text("SELECT value FROM content ORDER BY rowid"))
        assert list(stored.scalars()) == values
finally:
    engine.dispose()
""",
        ],
        env={**os.environ, "DATABASE_URL": "sqlite://"},
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
