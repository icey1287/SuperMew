from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import dataclass, field
from collections.abc import Callable
from typing import Optional

from backend.schemas.chat import HitlResumeState, normalize_rag_trace

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
    _checkpoint_pause: Optional[dict] = None
    _knowledge_tool_slots_used: int = 0
    _provider_deadline_at: Optional[float] = None
    _provider_cancellation_probe: Optional[Callable[[], bool]] = None
    _started_at: float = field(default_factory=time.monotonic)
    _last_step_at: Optional[float] = None

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
            now = time.monotonic()
            last_step_at = self._last_step_at or self._started_at
            elapsed_ms = max(int((now - self._started_at) * 1000), 0)
            stage_elapsed_ms = max(int((now - last_step_at) * 1000), 0)
            self._last_step_at = now
            queue = self.output_queue
            loop = self.loop

        step = {
            "icon": icon,
            "label": label,
            "detail": detail,
            "elapsed_ms": elapsed_ms,
            "stage_elapsed_ms": stage_elapsed_ms,
        }
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

    def emit_rag_warning(
        self,
        *,
        code: str,
        stage: str,
        retryable: bool,
        fallback_applied: bool,
        attempts: int | None = None,
    ) -> None:
        """Publish a redacted, operational RAG warning to the Run event pump."""
        with self._lock:
            if not self._active or self.output_queue is None or self.loop is None:
                return
            queue = self.output_queue
            loop = self.loop
        warning = {
            "code": code,
            "stage": stage,
            "retryable": retryable,
            "fallback_applied": fallback_applied,
        }
        if attempts is not None:
            warning["attempts"] = max(int(attempts), 0)
        try:
            if not loop.is_closed():
                loop.call_soon_threadsafe(
                    queue.put_nowait,
                    {"type": "rag_warning", "warning": warning},
                )
        except Exception:
            logger.exception("Failed to emit RAG warning")

    def store_rag_trace(
        self, rag_trace: dict, hitl_resume_state: Optional[dict] = None
    ) -> None:
        current_trace = normalize_rag_trace(rag_trace)
        if not current_trace:
            return
        with self._lock:
            if self._active:
                self._rag_trace = {"rag_trace": current_trace}
                if hitl_resume_state:
                    self._rag_trace["hitl_resume_state"] = (
                        HitlResumeState.model_validate(hitl_resume_state).model_dump()
                    )

    def take_rag_trace(self) -> Optional[dict]:
        with self._lock:
            context = self._rag_trace
            self._rag_trace = None
            return context

    def peek_rag_trace(self) -> Optional[dict]:
        with self._lock:
            return self._rag_trace

    def store_checkpoint_pause(self, pause: dict) -> None:
        with self._lock:
            if self._active:
                self._checkpoint_pause = dict(pause)

    def take_checkpoint_pause(self) -> Optional[dict]:
        with self._lock:
            pause = self._checkpoint_pause
            self._checkpoint_pause = None
            return pause

    def reset_knowledge_tool_budget(self) -> None:
        with self._lock:
            self._knowledge_tool_slots_used = 0

    def acquire_knowledge_tool_slot(self) -> bool:
        with self._lock:
            if self._knowledge_tool_slots_used >= 1:
                return False
            self._knowledge_tool_slots_used += 1
            return True

    def configure_provider_runtime(
        self,
        *,
        deadline_at: Optional[float] = None,
        cancellation_probe: Optional[Callable[[], bool]] = None,
    ) -> None:
        """Bind Run deadline/cancellation to downstream provider calls."""
        with self._lock:
            if deadline_at is not None:
                self._provider_deadline_at = deadline_at
            if cancellation_probe is not None:
                self._provider_cancellation_probe = cancellation_probe

    def provider_runtime(self) -> tuple[Optional[float], Optional[Callable[[], bool]]]:
        with self._lock:
            return self._provider_deadline_at, self._provider_cancellation_probe

    def elapsed_ms(self) -> int:
        with self._lock:
            return max(int((time.monotonic() - self._started_at) * 1000), 0)

    def close(self) -> None:
        with self._lock:
            self._active = False
            self.output_queue = None
            self.loop = None
