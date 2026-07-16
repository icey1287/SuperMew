from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from langchain.agents.middleware import (
    AgentMiddleware,
    ModelCallLimitMiddleware,
    ModelRequest,
    ToolCallLimitMiddleware,
    ToolCallRequest,
)
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.messages.utils import count_tokens_approximately

from backend.agent.context import AgentRuntimeContext, RuntimeBudget
from backend.core.errors import AppError, ErrorCode, public_error_from_exception
from backend.providers import (
    ProviderCallContext,
    ProviderExecutor,
    ProviderOperation,
    ProviderPolicy,
    provider_executor,
)
from backend.tools.contracts import ToolResultV1, new_tool_failure


DEFAULT_MIDDLEWARE_ORDER = (
    "RequestContextMiddleware",
    "RuntimeTracingMiddleware",
    "DynamicContextMiddleware",
    "ContextBudgetMiddleware",
    "ToolPolicyMiddleware",
    "ToolCallLimitMiddleware",
    "ModelCallLimitMiddleware",
    "LoopDetectionMiddleware",
    "TerminalResponseMiddleware",
    "ClarificationHITLMiddleware",
)

_DYNAMIC_CONTEXT_MARKER = "supermew_dynamic_context"
_ACTIVE_SKILL_MARKER = "supermew_active_skill"


def _runtime_context(runtime) -> AgentRuntimeContext:
    context = getattr(runtime, "context", None)
    if not isinstance(context, AgentRuntimeContext):
        raise RuntimeError("AgentRuntimeContext is required")
    return context


def _message_text(message: BaseMessage) -> str:
    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            item if isinstance(item, str) else str(item.get("text", ""))
            for item in content
            if isinstance(item, (str, dict))
        )
    return str(content or "")


def _typed_tool_result(response) -> ToolResultV1 | None:
    content = getattr(response, "content", None)
    if not isinstance(content, str) or not content.startswith("{"):
        return None
    try:
        result = ToolResultV1.model_validate_json(content)
    except ValueError:
        return None
    return result


def _typed_result_size(result: ToolResultV1 | None) -> int:
    if result is None:
        return 0
    value = result.observability_metadata.get("result_size")
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return 0


