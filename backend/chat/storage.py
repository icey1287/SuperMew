from __future__ import annotations

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from backend.chat.repository import (
    ConversationRepository,
    MessageRecord,
    UserAccessSnapshot,
    repository,
)
from backend.infra.cache import cache


class ConversationStorage:
    """旧聊天调用面的 compatibility adapter；持久化由 append-only journal 完成。"""

    def __init__(self, conversation_repository: ConversationRepository = repository):
        self.repository = conversation_repository

    @staticmethod
    def _sessions_cache_key(user_id: str) -> str:
        return f"chat_sessions:v2:{user_id}"

    @staticmethod
    def _legacy_messages_cache_key(user_id: str, session_id: str) -> str:
        return f"chat_messages:{user_id}:{session_id}"

    @staticmethod
    def _to_langchain_messages(records: list[dict]) -> list:
        messages = []
        for item in records:
            if item.get("type") == "human":
                messages.append(HumanMessage(content=item.get("content", "")))
            elif item.get("type") == "ai":
                messages.append(AIMessage(content=item.get("content", "")))
            elif item.get("type") == "system":
                messages.append(SystemMessage(content=item.get("content", "")))
        return messages

    @staticmethod
    def _serialize(record: MessageRecord) -> dict:
        return {
            "id": record.id,
            "run_id": record.run_id,
            "sequence": record.sequence,
            "status": record.status,
            "type": record.role,
            "content": record.content,
            "timestamp": record.timestamp.isoformat(),
            "rag_trace": record.rag_trace,
        }

    def _invalidate(self, user_id: str, session_id: str) -> None:
        cache.delete(self._sessions_cache_key(user_id))
        cache.delete(self._legacy_messages_cache_key(user_id, session_id))

    def save(
        self,
        user_id: str,
        session_id: str,
        messages: list,
        metadata: dict = None,
        extra_message_data: list = None,
    ) -> None:
        self.repository.sync_legacy_snapshot(
            user_id,
            session_id,
            messages,
            metadata=metadata,
            extra_message_data=extra_message_data,
        )
        self._invalidate(user_id, session_id)

    def load(self, user_id: str, session_id: str) -> list:
        return self._to_langchain_messages(
            self.get_session_messages(user_id, session_id)
        )

    def load_with_meta(self, user_id: str, session_id: str) -> tuple[list, dict]:
        return self.load(user_id, session_id), self.repository.thread_metadata(
            user_id, session_id
        )

    def current_user_access(self, user_id: str) -> UserAccessSnapshot:
        return self.repository.current_user_access(user_id)

    def list_sessions(self, user_id: str) -> list:
        return [item["session_id"] for item in self.list_session_infos(user_id)]

    def list_session_infos(self, user_id: str) -> list[dict]:
        cached = cache.get_json(self._sessions_cache_key(user_id))
        if cached is not None:
            return cached
        result = self.repository.list_threads(user_id)
        cache.set_json(self._sessions_cache_key(user_id), result)
        return result

    def get_session_messages(
        self,
        user_id: str,
        session_id: str,
        *,
        after: int = 0,
        limit: int = 200,
    ) -> list[dict]:
        return [
            self._serialize(record)
            for record in self.repository.list_messages(
                user_id,
                session_id,
                after=after,
                limit=limit,
            )
        ]

    def delete_session(self, user_id: str, session_id: str) -> bool:
        deleted = self.repository.delete_thread(user_id, session_id)
        if deleted:
            self._invalidate(user_id, session_id)
        return deleted


storage = ConversationStorage()
