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
        hitl_resume_state = (
            rag_result.get("hitl_resume_state")
            if isinstance(rag_result, dict)
            else None
        )
        ctx.store_rag_trace(rag_trace, hitl_resume_state)

        status = rag_trace.get("retrieval_status") if isinstance(rag_trace, dict) else None
        route = rag_trace.get("route") if isinstance(rag_trace, dict) else None
        if status == "needs_clarification" or route == "clarify":
            prompt = rag_trace.get("hitl_prompt") or "I found related knowledge, but need one more detail before answering."
            return f"NEEDS_CLARIFICATION: {prompt}"

        if status == "needs_scope_selection" or route == "scope_select":
            prompt = rag_trace.get("hitl_prompt") or "I found multiple related knowledge-base directions. Ask the user to choose one."
            options = rag_trace.get("hitl_options") or []
            if options:
                prompt = f"{prompt}\nOptions: " + "; ".join(str(item) for item in options)
            return f"NEEDS_SCOPE_SELECTION: {prompt}"

        if status == "no_knowledge" or route == "no_knowledge":
            return "NO_KNOWLEDGE: No reliable relevant documents were found in the knowledge base."

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
