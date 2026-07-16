from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Literal

from langchain_core.messages import AIMessageChunk, BaseMessage, HumanMessage

from backend.agent.context import AgentRuntimeContext
from backend.core.errors import AppError, ErrorCode
from backend.providers import (
    ProviderCallContext,
    ProviderError,
    ProviderOperation,
    classify_provider_exception,
)
from backend.schemas.chat import normalize_rag_trace
from backend.skills import SkillRegistryError


def extract_message_content(message) -> str:
    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        text = ""
        for block in content:
            if isinstance(block, str):
                text += block
            elif isinstance(block, dict) and block.get("type") == "text":
                text += str(block.get("text") or "")
        return text
    return str(content or "")


def extract_agent_content(result) -> str:
    if isinstance(result, dict):
        if "output" in result:
            return str(result["output"] or "")
        messages = result.get("messages") or []
        if messages:
            return extract_message_content(messages[-1])
        return str(result)
    if hasattr(result, "content"):
        return extract_message_content(result)
    return str(result or "")


def _is_hitl_trace(trace: dict | None) -> bool:
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
    rag_trace: dict | None
    hitl_resume_state: dict | None
    runtime_trace: tuple[dict, ...]
    checkpoint_pause: dict | None = None


@dataclass(frozen=True)
class AgentRuntimeEvent:
    type: Literal["content", "completed"]
    content: str = ""
    result: AgentRuntimeResult | None = None


class AgentRuntime:
    """Small interface over the compiled Agent graph and its middleware context."""

    def __init__(self, *, agent, context: AgentRuntimeContext) -> None:
        self.agent = agent
        self.context = context

    def _config(self) -> dict:
        return {"recursion_limit": self.context.budget.recursion_limit}

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

    async def astream(self, request: AgentRuntimeInput):
        full_response = ""
        final_state = None
        try:
            request = await self._aprepare(request)
            self.context.check_deadline()
            async with asyncio.timeout(self.context.remaining_seconds()):
                async for item in self.agent.astream(
                    {"messages": request.messages},
                    stream_mode=["messages", "values"],
                    config=self._config(),
                    context=self.context,
                ):
                    mode = None
                    payload = item
                    if (
                        isinstance(item, tuple)
                        and len(item) == 2
                        and isinstance(item[0], str)
                        and item[0] in {"messages", "values"}
                    ):
                        mode, payload = item
                    if mode == "values":
                        final_state = payload
                        continue
                    message = (
                        payload[0]
                        if isinstance(payload, tuple) and payload
                        else payload
                    )
                    if not isinstance(message, AIMessageChunk):
                        continue
                    if getattr(message, "tool_call_chunks", None):
                        continue
                    content = extract_message_content(message)
                    if not content:
                        continue
                    stored = self.context.request_context.peek_rag_trace() or {}
                    if _is_hitl_trace(normalize_rag_trace(stored.get("rag_trace"))):
                        continue
                    full_response += content
                    yield AgentRuntimeEvent(type="content", content=content)
        except TimeoutError as exc:
            raise self._timeout_error(exc) from exc

        authoritative_content = (
            extract_agent_content(final_state)
            if final_state is not None
            else full_response
        )
        result = self._finish(authoritative_content)
        yield AgentRuntimeEvent(type="completed", result=result)
