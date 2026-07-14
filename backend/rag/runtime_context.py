from __future__ import annotations

from contextlib import contextmanager
from threading import RLock
from uuid import uuid4

from backend.chat.request_context import ChatRequestContext


_contexts: dict[str, ChatRequestContext] = {}
_lock = RLock()


def register_rag_runtime_context(
    context: ChatRequestContext,
    context_id: str | None = None,
) -> str:
    resolved_id = context_id or f"ragctx_{uuid4().hex}"
    with _lock:
        _contexts[resolved_id] = context
    return resolved_id


def release_rag_runtime_context(context_id: str) -> None:
    with _lock:
        _contexts.pop(context_id, None)


@contextmanager
def bind_rag_runtime_context(
    context: ChatRequestContext,
    context_id: str | None = None,
):
    resolved_id = register_rag_runtime_context(context, context_id)
    try:
        yield resolved_id
    finally:
        release_rag_runtime_context(resolved_id)


def get_rag_runtime_context(context_id: str | None) -> ChatRequestContext | None:
    if not context_id:
        return None
    with _lock:
        return _contexts.get(context_id)
