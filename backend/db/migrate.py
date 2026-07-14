from __future__ import annotations

import argparse

from alembic import command
from sqlalchemy import inspect

from backend.infra.database import alembic_config, engine


LEGACY_TABLES = {"users", "chat_sessions", "chat_messages", "parent_chunks"}


def adopt_legacy() -> None:
    with engine.connect() as connection:
        existing = set(inspect(connection).get_table_names())
    missing = LEGACY_TABLES - existing
    if missing:
        raise RuntimeError(f"不是可识别的旧版数据库，缺少表：{sorted(missing)}")
    config = alembic_config()
    command.stamp(config, "0001_legacy")
    command.upgrade(config, "head")


def main() -> None:
    parser = argparse.ArgumentParser(description="SuperMew schema migration helper")
    parser.add_argument(
        "action", choices=["upgrade", "downgrade", "current", "adopt-legacy"]
    )
    parser.add_argument("revision", nargs="?", default="head")
    args = parser.parse_args()
    config = alembic_config()
    if args.action == "upgrade":
        command.upgrade(config, args.revision)
    elif args.action == "downgrade":
        command.downgrade(config, args.revision)
    elif args.action == "current":
        command.current(config, verbose=True)
    else:
        adopt_legacy()


if __name__ == "__main__":
    main()
