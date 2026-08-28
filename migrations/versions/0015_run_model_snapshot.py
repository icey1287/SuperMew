"""freeze non-secret Model Snapshots on Runs

Revision ID: 0015_run_model_snapshot
Revises: 0014_model_control_plane
Create Date: 2026-07-17
"""

from __future__ import annotations

import hashlib
import json

import sqlalchemy as sa
from alembic import op


revision = "0015_run_model_snapshot"
down_revision = "0014_model_control_plane"
branch_labels = None
depends_on = None


def _snapshot(model_name: str) -> dict:
    assignments = {}
    normalized_name = (model_name or "").strip()
    if normalized_name:
        identity = hashlib.sha256(
            f"legacy:{normalized_name}".encode("utf-8")
        ).hexdigest()
        assignments["answer"] = {
            "profile_id": f"model_{identity[:32]}",
            "profile_version": 1,
            "display_name": f"迁移模型 · {normalized_name}",
            "provider": "openai",
            "model_name": normalized_name,
            "base_url": "",
            "timeout_seconds": 30.0,
            "supports_stream": True,
            "supports_structured_output": True,
        }
    encoded = json.dumps(
        assignments,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return {
        "schema_version": 1,
        "catalog_hash": hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
        "assignments": assignments,
    }


def upgrade() -> None:
    op.add_column(
        "runs",
        sa.Column("model_catalog_hash", sa.CHAR(length=64), nullable=True),
    )
    op.add_column(
        "runs",
        sa.Column("model_snapshot_json", sa.JSON(), nullable=True),
    )

    runs = sa.table(
        "runs",
        sa.column("id", sa.String()),
        sa.column("model_name", sa.String()),
        sa.column("model_catalog_hash", sa.CHAR(length=64)),
        sa.column("model_snapshot_json", sa.JSON()),
    )
    connection = op.get_bind()
    for row in connection.execute(sa.select(runs.c.id, runs.c.model_name)):
        snapshot = _snapshot(str(row.model_name or ""))
        connection.execute(
            runs.update()
            .where(runs.c.id == row.id)
            .values(
                model_catalog_hash=snapshot["catalog_hash"],
                model_snapshot_json=snapshot,
            )
        )

    with op.batch_alter_table("runs") as batch_op:
        batch_op.alter_column(
            "model_catalog_hash",
            existing_type=sa.CHAR(length=64),
            nullable=False,
        )
        batch_op.alter_column(
            "model_snapshot_json",
            existing_type=sa.JSON(),
            nullable=False,
        )
        batch_op.create_check_constraint(
            "ck_runs_model_catalog_hash_length",
            "length(model_catalog_hash) = 64",
        )


def downgrade() -> None:
    with op.batch_alter_table("runs") as batch_op:
        batch_op.drop_constraint(
            "ck_runs_model_catalog_hash_length",
            type_="check",
        )
        batch_op.drop_column("model_snapshot_json")
        batch_op.drop_column("model_catalog_hash")
