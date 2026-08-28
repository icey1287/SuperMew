from __future__ import annotations

from backend.model_control import (
    ModelRole,
    ModelRuntimeSpec,
    build_model_catalog_snapshot,
)


TEST_MODEL_SNAPSHOT = build_model_catalog_snapshot(
    {
        role: ModelRuntimeSpec(
            profile_id=f"model_{index:032x}",
            profile_version=1,
            display_name=f"测试模型 · {role.value}",
            provider="openai",
            model_name=f"{role.value}-model",
            base_url="https://models.test/v1",
            timeout_seconds=15,
            supports_stream=True,
            supports_structured_output=True,
        )
        for index, role in enumerate(ModelRole, 1)
    }
)


class StaticModelControl:
    def runtime_snapshot(
        self,
        *,
        required_roles: frozenset[ModelRole] = frozenset(),
    ):
        missing = required_roles.difference(TEST_MODEL_SNAPSHOT.assignments)
        if missing:
            raise AssertionError(f"测试模型快照缺少角色: {sorted(missing)}")
        return TEST_MODEL_SNAPSHOT


static_model_control = StaticModelControl()
