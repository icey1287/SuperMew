from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import threading
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from html import escape
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator
from yaml.constructor import ConstructorError
from yaml.nodes import MappingNode


_KEBAB_NAME = re.compile(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*\Z")
_TOOL_NAME = re.compile(r"[a-z][a-z0-9_.-]*\Z")
_ROLE_NAME = re.compile(r"[a-z][a-z0-9:_-]*\Z")
_SECRET_NAME = re.compile(r"[A-Z][A-Z0-9_]*\Z")
_SEMVER = re.compile(
    r"(?:0|[1-9]\d*)\."
    r"(?:0|[1-9]\d*)\."
    r"(?:0|[1-9]\d*)"
    r"(?:-(?:"
    r"(?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*)"
    r"(?:\.(?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*))*"
    r"))?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?\Z"
)
_EXPLICIT_SKILL = re.compile(
    r"\A/(?P<name>[a-z][a-z0-9]*(?:-[a-z0-9]+)*)"
    r"(?P<separator>[ \t\r\n]+)?"
    r"(?P<body>[\s\S]*)\Z"
)
_MAX_MANIFEST_BYTES = 64 * 1024


class SkillRegistryError(ValueError):
    """Base error for a rejected skill configuration or activation."""


class SkillNotFoundError(SkillRegistryError):
    """The requested skill is not registered."""


class SkillAccessDeniedError(SkillRegistryError):
    """The caller cannot use a registered skill."""


class SkillPinMismatchError(SkillRegistryError):
    """A replayed run requested a different immutable skill revision."""


class SkillAlreadyActiveError(SkillRegistryError):
    """A run attempted to switch skills after activation."""


class _UniqueKeySafeLoader(yaml.SafeLoader):
    """Safe YAML loader that also rejects aliases and duplicate keys."""

    def compose_node(self, parent, index):  # type: ignore[no-untyped-def]
        if self.check_event(yaml.AliasEvent):
            raise ConstructorError(
                None,
                None,
                "YAML aliases are not allowed in skill manifests",
                self.peek_event().start_mark,
            )
        return super().compose_node(parent, index)


def _construct_unique_mapping(
    loader: _UniqueKeySafeLoader,
    node: MappingNode,
    deep: bool = False,
) -> dict[Any, Any]:
    loader.flatten_mapping(node)
    result: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in result
        except TypeError as exc:
            raise ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                "found an unhashable mapping key",
                key_node.start_mark,
            ) from exc
        if duplicate:
            raise ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                f"found duplicate key {key!r}",
                key_node.start_mark,
            )
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


_UniqueKeySafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


def _normalize_unique_values(
    values: tuple[str, ...],
    *,
    label: str,
    pattern: re.Pattern[str],
    max_length: int,
) -> tuple[str, ...]:
    normalized: list[str] = []
    for value in values:
        if not isinstance(value, str):
            raise ValueError(f"{label} entries must be strings")
        item = value.strip()
        if not item or len(item) > max_length or pattern.fullmatch(item) is None:
            raise ValueError(f"invalid {label} entry: {value!r}")
        normalized.append(item)
    if len(set(normalized)) != len(normalized):
        raise ValueError(f"{label} must not contain duplicates")
    return tuple(sorted(normalized))


