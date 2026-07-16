from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import tool
from pydantic import Field

from backend.agent.factory import AgentRuntimeFactory
from backend.agent.models import ModelRole
from backend.agent.runtime import AgentRuntimeInput
from backend.chat.request_context import ChatRequestContext
from backend.core.settings import AgentSettings, RunSettings
from backend.core.errors import AppError, ErrorCode
from backend.skills import SkillAccess, SkillPin, SkillRegistry
from backend.tools.catalog import build_default_tool_registry
from backend.tools.registry import ToolDescriptor, ToolExposure, ToolRegistry


class ScriptedChatModel(BaseChatModel):
    responses: list[AIMessage]
    response_index: int = 0
    bound_tool_names: list[list[str]] = Field(default_factory=list)
    received_messages: list[list[BaseMessage]] = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "scripted-skill-tool-runtime-test"

    def bind_tools(self, tools, *, tool_choice=None, **kwargs):
        self.bound_tool_names.append(
            [
                str(
                    getattr(item, "name", None)
                    or (item.get("function") or {}).get("name")
                    or item.get("name")
                )
                for item in tools
            ]
        )
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.received_messages.append(list(messages))
        index = min(self.response_index, len(self.responses) - 1)
        self.response_index += 1
        return ChatResult(generations=[ChatGeneration(message=self.responses[index])])


class _FixedModels:
    def __init__(self, model: BaseChatModel) -> None:
        self.model = model

    def get(self, role: ModelRole | str):
        assert ModelRole(role) is ModelRole.ANSWER
        return self.model


class _QueuedModels:
    def __init__(self, *models: BaseChatModel) -> None:
        self.models = list(models)

    def get(self, role: ModelRole | str):
        assert ModelRole(role) is ModelRole.ANSWER
        return self.models.pop(0)


def _settings():
    return SimpleNamespace(
        agent=AgentSettings(_env_file=None),
        runs=RunSettings(_env_file=None, RUN_DEADLINE_SECONDS=30),
    )


def _load_project_skills(registry: ToolRegistry) -> SkillRegistry:
    project_root = Path(__file__).resolve().parents[1]
    return SkillRegistry.load(project_root / "skills", registry.names)


def _empty_skills(root: Path, registry: ToolRegistry) -> SkillRegistry:
    root.mkdir()
    return SkillRegistry.load(root, registry.names)


def _write_analysis_skill(root: Path) -> None:
    skill_dir = root / "analysis"
    skill_dir.mkdir(parents=True)
    (skill_dir / "skill.yaml").write_text(
        "\n".join(
            (
                "schema_version: 1",
                "name: analysis",
                "version: 1.0.0",
                "description: Analyze data with the deferred SQL tool.",
                "allowed_tools:",
                "  - analysis_query",
                "required_roles: []",
                "required_secrets: []",
                "entrypoint: SKILL.md",
            )
        ),
        encoding="utf-8",
    )
    (skill_dir / "SKILL.md").write_text(
        "# Analysis\nReveal analysis_query, execute it once, then summarize.",
        encoding="utf-8",
    )


def _descriptor(
    name: str,
    description: str,
    *,
    argument: str,
    group: str = "runtime-test",
) -> ToolDescriptor:
    return ToolDescriptor(
        name=name,
        description=description,
        group=group,
        version="1.0.0",
        input_schema={
            "type": "object",
            "properties": {argument: {"type": "string"}},
            "required": [argument],
            "additionalProperties": False,
        },
        output_schema={"type": "string"},
        timeout=2.0,
        max_concurrency=4,
        idempotent=True,
        required_roles=frozenset(),
        required_secrets=frozenset(),
        requires_approval=False,
        network_policy="none",
        result_size_limit=16_384,
    )


def _control_placeholder(name: str):
    def build(_request_context):
        raise AssertionError(f"{name} must be replaced by its Run-owned Adapter")

    return build


