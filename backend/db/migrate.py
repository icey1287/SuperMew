from __future__ import annotations

import argparse

from alembic import command

from backend.infra.database import alembic_config


def main() -> None:
    parser = argparse.ArgumentParser(description="SuperMew schema migration helper")
    parser.add_argument("action", choices=["upgrade", "downgrade", "current"])
    parser.add_argument("revision", nargs="?", default="head")
    args = parser.parse_args()
    config = alembic_config()
    if args.action == "upgrade":
        command.upgrade(config, args.revision)
    elif args.action == "downgrade":
        command.downgrade(config, args.revision)
    elif args.action == "current":
        command.current(config, verbose=True)


if __name__ == "__main__":
    main()