def _safe_relative_path(value: str, *, label: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a string")
    if not value or len(value) > 240:
        raise ValueError(f"invalid {label}")
    if "\x00" in value or "\\" in value:
        raise ValueError(f"invalid {label}")
    path = Path(value)
    if path.is_absolute():
        raise ValueError(f"{label} must be relative")
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError(f"invalid {label}")
    return value


class SkillManifest(BaseModel):
    """Validated on-disk contract for a versioned skill."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1]
    name: str = Field(min_length=1, max_length=64)
    version: str = Field(min_length=1, max_length=64)
    description: str = Field(min_length=1, max_length=500)
    allowed_tools: tuple[str, ...] = ()
    required_roles: tuple[str, ...] = ()
    required_secrets: tuple[str, ...] = ()
    entrypoint: str = Field(min_length=1, max_length=240)

    @field_validator("schema_version", mode="before")
    @classmethod
    def validate_schema_version_type(cls, value: object) -> object:
        if type(value) is not int:  # bool and float must not coerce to v1.
            raise ValueError("schema_version must be the integer 1")
        return value

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        normalized = value.strip()
        if _KEBAB_NAME.fullmatch(normalized) is None:
            raise ValueError("name must use lowercase kebab-case")
        return normalized

    @field_validator("version")
    @classmethod
    def validate_version(cls, value: str) -> str:
        normalized = value.strip()
        if _SEMVER.fullmatch(normalized) is None:
            raise ValueError("version must be a semantic version")
        return normalized

    @field_validator("description")
    @classmethod
    def validate_description(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("description must not be blank")
        return normalized

    @field_validator("allowed_tools")
    @classmethod
    def validate_allowed_tools(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _normalize_unique_values(
            value,
            label="allowed_tools",
            pattern=_TOOL_NAME,
            max_length=128,
        )

    @field_validator("required_roles")
    @classmethod
    def validate_required_roles(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _normalize_unique_values(
            value,
            label="required_roles",
            pattern=_ROLE_NAME,
            max_length=128,
        )

    @field_validator("required_secrets")
    @classmethod
    def validate_required_secrets(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _normalize_unique_values(
            value,
            label="required_secrets",
            pattern=_SECRET_NAME,
            max_length=128,
        )

    @field_validator("entrypoint")
    @classmethod
    def validate_entrypoint(cls, value: str) -> str:
        return _safe_relative_path(value, label="entrypoint")


@dataclass(frozen=True, slots=True)
class SkillAccess:
    """Names-only capability set; secret values never enter the registry."""

    roles: frozenset[str] = frozenset()
    available_secrets: frozenset[str] = field(default=frozenset(), repr=False)

    def __post_init__(self) -> None:
        roles = _normalize_unique_values(
            tuple(self.roles),
            label="roles",
            pattern=_ROLE_NAME,
            max_length=128,
        )
        secrets = _normalize_unique_values(
            tuple(self.available_secrets),
            label="available_secrets",
            pattern=_SECRET_NAME,
            max_length=128,
        )
        object.__setattr__(self, "roles", frozenset(roles))
        object.__setattr__(self, "available_secrets", frozenset(secrets))


@dataclass(frozen=True, slots=True)
class SkillPin:
    """Immutable identity persisted with a Run for deterministic replay."""

    name: str
    version: str
    content_hash: str

    @property
    def sha256(self) -> str:
        return self.content_hash


@dataclass(frozen=True, slots=True)
class SkillSummary:
    """Progressive-disclosure view safe to place in the base context."""

    name: str
    version: str
    description: str

    @property
    def activation(self) -> str:
        return f"/{self.name}"


@dataclass(frozen=True, slots=True)
class SkillControlPlaneSummary:
    """Secret-free Skill projection for authenticated control-plane callers."""

    name: str
    version: str
    description: str
    activation: str
    available: bool
    availability_reason: Literal["permission_required", "not_configured"] | None
    required_roles: tuple[str, ...]
    tool_names: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ActivatedSkill:
    """The only view that exposes frozen skill instructions and tool scope."""

    name: str
    version: str
    description: str
    allowed_tools: frozenset[str]
    content: str = field(repr=False)
    pin: SkillPin
    source: str

    @property
    def instructions(self) -> str:
        return self.content

    @property
    def content_hash(self) -> str:
        return self.pin.content_hash


@dataclass(frozen=True, slots=True)
class _SkillRecord:
    manifest: SkillManifest
    content_bytes: bytes = field(repr=False)
    content: str = field(repr=False)
    pin: SkillPin


def _canonical_manifest(manifest: SkillManifest) -> bytes:
    payload = manifest.model_dump(mode="json")
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


_OPEN_COMMON_FLAGS = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
_OPEN_DIRECTORY_FLAGS = (
    _OPEN_COMMON_FLAGS | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
)
_OPEN_FILE_FLAGS = (
    _OPEN_COMMON_FLAGS | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
)


def _require_secure_open_flags() -> None:
    if not hasattr(os, "O_DIRECTORY") or not hasattr(os, "O_NOFOLLOW"):
        raise SkillRegistryError(
            "skill registry requires O_DIRECTORY and O_NOFOLLOW support"
        )


def _is_symlink_at(parent_fd: int, name: str) -> bool:
    try:
        metadata = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except OSError:
        return False
    return stat.S_ISLNK(metadata.st_mode)


def _open_directory_at(
    parent_fd: int,
    name: str,
    *,
    label: str,
    display_path: Path,
) -> int:
    try:
        opened_fd = os.open(name, _OPEN_DIRECTORY_FLAGS, dir_fd=parent_fd)
    except OSError as exc:
        if _is_symlink_at(parent_fd, name):
            raise SkillRegistryError(
                f"symlinks are not allowed for {label}: {display_path}"
            ) from exc
        raise SkillRegistryError(f"cannot open {label}: {display_path}") from exc
    try:
        mode = os.fstat(opened_fd).st_mode
    except OSError as exc:
        os.close(opened_fd)
        raise SkillRegistryError(f"cannot inspect {label}: {display_path}") from exc
    if not stat.S_ISDIR(mode):
        os.close(opened_fd)
        raise SkillRegistryError(f"{label} must be a directory: {display_path}")
    return opened_fd


def _open_regular_file_at(
    base_fd: int,
    relative: str,
    *,
    max_bytes: int,
    label: str,
    display_path: Path,
) -> bytes:
    parts = relative.split("/")
    current_fd = os.dup(base_fd)
    try:
        current_path = display_path
        for _part in parts:
            current_path = current_path.parent
        for part in parts[:-1]:
            current_path = current_path / part
            next_fd = _open_directory_at(
                current_fd,
                part,
                label=label,
                display_path=current_path,
            )
            os.close(current_fd)
            current_fd = next_fd

        filename = parts[-1]
        try:
            file_fd = os.open(filename, _OPEN_FILE_FLAGS, dir_fd=current_fd)
        except OSError as exc:
            if _is_symlink_at(current_fd, filename):
                raise SkillRegistryError(
                    f"symlinks are not allowed for {label}: {display_path}"
                ) from exc
            raise SkillRegistryError(f"cannot open {label}: {display_path}") from exc
        try:
            try:
                metadata = os.fstat(file_fd)
            except OSError as exc:
                raise SkillRegistryError(
                    f"cannot inspect {label}: {display_path}"
                ) from exc
            if not stat.S_ISREG(metadata.st_mode):
                raise SkillRegistryError(
                    f"{label} must be a regular file: {display_path}"
                )
            if metadata.st_size > max_bytes:
                raise SkillRegistryError(
                    f"{label} exceeds {max_bytes} bytes: {display_path}"
                )

            chunks: list[bytes] = []
            total = 0
            while total <= max_bytes:
                try:
                    chunk = os.read(file_fd, min(65_536, max_bytes + 1 - total))
                except OSError as exc:
                    raise SkillRegistryError(
                        f"cannot read {label}: {display_path}"
                    ) from exc
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
            if total > max_bytes:
                raise SkillRegistryError(
                    f"{label} exceeds {max_bytes} bytes: {display_path}"
                )
            data = b"".join(chunks)
            if b"\x00" in data:
                raise SkillRegistryError(
                    f"NUL bytes are not allowed in {label}: {display_path}"
                )
            return data
        finally:
            os.close(file_fd)
    finally:
        os.close(current_fd)


class SkillRegistry:
    """Fail-closed startup loader and immutable skill lookup boundary."""

    def __init__(
        self,
        *,
        root: Path,
        records: Mapping[str, _SkillRecord],
        known_tools: frozenset[str],
    ) -> None:
        self._root = root
        self._records = MappingProxyType(dict(records))
        self._known_tools = known_tools

    @classmethod
    def load(
        cls,
        root: str | Path,
        known_tools: Iterable[str],
        manifest_name: str = "skill.yaml",
        max_content_bytes: int = 262_144,
    ) -> SkillRegistry:
        if max_content_bytes <= 0:
            raise SkillRegistryError("max_content_bytes must be positive")
        _require_secure_open_flags()
        safe_manifest_name = _safe_relative_path(
            manifest_name,
            label="manifest_name",
        )
        raw_root = Path(root)
        try:
            root_mode = raw_root.lstat().st_mode
        except OSError as exc:
            raise SkillRegistryError(f"cannot inspect skill root: {raw_root}") from exc
        if stat.S_ISLNK(root_mode):
            raise SkillRegistryError("skill root must not be a symlink")
        if not stat.S_ISDIR(root_mode):
            raise SkillRegistryError(f"skill root must be a directory: {raw_root}")
        try:
            resolved_root = raw_root.resolve(strict=True)
        except OSError as exc:
            raise SkillRegistryError(f"cannot resolve skill root: {raw_root}") from exc

        try:
            root_fd = os.open(resolved_root, _OPEN_DIRECTORY_FLAGS)
        except OSError as exc:
            raise SkillRegistryError(f"cannot open skill root: {raw_root}") from exc
        try:
            try:
                opened_root_mode = os.fstat(root_fd).st_mode
            except OSError as exc:
                raise SkillRegistryError(
                    f"cannot inspect opened skill root: {raw_root}"
                ) from exc
            if not stat.S_ISDIR(opened_root_mode):
                raise SkillRegistryError(f"skill root must be a directory: {raw_root}")

            normalized_tools = _normalize_unique_values(
                tuple(known_tools),
                label="known_tools",
                pattern=_TOOL_NAME,
                max_length=128,
            )
            known_tool_set = frozenset(normalized_tools)
            records: dict[str, _SkillRecord] = {}

            try:
                entries = sorted(os.listdir(root_fd))
            except OSError as exc:
                raise SkillRegistryError(
                    f"cannot list skill root: {resolved_root}"
                ) from exc

            for entry_name in entries:
                skill_dir = resolved_root / entry_name
                try:
                    entry_mode = os.stat(
                        entry_name,
                        dir_fd=root_fd,
                        follow_symlinks=False,
                    ).st_mode
                except OSError as exc:
                    raise SkillRegistryError(
                        f"cannot inspect skill entry: {skill_dir}"
                    ) from exc
                if stat.S_ISLNK(entry_mode):
                    raise SkillRegistryError(
                        f"symlinks are not allowed in the skill root: {skill_dir}"
                    )
                if not stat.S_ISDIR(entry_mode):
                    continue

                skill_fd = _open_directory_at(
                    root_fd,
                    entry_name,
                    label="skill directory",
                    display_path=skill_dir,
                )
                try:
                    manifest_path = skill_dir / safe_manifest_name
                    raw_manifest = _open_regular_file_at(
                        skill_fd,
                        safe_manifest_name,
                        max_bytes=_MAX_MANIFEST_BYTES,
                        label="skill manifest",
                        display_path=manifest_path,
                    )
                    try:
                        manifest_text = raw_manifest.decode("utf-8")
                    except UnicodeDecodeError as exc:
                        raise SkillRegistryError(
                            f"skill manifest is not valid UTF-8: {manifest_path}"
                        ) from exc
                    try:
                        payload = yaml.load(
                            manifest_text,
                            Loader=_UniqueKeySafeLoader,
                        )
                    except yaml.YAMLError as exc:
                        raise SkillRegistryError(
                            f"invalid skill manifest YAML: {manifest_path}"
                        ) from exc
                    if not isinstance(payload, dict):
                        raise SkillRegistryError(
                            f"skill manifest must contain a mapping: {manifest_path}"
                        )
                    try:
                        manifest = SkillManifest.model_validate(payload)
                    except (TypeError, ValueError) as exc:
                        raise SkillRegistryError(
                            f"invalid skill manifest contract: {manifest_path}: {exc}"
                        ) from exc
                    if manifest.name in records:
                        raise SkillRegistryError(
                            f"duplicate skill name: {manifest.name}"
                        )

                    unknown_tools = set(manifest.allowed_tools) - known_tool_set
                    if unknown_tools:
                        unknown = ", ".join(sorted(unknown_tools))
                        raise SkillRegistryError(
                            f"skill {manifest.name!r} references unknown tools: {unknown}"
                        )

                    entrypoint_path = skill_dir / manifest.entrypoint
                    content_bytes = _open_regular_file_at(
                        skill_fd,
                        manifest.entrypoint,
                        max_bytes=max_content_bytes,
                        label="skill entrypoint",
                        display_path=entrypoint_path,
                    )
                    try:
                        content = content_bytes.decode("utf-8")
                    except UnicodeDecodeError as exc:
                        raise SkillRegistryError(
                            f"skill entrypoint is not valid UTF-8: {entrypoint_path}"
                        ) from exc
                    digest = hashlib.sha256(
                        _canonical_manifest(manifest) + b"\x00" + content_bytes
                    ).hexdigest()
                    pin = SkillPin(
                        name=manifest.name,
                        version=manifest.version,
                        content_hash=digest,
                    )
                    records[manifest.name] = _SkillRecord(
                        manifest=manifest,
                        content_bytes=content_bytes,
                        content=content,
                        pin=pin,
                    )
                finally:
                    os.close(skill_fd)
        finally:
            os.close(root_fd)

        return cls(
            root=resolved_root,
            records=records,
            known_tools=known_tool_set,
        )

    @property
    def root(self) -> Path:
        return self._root

    @property
    def known_tools(self) -> frozenset[str]:
        return self._known_tools

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(self._records)

    def contains(self, name: str) -> bool:
        return name in self._records

    def catalog(self, access: SkillAccess) -> tuple[SkillSummary, ...]:
        summaries: list[SkillSummary] = []
        for name in self.names:
            record = self._records[name]
            if not self._is_accessible(record.manifest, access):
                continue
            summaries.append(
                SkillSummary(
                    name=record.manifest.name,
                    version=record.manifest.version,
                    description=record.manifest.description,
                )
            )
        return tuple(summaries)

    def control_plane_catalog(
        self,
        access: SkillAccess,
    ) -> tuple[SkillControlPlaneSummary, ...]:
        """Return every Skill with a stable, secret-free availability decision."""

        summaries: list[SkillControlPlaneSummary] = []
        for name in self.names:
            manifest = self._records[name].manifest
            missing_roles = not set(manifest.required_roles) <= access.roles
            missing_configuration = not (
                set(manifest.required_secrets) <= access.available_secrets
            )
            reason: Literal["permission_required", "not_configured"] | None = None
            if missing_roles:
                reason = "permission_required"
            elif missing_configuration:
                reason = "not_configured"
            summaries.append(
                SkillControlPlaneSummary(
                    name=manifest.name,
                    version=manifest.version,
                    description=manifest.description,
                    activation=f"/{manifest.name}",
                    available=reason is None,
                    availability_reason=reason,
                    required_roles=manifest.required_roles,
                    tool_names=manifest.allowed_tools,
                )
            )
        return tuple(summaries)

    def activate(
        self,
        name: str,
        access: SkillAccess,
        source: str,
        expected_pin: SkillPin | None = None,
    ) -> ActivatedSkill:
        record = self._records.get(name)
        if record is None:
            raise SkillNotFoundError(f"skill not found: {name}")
        if not self._is_accessible(record.manifest, access):
            raise SkillAccessDeniedError(f"skill is unavailable: {name}")
        normalized_source = self._normalize_source(source)
        if expected_pin is not None and expected_pin != record.pin:
            raise SkillPinMismatchError(f"skill pin mismatch: {name}")
        return ActivatedSkill(
            name=record.manifest.name,
            version=record.manifest.version,
            description=record.manifest.description,
            allowed_tools=frozenset(record.manifest.allowed_tools),
            content=record.content,
            pin=record.pin,
            source=normalized_source,
        )

    @staticmethod
    def _is_accessible(manifest: SkillManifest, access: SkillAccess) -> bool:
        return (
            set(manifest.required_roles) <= access.roles
            and set(manifest.required_secrets) <= access.available_secrets
        )

    @staticmethod
    def _normalize_source(source: str) -> str:
        if not isinstance(source, str):
            raise SkillRegistryError("skill activation source must be a string")
        normalized = source.strip()
        if (
            not normalized
            or len(normalized) > 32
            or any(ord(character) < 32 for character in normalized)
        ):
            raise SkillRegistryError("invalid skill activation source")
        return normalized

    def __repr__(self) -> str:
        return f"SkillRegistry(root={self._root!r}, names={self.names!r})"


class SkillActivationSession:
    """Per-Run activation state; one pinned skill may be activated once."""

    def __init__(
        self,
        registry: SkillRegistry,
        access: SkillAccess,
        *,
        expected_pin: SkillPin | None = None,
        on_activate: Callable[[ActivatedSkill], None] | None = None,
        on_tools_changed: Callable[[frozenset[str]], None] | None = None,
    ) -> None:
        self._registry = registry
        self._access = access
        self._expected_pin = expected_pin
        self._on_activate = on_activate
        self._on_tools_changed = on_tools_changed
        self._active: ActivatedSkill | None = None
        self._lock = threading.RLock()

    @property
    def active(self) -> ActivatedSkill | None:
        with self._lock:
            return self._active

    @property
    def pin(self) -> SkillPin | None:
        with self._lock:
            return self._active.pin if self._active is not None else None

    @property
    def allowed_tools(self) -> frozenset[str] | None:
        with self._lock:
            if self._active is None:
                return None
            return self._active.allowed_tools

    def prepare_user_text(self, text: str) -> str:
        """Activate an exact, column-zero ``/skill-name`` prefix and remove it."""

        if not isinstance(text, str):
            raise TypeError("user text must be a string")
        match = _EXPLICIT_SKILL.fullmatch(text)
        if match is None:
            return text
        name = match.group("name")
        separator = match.group("separator")
        body = match.group("body")
        if separator is None and body:
            return text
        try:
            self.activate(name, source="explicit_slash")
        except SkillRegistryError:
            raise SkillAccessDeniedError("skill is unavailable") from None
        return body if separator is not None else ""

    def activate(self, name: str, source: str = "router") -> ActivatedSkill:
        with self._lock:
            if self._active is not None:
                if self._active.name != name:
                    raise SkillAlreadyActiveError(
                        f"cannot switch active skill from {self._active.name} to {name}"
                    )
                return self._active

            activated = self._registry.activate(
                name,
                self._access,
                source,
                expected_pin=self._expected_pin,
            )
            if self._on_tools_changed is not None:
                self._on_tools_changed(activated.allowed_tools)
            if self._on_activate is not None:
                self._on_activate(activated)
            self._active = activated
            return activated

    def describe(self, name: str) -> ActivatedSkill:
        return self.activate(name, source="describe_skill")

    def catalog_context(self) -> str:
        summaries = self._registry.catalog(self._access)
        lines = ['<skill_catalog disclosure="summary-only">']
        for summary in summaries:
            lines.extend(
                (
                    "  <skill",
                    f'    name="{escape(summary.name, quote=True)}"',
                    f'    version="{escape(summary.version, quote=True)}"',
                    f'    activation="{escape(summary.activation, quote=True)}">',
                    f"    {escape(summary.description)}",
                    "  </skill>",
                )
            )
        lines.extend(
            (
                "</skill_catalog>",
                "Only an explicit slash command, a high-confidence router decision, "
                "or describe_skill may activate full skill instructions.",
            )
        )
        return "\n".join(lines)

    def active_context(self) -> str:
        with self._lock:
            active = self._active
        if active is None:
            return '<active_skill state="inactive" />'
        return "\n".join(
            (
                (
                    '<active_skill state="active" '
                    f'name="{escape(active.name, quote=True)}" '
                    f'version="{escape(active.version, quote=True)}" '
                    f'sha256="{escape(active.pin.content_hash, quote=True)}" '
                    f'source="{escape(active.source, quote=True)}">'
                ),
                '  <instructions trust="configured-skill">',
                escape(active.content),
                "  </instructions>",
                "</active_skill>",
            )
        )
