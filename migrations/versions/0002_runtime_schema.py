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
    from backend.db.models import Base

    bind = op.get_bind()
    for name in NEW_TABLES_IN_ORDER:
        Base.metadata.tables[name].create(bind=bind, checkfirst=True)


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

    from backend.db.models import Base

    bind = op.get_bind()
    for name in reversed(NEW_TABLES_IN_ORDER):
        Base.metadata.tables[name].drop(bind=bind, checkfirst=True)
