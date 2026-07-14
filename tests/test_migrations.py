import tempfile
import unittest
from pathlib import Path

from alembic import command
from sqlalchemy import create_engine, inspect

from backend.infra.database import alembic_config


class MigrationTests(unittest.TestCase):
    def test_empty_database_upgrades_and_downgrades(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "schema.db"
            url = f"sqlite:///{database_path}"
            config = alembic_config(url)

            command.upgrade(config, "head")
            engine = create_engine(url)
            inspector = inspect(engine)
            tables = set(inspector.get_table_names())
            self.assertTrue(
                {
                    "runs",
                    "run_events",
                    "run_checkpoints",
                    "documents",
                    "document_versions",
                    "index_jobs",
                }.issubset(tables)
            )
            self.assertTrue(
                {"status", "version", "message_count", "last_sequence"}.issubset(
                    {
                        column["name"]
                        for column in inspector.get_columns("chat_sessions")
                    }
                )
            )
            self.assertTrue(
                {"run_id", "sequence", "content_json", "status"}.issubset(
                    {
                        column["name"]
                        for column in inspector.get_columns("chat_messages")
                    }
                )
            )
            engine.dispose()

            command.downgrade(config, "base")
            engine = create_engine(url)
            remaining = set(inspect(engine).get_table_names())
            self.assertLessEqual(remaining, {"alembic_version"})
            engine.dispose()


if __name__ == "__main__":
    unittest.main()
