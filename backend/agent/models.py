from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import hashlib
import json
from threading import RLock

from langchain.chat_models import init_chat_model
from langchain_core.language_models.chat_models import BaseChatModel

from backend.core.errors import AppError, ErrorCode
from backend.core.settings import AppSettings, get_settings
from backend.model_control import (
    MODEL_ROLE_REQUIREMENTS,
    ModelCatalogSnapshot,
    ModelRole,
    ModelRuntimeSpec,
    build_model_catalog_snapshot,
)


@dataclass(frozen=True)
class ModelSpec:
    role: ModelRole
    profile_id: str
    profile_version: int
    display_name: str
    name: str
    provider: str = "openai"
    base_url: str = ""
    timeout_seconds: float = 30.0
    temperature: float = 0.0
    supports_stream: bool = True
    supports_structured_output: bool = True

    @property
    def cache_key(self) -> tuple[object, ...]:
        return (
            self.role.value,
            self.profile_id,
            self.profile_version,
            self.provider,
            self.name,
            self.base_url,
            self.timeout_seconds,
            self.temperature,
            self.supports_stream,
            self.supports_structured_output,
        )


ModelInitializer = Callable[..., BaseChatModel]


class ModelRegistry:
    """Build model clients from immutable, non-secret Model Snapshots."""

    def __init__(
        self,
        settings: AppSettings | None = None,
        *,
        initializer: ModelInitializer = init_chat_model,
    ) -> None:
        self.settings = settings or get_settings()
        self.initializer = initializer
        self._environment_snapshot = self._build_environment_snapshot()
        self._models: dict[tuple[object, ...], BaseChatModel] = {}
        self._lock = RLock()

    def _build_environment_snapshot(self) -> ModelCatalogSnapshot:
        model_settings = self.settings.models
        names = {
            ModelRole.ANSWER: model_settings.answer_model.strip(),
            ModelRole.FAST: model_settings.fast_model.strip(),
            ModelRole.GRADER: model_settings.grade_model.strip(),
            ModelRole.EVALUATOR: model_settings.evaluation_model.strip(),
        }
        assignments: dict[ModelRole, ModelRuntimeSpec] = {}
        for role, name in names.items():
            if not name:
                continue
            identity = json.dumps(
                {
                    "role": role.value,
                    "model_name": name,
                    "base_url": model_settings.base_url,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            assignments[role] = ModelRuntimeSpec(
                profile_id=(
                    "model_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]
                ),
                profile_version=1,
                display_name=f"环境模型 · {name}",
                provider="openai",
                model_name=name,
                base_url=model_settings.base_url,
                timeout_seconds=model_settings.timeout_seconds,
                supports_stream=True,
                supports_structured_output=True,
            )
        return build_model_catalog_snapshot(assignments)

    def environment_snapshot(self) -> ModelCatalogSnapshot:
        return self._environment_snapshot

    def describe(
        self,
        role: ModelRole | str,
        *,
        snapshot: ModelCatalogSnapshot | None = None,
    ) -> ModelSpec:
        resolved_role = ModelRole(role)
        catalog = snapshot or self._environment_snapshot
        runtime = catalog.assignments.get(resolved_role)
        if runtime is None:
            raise AppError(
                ErrorCode.MODEL_UNAVAILABLE,
                f"模型角色 {resolved_role.value} 尚未配置",
                status_code=503,
                retryable=False,
                category="model",
                stage="snapshot",
                safe_details={"missing_roles": [resolved_role.value]},
            )
        requirement = MODEL_ROLE_REQUIREMENTS[resolved_role]
        if requirement.supports_stream and not runtime.supports_stream:
            raise AppError(
                ErrorCode.MODEL_UNAVAILABLE,
                f"模型角色 {resolved_role.value} 不支持流式输出",
                status_code=503,
                retryable=False,
                category="model",
                stage="capability",
            )
        if (
            requirement.supports_structured_output
            and not runtime.supports_structured_output
        ):
            raise AppError(
                ErrorCode.MODEL_UNAVAILABLE,
                f"模型角色 {resolved_role.value} 不支持结构化输出",
                status_code=503,
                retryable=False,
                category="model",
                stage="capability",
            )
        return ModelSpec(
            role=resolved_role,
            profile_id=runtime.profile_id,
            profile_version=runtime.profile_version,
            display_name=runtime.display_name,
            name=runtime.model_name,
            provider=runtime.provider,
            base_url=runtime.base_url,
            timeout_seconds=runtime.timeout_seconds,
            temperature=requirement.temperature,
            supports_stream=runtime.supports_stream,
            supports_structured_output=runtime.supports_structured_output,
        )

    def available_roles(
        self,
        *,
        snapshot: ModelCatalogSnapshot | None = None,
    ) -> tuple[ModelRole, ...]:
        catalog = snapshot or self._environment_snapshot
        return tuple(role for role in ModelRole if role in catalog.assignments)

    def get(
        self,
        role: ModelRole | str,
        *,
        snapshot: ModelCatalogSnapshot | None = None,
    ) -> BaseChatModel:
        spec = self.describe(role, snapshot=snapshot)
        api_key = self.settings.models.api_key.get_secret_value().strip()
        if not api_key:
            raise AppError(
                ErrorCode.MODEL_UNAVAILABLE,
                "服务端尚未配置模型 API Key",
                status_code=503,
                retryable=False,
                category="model",
                stage="credential",
            )
        with self._lock:
            cached = self._models.get(spec.cache_key)
            if cached is not None:
                return cached
            model = self.initializer(
                model=spec.name,
                model_provider=spec.provider,
                api_key=api_key,
                base_url=spec.base_url,
                temperature=spec.temperature,
                stream_usage=True,
                max_retries=0,
                timeout=spec.timeout_seconds,
            )
            self._models[spec.cache_key] = model
            return model


model_registry = ModelRegistry()