def _registry_with_deferred_sql(calls: list[str]) -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(
        _descriptor(
            "describe_skill",
            "Activate an authorized skill.",
            argument="name",
            group="registry-control",
        ),
        _control_placeholder("describe_skill"),
        exposure=ToolExposure.CONTROL,
    )
    registry.register(
        _descriptor(
            "tool_search",
            "Reveal authorized deferred SQL schemas.",
            argument="query",
            group="registry-control",
        ),
        _control_placeholder("tool_search"),
        exposure=ToolExposure.CONTROL,
    )

    def make_analysis_query(_request_context):
        @tool("analysis_query")
        def analysis_query(query: str) -> str:
            """Execute a read-only SQL query for an authorized Run."""

            calls.append(query)
            return f"rows for: {query}"

        return analysis_query

    registry.register(
        _descriptor(
            "analysis_query",
            "Execute a read-only SQL query against authorized business data.",
            argument="query",
        ),
        make_analysis_query,
        exposure=ToolExposure.DEFERRED,
    )
    return registry


def _message_texts(messages: list[BaseMessage]) -> list[str]:
    return [message.content for message in messages if isinstance(message.content, str)]


def test_factory_defaults_to_an_empty_tool_ceiling():
    registry = build_default_tool_registry()
    built: dict[str, object] = {}
    factory = AgentRuntimeFactory(
        settings=_settings(),
        models=_FixedModels(ScriptedChatModel(responses=[AIMessage(content="ok")])),
        agent_builder=lambda **kwargs: built.update(kwargs) or object(),
        tools=registry,
        skills=_load_project_skills(registry),
        secret_names_provider=lambda _registry: frozenset(
            {"AMAP_WEATHER_API", "AMAP_API_KEY"}
        ),
    )
    request_context = ChatRequestContext.for_sync(
        user_id="alice",
        session_id="factory-empty-default",
    )
    try:
        runtime = factory.create(request_context)
    finally:
        request_context.close()

    assert built["tools"] == []
    assert runtime.context.allowed_tools == frozenset()
    assert runtime.context.tool_session.authorized_names == frozenset()


def test_project_sql_assistant_skill_is_admin_and_secret_gated():
    registry = build_default_tool_registry()
    skills = _load_project_skills(registry)

    user_catalog = skills.catalog(
        SkillAccess(
            roles=frozenset({"user"}),
            available_secrets=frozenset({"SQL_ASSISTANT_DSN"}),
        )
    )
    admin_without_secret_catalog = skills.catalog(
        SkillAccess(roles=frozenset({"admin"}))
    )
    admin_catalog = skills.catalog(
        SkillAccess(
            roles=frozenset({"admin"}),
            available_secrets=frozenset({"SQL_ASSISTANT_DSN"}),
        )
    )

    assert "sql-assistant" not in {item.name for item in user_catalog}
    assert "sql-assistant" not in {item.name for item in admin_without_secret_catalog}
    assert "sql-assistant" in {item.name for item in admin_catalog}

    activated = skills.activate(
        "sql-assistant",
        SkillAccess(
            roles=frozenset({"admin"}),
            available_secrets=frozenset({"SQL_ASSISTANT_DSN"}),
        ),
        source="test",
    )
    assert activated.allowed_tools == frozenset({"sql_schema", "sql_query"})
    assert "get_current_weather" not in activated.allowed_tools
    assert "search_knowledge_base" not in activated.allowed_tools


def test_project_web_research_skill_allows_user_and_admin_but_requires_secret():
    registry = build_default_tool_registry()
    skills = _load_project_skills(registry)

    for role in ("user", "admin"):
        without_secret = skills.catalog(SkillAccess(roles=frozenset({role})))
        with_secret = skills.catalog(
            SkillAccess(
                roles=frozenset({role}),
                available_secrets=frozenset({"BRAVE_SEARCH_API_KEY"}),
            )
        )

        assert "web-research" not in {item.name for item in without_secret}
        assert "web-research" in {item.name for item in with_secret}

    activated = skills.activate(
        "web-research",
        SkillAccess(
            roles=frozenset({"user"}),
            available_secrets=frozenset({"BRAVE_SEARCH_API_KEY"}),
        ),
        source="test",
    )
    assert activated.allowed_tools == frozenset({"web_search", "web_fetch"})
    assert "search_knowledge_base" not in activated.allowed_tools
    assert "sql_query" not in activated.allowed_tools
    instructions = activated.instructions.casefold()
    assert "untrusted evidence" in instructions
    assert "markdown link" in instructions
    assert "source conflicts" in instructions
    assert "coverage gaps" in instructions
    assert "example.com" not in instructions


