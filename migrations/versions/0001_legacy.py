"""legacy schema baseline

Revision ID: 0001_legacy
Revises:
Create Date: 2026-07-14
"""

from alembic import op
import sqlalchemy as sa


revision = "0001_legacy"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("username", sa.String(length=100), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("role", sa.String(length=20), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("username"),
    )
    op.create_index("ix_users_id", "users", ["id"], unique=False)
    op.create_index("ix_users_username", "users", ["username"], unique=True)

    op.create_table(
        "chat_sessions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("session_id", sa.String(length=120), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "session_id", name="uq_user_session"),
    )
    op.create_index("ix_chat_sessions_id", "chat_sessions", ["id"], unique=False)
    op.create_index(
        "ix_chat_sessions_session_id", "chat_sessions", ["session_id"], unique=False
    )
    op.create_index(
        "ix_chat_sessions_user_id", "chat_sessions", ["user_id"], unique=False
    )

    op.create_table(
        "chat_messages",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("session_ref_id", sa.Integer(), nullable=False),
        sa.Column("message_type", sa.String(length=20), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("timestamp", sa.DateTime(), nullable=False),
        sa.Column("rag_trace", sa.JSON(), nullable=True),
        sa.ForeignKeyConstraint(
            ["session_ref_id"], ["chat_sessions.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_chat_messages_id", "chat_messages", ["id"], unique=False)
    op.create_index(
        "ix_chat_messages_session_ref_id",
        "chat_messages",
        ["session_ref_id"],
        unique=False,
    )

    op.create_table(
        "parent_chunks",
        sa.Column("chunk_id", sa.String(length=512), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("filename", sa.String(length=255), nullable=False),
        sa.Column("file_type", sa.String(length=50), nullable=False),
        sa.Column("file_path", sa.String(length=1024), nullable=False),
        sa.Column("page_number", sa.Integer(), nullable=False),
        sa.Column("parent_chunk_id", sa.String(length=512), nullable=False),
        sa.Column("root_chunk_id", sa.String(length=512), nullable=False),
        sa.Column("chunk_level", sa.Integer(), nullable=False),
        sa.Column("chunk_idx", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("chunk_id"),
    )
    op.create_index(
        "ix_parent_chunks_filename", "parent_chunks", ["filename"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_parent_chunks_filename", table_name="parent_chunks")
    op.drop_table("parent_chunks")
    op.drop_index("ix_chat_messages_session_ref_id", table_name="chat_messages")
    op.drop_index("ix_chat_messages_id", table_name="chat_messages")
    op.drop_table("chat_messages")
    op.drop_index("ix_chat_sessions_user_id", table_name="chat_sessions")
    op.drop_index("ix_chat_sessions_session_id", table_name="chat_sessions")
    op.drop_index("ix_chat_sessions_id", table_name="chat_sessions")
    op.drop_table("chat_sessions")
    op.drop_index("ix_users_username", table_name="users")
    op.drop_index("ix_users_id", table_name="users")
    op.drop_table("users")