def _tool_audit_key(tool_call: dict) -> str:
    payload = json.dumps(
        {
            "id": str(tool_call.get("id") or ""),
            "name": str(tool_call.get("name") or ""),
            "args": tool_call.get("args") or {},
        },
        ensure_ascii=False,
        sort_keys=True,
        default=str,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def estimate_request_tokens(
    messages: Sequence[BaseMessage],
    *,
    system_message: SystemMessage | None = None,
    tools: list | None = None,
) -> int:
    all_messages = [system_message, *messages] if system_message else list(messages)
    # One character per token is intentionally conservative for mixed CJK/Latin text.
    return count_tokens_approximately(
        all_messages,
        chars_per_token=1.0,
        extra_tokens_per_message=6.0,
        tools=tools,
    )


@dataclass(frozen=True)
class ContextPackingResult:
    messages: list[BaseMessage]
    removed_count: int
    truncated_count: int
    estimated_tokens: int


def _conversation_turns(messages: Sequence[BaseMessage]) -> list[list[BaseMessage]]:
    turns: list[list[BaseMessage]] = []
    current: list[BaseMessage] = []
    for message in messages:
        if isinstance(message, HumanMessage) and current:
            turns.append(current)
            current = [message]
        else:
            current.append(message)
    if current:
        turns.append(current)
    return turns


def _assert_tool_protocol(messages: Sequence[BaseMessage]) -> None:
    visible_tool_calls: set[str] = set()
    for message in messages:
        if isinstance(message, AIMessage):
            visible_tool_calls.update(
                str(item.get("id"))
                for item in (message.tool_calls or [])
                if item.get("id")
            )
        elif (
            isinstance(message, ToolMessage)
            and message.tool_call_id not in visible_tool_calls
        ):
            raise RuntimeError("Context packing produced an orphan ToolMessage")


def _truncate_message(message: BaseMessage, target_chars: int) -> BaseMessage:
    text = _message_text(message)
    if len(text) <= target_chars:
        return message
    marker = "\n…[truncated by context budget]"
    prefix_size = max(target_chars - len(marker), 1)
    return message.model_copy(update={"content": text[:prefix_size] + marker})


def _is_atomic_dynamic_context(message: BaseMessage) -> bool:
    return bool(
        isinstance(message, SystemMessage)
        and message.additional_kwargs.get(_DYNAMIC_CONTEXT_MARKER)
    )


def _compact_to_budget(
    messages: list[BaseMessage],
    *,
    token_budget: int,
    system_message: SystemMessage | None,
    tools: list | None,
) -> tuple[list[BaseMessage], int, int]:
    compacted = list(messages)
    truncated_indexes: set[int] = set()
    estimated = estimate_request_tokens(
        compacted,
        system_message=system_message,
        tools=tools,
    )
    while estimated > token_budget:
        candidates = sorted(
            (
                index
                for index in range(len(compacted))
                if not _is_atomic_dynamic_context(compacted[index])
            ),
            key=lambda index: (
                0 if isinstance(compacted[index], ToolMessage) else 1,
                0 if isinstance(compacted[index], AIMessage) else 1,
                -len(_message_text(compacted[index])),
            ),
        )
        changed = False
        for index in candidates:
            message = compacted[index]
            text = _message_text(message)
            minimum = 96 if isinstance(message, HumanMessage) else 32
            if len(text) <= minimum:
                continue
            over = estimated - token_budget
            target = max(minimum, len(text) - max(over, len(text) // 3))
            compacted[index] = _truncate_message(message, target)
            truncated_indexes.add(index)
            changed = True
            estimated = estimate_request_tokens(
                compacted,
                system_message=system_message,
                tools=tools,
            )
            if estimated <= token_budget:
                break
        if not changed:
            break
    return compacted, len(truncated_indexes), estimated


def trim_messages_to_budget(
    messages: Sequence[BaseMessage],
    token_budget: int,
    *,
    system_message: SystemMessage | None = None,
    tools: list | None = None,
) -> ContextPackingResult:
    if not messages:
        estimated = estimate_request_tokens(
            [], system_message=system_message, tools=tools
        )
        return ContextPackingResult([], 0, 0, estimated)
    system_messages = [item for item in messages if isinstance(item, SystemMessage)]
    conversation = [item for item in messages if not isinstance(item, SystemMessage)]
    turns = _conversation_turns(conversation)
    retained_turns = [turns[-1]] if turns else []
    retained = [*system_messages, *(retained_turns[0] if retained_turns else [])]

    for turn in reversed(turns[:-1]):
        candidate = [
            *system_messages,
            *turn,
            *[item for group in retained_turns for item in group],
        ]
        if (
            estimate_request_tokens(
                candidate,
                system_message=system_message,
                tools=tools,
            )
            <= token_budget
        ):
            retained_turns.insert(0, turn)
            retained = candidate

    retained, truncated_count, estimated = _compact_to_budget(
        retained,
        token_budget=token_budget,
        system_message=system_message,
        tools=tools,
    )
    if estimated > token_budget:
        raise RuntimeError(
            "Agent context cannot fit within the configured token budget"
        )
    _assert_tool_protocol(retained)
    return ContextPackingResult(
        messages=retained,
        removed_count=max(0, len(messages) - len(retained)),
        truncated_count=truncated_count,
        estimated_tokens=estimated,
    )


class RequestContextMiddleware(AgentMiddleware):
    def before_agent(self, state, runtime):
        context = _runtime_context(runtime)
        if context.user_id != context.request_context.user_id:
            raise RuntimeError("Agent user_id does not match request context")
        if context.thread_id != context.request_context.session_id:
            raise RuntimeError("Agent thread_id does not match request context")
        context.check_deadline()
        context.request_context.reset_knowledge_tool_budget()
        context.record_trace("agent.started")
        return None

    async def abefore_agent(self, state, runtime):
        return self.before_agent(state, runtime)


class RuntimeTracingMiddleware(AgentMiddleware):
    def __init__(
        self,
        *,
        executor: ProviderExecutor = provider_executor,
        model_policy: ProviderPolicy = ProviderPolicy(max_attempts=1),
    ) -> None:
        # Answer calls can publish deltas before an upstream failure surfaces.
        # Retrying the whole call would duplicate already-visible content. Structured
        # planner/grader/rewrite calls own their separate, non-streaming retry policy.
        self.executor = executor
        self.model_policy = model_policy

    @staticmethod
    def _model_context(request, context: AgentRuntimeContext) -> ProviderCallContext:
        request_deadline, cancellation = context.request_context.provider_runtime()
        deadlines = [
            value
            for value in (context.deadline_at, request_deadline)
            if value is not None
        ]
        model = request.model
        provider = (
            getattr(model, "model_name", None)
            or getattr(model, "model", None)
            or type(model).__name__
            or "answer-model"
        )
        return ProviderCallContext(
            provider=str(provider),
            operation=ProviderOperation.MODEL,
            deadline=min(deadlines) if deadlines else None,
            cancellation=cancellation,
        )

    @staticmethod
    def _record_model_failure(
        context: AgentRuntimeContext,
        exc: Exception,
        *,
        started: float,
        attempts: int,
    ) -> None:
        code = getattr(getattr(exc, "code", None), "value", None)
        context.record_trace(
            "model.failed",
            duration_ms=int((time.monotonic() - started) * 1000),
            error_code=code or "MODEL_UNAVAILABLE",
            retryable=bool(getattr(exc, "retryable", False)),
            attempts=max(attempts, getattr(exc, "attempts", 1)),
        )

    def wrap_model_call(self, request, handler):
        context = _runtime_context(request.runtime)
        context.check_deadline()
        started = time.monotonic()
        attempts = 0

        def _invoke():
            nonlocal attempts
            attempts += 1
            return handler(request)

        try:
            response = self.executor.call(
                _invoke,
                context=self._model_context(request, context),
                policy=self.model_policy,
            )
        except Exception as exc:
            self._record_model_failure(
                context,
                exc,
                started=started,
                attempts=attempts,
            )
            raise
        context.record_trace(
            "model.completed",
            duration_ms=int((time.monotonic() - started) * 1000),
            attempts=attempts,
        )
        return response

    async def awrap_model_call(self, request, handler):
        context = _runtime_context(request.runtime)
        context.check_deadline()
        started = time.monotonic()
        attempts = 0

        async def _invoke():
            nonlocal attempts
            attempts += 1
            return await handler(request)

        try:
            response = await self.executor.acall(
                _invoke,
                context=self._model_context(request, context),
                policy=self.model_policy,
            )
        except Exception as exc:
            self._record_model_failure(
                context,
                exc,
                started=started,
                attempts=attempts,
            )
            raise
        context.record_trace(
            "model.completed",
            duration_ms=int((time.monotonic() - started) * 1000),
            attempts=attempts,
        )
        return response

    def wrap_tool_call(self, request, handler):
        context = _runtime_context(request.runtime)
        context.check_deadline()
        started = time.monotonic()
        tool_name = str(request.tool_call.get("name") or "unknown")
        tool_call_id = str(request.tool_call.get("id") or "")
        tool_audit_key = _tool_audit_key(dict(request.tool_call))
        try:
            response = handler(request)
        except Exception as exc:
            public = public_error_from_exception(exc)
            context.record_trace(
                "tool.failed",
                tool_name=tool_name,
                tool_call_id=tool_call_id,
                tool_audit_key=tool_audit_key,
                duration_ms=int((time.monotonic() - started) * 1000),
                error_code=str(public.code),
                retryable=public.retryable,
                fallback_applied=False,
            )
            raise
        typed_result = _typed_tool_result(response)
        if typed_result is not None and not typed_result.success:
            if typed_result.error_code == "TOOL_POLICY_DENIED":
                return response
            context.record_trace(
                "tool.failed",
                tool_name=tool_name,
                tool_call_id=tool_call_id,
                tool_audit_key=tool_audit_key,
                duration_ms=typed_result.duration_ms,
                error_code=typed_result.error_code,
                retryable=typed_result.retryable,
                fallback_applied=False,
                result_size=_typed_result_size(typed_result),
            )
            return response
        context.record_trace(
            "tool.completed",
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            tool_audit_key=tool_audit_key,
            duration_ms=int((time.monotonic() - started) * 1000),
            result_size=_typed_result_size(typed_result),
        )
        return response

    async def awrap_tool_call(self, request, handler):
        context = _runtime_context(request.runtime)
        context.check_deadline()
        started = time.monotonic()
        tool_name = str(request.tool_call.get("name") or "unknown")
        tool_call_id = str(request.tool_call.get("id") or "")
        tool_audit_key = _tool_audit_key(dict(request.tool_call))
        try:
            response = await handler(request)
        except Exception as exc:
            public = public_error_from_exception(exc)
            context.record_trace(
                "tool.failed",
                tool_name=tool_name,
                tool_call_id=tool_call_id,
                tool_audit_key=tool_audit_key,
                duration_ms=int((time.monotonic() - started) * 1000),
                error_code=str(public.code),
                retryable=public.retryable,
                fallback_applied=False,
            )
            raise
        typed_result = _typed_tool_result(response)
        if typed_result is not None and not typed_result.success:
            if typed_result.error_code == "TOOL_POLICY_DENIED":
                return response
            context.record_trace(
                "tool.failed",
                tool_name=tool_name,
                tool_call_id=tool_call_id,
                tool_audit_key=tool_audit_key,
                duration_ms=typed_result.duration_ms,
                error_code=typed_result.error_code,
                retryable=typed_result.retryable,
                fallback_applied=False,
                result_size=_typed_result_size(typed_result),
            )
            return response
        context.record_trace(
            "tool.completed",
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            tool_audit_key=tool_audit_key,
            duration_ms=int((time.monotonic() - started) * 1000),
            result_size=_typed_result_size(typed_result),
        )
        return response


class DynamicContextMiddleware(AgentMiddleware):
    @staticmethod
    def _override(request: ModelRequest) -> ModelRequest:
        context = _runtime_context(request.runtime)
        visible_tools = (
            None
            if request.tools is None
            else [
                tool
                for tool in request.tools
                if context.is_tool_allowed(_tool_name(tool))
            ]
        )
        active_skill = context.has_active_skill()

        def project(
            *,
            memory_char_limit: int | None,
            include_skill_catalog: bool,
        ) -> tuple[SystemMessage, int]:
            message = SystemMessage(
                content=context.dynamic_context_message(
                    memory_char_limit=memory_char_limit,
                    include_skill_catalog=include_skill_catalog,
                ),
                additional_kwargs={
                    _DYNAMIC_CONTEXT_MARKER: True,
                    _ACTIVE_SKILL_MARKER: active_skill,
                },
            )
            estimated = estimate_request_tokens(
                [message, *request.messages],
                system_message=request.system_message,
                tools=visible_tools,
            )
            return message, estimated

        note_length = len(context.persistent_note.strip())
        dynamic_message, estimated = project(
            memory_char_limit=None,
            include_skill_catalog=True,
        )
        memory_truncated = False
        catalog_omitted = False
        if estimated > context.budget.input_token_budget:
            without_memory, without_memory_estimated = project(
                memory_char_limit=0,
                include_skill_catalog=True,
            )
            memory_truncated = note_length > 0
            if without_memory_estimated <= context.budget.input_token_budget:
                low = 0
                high = note_length
                while low < high:
                    candidate = (low + high + 1) // 2
                    candidate_message, candidate_estimated = project(
                        memory_char_limit=candidate,
                        include_skill_catalog=True,
                    )
                    if candidate_estimated <= context.budget.input_token_budget:
                        low = candidate
                        dynamic_message = candidate_message
                        estimated = candidate_estimated
                    else:
                        high = candidate - 1
                if low == 0:
                    dynamic_message = without_memory
                    estimated = without_memory_estimated
                memory_truncated = low < note_length
            else:
                dynamic_message, estimated = project(
                    memory_char_limit=0,
                    include_skill_catalog=False,
                )
                catalog_omitted = True
        if memory_truncated or catalog_omitted:
            context.record_trace(
                "context.dynamic_trimmed",
                memory_truncated=memory_truncated,
                catalog_omitted=catalog_omitted,
                active_skill_preserved=active_skill,
                estimated_tokens=estimated,
            )
        return request.override(messages=[dynamic_message, *request.messages])

    def wrap_model_call(self, request, handler):
        return handler(self._override(request))

    async def awrap_model_call(self, request, handler):
        return await handler(self._override(request))


class ContextBudgetMiddleware(AgentMiddleware):
    @staticmethod
    def _override(request: ModelRequest) -> ModelRequest:
        context = _runtime_context(request.runtime)
        visible_tools = (
            None
            if request.tools is None
            else [
                tool
                for tool in request.tools
                if context.is_tool_allowed(_tool_name(tool))
            ]
        )
        try:
            packed = trim_messages_to_budget(
                request.messages,
                context.budget.input_token_budget,
                system_message=request.system_message,
                tools=visible_tools,
            )
        except RuntimeError as exc:
            active_skill_required = any(
                bool(message.additional_kwargs.get(_ACTIVE_SKILL_MARKER))
                for message in request.messages
                if _is_atomic_dynamic_context(message)
            )
            if active_skill_required:
                context.record_trace(
                    "skill.context_rejected",
                    error_code="ACTIVE_SKILL_CONTEXT_BUDGET_EXCEEDED",
                )
                raise AppError(
                    ErrorCode.POLICY_DENIED,
                    "Active Skill 指令无法完整放入当前上下文预算。",
                    status_code=403,
                    category="skill",
                    stage="context_budget",
                ) from exc
            raise
        changed = packed.removed_count + packed.truncated_count
        if changed:
            context.trimmed_message_count += changed
            context.record_trace(
                "context.trimmed",
                removed_count=packed.removed_count,
                truncated_count=packed.truncated_count,
                estimated_tokens=packed.estimated_tokens,
            )
        return request.override(messages=packed.messages)

    def wrap_model_call(self, request, handler):
        return handler(self._override(request))

    async def awrap_model_call(self, request, handler):
        return await handler(self._override(request))


def _tool_name(tool: Any) -> str:
    if isinstance(tool, dict):
        function = tool.get("function")
        if isinstance(function, dict):
            return str(function.get("name") or "")
        return str(tool.get("name") or "")
    return str(getattr(tool, "name", "") or "")


class ToolPolicyMiddleware(AgentMiddleware):
    @staticmethod
    def _override(request: ModelRequest) -> ModelRequest:
        context = _runtime_context(request.runtime)
        if request.tools is None:
            return request
        tools = [
            tool for tool in request.tools if context.is_tool_allowed(_tool_name(tool))
        ]
        return request.override(tools=tools)

    def wrap_model_call(self, request, handler):
        return handler(self._override(request))

    async def awrap_model_call(self, request, handler):
        return await handler(self._override(request))

    @staticmethod
    def _deny(request: ToolCallRequest) -> ToolMessage | None:
        context = _runtime_context(request.runtime)
        tool_name = str(request.tool_call.get("name") or "")
        if context.is_tool_allowed(tool_name):
            return None
        context.record_trace(
            "tool.denied",
            tool_name=tool_name,
            tool_call_id=str(request.tool_call.get("id") or ""),
            tool_audit_key=_tool_audit_key(dict(request.tool_call)),
        )
        return ToolMessage(
            content=new_tool_failure(
                error_code="TOOL_POLICY_DENIED",
                retryable=False,
                data={"message": "当前 Run 无权执行该工具。"},
                observability_metadata={"tool_name": tool_name},
            ).model_dump_json(),
            tool_call_id=str(request.tool_call.get("id") or "unknown"),
            status="error",
        )

    def wrap_tool_call(self, request, handler):
        denied = self._deny(request)
        return denied if denied is not None else handler(request)

    async def awrap_tool_call(self, request, handler):
        denied = self._deny(request)
        return denied if denied is not None else await handler(request)


def _tool_fingerprint(tool_call: dict) -> str:
    payload = json.dumps(
        {
            "name": tool_call.get("name"),
            "args": tool_call.get("args") or {},
        },
        ensure_ascii=False,
        sort_keys=True,
        default=str,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class LoopDetectionMiddleware(AgentMiddleware):
    @staticmethod
    def _check(request: ToolCallRequest) -> ToolMessage | None:
        context = _runtime_context(request.runtime)
        context.check_deadline()
        count, alternating = context.register_tool_fingerprint(
            _tool_fingerprint(dict(request.tool_call))
        )
        if count <= context.budget.max_repeated_tool_calls and not alternating:
            return None
        tool_name = str(request.tool_call.get("name") or "unknown")
        context.record_trace(
            "tool.loop_blocked",
            tool_name=tool_name,
            tool_call_id=str(request.tool_call.get("id") or ""),
            tool_audit_key=_tool_audit_key(dict(request.tool_call)),
            repeat_count=count,
            alternating=alternating,
        )
        return ToolMessage(
            content=new_tool_failure(
                error_code="TOOL_LOOP_BLOCKED",
                retryable=False,
                data={
                    "message": (
                        "相同工具与参数已重复调用，请基于现有结果总结并结束本轮。"
                    )
                },
                observability_metadata={
                    "tool_name": tool_name,
                    "repeat_count": count,
                    "alternating": alternating,
                },
            ).model_dump_json(),
            tool_call_id=str(request.tool_call.get("id") or "unknown"),
            status="error",
        )

    def wrap_tool_call(self, request, handler):
        blocked = self._check(request)
        return blocked if blocked is not None else handler(request)

    async def awrap_tool_call(self, request, handler):
        blocked = self._check(request)
        return blocked if blocked is not None else await handler(request)


class TerminalResponseMiddleware(AgentMiddleware):
    @staticmethod
    def _finish(state, runtime):
        context = _runtime_context(runtime)
        messages = list(state.get("messages") or [])
        last = messages[-1] if messages else None
        if not isinstance(last, AIMessage) or not _message_text(last).strip():
            context.record_trace("terminal.fallback")
            return {
                "messages": [
                    AIMessage(content="任务未能生成有效的最终回答，请稍后重试。")
                ]
            }
        context.record_trace("agent.completed")
        return None

    def after_agent(self, state, runtime):
        return self._finish(state, runtime)

    async def aafter_agent(self, state, runtime):
        return self._finish(state, runtime)


class ClarificationHITLMiddleware(AgentMiddleware):
    @staticmethod
    def _observe(runtime):
        context = _runtime_context(runtime)
        stored = context.request_context.peek_rag_trace() or {}
        trace = stored.get("rag_trace") or {}
        if trace.get("retrieval_status") in {
            "needs_clarification",
            "needs_scope_selection",
        } or trace.get("route") in {"clarify", "scope_select"}:
            context.record_trace("agent.waiting_input")
        return None

    def after_agent(self, state, runtime):
        return self._observe(runtime)

    async def aafter_agent(self, state, runtime):
        return self._observe(runtime)


def build_default_middleware(budget: RuntimeBudget) -> tuple[AgentMiddleware, ...]:
    middleware: tuple[AgentMiddleware, ...] = (
        RequestContextMiddleware(),
        RuntimeTracingMiddleware(),
        DynamicContextMiddleware(),
        ContextBudgetMiddleware(),
        ToolPolicyMiddleware(),
        ToolCallLimitMiddleware(
            run_limit=budget.max_tool_calls,
            exit_behavior="continue",
        ),
        ModelCallLimitMiddleware(
            run_limit=budget.max_model_calls,
            exit_behavior="end",
        ),
        LoopDetectionMiddleware(),
        TerminalResponseMiddleware(),
        ClarificationHITLMiddleware(),
    )
    names = tuple(item.name for item in middleware)
    if names != DEFAULT_MIDDLEWARE_ORDER:
        raise RuntimeError(f"Unexpected middleware order: {names}")
    return middleware
