from __future__ import annotations

import asyncio
import logging
import threading
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class ChatRequestContext:
    """Request-owned state shared explicitly across agent tools and RAG nodes."""

    user_id: str
    session_id: str
    output_queue: Optional[asyncio.Queue] = None
    loop: Optional[asyncio.AbstractEventLoop] = None

    _lock: threading.RLock = field(default_factory=threading.RLock)
    _active: bool = True
    _rag_trace: Optional[dict] = None
    _knowledge_tool_slots_used: int = 0

    @classmethod
    def for_stream(
        cls,
        *,
        user_id: str,
        session_id: str,
        output_queue: asyncio.Queue,
    ) -> ChatRequestContext:
        return cls(
            user_id=user_id,
            session_id=session_id,
            output_queue=output_queue,
            loop=asyncio.get_running_loop(),
        )

    @classmethod
    def for_sync(
        cls,
        *,
        user_id: str,
        session_id: str,
    ) -> ChatRequestContext:
        return cls(user_id=user_id, session_id=session_id)

    def emit_rag_step(
        self,
        icon: str,
        label: str,
        detail: str = "",
        *,
        group: Optional[str] = None,
        group_label: Optional[str] = None,
    ) -> None:
        with self._lock:
            if not self._active:
                return
            if self.output_queue is None or self.loop is None:
                return
            queue = self.output_queue
            loop = self.loop

        step = {"icon": icon, "label": label, "detail": detail}
        if group:
            step["group"] = group
        if group_label:
            step["group_label"] = group_label

        try:
            if not loop.is_closed():
                loop.call_soon_threadsafe(
                    queue.put_nowait,
                    {"type": "rag_step", "step": step},
                )
        except Exception:
            logger.exception("Failed to emit RAG step")

    def store_rag_trace(self, rag_trace: dict, hitl_resume_state: Optional[dict] = None) -> None:
        if not rag_trace:
            return
        with self._lock:
            if self._active:
                self._rag_trace = {"rag_trace": rag_trace}
                if hitl_resume_state:
                    self._rag_trace["hitl_resume_state"] = hitl_resume_state

    def take_rag_trace(self) -> Optional[dict]:
        with self._lock:
            context = self._rag_trace
            self._rag_trace = None
            return context

    def peek_rag_trace(self) -> Optional[dict]:
        with self._lock:
            return self._rag_trace

    def reset_knowledge_tool_budget(self) -> None:
        with self._lock:
            self._knowledge_tool_slots_used = 0

    def acquire_knowledge_tool_slot(self) -> bool:
        with self._lock:
            if self._knowledge_tool_slots_used >= 1:
                return False
            self._knowledge_tool_slots_used += 1
            return True

    def close(self) -> None:
        with self._lock:
            self._active = False
            self.output_queue = None
            self.loop = None
