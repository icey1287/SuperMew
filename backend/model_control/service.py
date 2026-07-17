from __future__ import annotations

import re
from urllib.parse import urlsplit

from backend.core.errors import AppError, ErrorCode
from backend.core.settings import AppSettings, get_settings
from backend.model_control.contracts import (
    MODEL_ROLE_REQUIREMENTS,
    ModelCatalogSnapshot,
    ModelProfileRecord,
    ModelRole,
    ModelRuntimeSpec,
    build_model_catalog_snapshot,
)
from backend.model_control.repository import ModelControlRepository


_DISPLAY_NAME_RE = re.compile(r"^[^\x00-\x1f\x7f]{1,120}$")


def _normalize_display_name(value: str) -> str:
    normalized = " ".join((value or "").split())
    if _DISPLAY_NAME_RE.fullmatch(normalized) is None:
        raise AppError(
            ErrorCode.INVALID_REQUEST,
            "Model Profile 名称无效",
            status_code=400,
            category="model",
            stage="validation",
        )
    return normalized


def _normalize_model_name(value: str) -> str:
    normalized = (value or "").strip()
    if (
        not normalized
        or len(normalized) > 160
        or any(ord(character) < 32 for character in normalized)
    ):
        raise AppError(
            ErrorCode.INVALID_REQUEST,
            "模型标识无效",
            status_code=400,
            category="model",
            stage="validation",
        )
    return normalized


def _normalize_base_url(value: str) -> str:
    normalized = (value or "").strip().rstrip("/")
    if not normalized:
        return ""
    parsed = urlsplit(normalized)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise AppError(
            ErrorCode.INVALID_REQUEST,
            "Base URL 必须是无凭据、query 和 fragment 的 HTTP/HTTPS 地址",
            status_code=400,
            category="model",
            stage="validation",
        )
    return normalized


class ModelControlService:
    """Deep Module for Model Profile lifecycle, Assignments and runtime snapshots."""

    def __init__(
        self,
        repository: ModelControlRepository | None = None,
        *,
        settings: AppSettings | None = None,
    ) -> None:
        self.repository = repository or ModelControlRepository()
        self.settings = settings or get_settings()

    def create_profile(
        self,
        *,
        username: str,
        display_name: str,
        provider: str,
        model_name: str,
        base_url: str,
        timeout_seconds: float,
        supports_stream: bool,
        supports_structured_output: bool,
        enabled: bool = True,
    ) -> ModelProfileRecord:
        if provider != "openai":
            raise AppError(
                ErrorCode.INVALID_REQUEST,
                "当前仅支持 OpenAI-compatible 模型",
                status_code=400,
                category="model",
                stage="validation",
            )
        return self.repository.create_profile(
            display_name=_normalize_display_name(display_name),
            provider=provider,
            model_name=_normalize_model_name(model_name),
            base_url=_normalize_base_url(base_url),
            timeout_seconds=timeout_seconds,
            supports_stream=supports_stream,
            supports_structured_output=supports_structured_output,
            enabled=enabled,
            source="user",
            username=username,
        )

    def update_profile(
        self,
        *,
        username: str,
        profile_id: str,
        display_name: str,
        provider: str,
        model_name: str,
        base_url: str,
        timeout_seconds: float,
        supports_stream: bool,
        supports_structured_output: bool,
        enabled: bool,
    ) -> ModelProfileRecord:
        if provider != "openai":
            raise AppError(
                ErrorCode.INVALID_REQUEST,
                "当前仅支持 OpenAI-compatible 模型",
                status_code=400,
                category="model",
                stage="validation",
            )
        return self.repository.update_profile(
            profile_id=profile_id,
            display_name=_normalize_display_name(display_name),
            provider=provider,
            model_name=_normalize_model_name(model_name),
            base_url=_normalize_base_url(base_url),
            timeout_seconds=timeout_seconds,
            supports_stream=supports_stream,
            supports_structured_output=supports_structured_output,
            enabled=enabled,
            username=username,
        )

    def delete_profile(self, *, username: str, profile_id: str) -> None:
        self.repository.delete_profile(profile_id=profile_id, username=username)

    def assign_role(
        self,
        *,
        username: str,
        role: ModelRole | str,
        profile_id: str,
    ) -> ModelProfileRecord:
        return self.repository.assign(
            role=ModelRole(role),
            profile_id=profile_id,
            username=username,
        )

    def runtime_snapshot(
        self,
        *,
        required_roles: frozenset[ModelRole] = frozenset(),
    ) -> ModelCatalogSnapshot:
        assigned = self.repository.assignments()
        enabled = {
            role: profile for role, profile in assigned.items() if profile.enabled
        }
        missing = sorted(role.value for role in required_roles.difference(enabled))
        if missing:
            raise AppError(
                ErrorCode.MODEL_UNAVAILABLE,
                "必需的模型角色尚未配置",
                status_code=503,
                retryable=False,
                category="model",
                stage="assignment",
                safe_details={"missing_roles": missing},
            )
        return build_model_catalog_snapshot(
            {
                role: ModelRuntimeSpec.from_profile(profile)
                for role, profile in enabled.items()
            }
        )

    def control_plane(self) -> dict:
        profiles = self.repository.list_profiles()
        assignments = self.repository.assignments()
        snapshot = self.runtime_snapshot()
        return {
            "schema_version": 1,
            "catalog_hash": snapshot.catalog_hash,
            "api_key_configured": bool(
                self.settings.models.api_key.get_secret_value().strip()
            ),
            "profiles": profiles,
            "assignments": {role.value: assignments.get(role) for role in ModelRole},
            "requirements": {
                role.value: MODEL_ROLE_REQUIREMENTS[role] for role in ModelRole
            },
        }

    def ensure_environment_defaults(self) -> None:
        model_settings = self.settings.models
        role_models = {
            ModelRole.ANSWER: model_settings.answer_model.strip(),
            ModelRole.FAST: model_settings.fast_model.strip(),
            ModelRole.GRADER: model_settings.grade_model.strip(),
            ModelRole.EVALUATOR: model_settings.evaluation_model.strip(),
        }
        existing_assignments = self.repository.assignments()
        for role, model_name in role_models.items():
            if not model_name or role in existing_assignments:
                continue
            base_url = _normalize_base_url(model_settings.base_url)
            profile = self.repository.find_profile(
                provider="openai",
                model_name=model_name,
                base_url=base_url,
            )
            if profile is None:
                profile = self.repository.create_profile(
                    display_name=self._environment_display_name(
                        model_name=model_name,
                        role=role,
                    ),
                    provider="openai",
                    model_name=model_name,
                    base_url=base_url,
                    timeout_seconds=model_settings.timeout_seconds,
                    supports_stream=True,
                    supports_structured_output=True,
                    enabled=True,
                    source="environment",
                    username=None,
                )
            self.repository.assign(
                role=role,
                profile_id=profile.id,
                username=None,
            )

    def _environment_display_name(self, *, model_name: str, role: ModelRole) -> str:
        base = f"环境模型 · {model_name}"
        names = {profile.display_name for profile in self.repository.list_profiles()}
        if base not in names:
            return base
        candidate = f"{base} · {role.value}"
        index = 2
        while candidate in names:
            candidate = f"{base} · {role.value}-{index}"
            index += 1
        return candidate


model_control_service = ModelControlService()


__all__ = ["ModelControlService", "model_control_service"]
