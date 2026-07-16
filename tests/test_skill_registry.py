from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path

import pytest

from backend.skills import (
    SkillAccess,
    SkillAccessDeniedError,
    SkillActivationSession,
    SkillAlreadyActiveError,
    SkillNotFoundError,
    SkillPin,
    SkillPinMismatchError,
    SkillRegistry,
    SkillRegistryError,
)


def _write_skill(
    root: Path,
    *,
    directory: str = "analysis",
    name: str = "analysis",
    version: str = "1.2.3",
    description: str = "Analyze trusted data.",
    allowed_tools: tuple[str, ...] = ("query_data",),
    required_roles: tuple[str, ...] = (),
    required_secrets: tuple[str, ...] = (),
    entrypoint: str = "SKILL.md",
    content: bytes = b"# Analysis\nUse the approved query tool.",
    extra_manifest: str = "",
) -> Path:
    skill_dir = root / directory
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / entrypoint).parent.mkdir(parents=True, exist_ok=True)
    (skill_dir / entrypoint).write_bytes(content)
    manifest = [
        "schema_version: 1",
        f"name: {name}",
        f"version: {version}",
        f"description: {description}",
        "allowed_tools:" if allowed_tools else "allowed_tools: []",
        *[f"  - {tool}" for tool in allowed_tools],
        "required_roles:" if required_roles else "required_roles: []",
        *[f"  - {role}" for role in required_roles],
        "required_secrets:" if required_secrets else "required_secrets: []",
        *[f"  - {secret}" for secret in required_secrets],
        f"entrypoint: {entrypoint}",
    ]
    if extra_manifest:
        manifest.append(extra_manifest)
    (skill_dir / "skill.yaml").write_text("\n".join(manifest), encoding="utf-8")
    return skill_dir


