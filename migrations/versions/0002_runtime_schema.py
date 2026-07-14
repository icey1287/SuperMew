"""run, event, checkpoint and document catalog schema

Revision ID: 0002_runtime
Revises: 0001_legacy
Create Date: 2026-07-14
"""

from alembic import op
import sqlalchemy as sa


revision = "0002_runtime"
down_revision = "0001_legacy"
branch_labels = None
depends_on = None


NEW_TABLES_IN_ORDER = [
    "refresh_tokens",
    "runs",
    "run_events",
    "run_checkpoints",
    "knowledge_bases",
    "documents",
    "document_versions",
    "index_jobs",
    "index_manifests",
    "transaction_outbox",
    "tool_audits",
]


def _create_new_tables() -> None:
    op.create_table(
        "refresh_tokens",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash"),
    )
    op.create_index("ix_refresh_tokens_user_id", "refresh_tokens", ["user_id"])

    op.create_table(
        "runs",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("thread_ref_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("model_name", sa.String(length=160), nullable=False),
        sa.Column("on_disconnect", sa.String(length=16), nullable=False),
        sa.Column("multitask_strategy", sa.String(length=24), nullable=False),
        sa.Column("owner_worker_id", sa.String(length=128), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(), nullable=True),
        sa.Column("deadline_at", sa.DateTime(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_detail_redacted", sa.Text(), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False),
        sa.Column("cost", sa.Numeric(18, 6), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["thread_ref_id"], ["chat_sessions.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "user_id",
            "thread_ref_id",
            "idempotency_key",
            name="uq_run_user_thread_idempotency",
        ),
    )
    op.create_index("ix_runs_thread_ref_id", "runs", ["thread_ref_id"])
    op.create_index("ix_runs_user_id", "runs", ["user_id"])
    op.create_index("ix_runs_status", "runs", ["status"])
    op.create_index(
        "uq_runs_one_active_per_thread",
        "runs",
        ["thread_ref_id"],
        unique=True,
        postgresql_where=sa.text(
            "status IN ('pending', 'running', 'waiting_input', 'cancelling')"
        ),
        sqlite_where=sa.text(
            "status IN ('pending', 'running', 'waiting_input', 'cancelling')"
        ),
    )

    op.create_table(
        "run_events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("event_id", sa.String(length=80), nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(length=80), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("event_id"),
        sa.UniqueConstraint("run_id", "sequence", name="uq_run_event_sequence"),
    )
    op.create_index("ix_run_events_run_id", "run_events", ["run_id"])
    op.create_index("ix_run_events_event_type", "run_events", ["event_type"])

    op.create_table(
        "run_checkpoints",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("thread_ref_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("checkpoint_id", sa.String(length=128), nullable=False),
        sa.Column("hitl_token", sa.String(length=128), nullable=True),
        sa.Column("state_json", sa.JSON(), nullable=False),
        sa.Column("next_nodes_json", sa.JSON(), nullable=False),
        sa.Column("consumed_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["thread_ref_id"], ["chat_sessions.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("hitl_token"),
        sa.UniqueConstraint("run_id", "checkpoint_id", name="uq_run_checkpoint"),
    )
    op.create_index("ix_run_checkpoints_run_id", "run_checkpoints", ["run_id"])
    op.create_index(
        "ix_run_checkpoints_thread_ref_id", "run_checkpoints", ["thread_ref_id"]
    )
    op.create_index("ix_run_checkpoints_user_id", "run_checkpoints", ["user_id"])

    op.create_table(
        "knowledge_bases",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("owner_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["owner_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "name", name="uq_knowledge_base_tenant_name"),
    )
    op.create_index("ix_knowledge_bases_tenant_id", "knowledge_bases", ["tenant_id"])
    op.create_index("ix_knowledge_bases_owner_id", "knowledge_bases", ["owner_id"])

    op.create_table(
        "documents",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("knowledge_base_id", sa.String(length=64), nullable=False),
        sa.Column("canonical_name", sa.String(length=255), nullable=False),
        sa.Column("owner_id", sa.Integer(), nullable=False),
        sa.Column("current_version_id", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["knowledge_base_id"], ["knowledge_bases.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["owner_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "knowledge_base_id", "canonical_name", name="uq_document_canonical_name"
        ),
    )
    op.create_index("ix_documents_tenant_id", "documents", ["tenant_id"])
    op.create_index(
        "ix_documents_knowledge_base_id", "documents", ["knowledge_base_id"]
    )
    op.create_index("ix_documents_owner_id", "documents", ["owner_id"])
    op.create_index("ix_documents_status", "documents", ["status"])

    op.create_table(
        "document_versions",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("document_id", sa.String(length=64), nullable=False),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.Column("source_object_key", sa.String(length=512), nullable=False),
        sa.Column("media_type", sa.String(length=160), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("parser_version", sa.String(length=64), nullable=False),
        sa.Column("chunker_version", sa.String(length=64), nullable=False),
        sa.Column("embedding_model", sa.String(length=160), nullable=False),
        sa.Column("index_version", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("chunk_count", sa.Integer(), nullable=False),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_detail_redacted", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "document_id", "content_sha256", name="uq_document_content_hash"
        ),
    )
    op.create_index(
        "ix_document_versions_document_id", "document_versions", ["document_id"]
    )
    op.create_index(
        "ix_document_versions_content_sha256", "document_versions", ["content_sha256"]
    )
    op.create_index("ix_document_versions_status", "document_versions", ["status"])

    op.create_table(
        "index_jobs",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("document_version_id", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("current_step", sa.String(length=64), nullable=False),
        sa.Column("progress", sa.Integer(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("owner_worker_id", sa.String(length=128), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(), nullable=True),
        sa.Column("next_retry_at", sa.DateTime(), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_detail_redacted", sa.Text(), nullable=True),
        sa.Column("step_state_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["document_version_id"], ["document_versions.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_index_jobs_document_version_id", "index_jobs", ["document_version_id"]
    )
    op.create_index("ix_index_jobs_status", "index_jobs", ["status"])

    op.create_table(
        "index_manifests",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("document_version_id", sa.String(length=64), nullable=False),
        sa.Column("chunk_id", sa.String(length=512), nullable=False),
        sa.Column("section_id", sa.String(length=256), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("indexed_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["document_version_id"], ["document_versions.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "document_version_id", "chunk_id", name="uq_index_manifest_chunk"
        ),
    )
    op.create_index(
        "ix_index_manifests_document_version_id",
        "index_manifests",
        ["document_version_id"],
    )

    op.create_table(
        "transaction_outbox",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("topic", sa.String(length=128), nullable=False),
        sa.Column("aggregate_id", sa.String(length=128), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("published_at", sa.DateTime(), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_transaction_outbox_topic", "transaction_outbox", ["topic"])
    op.create_index(
        "ix_transaction_outbox_aggregate_id", "transaction_outbox", ["aggregate_id"]
    )

    op.create_table(
        "tool_audits",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("thread_id", sa.String(length=120), nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=True),
        sa.Column("tool_name", sa.String(length=128), nullable=False),
        sa.Column("decision", sa.String(length=32), nullable=False),
        sa.Column("success", sa.Boolean(), nullable=False),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_tool_audits_user_id", "tool_audits", ["user_id"])
    op.create_index("ix_tool_audits_run_id", "tool_audits", ["run_id"])
    op.create_index("ix_tool_audits_tool_name", "tool_audits", ["tool_name"])


def upgrade() -> None:
    _create_new_tables()

    op.add_column(
        "chat_sessions",
        sa.Column(
            "status", sa.String(length=24), server_default="active", nullable=False
        ),
    )
    op.add_column(
        "chat_sessions",
        sa.Column("version", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column(
        "chat_sessions",
        sa.Column("message_count", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column(
        "chat_sessions",
        sa.Column("last_sequence", sa.Integer(), server_default="0", nullable=False),
    )
    op.create_index(
        "ix_chat_sessions_status", "chat_sessions", ["status"], unique=False
    )

    op.add_column(
        "chat_messages", sa.Column("run_id", sa.String(length=64), nullable=True)
    )
    op.add_column(
        "chat_messages",
        sa.Column("sequence", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column("chat_messages", sa.Column("content_json", sa.JSON(), nullable=True))
    op.add_column(
        "chat_messages",
        sa.Column(
            "status", sa.String(length=24), server_default="completed", nullable=False
        ),
    )
    op.add_column(
        "chat_messages",
        sa.Column(
            "updated_at",
            sa.DateTime(),
            server_default=sa.func.current_timestamp(),
            nullable=False,
        ),
    )
    op.execute(
        """
        WITH ranked AS (
            SELECT id, ROW_NUMBER() OVER (PARTITION BY session_ref_id ORDER BY id) AS seq
            FROM chat_messages
        )
        UPDATE chat_messages
        SET sequence = (SELECT seq FROM ranked WHERE ranked.id = chat_messages.id)
        """
    )
    op.execute(
        """
        UPDATE chat_sessions
        SET message_count = (
                SELECT COUNT(*) FROM chat_messages WHERE chat_messages.session_ref_id = chat_sessions.id
            ),
            last_sequence = COALESCE((
                SELECT MAX(sequence) FROM chat_messages WHERE chat_messages.session_ref_id = chat_sessions.id
            ), 0)
        """
    )
    with op.batch_alter_table("chat_messages") as batch:
        batch.create_foreign_key(
            "fk_chat_messages_run_id_runs",
            "runs",
            ["run_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch.create_unique_constraint(
            "uq_chat_message_thread_sequence", ["session_ref_id", "sequence"]
        )
    op.create_index(
        "ix_chat_messages_run_id", "chat_messages", ["run_id"], unique=False
    )
    op.create_index(
        "ix_chat_messages_status", "chat_messages", ["status"], unique=False
    )

    parent_columns = [
        sa.Column(
            "tenant_id", sa.String(length=64), server_default="default", nullable=False
        ),
        sa.Column(
            "knowledge_base_id", sa.String(length=64), server_default="", nullable=False
        ),
        sa.Column(
            "document_id", sa.String(length=64), server_default="", nullable=False
        ),
        sa.Column(
            "document_version_id",
            sa.String(length=64),
            server_default="",
            nullable=False,
        ),
        sa.Column(
            "section_id", sa.String(length=256), server_default="", nullable=False
        ),
        sa.Column(
            "index_version", sa.String(length=64), server_default="v1", nullable=False
        ),
        sa.Column("acl_tags", sa.JSON(), server_default="[]", nullable=False),
    ]
    for column in parent_columns:
        op.add_column("parent_chunks", column)
    for name in (
        "tenant_id",
        "knowledge_base_id",
        "document_id",
        "document_version_id",
    ):
        op.create_index(
            f"ix_parent_chunks_{name}", "parent_chunks", [name], unique=False
        )


def downgrade() -> None:
    for name in (
        "document_version_id",
        "document_id",
        "knowledge_base_id",
        "tenant_id",
    ):
        op.drop_index(f"ix_parent_chunks_{name}", table_name="parent_chunks")
    with op.batch_alter_table("parent_chunks") as batch:
        for name in (
            "acl_tags",
            "index_version",
            "section_id",
            "document_version_id",
            "document_id",
            "knowledge_base_id",
            "tenant_id",
        ):
            batch.drop_column(name)

    op.drop_index("ix_chat_messages_status", table_name="chat_messages")
    op.drop_index("ix_chat_messages_run_id", table_name="chat_messages")
    with op.batch_alter_table("chat_messages") as batch:
        batch.drop_constraint("uq_chat_message_thread_sequence", type_="unique")
        batch.drop_constraint("fk_chat_messages_run_id_runs", type_="foreignkey")
        batch.drop_column("updated_at")
        batch.drop_column("status")
        batch.drop_column("content_json")
        batch.drop_column("sequence")
        batch.drop_column("run_id")

    op.drop_index("ix_chat_sessions_status", table_name="chat_sessions")
    with op.batch_alter_table("chat_sessions") as batch:
        batch.drop_column("last_sequence")
        batch.drop_column("message_count")
        batch.drop_column("version")
        batch.drop_column("status")

    for name in reversed(NEW_TABLES_IN_ORDER):
        op.drop_table(name)
