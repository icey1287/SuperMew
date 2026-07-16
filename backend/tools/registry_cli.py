from __future__ import annotations

import argparse
import json
from collections.abc import Sequence

from backend.core.settings import SkillSettings
from backend.skills import SkillAccess, SkillRegistry
from backend.tools.catalog import configured_secret_names, tool_registry
from backend.tools.registry import ToolAccess


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate and inspect the SuperMew Skill/Tool registries.",
    )
    parser.add_argument(
        "command",
        choices=("validate", "list-tools", "list-skills", "describe-skill"),
    )
    parser.add_argument("name", nargs="?")
    parser.add_argument("--role", action="append", default=[])
    parser.add_argument("--secret-name", action="append", default=[])
    return parser


def _load_skills() -> SkillRegistry:
    settings = SkillSettings(_env_file=None)
    return SkillRegistry.load(
        settings.skill_dir,
        tool_registry.names,
        manifest_name=settings.manifest_name,
        max_content_bytes=settings.max_content_bytes,
    )


def _access(args) -> tuple[ToolAccess, SkillAccess]:
    roles = frozenset(args.role or ["user"])
    configured = configured_secret_names(tool_registry)
    requested = frozenset(args.secret_name)
    available = configured.intersection(requested) if requested else configured
    return (
        ToolAccess(
            roles=roles,
            available_secrets=available,
            caller_allowed_tools=frozenset(tool_registry.names),
            approved_tools=frozenset(),
            allowed_network_policies=frozenset({"none", "restricted"}),
        ),
        SkillAccess(roles=roles, available_secrets=available),
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    skills = _load_skills()
    tool_access, skill_access = _access(args)

    if args.command == "validate":
        payload = {
            "valid": True,
            "tool_count": len(tool_registry.names),
            "skill_count": len(skills.names),
            "tool_catalog_hash": tool_registry.catalog_hash,
        }
    elif args.command == "list-tools":
        payload = {
            "tools": [
                {
                    "name": descriptor.name,
                    "version": descriptor.version,
                    "group": descriptor.group,
                    "description": descriptor.description,
                    "exposure": tool_registry.exposure(name).value,
                }
                for name in tool_registry.names
                if (descriptor := tool_registry.describe(name, tool_access)) is not None
            ]
        }
    elif args.command == "list-skills":
        payload = {
            "skills": [
                {
                    "name": item.name,
                    "version": item.version,
                    "description": item.description,
                    "activation": item.activation,
                }
                for item in skills.catalog(skill_access)
            ]
        }
    else:
        if not args.name:
            raise SystemExit("describe-skill requires a skill name")
        activated = skills.activate(
            args.name,
            skill_access,
            source="operator_cli",
        )
        payload = {
            "name": activated.name,
            "version": activated.version,
            "description": activated.description,
            "content_hash": activated.pin.content_hash,
            "allowed_tools": sorted(activated.allowed_tools),
            "instructions": activated.instructions,
        }

    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
