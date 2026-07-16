import hashlib
import tempfile
import unittest
from pathlib import Path

from alembic import command
from sqlalchemy import create_engine, inspect, text

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
                {
                    "tool_call_id",
                    "audit_key",
                    "tool_version",
                    "skill_name",
                    "result_size",
                }.issubset(
                    {column["name"] for column in inspector.get_columns("tool_audits")}
                )
            )
            self.assertIn(
                {"run_id", "audit_key"},
                {
                    frozenset(constraint["column_names"])
                    for constraint in inspector.get_unique_constraints("tool_audits")
                },
            )
            self.assertTrue(
                {"run_id", "sequence", "content_json", "status"}.issubset(
                    {
                        column["name"]
                        for column in inspector.get_columns("chat_messages")
                    }
                )
            )
            self.assertTrue(
                {
                    "skill_name",
                    "skill_version",
                    "skill_content_hash",
                    "skill_activation_source",
                }.issubset({column["name"] for column in inspector.get_columns("runs")})
            )
            engine.dispose()

            command.downgrade(config, "base")
            engine = create_engine(url)
            remaining = set(inspect(engine).get_table_names())
            self.assertLessEqual(remaining, {"alembic_version"})
            engine.dispose()

    def test_skill_tool_registry_migrates_legacy_audits_and_downgrades_one_step(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "legacy-audit.db"
            url = f"sqlite:///{database_path}"
            config = alembic_config(url)
            command.upgrade(config, "0008_indexing_worker")

            engine = create_engine(url)
            with engine.begin() as connection:
                connection.execute(
                    text(
                        """
                        INSERT INTO tool_audits (
                            id, user_id, thread_id, run_id, tool_name, decision,
                            success, error_code, duration_ms, metadata_json, created_at
                        ) VALUES (
                            41, NULL, 'legacy-thread', NULL, 'legacy_tool', 'allowed',
                            1, NULL, 7, '{}', CURRENT_TIMESTAMP
                        )
                        """
                    )
                )
            engine.dispose()

            command.upgrade(config, "0009_skill_tool_registry")
            engine = create_engine(url)
            with engine.connect() as connection:
                migrated = (
                    connection.execute(
                        text(
                            """
                        SELECT audit_key, tool_call_id, tool_version, skill_name,
                               result_size
                        FROM tool_audits WHERE id = 41
                        """
                        )
                    )
                    .mappings()
                    .one()
                )
            self.assertEqual(
                hashlib.sha256(b"legacy-tool-audit:41").hexdigest(),
                migrated["audit_key"],
            )
            self.assertIsNone(migrated["tool_call_id"])
            self.assertEqual("", migrated["tool_version"])
            self.assertEqual("", migrated["skill_name"])
            self.assertEqual(0, migrated["result_size"])
            self.assertFalse(migrated["audit_key"].startswith("legacy-"))
            engine.dispose()

            command.downgrade(config, "0008_indexing_worker")
            engine = create_engine(url)
            columns = {
                column["name"] for column in inspect(engine).get_columns("tool_audits")
            }
            self.assertTrue(
                {
                    "tool_call_id",
                    "audit_key",
                    "tool_version",
                    "skill_name",
                    "result_size",
                }.isdisjoint(columns)
            )
            with engine.connect() as connection:
                remaining = (
                    connection.execute(
                        text(
                            "SELECT tool_name, duration_ms FROM tool_audits WHERE id = 41"
                        )
                    )
                    .mappings()
                    .one()
                )
            self.assertEqual("legacy_tool", remaining["tool_name"])
            self.assertEqual(7, remaining["duration_ms"])
            engine.dispose()

            command.downgrade(config, "base")


if __name__ == "__main__":
    unittest.main()