def test_load_catalog_activate_and_freeze_content(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    root.mkdir()
    skill_dir = _write_skill(root)

    registry = SkillRegistry.load(root, {"query_data"})
    summary = registry.catalog(SkillAccess())[0]
    assert (summary.name, summary.version, summary.activation) == (
        "analysis",
        "1.2.3",
        "/analysis",
    )

    first = registry.activate("analysis", SkillAccess(), "router")
    (skill_dir / "SKILL.md").write_text("changed after startup", encoding="utf-8")
    second = registry.activate("analysis", SkillAccess(), "describe_skill")

    assert first.content == "# Analysis\nUse the approved query tool."
    assert second.content == first.content
    assert first.pin == second.pin
    assert first.allowed_tools == frozenset({"query_data"})

    canonical_manifest = json.dumps(
        {
            "allowed_tools": ["query_data"],
            "description": "Analyze trusted data.",
            "entrypoint": "SKILL.md",
            "name": "analysis",
            "required_roles": [],
            "required_secrets": [],
            "schema_version": 1,
            "version": "1.2.3",
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    expected_hash = hashlib.sha256(
        canonical_manifest + b"\x00# Analysis\nUse the approved query tool."
    ).hexdigest()
    assert first.pin.content_hash == expected_hash
    assert first.pin.sha256 == expected_hash


def test_catalog_and_activation_enforce_roles_and_secret_names(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    root.mkdir()
    _write_skill(
        root,
        required_roles=("analyst",),
        required_secrets=("DATABASE_DSN",),
    )
    registry = SkillRegistry.load(root, {"query_data"})

    assert registry.catalog(SkillAccess(roles=frozenset({"analyst"}))) == ()
    with pytest.raises(SkillAccessDeniedError, match="unavailable") as denied:
        registry.activate(
            "analysis",
            SkillAccess(roles=frozenset({"analyst"})),
            "router",
        )
    assert "DATABASE_DSN" not in str(denied.value)

    access = SkillAccess(
        roles=frozenset({"analyst"}),
        available_secrets=frozenset({"DATABASE_DSN"}),
    )
    assert [item.name for item in registry.catalog(access)] == ["analysis"]
    assert registry.activate("analysis", access, "router").name == "analysis"
    assert "DATABASE_DSN" not in repr(access)


def test_activation_requires_matching_immutable_pin(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    root.mkdir()
    _write_skill(root)
    registry = SkillRegistry.load(root, {"query_data"})
    active = registry.activate("analysis", SkillAccess(), "router")

    assert (
        registry.activate(
            "analysis",
            SkillAccess(),
            "replay",
            expected_pin=active.pin,
        ).pin
        == active.pin
    )
    with pytest.raises(SkillPinMismatchError, match="pin mismatch"):
        registry.activate(
            "analysis",
            SkillAccess(),
            "replay",
            expected_pin=SkillPin("analysis", "1.2.3", "0" * 64),
        )


def test_activation_session_slash_is_exact_idempotent_and_cannot_switch(
    tmp_path: Path,
) -> None:
    root = tmp_path / "skills"
    root.mkdir()
    _write_skill(root)
    _write_skill(
        root,
        directory="reporting",
        name="reporting",
        allowed_tools=("write_report",),
    )
    registry = SkillRegistry.load(root, {"query_data", "write_report"})
    activations = []
    tool_changes = []
    session = SkillActivationSession(
        registry,
        SkillAccess(),
        on_activate=activations.append,
        on_tools_changed=tool_changes.append,
    )

    assert session.prepare_user_text(" /analysis question") == " /analysis question"
    assert session.prepare_user_text("/analysis? question") == "/analysis? question"
    with pytest.raises(SkillAccessDeniedError, match=r"^skill is unavailable$"):
        session.prepare_user_text("/analysis-extra question")
    with pytest.raises(SkillAccessDeniedError, match=r"^skill is unavailable$"):
        session.prepare_user_text("/unknown question")
    assert session.active is None

    assert session.prepare_user_text("/analysis\nquestion") == "question"
    assert session.active is not None
    assert session.active.source == "explicit_slash"
    assert session.allowed_tools == frozenset({"query_data"})
    assert len(activations) == 1
    assert tool_changes == [frozenset({"query_data"})]

    assert session.prepare_user_text("/analysis another") == "another"
    assert len(activations) == 1
    with pytest.raises(SkillAlreadyActiveError, match="cannot switch"):
        session.describe("reporting")


def test_slash_activation_failure_does_not_reveal_skill_existence_or_access(
    tmp_path: Path,
) -> None:
    root = tmp_path / "skills"
    root.mkdir()
    _write_skill(
        root,
        required_roles=("analyst",),
        required_secrets=("DATABASE_DSN",),
    )
    registry = SkillRegistry.load(root, {"query_data"})
    session = SkillActivationSession(registry, SkillAccess())

    failures = []
    for user_text in ("/missing question", "/analysis question"):
        with pytest.raises(SkillAccessDeniedError) as denied:
            session.prepare_user_text(user_text)
        failures.append((type(denied.value), str(denied.value)))

    assert failures == [
        (SkillAccessDeniedError, "skill is unavailable"),
        (SkillAccessDeniedError, "skill is unavailable"),
    ]
    assert "analysis" not in failures[1][1]
    assert "DATABASE_DSN" not in failures[1][1]
    assert session.active is None


def test_session_describe_activates_and_context_uses_progressive_disclosure(
    tmp_path: Path,
) -> None:
    root = tmp_path / "skills"
    root.mkdir()
    secret_body = "Never render before activation. <unsafe>&"
    _write_skill(
        root,
        description="Analyze <records> safely.",
        content=secret_body.encode(),
    )
    registry = SkillRegistry.load(root, {"query_data"})
    session = SkillActivationSession(registry, SkillAccess())

    catalog_context = session.catalog_context()
    assert "Analyze &lt;records&gt; safely." in catalog_context
    assert "/analysis" in catalog_context
    assert secret_body not in catalog_context
    assert session.active_context() == '<active_skill state="inactive" />'

    activated = session.describe("analysis")
    active_context = session.active_context()
    assert activated.source == "describe_skill"
    assert "Never render before activation. &lt;unsafe&gt;&amp;" in active_context
    assert activated.pin.content_hash in active_context
    assert "required_secrets" not in active_context


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("name", "Not-Kebab"),
        ("version", "1"),
        ("entrypoint", "../outside.md"),
        ("entrypoint", "/tmp/outside.md"),
        ("entrypoint", "nested\\SKILL.md"),
    ],
)
def test_manifest_rejects_invalid_contract_values(
    tmp_path: Path,
    field: str,
    value: str,
) -> None:
    root = tmp_path / "skills"
    root.mkdir()
    skill_dir = _write_skill(root)
    manifest_path = skill_dir / "skill.yaml"
    lines = manifest_path.read_text(encoding="utf-8").splitlines()
    manifest_path.write_text(
        "\n".join(
            f"{field}: {value}" if line.startswith(f"{field}:") else line
            for line in lines
        ),
        encoding="utf-8",
    )

    with pytest.raises(SkillRegistryError, match="invalid skill manifest"):
        SkillRegistry.load(root, {"query_data"})


def test_skill_version_and_activation_source_fit_the_run_snapshot(
    tmp_path: Path,
) -> None:
    oversized_version_root = tmp_path / "oversized-version"
    oversized_version_root.mkdir()
    _write_skill(
        oversized_version_root,
        version="1.0.0+" + ("a" * 59),
    )
    with pytest.raises(SkillRegistryError, match="invalid skill manifest"):
        SkillRegistry.load(oversized_version_root, {"query_data"})

    valid_root = tmp_path / "valid"
    valid_root.mkdir()
    _write_skill(valid_root)
    registry = SkillRegistry.load(valid_root, {"query_data"})
    with pytest.raises(SkillRegistryError, match="invalid skill activation source"):
        registry.activate("analysis", SkillAccess(), "s" * 33)


def test_manifest_rejects_extra_duplicate_and_unknown_tool(tmp_path: Path) -> None:
    extra_root = tmp_path / "extra"
    extra_root.mkdir()
    _write_skill(extra_root, extra_manifest="unexpected: true")
    with pytest.raises(SkillRegistryError, match="extra_forbidden"):
        SkillRegistry.load(extra_root, {"query_data"})

    duplicate_root = tmp_path / "duplicate-key"
    duplicate_root.mkdir()
    _write_skill(duplicate_root, extra_manifest="name: duplicate")
    with pytest.raises(SkillRegistryError, match="invalid skill manifest YAML"):
        SkillRegistry.load(duplicate_root, {"query_data"})

    unknown_root = tmp_path / "unknown"
    unknown_root.mkdir()
    _write_skill(unknown_root, allowed_tools=("missing_tool",))
    with pytest.raises(SkillRegistryError, match="unknown tools: missing_tool"):
        SkillRegistry.load(unknown_root, {"query_data"})


@pytest.mark.parametrize("schema_version", ("true", "1.0"))
def test_manifest_schema_version_does_not_coerce_yaml_scalars(
    tmp_path: Path,
    schema_version: str,
) -> None:
    root = tmp_path / schema_version.replace(".", "-")
    root.mkdir()
    skill_dir = _write_skill(root)
    manifest_path = skill_dir / "skill.yaml"
    manifest = manifest_path.read_text(encoding="utf-8")
    manifest_path.write_text(
        manifest.replace("schema_version: 1", f"schema_version: {schema_version}"),
        encoding="utf-8",
    )

    with pytest.raises(
        SkillRegistryError, match="schema_version must be the integer 1"
    ):
        SkillRegistry.load(root, {"query_data"})


def test_registry_rejects_duplicate_skill_names(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    root.mkdir()
    _write_skill(root, directory="one", name="duplicate")
    _write_skill(root, directory="two", name="duplicate")
    with pytest.raises(SkillRegistryError, match="duplicate skill name"):
        SkillRegistry.load(root, {"query_data"})


def test_registry_rejects_symlinks_bad_utf8_and_oversized_content(
    tmp_path: Path,
) -> None:
    symlink_root = tmp_path / "symlink"
    symlink_root.mkdir()
    real_skill = _write_skill(symlink_root, directory="real")
    (symlink_root / "linked").symlink_to(real_skill, target_is_directory=True)
    with pytest.raises(SkillRegistryError, match="symlinks are not allowed"):
        SkillRegistry.load(symlink_root, {"query_data"})

    utf8_root = tmp_path / "utf8"
    utf8_root.mkdir()
    _write_skill(utf8_root, content=b"\xff")
    with pytest.raises(SkillRegistryError, match="not valid UTF-8"):
        SkillRegistry.load(utf8_root, {"query_data"})

    large_root = tmp_path / "large"
    large_root.mkdir()
    _write_skill(large_root, content=b"12345")
    with pytest.raises(SkillRegistryError, match="exceeds 4 bytes"):
        SkillRegistry.load(large_root, {"query_data"}, max_content_bytes=4)


def test_registry_rejects_entrypoint_symlink_even_when_target_stays_inside_root(
    tmp_path: Path,
) -> None:
    root = tmp_path / "skills"
    root.mkdir()
    skill_dir = _write_skill(root, entrypoint="instructions/SKILL.md")
    entrypoint = skill_dir / "instructions" / "SKILL.md"
    target = skill_dir / "real.md"
    target.write_text("real", encoding="utf-8")
    entrypoint.unlink()
    entrypoint.symlink_to(target)

    with pytest.raises(SkillRegistryError, match="symlinks are not allowed"):
        SkillRegistry.load(root, {"query_data"})


def test_registry_rejects_symlinked_manifest_and_entrypoint_directory(
    tmp_path: Path,
) -> None:
    manifest_root = tmp_path / "manifest-symlink"
    manifest_root.mkdir()
    manifest_skill = _write_skill(manifest_root)
    manifest = manifest_skill / "skill.yaml"
    real_manifest = manifest_skill / "real-skill.yaml"
    manifest.rename(real_manifest)
    manifest.symlink_to(real_manifest)
    with pytest.raises(SkillRegistryError, match="symlinks are not allowed"):
        SkillRegistry.load(manifest_root, {"query_data"})

    entrypoint_root = tmp_path / "entrypoint-directory-symlink"
    entrypoint_root.mkdir()
    entrypoint_skill = _write_skill(
        entrypoint_root,
        entrypoint="instructions/SKILL.md",
    )
    instructions = entrypoint_skill / "instructions"
    real_instructions = entrypoint_skill / "real-instructions"
    instructions.rename(real_instructions)
    instructions.symlink_to(real_instructions, target_is_directory=True)
    with pytest.raises(SkillRegistryError, match="symlinks are not allowed"):
        SkillRegistry.load(entrypoint_root, {"query_data"})


def test_registry_reads_manifest_and_entrypoint_from_opened_file_descriptors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "skills"
    root.mkdir()
    skill_dir = _write_skill(root)
    attacker_root = tmp_path / "attacker-skills"
    attacker_root.mkdir()
    attacker_dir = _write_skill(
        attacker_root,
        directory="payload",
        name="payload",
        version="9.9.9",
        content=b"attacker-controlled instructions",
    )
    replacement_manifest = attacker_dir / "skill.yaml"
    replacement_entrypoint = attacker_dir / "SKILL.md"
    real_open = os.open
    relative_opens: list[tuple[str, int]] = []
    replaced: set[str] = set()

    def racing_open(
        path: str | bytes | os.PathLike[str],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        opened_fd = real_open(path, flags, mode, dir_fd=dir_fd)
        opened_name = os.fsdecode(path)
        if dir_fd is not None:
            relative_opens.append((opened_name, flags))
        if opened_name == "skill.yaml" and opened_name not in replaced:
            os.replace(replacement_manifest, skill_dir / "skill.yaml")
            replaced.add(opened_name)
        elif opened_name == "SKILL.md" and opened_name not in replaced:
            os.replace(replacement_entrypoint, skill_dir / "SKILL.md")
            replaced.add(opened_name)
        return opened_fd

    monkeypatch.setattr(os, "open", racing_open)

    registry = SkillRegistry.load(root, {"query_data"})
    activated = registry.activate("analysis", SkillAccess(), "router")

    assert registry.names == ("analysis",)
    assert activated.version == "1.2.3"
    assert activated.content == "# Analysis\nUse the approved query tool."
    assert {name for name, _flags in relative_opens} >= {
        "analysis",
        "skill.yaml",
        "SKILL.md",
    }
    assert all(
        flags & os.O_NOFOLLOW
        for name, flags in relative_opens
        if name in {"analysis", "skill.yaml", "SKILL.md"}
    )
    assert any(
        name == "analysis" and flags & os.O_DIRECTORY for name, flags in relative_opens
    )


def test_concurrent_same_skill_activation_is_idempotent(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    root.mkdir()
    _write_skill(root, name="analysis")
    registry = SkillRegistry.load(root, {"query_data"})
    callbacks = []
    session = SkillActivationSession(
        registry,
        SkillAccess(),
        on_activate=callbacks.append,
    )

    with ThreadPoolExecutor(max_workers=8) as executor:
        activated = list(
            executor.map(
                lambda _index: session.activate("analysis", "router"),
                range(16),
            )
        )

    assert len({item.pin.content_hash for item in activated}) == 1
    assert len(callbacks) == 1


def test_activation_is_published_only_after_scope_and_persistence_callbacks(
    tmp_path: Path,
) -> None:
    root = tmp_path / "skills"
    root.mkdir()
    _write_skill(root, name="analysis")
    registry = SkillRegistry.load(root, {"query_data"})
    callback_order: list[str] = []

    def apply_scope(_allowed_tools: frozenset[str]) -> None:
        callback_order.append("scope")

    def persist_activation(_activated) -> None:
        callback_order.append("persist")
        raise RuntimeError("persistence failed")

    session = SkillActivationSession(
        registry,
        SkillAccess(),
        on_activate=persist_activation,
        on_tools_changed=apply_scope,
    )

    with pytest.raises(RuntimeError, match="persistence failed"):
        session.activate("analysis", "router")

    assert callback_order == ["scope", "persist"]
    assert session.active is None
    assert session.pin is None
    assert session.active_context() == '<active_skill state="inactive" />'


def test_scope_callback_failure_does_not_publish_or_persist_activation(
    tmp_path: Path,
) -> None:
    root = tmp_path / "skills"
    root.mkdir()
    _write_skill(root, name="analysis")
    registry = SkillRegistry.load(root, {"query_data"})
    persisted = []

    def reject_scope(_allowed_tools: frozenset[str]) -> None:
        raise RuntimeError("scope failed")

    session = SkillActivationSession(
        registry,
        SkillAccess(),
        on_activate=persisted.append,
        on_tools_changed=reject_scope,
    )

    with pytest.raises(RuntimeError, match="scope failed"):
        session.activate("analysis", "router")

    assert persisted == []
    assert session.active is None
    assert session.pin is None


def test_registry_rejects_missing_skill_and_example_skill_loads() -> None:
    project_root = Path(__file__).resolve().parents[1]
    registry = SkillRegistry.load(
        project_root / "skills",
        {
            "search_knowledge_base",
            "sql_schema",
            "sql_query",
            "web_search",
            "web_fetch",
            "sandbox_execute",
        },
    )
    assert registry.names == (
        "knowledge-base",
        "sandbox",
        "sql-assistant",
        "web-research",
    )
    assert registry.activate(
        "knowledge-base",
        SkillAccess(),
        "explicit_slash",
    ).allowed_tools == frozenset({"search_knowledge_base"})
    with pytest.raises(SkillNotFoundError, match="not found"):
        registry.activate("missing", SkillAccess(), "router")
