from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import SecretStr
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.core.errors import AppError, ErrorCode
from backend.core.settings import ModelSettings
from backend.db.models import Base, User
from backend.model_control import (
    ModelControlRepository,
    ModelControlService,
    ModelRole,
)


@pytest.fixture()
def model_control():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    Base.metadata.create_all(engine)
    with factory.begin() as db:
        db.add(
            User(
                username="admin",
                password_hash="hash",
                role="admin",
            )
        )
    settings = SimpleNamespace(
        models=ModelSettings(
            _env_file=None,
            ARK_API_KEY=SecretStr("server-side-key"),
            BASE_URL="https://models.example.test/v1",
            MODEL="answer-v1",
            FAST_MODEL="fast-v1",
            GRADE_MODEL="grade-v1",
            EVALUATION_MODEL="judge-v1",
        )
    )
    repository = ModelControlRepository(factory)
    service = ModelControlService(repository, settings=settings)
    try:
        yield service
    finally:
        engine.dispose()


def test_environment_defaults_seed_all_roles_without_storing_api_key(model_control):
    model_control.ensure_environment_defaults()

    state = model_control.control_plane()

    assert set(state["assignments"]) == {
        "answer",
        "fast",
        "grader",
        "evaluator",
    }
    assert all(state["assignments"].values())
    assert state["api_key_configured"] is True
    serialized = str(state)
    assert "server-side-key" not in serialized
    assert (
        model_control.runtime_snapshot(required_roles=frozenset(ModelRole)).catalog_hash
        == state["catalog_hash"]
    )


def test_user_profiles_can_be_created_assigned_and_versioned(model_control):
    profile = model_control.create_profile(
        username="admin",
        display_name="  自定义 Answer  ",
        provider="openai",
        model_name="answer-v2",
        base_url="https://models.example.test/v1/",
        timeout_seconds=45,
        supports_stream=True,
        supports_structured_output=True,
    )
    model_control.assign_role(
        username="admin",
        role=ModelRole.ANSWER,
        profile_id=profile.id,
    )
    before = model_control.runtime_snapshot(
        required_roles=frozenset({ModelRole.ANSWER})
    )

    updated = model_control.update_profile(
        username="admin",
        profile_id=profile.id,
        display_name="自定义 Answer v2",
        provider="openai",
        model_name="answer-v2.1",
        base_url="https://models.example.test/v1",
        timeout_seconds=50,
        supports_stream=True,
        supports_structured_output=True,
        enabled=True,
    )
    after = model_control.runtime_snapshot(required_roles=frozenset({ModelRole.ANSWER}))

    assert profile.display_name == "自定义 Answer"
    assert profile.base_url == "https://models.example.test/v1"
    assert updated.version == 2
    assert before.catalog_hash != after.catalog_hash
    assert after.require(ModelRole.ANSWER).model_name == "answer-v2.1"


def test_assignment_capabilities_and_profile_lifecycle_fail_closed(model_control):
    profile = model_control.create_profile(
        username="admin",
        display_name="无结构化输出",
        provider="openai",
        model_name="plain-model",
        base_url="",
        timeout_seconds=30,
        supports_stream=True,
        supports_structured_output=False,
    )

    with pytest.raises(AppError) as raised:
        model_control.assign_role(
            username="admin",
            role=ModelRole.GRADER,
            profile_id=profile.id,
        )
    assert raised.value.code == ErrorCode.CONFLICT

    model_control.assign_role(
        username="admin",
        role=ModelRole.ANSWER,
        profile_id=profile.id,
    )
    with pytest.raises(AppError, match="不能停用"):
        model_control.update_profile(
            username="admin",
            profile_id=profile.id,
            display_name=profile.display_name,
            provider=profile.provider,
            model_name=profile.model_name,
            base_url=profile.base_url,
            timeout_seconds=profile.timeout_seconds,
            supports_stream=True,
            supports_structured_output=False,
            enabled=False,
        )
    with pytest.raises(AppError, match="不能删除"):
        model_control.delete_profile(username="admin", profile_id=profile.id)


def test_environment_seed_never_overrides_admin_assignment(model_control):
    chosen = model_control.create_profile(
        username="admin",
        display_name="管理员选择",
        provider="openai",
        model_name="chosen-answer",
        base_url="https://chosen.example.test/v1",
        timeout_seconds=30,
        supports_stream=True,
        supports_structured_output=True,
    )
    model_control.assign_role(
        username="admin",
        role=ModelRole.ANSWER,
        profile_id=chosen.id,
    )

    model_control.ensure_environment_defaults()

    assert (
        model_control.runtime_snapshot().require(ModelRole.ANSWER).profile_id
        == chosen.id
    )
