from __future__ import annotations

import asyncio
import logging
import time

from langchain_core.messages import HumanMessage

from backend.agent.models import ModelRegistry, ModelRole, model_registry
from backend.agent.runtime import extract_message_content
from backend.core.errors import public_error_from_exception
from backend.core.settings import AppSettings, get_settings
from backend.model_control import ModelCatalogSnapshot
from backend.providers import (
    ProviderCallContext,
    ProviderExecutor,
    ProviderOperation,
    ProviderPolicy,
    provider_executor,
)


logger = logging.getLogger(__name__)


class PersistentMemoryManager:
    """Debounced conversation-note maintenance behind one testable interface."""

    def __init__(
        self,
        *,
        settings: AppSettings | None = None,
        models: ModelRegistry = model_registry,
        executor: ProviderExecutor = provider_executor,
        policy: ProviderPolicy = ProviderPolicy(max_attempts=2),
    ) -> None:
        self.settings = settings or get_settings()
        self.models = models
        self.executor = executor
        self.policy = policy

    def should_update(self, messages: list, current_note: str) -> bool:
        return (
            bool(current_note)
            or len(messages) > self.settings.agent.memory_message_threshold
        )

    def update_sync(
        self,
        current_note: str,
        user_text: str,
        ai_response: str,
        *,
        history_messages: list | None = None,
        model_snapshot: ModelCatalogSnapshot | None = None,
    ) -> str:
        try:
            history_text = ""
            if history_messages:
                history_lines = []
                for message in history_messages:
                    role = "用户" if isinstance(message, HumanMessage) else "AI"
                    history_lines.append(f"{role}：{extract_message_content(message)}")
                history_text = (
                    "\n\n▼ 首次建立笔记时需要一并概括的此前对话：\n"
                    + "\n".join(history_lines)
                    + "\n\n"
                )
            prompt = (
                "你是一个上下文管理器，负责维护多轮对话的持久化笔记。\n"
                "只记录用户明确表达、未来仍有价值的事实与已完成事项。\n"
                "不要把模型推断写成用户事实；冲突信息保留最新来源说明。\n"
                "将新信息与现有笔记合并，过滤噪音，控制在 500 字以内。\n\n"
                f"▼ 现有笔记：\n{current_note if current_note else '无'}\n\n"
                f"{history_text}"
                f"▼ 最新一轮对话：\n用户：{user_text}\nAI：{ai_response}\n\n"
                "请直接输出更新后的纯文本笔记："
            )
            model = (
                self.models.get(ModelRole.FAST, snapshot=model_snapshot)
                if model_snapshot is not None
                else self.models.get(ModelRole.FAST)
            )
            timeout_seconds = self.settings.models.timeout_seconds
            if model_snapshot is not None:
                timeout_seconds = self.models.describe(
                    ModelRole.FAST,
                    snapshot=model_snapshot,
                ).timeout_seconds
            provider = str(
                getattr(model, "model_name", None)
                or getattr(model, "model", None)
                or "fast-model"
            )
            result = self.executor.call(
                lambda: model.invoke([HumanMessage(content=prompt)]),
                context=ProviderCallContext(
                    provider=provider,
                    operation=ProviderOperation.MODEL,
                    deadline=time.monotonic() + timeout_seconds,
                ),
                policy=self.policy,
            )
            return extract_message_content(result).strip()
        except Exception as exc:
            public = public_error_from_exception(exc)
            logger.warning(
                "Persistent memory update failed error_code=%s",
                public.code,
            )
            return current_note

    async def update(
        self,
        current_note: str,
        user_text: str,
        ai_response: str,
        *,
        history_messages: list | None = None,
        model_snapshot: ModelCatalogSnapshot | None = None,
    ) -> str:
        return await asyncio.to_thread(
            self.update_sync,
            current_note,
            user_text,
            ai_response,
            history_messages=history_messages,
            model_snapshot=model_snapshot,
        )


memory_manager = PersistentMemoryManager()
