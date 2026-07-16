from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
from pydantic import ValidationError

import backend.api.routes.runs as routes
from backend.core.errors import AppError, ErrorCode
from backend.schemas.runs import RunCreateRequest


def _request(*approved_tools: str) -> RunCreateRequest:
    return RunCreateRequest(
        message="run code",
        idempotency_key="request-1",
        approved_tools=approved_tools,
    )


@pytest.mark.asyncio
async def test_non_admin_cannot_self_approve_a_high_risk_tool() -> None:
    user = SimpleNamespace(username="alice", role="user")
    create_run = Mock()

    with patch.object(routes.service, "create_run", create_run):
        with pytest.raises(AppError) as raised:
            await routes._reserve_run(
                user=user,
                thread_id="thread-1",
                request=_request("sandbox_execute"),
            )

    assert raised.value.code is ErrorCode.POLICY_DENIED
    create_run.assert_not_called()


@pytest.mark.asyncio
async def test_admin_can_persist_an_explicit_sandbox_approval() -> None:
    user = SimpleNamespace(username="admin", role="admin")
    reservation = SimpleNamespace(
        run=SimpleNamespace(id="run-1", supersedes_run_id=None)
    )
    create_run = Mock(return_value=reservation)
    spawn_once = AsyncMock(return_value=None)

    with (
        patch.object(routes.service, "create_run", create_run),
        patch.object(routes.run_agent_executor, "spawn_once", spawn_once),
        patch.object(
            routes,
            "get_settings",
            return_value=SimpleNamespace(
                app=SimpleNamespace(default_tenant_id="default")
            ),
        ),
    ):
        result = await routes._reserve_run(
            user=user,
            thread_id="thread-1",
            request=_request("sandbox_execute"),
        )

    assert result is reservation
    assert create_run.call_args.kwargs["approved_tools"] == frozenset(
        {"sandbox_execute"}
    )
    assert create_run.call_args.kwargs["tenant_id"] == "default"
    spawn_once.assert_awaited_once_with(username="admin", run_id="run-1")


@pytest.mark.asyncio
async def test_admin_cannot_mark_a_low_risk_tool_as_approved() -> None:
    user = SimpleNamespace(username="admin", role="admin")

    with pytest.raises(AppError) as raised:
        await routes._reserve_run(
            user=user,
            thread_id="thread-1",
            request=_request("web_search"),
        )

    assert raised.value.code is ErrorCode.INVALID_REQUEST


def test_approval_request_contract_rejects_duplicates_and_invalid_names() -> None:
    with pytest.raises(ValidationError):
        _request("sandbox_execute", "sandbox_execute")
    with pytest.raises(ValidationError):
        _request("../sandbox_execute")