def test_factory_authorizes_sql_only_with_configured_and_caller_secret():
    registry = build_default_tool_registry()
    factory = AgentRuntimeFactory(
        settings=_settings(),
        models=_FixedModels(ScriptedChatModel(responses=[AIMessage(content="ok")])),
        agent_builder=lambda **_kwargs: object(),
        tools=registry,
        skills=_load_project_skills(registry),
        secret_names_provider=lambda _registry: frozenset({"SQL_ASSISTANT_DSN"}),
    )
    request_context = ChatRequestContext.for_sync(
        user_id="admin",
        session_id="sql-enabled",
    )
    try:
        runtime = factory.create(
            request_context,
            roles=frozenset({"admin"}),
            allowed_tools=factory.tool_ceiling,
            available_secrets=frozenset({"SQL_ASSISTANT_DSN"}),
            allowed_network_policies=frozenset({"none", "restricted", "private-data"}),
            routed_skill="sql-assistant",
        )

        assert runtime.context.skill_session.active.name == "sql-assistant"
        assert {"sql_schema", "sql_query"}.issubset(
            runtime.context.tool_session.authorized_names
        )
        assert not runtime.context.tool_session.is_allowed("sql_query")
        runtime.context.tool_session.search("read-only SQL query")
        assert runtime.context.tool_session.is_allowed("sql_query")
    finally:
        request_context.close()


def test_factory_disabled_sql_cannot_be_enabled_by_a_forged_secret_name():
    registry = build_default_tool_registry()
    built: dict[str, object] = {}
    factory = AgentRuntimeFactory(
        settings=_settings(),
        models=_FixedModels(ScriptedChatModel(responses=[AIMessage(content="ok")])),
        agent_builder=lambda **kwargs: built.update(kwargs) or object(),
        tools=registry,
        skills=_load_project_skills(registry),
        secret_names_provider=lambda _registry: frozenset(),
    )
    request_context = ChatRequestContext.for_sync(
        user_id="admin",
        session_id="sql-disabled",
    )
    try:
        runtime = factory.create(
            request_context,
            roles=frozenset({"admin"}),
            allowed_tools=factory.tool_ceiling,
            available_secrets=frozenset({"SQL_ASSISTANT_DSN"}),
            allowed_network_policies=frozenset({"none", "restricted", "private-data"}),
        )
    finally:
        request_context.close()

    assert "sql_schema" not in runtime.context.tool_session.authorized_names
    assert "sql_query" not in runtime.context.tool_session.authorized_names
    assert "sql_schema" not in {item.name for item in built["tools"]}
    assert "sql_query" not in {item.name for item in built["tools"]}


def test_factory_authorizes_web_only_with_configured_and_caller_secret():
    registry = build_default_tool_registry()
    factory = AgentRuntimeFactory(
        settings=_settings(),
        models=_FixedModels(ScriptedChatModel(responses=[AIMessage(content="ok")])),
        agent_builder=lambda **_kwargs: object(),
        tools=registry,
        skills=_load_project_skills(registry),
        secret_names_provider=lambda _registry: frozenset({"BRAVE_SEARCH_API_KEY"}),
    )
    request_context = ChatRequestContext.for_sync(
        user_id="alice",
        session_id="web-enabled",
    )
    try:
        runtime = factory.create(
            request_context,
            roles=frozenset({"user"}),
            allowed_tools=factory.tool_ceiling,
            available_secrets=frozenset({"BRAVE_SEARCH_API_KEY"}),
            allowed_network_policies=frozenset({"none", "restricted"}),
            routed_skill="web-research",
        )

        assert runtime.context.skill_session.active.name == "web-research"
        assert {"web_search", "web_fetch"}.issubset(
            runtime.context.tool_session.authorized_names
        )
        assert not runtime.context.tool_session.is_allowed("web_search")
        runtime.context.tool_session.search("public web evidence")
        assert runtime.context.tool_session.is_allowed("web_search")
    finally:
        request_context.close()


