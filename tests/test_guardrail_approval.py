from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from langchain.agents.middleware import ToolCallRequest
from langchain_core.messages import ToolMessage

from backend.agent.factory import AgentRuntimeFactory
from backend.agent.middleware import ToolPolicyMiddleware
from backend.runs.request_context import RunRequestContext
from backend.core.errors import AppError, ErrorCode
from backend.core.settings import AgentSettings, RunSettings, SandboxSettings
from backend.guardrails import RunToolApprovalGrant
from backend.skills import SkillRegistry
from backend.tools.catalog import build_default_tool_registry


_IMAGE = "sha256:" + ("a" * 64)


class _Models:
    @staticmethod
    def get(_role):
        return object()


def _factory() -> AgentRuntimeFactory:
    sandbox = SandboxSettings(
        _env_file=None,
        SANDBOX_ENABLED=True,
        SANDBOX_DOCKER_IMAGE=_IMAGE,
    )
    registry = build_default_tool_registry(sandbox_settings=sandbox)
    root = Path(__file__).resolve().parents[1]
    skills = SkillRegistry.load(root / "skills", registry.names)
    settings = SimpleNamespace(
        agent=AgentSettings(_env_file=None),
        runs=RunSettings(_env_file=None),
        app=SimpleNamespace(default_tenant_id="default"),
    )
    return AgentRuntimeFactory(
        settings=settings,
        models=_Models(),
        agent_builder=lambda **_kwargs: object(),
        tools=registry,
        skills=skills,
        secret_names_provider=lambda _registry: frozenset({"SANDBOX_RUNTIME"}),
    )


def _grant(*, run_id: str = "run-1") -> RunToolApprovalGrant:
    return RunToolApprovalGrant(
        user_id="admin",
        tenant_id="default",
        thread_id="thread-1",
        run_id=run_id,
        tool_names=frozenset({"sandbox_execute"}),
    )


def _runtime(*, approval_grant: RunToolApprovalGrant):
    context = RunRequestContext.for_sync(user_id="admin", thread_id="thread-1")
    runtime = _factory().create(
        context,
        roles=frozenset({"admin"}),
        run_id="run-1",
        allowed_tools=frozenset({"sandbox_execute"}),
        approval_grant=approval_grant,
        routed_skill="sandbox",
    )
    runtime.context.tool_session.search("sandbox", limit=1)
    return context, runtime


def _request(context) -> ToolCallRequest:
    return ToolCallRequest(
        tool_call={
            "name": "sandbox_execute",
            "args": {"language": "python", "source": "print(1)"},
            "id": "call-sandbox",
            "type": "tool_call",
        },
        tool=None,
        state={"messages": []},
        runtime=SimpleNamespace(context=context),
    )


def test_approval_grant_is_names_only_and_bound_to_one_run() -> None:
    grant = _grant()

    assert grant.allows(
        "sandbox_execute",
        user_id="admin",
        tenant_id="default",
        thread_id="thread-1",
        run_id="run-1",
    )
    assert not grant.allows(
        "sandbox_execute",
        user_id="admin",
        tenant_id="default",
        thread_id="thread-1",
        run_id="run-2",
    )
    rendered = repr(grant)
    assert "sandbox_execute" not in rendered
    assert "run-1" not in rendered


def test_factory_rejects_a_cross_run_approval_grant() -> None:
    request_context = RunRequestContext.for_sync(
        user_id="admin",
        thread_id="thread-1",
    )
    with pytest.raises(AppError) as raised:
        _factory().create(
            request_context,
            roles=frozenset({"admin"}),
            run_id="run-1",
            allowed_tools=frozenset({"sandbox_execute"}),
            approval_grant=_grant(run_id="run-2"),
        )

    assert raised.value.code is ErrorCode.POLICY_DENIED
    request_context.close()


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("user_id", "other-user"),
        ("tenant_id", "other-tenant"),
        ("thread_id", "other-thread"),
        ("run_id", "other-run"),
    ],
)
def test_execution_revalidates_every_approval_identity_field(
    field_name: str,
    value: str,
) -> None:
    request_context, runtime = _runtime(approval_grant=_grant())
    handler = Mock(return_value=ToolMessage(content="unexpected", tool_call_id="call"))
    setattr(runtime.context, field_name, value)
    try:
        denied = ToolPolicyMiddleware().wrap_tool_call(
            _request(runtime.context),
            handler,
        )
    finally:
        request_context.close()

    handler.assert_not_called()
    assert denied.status == "error"
    assert "TOOL_APPROVAL_REQUIRED" in denied.content


def test_execution_seam_rechecks_grant_and_requires_approval_before_handler() -> None:
    request_context, runtime = _runtime(approval_grant=_grant())
    handler = Mock(return_value=ToolMessage(content="unexpected", tool_call_id="call"))
    runtime.context.approval_grant = None
    try:
        denied = ToolPolicyMiddleware().wrap_tool_call(
            _request(runtime.context),
            handler,
        )
    finally:
        request_context.close()

    handler.assert_not_called()
    assert denied.status == "error"
    assert "TOOL_APPROVAL_REQUIRED" in denied.content
    assert "DESCRIPTOR_APPROVAL_REQUIRED" not in denied.content
    audit = runtime.context.trace_events[-1]["guardrail_audit"]
    assert audit["decision"] == "REQUIRE_APPROVAL"
    assert audit["reason_code"] == "DESCRIPTOR_APPROVAL_REQUIRED"
    assert "print(1)" not in str(runtime.context.trace_events)


def test_execution_seam_denial_has_zero_side_effects() -> None:
    request_context, runtime = _runtime(approval_grant=_grant())
    handler = Mock(return_value=ToolMessage(content="unexpected", tool_call_id="call"))
    runtime.context.channel = "unknown-channel"
    try:
        denied = ToolPolicyMiddleware().wrap_tool_call(
            _request(runtime.context),
            handler,
        )
    finally:
        request_context.close()

    handler.assert_not_called()
    assert denied.status == "error"
    assert "TOOL_GUARDRAIL_DENIED" in denied.content
    assert "CHANNEL_UNKNOWN" not in denied.content


def test_registry_denial_audit_marks_guardrail_context_incomplete() -> None:
    request_context, runtime = _runtime(approval_grant=_grant())
    handler = Mock(return_value=ToolMessage(content="unexpected", tool_call_id="call"))
    request = _request(runtime.context)
    request.tool_call["name"] = "forged_tool"
    try:
        denied = ToolPolicyMiddleware().wrap_tool_call(request, handler)
    finally:
        request_context.close()

    handler.assert_not_called()
    assert denied.status == "error"
    audit = runtime.context.trace_events[-1]["guardrail_audit"]
    assert audit["reason_code"] == "REGISTRY_POLICY_DENIED"
    assert audit["safe_metadata"]["context_complete"] is False


def test_valid_run_grant_allows_handler_execution() -> None:
    request_context, runtime = _runtime(approval_grant=_grant())
    response = ToolMessage(content="ok", tool_call_id="call-sandbox")
    handler = Mock(return_value=response)
    try:
        returned = ToolPolicyMiddleware().wrap_tool_call(
            _request(runtime.context),
            handler,
        )
    finally:
        request_context.close()

    assert returned is response
    handler.assert_called_once()
    assert not any(
        event.get("stage") == "tool.denied" for event in runtime.context.trace_events
    )
