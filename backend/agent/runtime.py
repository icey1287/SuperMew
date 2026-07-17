from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Literal, Protocol, TypeGuard, runtime_checkable

from langchain_core.messages import AIMessageChunk, BaseMessage, HumanMessage

from backend.agent.context import AgentRuntimeContext
from backend.core.errors import AppError, ErrorCode
from backend.providers import (
    ProviderCallContext,
    ProviderError,
    ProviderOperation,
    classify_provider_exception,
)
from backend.schemas.rag import normalize_rag_trace
from backend.skills import SkillRegistryError


@runtime_checkable
class _ContentCarrier(Protocol):
    @property
    def content(self) -> object: ...


class CompiledAgent(Protocol):
    def invoke(
        self,
        payload: dict[str, list[BaseMessage]],
        *,
        config: dict[str, int],
        context: AgentRuntimeContext,
    ) -> object: ...

    async def ainvoke(
        self,
        payload: dict[str, list[BaseMessage]],
        *,
        config: dict[str, int],
        context: AgentRuntimeContext,
    ) -> object: ...

    def astream(
        self,
        payload: dict[str, list[BaseMessage]],
        *,
        stream_mode: list[str],
        config: dict[str, int],
        context: AgentRuntimeContext,
    ) -> AsyncIterator[object]: ...


def _is_object_mapping(value: object) -> TypeGuard[Mapping[object, object]]:
    return isinstance(value, Mapping)


def _is_object_sequence(value: object) -> TypeGuard[Sequence[object]]:
    return isinstance(value, Sequence) and not isinstance(
        value,
        (str, bytes, bytearray),
    )


def _is_object_tuple(value: object) -> TypeGuard[tuple[object, ...]]:
    return isinstance(value, tuple)


def extract_message_content(message: object) -> str:
    if not isinstance(message, _ContentCarrier):
        return ""
    content = message.content
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        text = ""
        for block in content:
            if isinstance(block, str):
                text += block
            elif _is_object_mapping(block) and block.get("type") == "text":
                text += str(block.get("text") or "")
        return text
    return str(content or "")


def extract_agent_content(result: object) -> str:
    if _is_object_mapping(result):
        if "output" in result:
            return str(result["output"] or "")
        messages = result.get("messages")
        if _is_object_sequence(messages) and messages:
            return extract_message_content(messages[-1])
        return str(result)
    if isinstance(result, _ContentCarrier):
        return extract_message_content(result)
    return str(result or "")


def _is_hitl_trace(trace: Mapping[str, object] | None) -> bool:
    if not trace:
        return False
    return trace.get("retrieval_status") in {
        "needs_clarification",
        "needs_scope_selection",
    } or trace.get("route") in {"clarify", "scope_select"}


@dataclass(frozen=True)
class AgentRuntimeInput:
    history: list[BaseMessage]
    user_text: str

    @property
    def messages(self) -> list[BaseMessage]:
        return [*self.history, HumanMessage(content=self.user_text)]


@dataclass(frozen=True)
class AgentRuntimeResult:
    content: str
    rag_trace: dict[str, object] | None
    hitl_resume_state: dict[str, object] | None
    runtime_trace: tuple[dict[str, object], ...]
    checkpoint_pause: dict[str, object] | None = None


@dataclass(frozen=True)
class AgentRuntimeEvent:
    type: Literal["content", "completed"]
    content: str = ""
    result: AgentRuntimeResult | None = None