def test_factory_disabled_web_cannot_be_enabled_by_a_forged_secret_name():
    registry = build_default_tool_registry()
    built: dict[str, object] = {}
    factory = AgentRuntimeFactory(
        settings=_settings(),
        models=_FixedModels(ScriptedChatModel(responses=[AIMessage(content="ok")])),
        agent_builder=lambda **kwargs: built.update(kwargs) or object(),
        tools=registry,
        skills=_load_project_skills(registry),
        secret_names_provider=lambda _registry: frozenset(),
    )
    request_context = ChatRequestContext.for_sync(
        user_id="alice",
        session_id="web-disabled",
    )
    try:
        runtime = factory.create(
            request_context,
            roles=frozenset({"user"}),
            allowed_tools=factory.tool_ceiling,
            available_secrets=frozenset({"BRAVE_SEARCH_API_KEY"}),
            allowed_network_policies=frozenset({"none", "restricted"}),
        )
    finally:
        request_context.close()

    assert "web_search" not in runtime.context.tool_session.authorized_names
    assert "web_fetch" not in runtime.context.tool_session.authorized_names
    assert "web_search" not in {item.name for item in built["tools"]}
    assert "web_fetch" not in {item.name for item in built["tools"]}


def test_factory_excludes_secret_gated_weather_and_sets_explicit_allowed_tools():
    registry = build_default_tool_registry()
    built: dict[str, object] = {}

    def build_agent(**kwargs):
        built.update(kwargs)
        return object()

    factory = AgentRuntimeFactory(
        settings=_settings(),
        models=_FixedModels(ScriptedChatModel(responses=[AIMessage(content="ok")])),
        agent_builder=build_agent,
        tools=registry,
        skills=_load_project_skills(registry),
        secret_names_provider=lambda _registry: frozenset(),
    )
    request_context = ChatRequestContext.for_sync(
        user_id="alice",
        session_id="factory-no-secrets",
    )
    try:
        runtime = factory.create(
            request_context,
            allowed_tools=factory.tool_ceiling,
        )
    finally:
        request_context.close()

    compiled_names = {item.name for item in built["tools"]}
    assert compiled_names == {
        "describe_skill",
        "search_knowledge_base",
        "tool_search",
    }
    assert "get_current_weather" not in compiled_names
    assert isinstance(runtime.context.allowed_tools, frozenset)
    assert runtime.context.allowed_tools == frozenset(compiled_names)
    assert runtime.context.tool_session.authorized_names == frozenset(compiled_names)


def test_trusted_router_can_activate_a_pinned_skill_before_graph_creation():
    registry = build_default_tool_registry()
    factory = AgentRuntimeFactory(
        settings=_settings(),
        models=_FixedModels(ScriptedChatModel(responses=[AIMessage(content="ok")])),
        agent_builder=lambda **_kwargs: object(),
        tools=registry,
        skills=_load_project_skills(registry),
        secret_names_provider=lambda _registry: frozenset(),
    )
    request_context = ChatRequestContext.for_sync(
        user_id="alice",
        session_id="router-skill",
    )
    try:
        runtime = factory.create(
            request_context,
            routed_skill="knowledge-base",
            allowed_tools=factory.tool_ceiling,
        )
    finally:
        request_context.close()

    assert runtime.context.skill_session.active.name == "knowledge-base"
    assert runtime.context.skill_session.active.source == "router"
    assert runtime.context.visible_tool_names() == frozenset(
        {"describe_skill", "search_knowledge_base", "tool_search"}
    )


