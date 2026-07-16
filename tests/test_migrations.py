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
                    "reason_code",
                    "policy_version",
                    "policy_hash",
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
                    "tenant_id",
                    "channel",
                    "approved_tools_json",
                }.issubset({column["name"] for column in inspector.get_columns("runs")})
            )
            engine.dispose()

            command.downgrade(config, "base")
            engine = create_engine(url)
            remaining = set(inspect(engine).get_table_names())
            self.assertLessEqual(remaining, {"alembic_version"})
            engine.dispose()

    def test_guardrail_context_migrates_existing_runs_and_downgrades_one_step(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "guardrail-context.db"
            url = f"sqlite:///{database_path}"
            config = alembic_config(url)
            command.upgrade(config, "0009_skill_tool_registry")

            engine = create_engine(url)
            with engine.begin() as connection:
                connection.execute(
                    text(
                        """
                        INSERT INTO users (
                            id, username, password_hash, role, created_at
                        ) VALUES (1, 'legacy', 'hash', 'user', CURRENT_TIMESTAMP)
                        """
                    )
                )
                connection.execute(
                    text(
                        """
                        INSERT INTO chat_sessions (
                            id, user_id, session_id, status, version,
                            message_count, last_sequence, metadata_json,
                            updated_at, created_at
                        ) VALUES (
                            1, 1, 'legacy-thread', 'active', 0,
                            0, 0, '{}', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                        )
                        """
                    )
                )
                connection.execute(
                    text(
                        """
                        INSERT INTO runs (
                            id, thread_ref_id, user_id, status, idempotency_key,
                            request_hash, model_name, on_disconnect,
                            multitask_strategy, fencing_token, last_event_sequence,
                            input_tokens, output_tokens, cost, created_at, updated_at
                        ) VALUES (
                            'run_legacy', 1, 1, 'pending', 'legacy-key',
                            :request_hash, 'answer', 'continue', 'reject', 1, 0,
                            0, 0, 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                        )
                        """
                    ),
                    {"request_hash": "a" * 64},
                )
                connection.execute(
                    text(
                        """
                        INSERT INTO tool_audits (
                            user_id, thread_id, run_id, tool_call_id, audit_key,
                            tool_name, tool_version, skill_name, decision, success,
                            error_code, duration_ms, result_size, metadata_json,
                            created_at
                        ) VALUES (
                            1, 'legacy-thread', 'run_legacy', 'call-1', :audit_key,
                            'legacy_tool', '1.0.0', '', 'allowed', 1,
                            NULL, 3, 4, '{}', CURRENT_TIMESTAMP
                        )
                        """
                    ),
                    {"audit_key": "b" * 64},
                )
            engine.dispose()

            command.upgrade(config, "0010_guardrails_and_sandbox")
            engine = create_engine(url)
            with engine.connect() as connection:
                migrated_run = (
                    connection.execute(
                        text(
                            """
                        SELECT tenant_id, channel, approved_tools_json
                        FROM runs WHERE id = 'run_legacy'
                        """
                        )
                    )
                    .mappings()
                    .one()
                )
                migrated_audit = (
                    connection.execute(
                        text(
                            """
                        SELECT reason_code, policy_version, policy_hash
                        FROM tool_audits WHERE run_id = 'run_legacy'
                        """
                        )
                    )
                    .mappings()
                    .one()
                )
            self.assertEqual("default", migrated_run["tenant_id"])
            self.assertEqual("chat", migrated_run["channel"])
            self.assertIn(migrated_run["approved_tools_json"], ("[]", []))
            self.assertEqual(
                {
                    "reason_code": None,
                    "policy_version": None,
                    "policy_hash": None,
                },
                dict(migrated_audit),
            )
            engine.dispose()

            command.downgrade(config, "0009_skill_tool_registry")
            engine = create_engine(url)
            inspector = inspect(engine)
            self.assertTrue(
                {"tenant_id", "channel", "approved_tools_json"}.isdisjoint(
                    {column["name"] for column in inspector.get_columns("runs")}
                )
            )
            self.assertTrue(
                {"reason_code", "policy_version", "policy_hash"}.isdisjoint(
                    {column["name"] for column in inspector.get_columns("tool_audits")}
                )
            )
            with engine.connect() as connection:
                self.assertEqual(
                    "run_legacy",
                    connection.execute(
                        text("SELECT id FROM runs WHERE id = 'run_legacy'")
                    ).scalar_one(),
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
