from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from threading import RLock

from langchain.chat_models import init_chat_model
from langchain_core.language_models.chat_models import BaseChatModel

from backend.core.settings import AppSettings, get_settings


class ModelRole(StrEnum):
    ANSWER = "answer"
    FAST = "fast"
    GRADER = "grader"


@dataclass(frozen=True)
class ModelSpec:
    role: ModelRole
    name: str
    provider: str = "openai"
    temperature: float = 0.0
    supports_stream: bool = True
    supports_structured_output: bool = True


ModelInitializer = Callable[..., BaseChatModel]


class ModelRegistry:
    """Allowlisted model roles with lazy, process-wide client reuse."""

    def __init__(
        self,
        settings: AppSettings | None = None,
        *,
        initializer: ModelInitializer = init_chat_model,
    ) -> None:
        self.settings = settings or get_settings()
        self.initializer = initializer
        model_settings = self.settings.models
        self._specs = {
            ModelRole.ANSWER: ModelSpec(
                role=ModelRole.ANSWER,
                name=model_settings.answer_model.strip(),
                temperature=0.3,
            ),
            ModelRole.FAST: ModelSpec(
                role=ModelRole.FAST,
                name=model_settings.fast_model.strip(),
                temperature=0.2,
            ),
            ModelRole.GRADER: ModelSpec(
                role=ModelRole.GRADER,
                name=model_settings.grade_model.strip(),
                temperature=0.0,
            ),
        }
        self._models: dict[ModelRole, BaseChatModel] = {}
        self._lock = RLock()

    def describe(self, role: ModelRole | str) -> ModelSpec:
        return self._specs[ModelRole(role)]

    def available_roles(self) -> tuple[ModelRole, ...]:
        return tuple(role for role, spec in self._specs.items() if spec.name)

    def get(self, role: ModelRole | str) -> BaseChatModel:
        resolved_role = ModelRole(role)
        with self._lock:
            cached = self._models.get(resolved_role)
            if cached is not None:
                return cached
            spec = self._specs[resolved_role]
            if not spec.name:
                raise RuntimeError(
                    f"Model role {resolved_role.value} is not configured"
                )
            model_settings = self.settings.models
            model = self.initializer(
                model=spec.name,
                model_provider=spec.provider,
                api_key=model_settings.api_key.get_secret_value(),
                base_url=model_settings.base_url,
                temperature=spec.temperature,
                stream_usage=True,
            )
            self._models[resolved_role] = model
            return model


model_registry = ModelRegistry()