def test_factory_maps_unavailable_and_drifted_skill_to_stable_errors():
    registry = build_default_tool_registry()
    factory = AgentRuntimeFactory(
        settings=_settings(),
        models=_FixedModels(ScriptedChatModel(responses=[AIMessage(content="ok")])),
        agent_builder=lambda **_kwargs: object(),
        tools=registry,
        skills=_load_project_skills(registry),
        secret_names_provider=lambda _registry: frozenset(),
    )

    unavailable_context = ChatRequestContext.for_sync(
        user_id="alice",
        session_id="missing-skill",
    )
    try:
        with pytest.raises(AppError) as unavailable:
            factory.create(unavailable_context, routed_skill="missing")
    finally:
        unavailable_context.close()
    assert unavailable.value.code is ErrorCode.POLICY_DENIED

    drift_context = ChatRequestContext.for_sync(
        user_id="alice",
        session_id="drifted-skill",
    )
    try:
        with pytest.raises(AppError) as drifted:
            factory.create(
                drift_context,
                pinned_skill=SkillPin(
                    name="knowledge-base",
                    version="1.0.0",
                    content_hash="0" * 64,
                ),
                pinned_skill_source="explicit_slash",
            )
    finally:
        drift_context.close()
    assert drifted.value.code is ErrorCode.RUN_STATE_CONFLICT


def test_factory_read_only_validation_enforces_pin_and_current_tool_ceiling():
    registry = build_default_tool_registry()
    build_agent = Mock()
    factory = AgentRuntimeFactory(
        settings=_settings(),
        models=_FixedModels(ScriptedChatModel(responses=[AIMessage(content="ok")])),
        agent_builder=build_agent,
        tools=registry,
        skills=_load_project_skills(registry),
        secret_names_provider=lambda _registry: frozenset(),
    )
    activated = factory.skills.activate(
        "knowledge-base",
        SkillAccess(roles=frozenset({"user"})),
        "test",
    )

    authorized = factory.validate_access(
        roles=frozenset({"user"}),
        allowed_tools=factory.tool_ceiling,
        pinned_skill=activated.pin,
        pinned_skill_source="explicit_slash",
        required_tools=frozenset({"search_knowledge_base"}),
    )

    assert authorized == frozenset({"search_knowledge_base"})
    build_agent.assert_not_called()

    with pytest.raises(AppError) as denied:
        factory.validate_access(
            roles=frozenset({"user"}),
            allowed_tools=frozenset(),
            pinned_skill=activated.pin,
            pinned_skill_source="explicit_slash",
            required_tools=frozenset({"search_knowledge_base"}),
        )
    assert denied.value.code is ErrorCode.POLICY_DENIED


