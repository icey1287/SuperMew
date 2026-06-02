"""单轮对话内 RAG trace 暂存（工具执行 → 流式结束后写入会话）。"""

from typing import Optional

_LAST_RAG_CONTEXT: Optional[dict] = None


def get_last_rag_context(clear: bool = True) -> Optional[dict]:
    """获取最近一次 RAG 检索上下文，默认读取后清空。"""
    global _LAST_RAG_CONTEXT
    context = _LAST_RAG_CONTEXT
    if clear:
        _LAST_RAG_CONTEXT = None
    return context


def record_rag_context(rag_trace: dict) -> None:
    if rag_trace:
        global _LAST_RAG_CONTEXT
        _LAST_RAG_CONTEXT = {"rag_trace": rag_trace}
