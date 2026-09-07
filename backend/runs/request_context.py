from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Optional

from backend.model_control import ModelCatalogSnapshot
from backend.schemas.rag import HitlResumeState, normalize_rag_trace
from backend.web_research.citations import (
    WebSourceLedger,
    WebSourceLedgerCode,
    WebSourceLedgerError,
    WebSourceReference,
)
from backend.web_research.contracts import WebResearchResult

logger = logging.getLogger(__name__)


def _optional_tenant_id(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError("tenant_id must be a string")
    tenant_id = value.strip()
    if not tenant_id:
        raise ValueError("tenant_id must not be empty")
    return tenant_id


@dataclass
class RunRequestContext:
    """Run-owned state shared explicitly across agent tools and RAG nodes."""

    user_id: str
    thread_id: str
    output_queue: Optional[asyncio.Queue] = None
    loop: Optional[asyncio.AbstractEventLoop] = None

    _tenant_id: str | None = field(default=None, repr=False)
    _lock: threading.RLock = field(default_factory=threading.RLock)
    _active: bool = True
    _rag_trace: Optional[dict] = None
    _checkpoint_pause: Optional[dict] = None
    _knowledge_tool_slots_used: int = 0
    _provider_deadline_at: Optional[float] = None
    _provider_cancellation_probe: Optional[Callable[[], bool]] = None
    _model_snapshot: ModelCatalogSnapshot | None = field(default=None, repr=False)
    _rag_retrieval_snapshot: object | None = field(default=None, repr=False)
    _web_source_ledger: WebSourceLedger = field(
        default_factory=WebSourceLedger,
        repr=False,
    )
    _web_fetch_result_budget_limit: int | None = field(default=None, repr=False)
    _web_fetch_result_bytes_claimed: int = field(default=0, repr=False)
    _started_at: float = field(default_factory=time.monotonic)
    _last_step_at: Optional[float] = None

    @classmethod
    def for_stream(
        cls,
        *,
        user_id: str,
        thread_id: str,
        output_queue: asyncio.Queue,
        model_snapshot: ModelCatalogSnapshot | None = None,
        tenant_id: str | None = None,
    ) -> RunRequestContext:
        return cls(
            user_id=user_id,
            thread_id=thread_id,
            output_queue=output_queue,
            loop=asyncio.get_running_loop(),
            _tenant_id=_optional_tenant_id(tenant_id),
            _model_snapshot=model_snapshot,
        )

    @classmethod
    def for_sync(
        cls,
        *,
        user_id: str,
        thread_id: str,
        model_snapshot: ModelCatalogSnapshot | None = None,
        tenant_id: str | None = None,
    ) -> RunRequestContext:
        return cls(
            user_id=user_id,
            thread_id=thread_id,
            _tenant_id=_optional_tenant_id(tenant_id),
            _model_snapshot=model_snapshot,
        )

    @property
    def tenant_id(self) -> str | None:
        """Return the immutable tenant bound when this request was created."""

        return self._tenant_id

    def require_tenant_id(self) -> str:
        tenant_id = self._tenant_id
        if tenant_id is None:
            raise ValueError("tenant_id is required for tenant-scoped operations")
        return tenant_id

    def configure_model_snapshot(self, snapshot: ModelCatalogSnapshot) -> None:
        with self._lock:
            if not self._active:
                raise RuntimeError("request context is closed")
            current = self._model_snapshot
            if current is not None and current.catalog_hash != snapshot.catalog_hash:
                raise ValueError("RunRequestContext model snapshot cannot be rebound")
            self._model_snapshot = snapshot

    def model_catalog_snapshot(self) -> ModelCatalogSnapshot | None:
        with self._lock:
            return self._model_snapshot

    def model_snapshot_payload(self) -> dict | None:
        snapshot = self.model_catalog_snapshot()
        return snapshot.model_dump(mode="json") if snapshot is not None else None

    def get_or_resolve_rag_retrieval_snapshot(
        self,
        resolver: Callable[[], object],
    ) -> object:
        """Resolve the immutable document snapshot once for this request."""

        with self._lock:
            if not self._active:
                raise RuntimeError("request context is closed")
            if self._rag_retrieval_snapshot is None:
                self._rag_retrieval_snapshot = resolver()
            return self._rag_retrieval_snapshot

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

    def mark_web_research_attempted(self) -> None:
        """Record that this Run used a Web Research tool."""

        with self._lock:
            if self._active:
                self._web_source_ledger.mark_attempted()

    def remaining_web_fetch_result_budget(self, limit_bytes: int) -> int:
        """Return the unclaimed Run-local web_fetch ToolResult budget."""

        if isinstance(limit_bytes, bool) or not isinstance(limit_bytes, int):
            raise TypeError("limit_bytes must be an integer")
        if limit_bytes <= 0:
            raise ValueError("limit_bytes must be positive")
        with self._lock:
            if not self._active:
                return 0
            if self._web_fetch_result_budget_limit is None:
                self._web_fetch_result_budget_limit = limit_bytes
            elif self._web_fetch_result_budget_limit != limit_bytes:
                raise ValueError("web_fetch ToolResult budget cannot be rebound")
            return max(limit_bytes - self._web_fetch_result_bytes_claimed, 0)

    def claim_web_fetch_result_budget(
        self,
        requested_bytes: int,
        *,
        limit_bytes: int,
    ) -> int:
        """Atomically claim remaining Run-local web_fetch ToolResult bytes."""

        if isinstance(requested_bytes, bool) or not isinstance(requested_bytes, int):
            raise TypeError("requested_bytes must be an integer")
        if requested_bytes <= 0:
            raise ValueError("requested_bytes must be positive")
        if isinstance(limit_bytes, bool) or not isinstance(limit_bytes, int):
            raise TypeError("limit_bytes must be an integer")
        if limit_bytes <= 0:
            raise ValueError("limit_bytes must be positive")
        with self._lock:
            if not self._active:
                return 0
            if self._web_fetch_result_budget_limit is None:
                self._web_fetch_result_budget_limit = limit_bytes
            elif self._web_fetch_result_budget_limit != limit_bytes:
                raise ValueError("web_fetch ToolResult budget cannot be rebound")
            remaining = max(limit_bytes - self._web_fetch_result_bytes_claimed, 0)
            claimed = min(requested_bytes, remaining)
            self._web_fetch_result_bytes_claimed += claimed
            return claimed

    def record_web_search_result(
        self,
        result: WebResearchResult,
        *,
        query: str,
    ) -> tuple[str, ...]:
        """Assign stable Run-local Source IDs to a successful search result."""

        if not isinstance(result, WebResearchResult):
            raise TypeError("result must be WebResearchResult")
        with self._lock:
            if not self._active:
                return ()
            return self._web_source_ledger.register_search_result(
                result,
                query=query,
            )

    def resolve_web_source(self, source_id: str) -> WebSourceReference | None:
        """Resolve one Source ID from this Run's search results."""

        with self._lock:
            if not self._active:
                return None
            return self._web_source_ledger.resolve(source_id)

    def web_research_requires_source_rendering(self) -> bool:
        """Return whether terminal output may contain Run-local Source IDs."""

        with self._lock:
            return bool(self._active and self._web_source_ledger.status().attempted)

    def web_source_count(self) -> int:
        with self._lock:
            if not self._active:
                return 0
            return self._web_source_ledger.status().source_count

    def render_web_source_citations(self, content: str) -> str:
        """Render known [S1] tokens as links to their registered source URL."""

        with self._lock:
            if not self._active:
                raise WebSourceLedgerError(WebSourceLedgerCode.CONTEXT_CLOSED)
            return self._web_source_ledger.finalize(content).content

    def elapsed_ms(self) -> int:
        with self._lock:
            return max(int((time.monotonic() - self._started_at) * 1000), 0)

    def close(self) -> None:
        with self._lock:
            self._active = False
            self.output_queue = None
            self.loop = None
            self._web_source_ledger.clear()
            self._web_fetch_result_budget_limit = None
            self._web_fetch_result_bytes_claimed = 0
            self._rag_retrieval_snapshot = None