def test_factory_resume_validation_rechecks_role_secrets_and_skill_hash(tmp_path):
    registry = build_default_tool_registry()
    skill_dir = tmp_path / "skills" / "secured-kb"
    skill_dir.mkdir(parents=True)
    (skill_dir / "skill.yaml").write_text(
        "\n".join(
            (
                "schema_version: 1",
                "name: secured-kb",
                "version: 1.0.0",
                "description: Secured knowledge retrieval.",
                "allowed_tools:",
                "  - search_knowledge_base",
                "required_roles:",
                "  - analyst",
                "required_secrets:",
                "  - KB_ACCESS_TOKEN",
                "entrypoint: SKILL.md",
            )
        ),
        encoding="utf-8",
    )
    (skill_dir / "SKILL.md").write_text("# Secured KB", encoding="utf-8")
    skills = SkillRegistry.load(tmp_path / "skills", registry.names)
    available_secrets = {"KB_ACCESS_TOKEN"}
    factory = AgentRuntimeFactory(
        settings=_settings(),
        models=_FixedModels(ScriptedChatModel(responses=[AIMessage(content="ok")])),
        agent_builder=Mock(),
        tools=registry,
        skills=skills,
        secret_names_provider=lambda _registry: frozenset(available_secrets),
    )
    activated = skills.activate(
        "secured-kb",
        SkillAccess(
            roles=frozenset({"analyst"}),
            available_secrets=frozenset(available_secrets),
        ),
        "test",
    )

    def state(**changes):
        values = {
            "role": "analyst",
            "skill_name": activated.name,
            "skill_version": activated.version,
            "skill_content_hash": activated.pin.content_hash,
            "skill_activation_source": "explicit_slash",
        }
        values.update(changes)
        return SimpleNamespace(**values)

    factory.validate_resume_access(state())

    available_secrets.clear()
    with pytest.raises(AppError) as revoked_secret:
        factory.validate_resume_access(state())
    assert revoked_secret.value.code is ErrorCode.POLICY_DENIED

    available_secrets.add("KB_ACCESS_TOKEN")
    with pytest.raises(AppError) as revoked_role:
        factory.validate_resume_access(state(role="user"))
    assert revoked_role.value.code is ErrorCode.POLICY_DENIED

    with pytest.raises(AppError) as drifted:
        factory.validate_resume_access(state(skill_content_hash="0" * 64))
    assert drifted.value.code is ErrorCode.RUN_STATE_CONFLICT


async def test_slash_skill_activates_before_first_model_call_and_denies_forged_weather():
    weather_calls: list[str] = []

    @tool("get_current_weather")
    def fake_weather(location: str, extensions: str = "base") -> str:
        """A weather Adapter that must remain unreachable after Skill activation."""

        weather_calls.append(f"{location}:{extensions}")
        return "unexpected weather"

    model = ScriptedChatModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "get_current_weather",
                        "args": {"location": "上海", "extensions": "base"},
                        "id": "call-forged-weather",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content="weather denied safely"),
        ]
    )
    registry = build_default_tool_registry()
    factory = AgentRuntimeFactory(
        settings=_settings(),
        models=_FixedModels(model),
        tools=registry,
        skills=_load_project_skills(registry),
        secret_names_provider=lambda _registry: frozenset(),
    )
    request_context = ChatRequestContext.for_sync(
        user_id="alice",
        session_id="slash-skill",
    )
    runtime = factory.create(
        request_context,
        allowed_tools=factory.tool_ceiling,
        available_secrets=frozenset({"AMAP_WEATHER_API", "AMAP_API_KEY"}),
        tool_overrides={"get_current_weather": fake_weather},
    )
    try:
        result = await runtime.ainvoke(
            AgentRuntimeInput(
                history=[],
                user_text="/knowledge-base\n请查询上传文档中的发布流程",
            )
        )
    finally:
        request_context.close()

    assert result.content == "weather denied safely"
    assert weather_calls == []
    assert runtime.context.skill_session.active.name == "knowledge-base"
    assert runtime.context.skill_session.active.source == "explicit_slash"
    assert "get_current_weather" not in runtime.context.visible_tool_names()
    assert "get_current_weather" not in model.bound_tool_names[0]
    assert "search_knowledge_base" in model.bound_tool_names[0]

    first_messages = model.received_messages[0]
    human_messages = [
        message.content
        for message in first_messages
        if isinstance(message, HumanMessage)
    ]
    assert human_messages[-1] == "请查询上传文档中的发布流程"
    assert "/knowledge-base" not in human_messages[-1]
    assert any(
        '<active_skill state="active" name="knowledge-base"' in text
        for text in _message_texts(first_messages)
    )

    second_tool_messages = [
        message
        for message in model.received_messages[1]
        if isinstance(message, ToolMessage)
    ]
    assert any(
        "TOOL_POLICY_DENIED" in str(message.content) for message in second_tool_messages
    )
    assert "tool.denied" in {event["stage"] for event in result.runtime_trace}