class AgentRuntime:
    """Small interface over the compiled Agent graph and its middleware context."""

    def __init__(self, *, agent: CompiledAgent, context: AgentRuntimeContext) -> None:
        self.agent = agent
        self.context = context

    def _config(self) -> dict[str, int]:
        return {"recursion_limit": self.context.budget.recursion_limit}

    def _requires_terminal_buffering(self) -> bool:
        active = (
            self.context.skill_session.active
            if self.context.skill_session is not None
            else None
        )
        return bool(
            (active is not None and active.name == "web-research")
            or self.context.request_context.web_research_requires_terminal_validation()
        )

    def _prepare(self, request: AgentRuntimeInput) -> AgentRuntimeInput:
        try:
            user_text = self.context.prepare_user_text(request.user_text)
        except SkillRegistryError as exc:
            raise AppError(
                ErrorCode.POLICY_DENIED,
                "该 Skill 不可用或当前 Run 已激活其他 Skill。",
                status_code=403,
                category="skill",
                stage="activation",
            ) from exc
        return AgentRuntimeInput(history=request.history, user_text=user_text)

    async def _aprepare(self, request: AgentRuntimeInput) -> AgentRuntimeInput:
        if self.context.skill_session is None:
            return request
        try:
            user_text = await asyncio.to_thread(
                self.context.prepare_user_text,
                request.user_text,
            )
        except SkillRegistryError as exc:
            raise AppError(
                ErrorCode.POLICY_DENIED,
                "该 Skill 不可用或当前 Run 已激活其他 Skill。",
                status_code=403,
                category="skill",
                stage="activation",
            ) from exc
        return AgentRuntimeInput(history=request.history, user_text=user_text)

    def _timeout_error(self, exc: TimeoutError) -> ProviderError:
        request_deadline, cancellation = self.context.request_context.provider_runtime()
        deadlines = [
            value
            for value in (self.context.deadline_at, request_deadline)
            if value is not None
        ]
        deadline = min(deadlines) if deadlines else None
        provider_context = ProviderCallContext(
            provider="agent-runtime",
            operation=ProviderOperation.MODEL,
            deadline=deadline,
            cancellation=cancellation,
        )
        if deadline is not None and time.monotonic() >= deadline:
            return ProviderError.deadline_exceeded(provider_context)
        return classify_provider_exception(exc, context=provider_context)

    def _finish(self, content: str) -> AgentRuntimeResult:
        stored = self.context.request_context.take_rag_trace() or {}
        rag_trace = normalize_rag_trace(stored.get("rag_trace"))
        return AgentRuntimeResult(
            content=content,
            rag_trace=rag_trace,
            hitl_resume_state=stored.get("hitl_resume_state"),
            runtime_trace=tuple(self.context.trace_events),
            checkpoint_pause=self.context.request_context.take_checkpoint_pause(),
        )

    def invoke(self, request: AgentRuntimeInput) -> AgentRuntimeResult:
        try:
            request = self._prepare(request)
            self.context.check_deadline()
            result = self.agent.invoke(
                {"messages": request.messages},
                config=self._config(),
                context=self.context,
            )
            self.context.check_deadline()
        except TimeoutError as exc:
            raise self._timeout_error(exc) from exc
        return self._finish(extract_agent_content(result))

    async def ainvoke(self, request: AgentRuntimeInput) -> AgentRuntimeResult:
        try:
            request = await self._aprepare(request)
            self.context.check_deadline()
            async with asyncio.timeout(self.context.remaining_seconds()):
                result = await self.agent.ainvoke(
                    {"messages": request.messages},
                    config=self._config(),
                    context=self.context,
                )
        except TimeoutError as exc:
            raise self._timeout_error(exc) from exc
        return self._finish(extract_agent_content(result))

    async def astream(
        self,
        request: AgentRuntimeInput,
    ) -> AsyncIterator[AgentRuntimeEvent]:
        full_response = ""
        final_state: object | None = None
        terminal_buffering = False
        try:
            request = await self._aprepare(request)
            terminal_buffering = self._requires_terminal_buffering()
            self.context.check_deadline()
            async with asyncio.timeout(self.context.remaining_seconds()):
                async for item in self.agent.astream(
                    {"messages": request.messages},
                    stream_mode=["messages", "values"],
                    config=self._config(),
                    context=self.context,
                ):
                    mode: Literal["messages", "values"] | None = None
                    payload = item
                    if (
                        _is_object_tuple(item)
                        and len(item) == 2
                        and isinstance(item[0], str)
                    ):
                        candidate_mode = item[0]
                        if candidate_mode in ("messages", "values"):
                            mode = (
                                "messages" if candidate_mode == "messages" else "values"
                            )
                            payload = item[1]
                    if mode == "values":
                        final_state = payload
                        continue
                    message = (
                        payload[0] if _is_object_tuple(payload) and payload else payload
                    )
                    if not isinstance(message, AIMessageChunk):
                        continue
                    if message.tool_call_chunks:
                        continue
                    content = extract_message_content(message)
                    if not content:
                        continue
                    stored = self.context.request_context.peek_rag_trace() or {}
                    if _is_hitl_trace(normalize_rag_trace(stored.get("rag_trace"))):
                        continue
                    full_response += content
                    terminal_buffering = (
                        terminal_buffering or self._requires_terminal_buffering()
                    )
                    if not terminal_buffering:
                        yield AgentRuntimeEvent(type="content", content=content)
        except TimeoutError as exc:
            raise self._timeout_error(exc) from exc

        authoritative_content = (
            extract_agent_content(final_state)
            if final_state is not None
            else full_response
        )
        if terminal_buffering and final_state is None:
            self.context.record_trace(
                "web.citation_rejected",
                error_code="WEB_CITATION_FINAL_STATE_MISSING",
                evidence_count=self.context.request_context.web_evidence_count(),
            )
            authoritative_content = "网页引用校验未完成，本次回答未发布。请稍后重试。"
        result = self._finish(authoritative_content)
        if terminal_buffering and authoritative_content:
            yield AgentRuntimeEvent(type="content", content=authoritative_content)
        yield AgentRuntimeEvent(type="completed", result=result)
