from __future__ import annotations

import asyncio
import logging
import re
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Optional

from backend.schemas.chat import HitlResumeState, normalize_rag_trace
from backend.web_research.citations import (
    WebCitationLedger,
    WebCitationLedgerCode,
    WebCitationLedgerError,
    WebEvidenceKind,
)
from backend.web_research.contracts import WebResearchResult

logger = logging.getLogger(__name__)

_WEB_EVIDENCE_ID = re.compile(r"web_ev_[0-9a-f]{64}")
_MAX_WEB_EVIDENCE_ITEMS = 64
_MAX_WEB_EVIDENCE_URL_BYTES = 16 * 1024


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
    _web_evidence_urls: dict[str, str] = field(default_factory=dict, repr=False)
    _web_citation_ledger: WebCitationLedger = field(
        default_factory=WebCitationLedger,
        repr=False,
    )
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

    def mark_web_research_attempted(self) -> None:
        """Record a Web Tool attempt without retaining its query or arguments."""

        with self._lock:
            if self._active:
                self._web_citation_ledger.mark_attempted()

    def record_web_search_result(self, result: WebResearchResult) -> None:
        """Register search evidence and mint only its Run-local fetch capabilities."""

        if not isinstance(result, WebResearchResult):
            raise TypeError("result must be WebResearchResult")
        capabilities = {
            item.evidence_id: item.canonical_url for item in result.evidence
        }
        with self._lock:
            if not self._active:
                return
            if len(set(self._web_evidence_urls).union(capabilities)) > (
                _MAX_WEB_EVIDENCE_ITEMS
            ):
                raise WebCitationLedgerError(WebCitationLedgerCode.EVIDENCE_LIMIT)
            for evidence_id, canonical_url in capabilities.items():
                existing = self._web_evidence_urls.get(evidence_id)
                if existing is not None and existing != canonical_url:
                    raise ValueError("web evidence identity cannot be rebound")
            self._web_citation_ledger.register_result(
                result,
                kind=WebEvidenceKind.SEARCH_SNIPPET,
            )
            self._web_evidence_urls.update(capabilities)

    def record_web_fetch_result(self, result: WebResearchResult) -> None:
        """Register fetched evidence without minting a new network capability."""

        if not isinstance(result, WebResearchResult):
            raise TypeError("result must be WebResearchResult")
        with self._lock:
            if self._active:
                self._web_citation_ledger.register_result(
                    result,
                    kind=WebEvidenceKind.FETCHED_PAGE,
                )

    def web_research_requires_terminal_validation(self) -> bool:
        """Return whether terminal output must cross the citation policy Seam."""

        with self._lock:
            return bool(self._active and self._web_citation_ledger.status().attempted)

    def web_evidence_count(self) -> int:
        """Return an aggregate-only count; evidence identities remain private."""

        with self._lock:
            if not self._active:
                return 0
            return self._web_citation_ledger.status().evidence_count

    def finalize_web_citations(self, content: str) -> str:
        """Validate Run-local citation tokens and render authorized Markdown URLs."""

        with self._lock:
            if not self._active:
                raise WebCitationLedgerError(WebCitationLedgerCode.CONTEXT_CLOSED)
            return self._web_citation_ledger.finalize(content).content

    def record_web_evidence(self, evidence_urls: Mapping[str, str]) -> None:
        """Bind validated web evidence identities to URLs for this request only."""

        if not isinstance(evidence_urls, Mapping):
            raise TypeError("evidence_urls must be a mapping")
        if len(evidence_urls) > _MAX_WEB_EVIDENCE_ITEMS:
            raise ValueError("too many web evidence items")
        normalized: dict[str, str] = {}
        for evidence_id, canonical_url in evidence_urls.items():
            if not isinstance(evidence_id, str) or not _WEB_EVIDENCE_ID.fullmatch(
                evidence_id
            ):
                raise ValueError("invalid web evidence identity")
            if not isinstance(canonical_url, str):
                raise TypeError("web evidence URL must be a string")
            if (
                not canonical_url.startswith(("http://", "https://"))
                or not canonical_url.isascii()
                or "\x00" in canonical_url
                or len(canonical_url.encode("ascii")) > _MAX_WEB_EVIDENCE_URL_BYTES
            ):
                raise ValueError("invalid web evidence URL")
            normalized[evidence_id] = canonical_url
        with self._lock:
            if not self._active:
                return
            if len(set(self._web_evidence_urls).union(normalized)) > (
                _MAX_WEB_EVIDENCE_ITEMS
            ):
                raise ValueError("too many web evidence items")
            for evidence_id, canonical_url in normalized.items():
                existing = self._web_evidence_urls.get(evidence_id)
                if existing is not None and existing != canonical_url:
                    raise ValueError("web evidence identity cannot be rebound")
            self._web_evidence_urls.update(normalized)

    def resolve_web_evidence(self, evidence_id: str) -> str | None:
        """Resolve a fetch capability issued by web_search in this request."""

        if not isinstance(evidence_id, str) or not _WEB_EVIDENCE_ID.fullmatch(
            evidence_id
        ):
            return None
        with self._lock:
            if not self._active:
                return None
            return self._web_evidence_urls.get(evidence_id)

    def elapsed_ms(self) -> int:
        with self._lock:
            return max(int((time.monotonic() - self._started_at) * 1000), 0)

    def close(self) -> None:
        with self._lock:
            self._active = False
            self.output_queue = None
            self.loop = None
            self._web_evidence_urls.clear()
            self._web_citation_ledger.clear()
