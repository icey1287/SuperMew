from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path

import pytest

from backend.capabilities.catalog import CapabilityCatalog
from backend.skills import SkillRegistry
from backend.tools.registry import ToolDescriptor, ToolExposure, ToolRegistry


def _descriptor(
    name: str,
    *,
    group: str,
    required_roles: frozenset[str] = frozenset(),
    required_secrets: frozenset[str] = frozenset(),
    requires_approval: bool = False,
    network_policy: str = "none",
    resource_scope: str = "none",
    idempotent: bool = True,
) -> ToolDescriptor:
    return ToolDescriptor(
        name=name,
        description=f"Use {name} safely.",
        group=group,
        version="1.0.0",
        input_schema={
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
        output_schema={"type": "object"},
        timeout=5.0,
        max_concurrency=2,
        idempotent=idempotent,
        required_roles=required_roles,
        required_secrets=required_secrets,
        requires_approval=requires_approval,
        network_policy=network_policy,
        resource_scope=resource_scope,
        result_size_limit=16_384,
    )


def _write_skill(
    root: Path,
    *,
    name: str,
    description: str,
    tool_names: tuple[str, ...],
    required_roles: tuple[str, ...] = (),
    required_secrets: tuple[str, ...] = (),
) -> None:
    skill_dir = root / name
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        f"# {name}\nprivate instructions", encoding="utf-8"
    )
    manifest = [
        "schema_version: 1",
        f"name: {name}",
        "version: 1.0.0",
        f"description: {description}",
        "allowed_tools:",
        *[f"  - {tool_name}" for tool_name in tool_names],
        "required_roles:" if required_roles else "required_roles: []",
        *[f"  - {role}" for role in required_roles],
        "required_secrets:" if required_secrets else "required_secrets: []",
        *[f"  - {secret}" for secret in required_secrets],
        "entrypoint: SKILL.md",
    ]
    (skill_dir / "skill.yaml").write_text("\n".join(manifest), encoding="utf-8")


def _catalog(
    tmp_path: Path,
    *,
    secrets: frozenset[str],
    secret_names_provider: Callable[[ToolRegistry], frozenset[str]] | None = None,
) -> CapabilityCatalog:
    tools = ToolRegistry()
    tools.register(
        _descriptor(
            "web_search",
            group="web-research",
            required_secrets=frozenset({"BRAVE_SEARCH_API_KEY"}),
            network_policy="restricted",
            resource_scope="public-web",
        ),
        lambda _context: None,
        exposure=ToolExposure.DEFERRED,
    )
    tools.register(
        _descriptor(
            "sandbox_execute",
            group="sandbox-execution",
            required_roles=frozenset({"admin"}),
            required_secrets=frozenset({"SANDBOX_RUNTIME"}),
            requires_approval=True,
            resource_scope="code-execution",
            idempotent=False,
        ),
        lambda _context: None,
        exposure=ToolExposure.DEFERRED,
    )
    tools.freeze()

    root = tmp_path / "skills"
    root.mkdir()
    _write_skill(
        root,
        name="web-research",
        description="Research the public web.",
        tool_names=("web_search",),
        required_secrets=("BRAVE_SEARCH_API_KEY",),
    )
    _write_skill(
        root,
        name="sandbox",
        description="Run isolated code.",
        tool_names=("sandbox_execute",),
        required_roles=("admin",),
        required_secrets=("SANDBOX_RUNTIME",),
    )
    skills = SkillRegistry.load(root, tools.names)
    return CapabilityCatalog(
        skills=skills,
        tools=tools,
        secret_names_provider=(
            secret_names_provider
            if secret_names_provider is not None
            else lambda _tools: secrets
        ),
    )


def _keys(value: object) -> set[str]:
    if isinstance(value, dict):
        return set(value).union(*(_keys(item) for item in value.values()))
    if isinstance(value, (list, tuple)):
        return set().union(*(_keys(item) for item in value))
    return set()


def test_snapshot_projects_role_and_configuration_without_secret_material(
    tmp_path: Path,
) -> None:
    catalog = _catalog(
        tmp_path,
        secrets=frozenset({"BRAVE_SEARCH_API_KEY", "SANDBOX_RUNTIME"}),
    )

    snapshot = catalog.snapshot(role="user")
    skills = {item.name: item for item in snapshot.skills}
    tools = {item.name: item for item in snapshot.tools}

    assert snapshot.schema_version == 1
    assert len(snapshot.catalog_hash) == 64
    assert skills["web-research"].available is True
    assert skills["sandbox"].available is False
    assert skills["sandbox"].availability_reason == "permission_required"
    assert skills["sandbox"].approval_tools == ("sandbox_execute",)
    assert skills["sandbox"].network_policies == ("none",)
    assert skills["sandbox"].resource_scopes == ("code-execution",)
    assert tools["sandbox_execute"].available is False
    assert tools["sandbox_execute"].requires_approval is True
    assert tools["sandbox_execute"].availability_reason == "permission_required"

    forbidden = {
        "required_secrets",
        "input_schema",
        "output_schema",
        "instructions",
        "content_hash",
        "secret",
    }
    assert _keys(asdict(snapshot)).isdisjoint(forbidden)
    assert "BRAVE_SEARCH_API_KEY" not in str(asdict(snapshot))
    assert "SANDBOX_RUNTIME" not in str(asdict(snapshot))
    assert "private instructions" not in str(asdict(snapshot))


def test_approval_only_tool_is_available_before_run_preapproval(tmp_path: Path) -> None:
    catalog = _catalog(
        tmp_path,
        secrets=frozenset({"BRAVE_SEARCH_API_KEY", "SANDBOX_RUNTIME"}),
    )

    snapshot = catalog.snapshot(role="admin")
    sandbox_skill = next(item for item in snapshot.skills if item.name == "sandbox")
    sandbox_tool = next(
        item for item in snapshot.tools if item.name == "sandbox_execute"
    )

    assert sandbox_skill.available is True
    assert sandbox_skill.availability_reason is None
    assert sandbox_skill.approval_tools == ("sandbox_execute",)
    assert sandbox_tool.available is True
    assert sandbox_tool.availability_reason is None
    assert sandbox_tool.requires_approval is True


def test_missing_runtime_configuration_is_a_stable_unavailable_reason(
    tmp_path: Path,
) -> None:
    catalog = _catalog(
        tmp_path,
        secrets=frozenset({"BRAVE_SEARCH_API_KEY"}),
    )

    snapshot = catalog.snapshot(role="admin")
    sandbox_skill = next(item for item in snapshot.skills if item.name == "sandbox")
    sandbox_tool = next(
        item for item in snapshot.tools if item.name == "sandbox_execute"
    )

    assert sandbox_skill.availability_reason == "not_configured"
    assert sandbox_tool.availability_reason == "not_configured"


def test_secret_provider_failures_are_not_silently_downgraded(tmp_path: Path) -> None:
    def unavailable(_tools: ToolRegistry) -> frozenset[str]:
        raise RuntimeError("secret provider unavailable")

    catalog = _catalog(
        tmp_path,
        secrets=frozenset(),
        secret_names_provider=unavailable,
    )

    with pytest.raises(RuntimeError, match="secret provider unavailable"):
        catalog.snapshot(role="admin")