async def test_tool_search_reveals_deferred_schema_then_executes_real_adapter(
    tmp_path: Path,
):
    sql_calls: list[str] = []
    registry = _registry_with_deferred_sql(sql_calls)
    skill_root = tmp_path / "skills"
    skill_root.mkdir()
    _write_analysis_skill(skill_root)
    skills = SkillRegistry.load(skill_root, registry.names)
    model = ScriptedChatModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "tool_search",
                        "args": {"query": "read-only SQL", "limit": 5},
                        "id": "call-tool-search",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "analysis_query",
                        "args": {"query": "select count(*) from orders"},
                        "id": "call-sql-query",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content="SQL completed"),
        ]
    )
    factory = AgentRuntimeFactory(
        settings=_settings(),
        models=_FixedModels(model),
        tools=registry,
        skills=skills,
        secret_names_provider=lambda _registry: frozenset(),
    )
    request_context = ChatRequestContext.for_sync(
        user_id="alice",
        session_id="deferred-tool-search",
    )
    runtime = factory.create(
        request_context,
        allowed_tools=frozenset({"tool_search", "analysis_query"}),
    )
    try:
        result = await runtime.ainvoke(
            AgentRuntimeInput(history=[], user_text="/analysis\nCount the orders")
        )
    finally:
        request_context.close()

    assert result.content == "SQL completed"
    assert model.bound_tool_names[0] == ["tool_search"]
    assert set(model.bound_tool_names[1]) == {"tool_search", "analysis_query"}
    assert sql_calls == ["select count(*) from orders"]
    assert runtime.context.tool_session.is_allowed("analysis_query")
    assert "tool.denied" not in {event["stage"] for event in result.runtime_trace}


async def test_runtime_skill_and_reveal_state_do_not_leak_between_runs(
    tmp_path: Path,
):
    sql_calls: list[str] = []
    registry = _registry_with_deferred_sql(sql_calls)
    skill_root = tmp_path / "skills"
    skill_root.mkdir()
    _write_analysis_skill(skill_root)
    skills = SkillRegistry.load(skill_root, registry.names)
    first_model = ScriptedChatModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "tool_search",
                        "args": {"query": "SQL", "limit": 5},
                        "id": "call-first-search",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content="first complete"),
        ]
    )
    second_model = ScriptedChatModel(responses=[AIMessage(content="second complete")])
    factory = AgentRuntimeFactory(
        settings=_settings(),
        models=_QueuedModels(first_model, second_model),
        tools=registry,
        skills=skills,
        secret_names_provider=lambda _registry: frozenset(),
    )
    first_request_context = ChatRequestContext.for_sync(
        user_id="alice",
        session_id="isolated-first",
    )
    second_request_context = ChatRequestContext.for_sync(
        user_id="alice",
        session_id="isolated-second",
    )
    first_runtime = factory.create(
        first_request_context,
        allowed_tools=factory.tool_ceiling,
    )
    second_runtime = factory.create(
        second_request_context,
        allowed_tools=factory.tool_ceiling,
    )
    try:
        first_result = await first_runtime.ainvoke(
            AgentRuntimeInput(
                history=[],
                user_text="/analysis\nFind order totals",
            )
        )
        second_result = await second_runtime.ainvoke(
            AgentRuntimeInput(history=[], user_text="Say hello")
        )
    finally:
        first_request_context.close()
        second_request_context.close()

    assert first_result.content == "first complete"
    assert second_result.content == "second complete"
    assert first_runtime.context.skill_session.active.name == "analysis"
    assert first_runtime.context.tool_session.is_allowed("analysis_query")
    assert "analysis_query" in first_model.bound_tool_names[-1]

    assert second_runtime.context.skill_session.active is None
    assert not second_runtime.context.tool_session.is_allowed("analysis_query")
    assert all(
        "analysis_query" not in bound_names
        for bound_names in second_model.bound_tool_names
    )
    assert any(
        '<active_skill state="inactive" />' in text
        for text in _message_texts(second_model.received_messages[0])
    )
    assert sql_calls == []
