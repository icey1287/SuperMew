from __future__ import annotations

import asyncio
import logging

from langchain_core.messages import HumanMessage

from backend.agent.models import ModelRegistry, ModelRole, model_registry
from backend.agent.runtime import extract_message_content
from backend.core.settings import AppSettings, get_settings


logger = logging.getLogger(__name__)


class PersistentMemoryManager:
    """Debounced conversation-note maintenance behind one testable interface."""

    def __init__(
        self,
        *,
        settings: AppSettings | None = None,
        models: ModelRegistry = model_registry,
    ) -> None:
        self.settings = settings or get_settings()
        self.models = models

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
            result = self.models.get(ModelRole.FAST).invoke(
                [HumanMessage(content=prompt)]
            )
            return extract_message_content(result).strip()
        except Exception:
            logger.exception("Persistent memory update failed")
            return current_note

    async def update(
        self,
        current_note: str,
        user_text: str,
        ai_response: str,
        *,
        history_messages: list | None = None,
    ) -> str:
        return await asyncio.to_thread(
            self.update_sync,
            current_note,
            user_text,
            ai_response,
            history_messages=history_messages,
        )


memory_manager = PersistentMemoryManager()
