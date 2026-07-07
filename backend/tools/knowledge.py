from langchain_core.tools import tool

from backend.chat.request_context import ChatRequestContext


def make_search_knowledge_base(ctx: ChatRequestContext):
    @tool("search_knowledge_base")
    def search_knowledge_base(query: str) -> str:
        """Search for information in the knowledge base using hybrid retrieval (dense + sparse vectors)."""
        if not ctx.acquire_knowledge_tool_slot():
            return (
                "TOOL_CALL_LIMIT_REACHED: search_knowledge_base has already been called once in this turn. "
                "Use the existing retrieval result and provide the final answer directly."
            )

        # Delayed import keeps tests and lightweight imports away from RAG/embedding startup.
        from backend.rag.pipeline import run_rag_graph

        rag_result = run_rag_graph(query, ctx)

        docs = rag_result.get("docs", []) if isinstance(rag_result, dict) else []
        rag_trace = rag_result.get("rag_trace", {}) if isinstance(rag_result, dict) else {}
        ctx.store_rag_trace(rag_trace)

        if not docs:
            return "No relevant documents found in the knowledge base."

        formatted = []
        for i, result in enumerate(docs, 1):
            source = result.get("filename", "Unknown")
            page = result.get("page_number", "N/A")
            text = result.get("text", "")
            formatted.append(f"[{i}] {source} (Page {page}):\n{text}")

        return "Retrieved Chunks:\n" + "\n\n---\n\n".join(formatted)

    return search_knowledge_base
