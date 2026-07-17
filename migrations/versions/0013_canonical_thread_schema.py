"""rename Thread and Message persistence schema to canonical domain names

Revision ID: 0013_canonical_thread_schema
Revises: 0012_remove_document_compat
Create Date: 2026-07-17
"""

import sqlalchemy as sa
from alembic import op


revision = "0013_canonical_thread_schema"
down_revision = "0012_remove_document_compat"
branch_labels = None
depends_on = None


def _rename_postgresql_constraint(
    table_name: str,
    old_name: str,
    new_name: str,
) -> None:
    connection = op.get_bind()
    if connection.dialect.name != "postgresql":
        return
    names = {
        item["name"]
        for item in sa.inspect(connection).get_foreign_keys(table_name)
        if item.get("name")
    }
    primary = sa.inspect(connection).get_pk_constraint(table_name).get("name")
    if primary:
        names.add(primary)
    if old_name in names:
        op.execute(
            sa.text(
                f'ALTER TABLE "{table_name}" RENAME CONSTRAINT '
                f'"{old_name}" TO "{new_name}"'
            )
        )


def upgrade() -> None:
    for index_name in (
        "ix_chat_sessions_id",
        "ix_chat_sessions_session_id",
        "ix_chat_sessions_status",
        "ix_chat_sessions_user_id",
    ):
        op.drop_index(index_name, table_name="chat_sessions")
    with op.batch_alter_table("chat_sessions") as batch:
        batch.drop_constraint("uq_user_session", type_="unique")
        batch.alter_column("session_id", new_column_name="thread_id")
    op.rename_table("chat_sessions", "threads")
    with op.batch_alter_table("threads") as batch:
        batch.create_unique_constraint(
            "uq_user_thread",
            ["user_id", "thread_id"],
        )
    op.create_index("ix_threads_id", "threads", ["id"], unique=False)
    op.create_index("ix_threads_thread_id", "threads", ["thread_id"], unique=False)
    op.create_index("ix_threads_status", "threads", ["status"], unique=False)
    op.create_index("ix_threads_user_id", "threads", ["user_id"], unique=False)

    for index_name in (
        "ix_chat_messages_client_message_id",
        "ix_chat_messages_id",
        "ix_chat_messages_run_id",
        "ix_chat_messages_session_ref_id",
        "ix_chat_messages_status",
    ):
        op.drop_index(index_name, table_name="chat_messages")
    with op.batch_alter_table("chat_messages") as batch:
        batch.drop_constraint("uq_chat_message_thread_sequence", type_="unique")
        batch.drop_constraint("uq_chat_message_client_id", type_="unique")
        batch.drop_constraint("fk_chat_messages_run_id_runs", type_="foreignkey")
        batch.alter_column("session_ref_id", new_column_name="thread_ref_id")
        batch.create_foreign_key(
            "fk_messages_run_id_runs",
            "runs",
            ["run_id"],
            ["id"],
            ondelete="SET NULL",
        )
    op.rename_table("chat_messages", "messages")
    with op.batch_alter_table("messages") as batch:
        batch.create_unique_constraint(
            "uq_message_thread_sequence",
            ["thread_ref_id", "sequence"],
        )
        batch.create_unique_constraint(
            "uq_message_client_id",
            ["thread_ref_id", "client_message_id"],
        )
    op.create_index(
        "ix_messages_client_message_id",
        "messages",
        ["client_message_id"],
        unique=False,
    )
    op.create_index("ix_messages_id", "messages", ["id"], unique=False)
    op.create_index("ix_messages_run_id", "messages", ["run_id"], unique=False)
    op.create_index(
        "ix_messages_thread_ref_id",
        "messages",
        ["thread_ref_id"],
        unique=False,
    )
    op.create_index("ix_messages_status", "messages", ["status"], unique=False)

    op.get_bind().execute(
        sa.text("UPDATE runs SET channel = 'run' WHERE channel = 'chat'")
    )
    with op.batch_alter_table("runs") as batch:
        batch.alter_column(
            "channel",
            existing_type=sa.String(length=32),
            server_default="run",
            existing_nullable=False,
        )

    _rename_postgresql_constraint(
        "threads",
        "chat_sessions_pkey",
        "threads_pkey",
    )
    _rename_postgresql_constraint(
        "threads",
        "chat_sessions_user_id_fkey",
        "threads_user_id_fkey",
    )
    _rename_postgresql_constraint(
        "messages",
        "chat_messages_pkey",
        "messages_pkey",
    )
    _rename_postgresql_constraint(
        "messages",
        "chat_messages_session_ref_id_fkey",
        "messages_thread_ref_id_fkey",
    )
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            "ALTER SEQUENCE IF EXISTS chat_sessions_id_seq RENAME TO threads_id_seq"
        )
        op.execute(
            "ALTER SEQUENCE IF EXISTS chat_messages_id_seq RENAME TO messages_id_seq"
        )


def downgrade() -> None:
    raise RuntimeError("canonical Thread schema migration is irreversible")
