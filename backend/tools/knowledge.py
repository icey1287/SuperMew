from langchain_core.tools import tool

from backend.chat.request_context import ChatRequestContext


def _render_rag_result(
    ctx: ChatRequestContext,
    rag_result: dict,
    *,
    checkpoint_pause: dict | None = None,
) -> str:
    docs = rag_result.get("docs", [])
    rag_trace = dict(rag_result.get("rag_trace") or {})
    if checkpoint_pause:
        rag_trace.update(
            {
                "retrieval_status": checkpoint_pause.get("retrieval_status"),
                "route": checkpoint_pause.get("route"),
                "hitl_prompt": checkpoint_pause.get("prompt"),
                "hitl_options": checkpoint_pause.get("options") or [],
            }
        )
        ctx.store_checkpoint_pause(checkpoint_pause)
    hitl_resume_state = rag_result.get("hitl_resume_state")
    ctx.store_rag_trace(rag_trace, hitl_resume_state)

    status = rag_trace.get("retrieval_status")
    route = rag_trace.get("route")
    if status == "needs_clarification" or route == "clarify":
        prompt = rag_trace.get("hitl_prompt") or (
            "I found related knowledge, but need one more detail before answering."
        )
        return f"NEEDS_CLARIFICATION: {prompt}"

    if status == "needs_scope_selection" or route == "scope_select":
        prompt = rag_trace.get("hitl_prompt") or (
            "I found multiple related knowledge-base directions. "
            "Ask the user to choose one."
        )
        options = rag_trace.get("hitl_options") or []
        if options:
            prompt = f"{prompt}\nOptions: " + "; ".join(str(item) for item in options)
        return f"NEEDS_SCOPE_SELECTION: {prompt}"

    if status == "no_knowledge" or route == "no_knowledge":
        return (
            "NO_KNOWLEDGE: No reliable relevant documents were found "
            "in the knowledge base."
        )

    if not docs:
        return "No relevant documents found in the knowledge base."

    formatted = []
    for i, result in enumerate(docs, 1):
        source = result.get("filename", "Unknown")
        page = result.get("page_number", "N/A")
        text = result.get("text", "")
        formatted.append(f"[{i}] {source} (Page {page}):\n{text}")

    return "Retrieved Chunks:\n" + "\n\n---\n\n".join(formatted)


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
        return _render_rag_result(ctx, rag_result)

    return search_knowledge_base


def make_checkpointed_search_knowledge_base(
    ctx: ChatRequestContext,
    *,
    run_id: str,
    worker_id: str,
    fencing_token: int,
    runner,
):
    @tool("search_knowledge_base")
    def search_knowledge_base(query: str) -> str:
        """Search the knowledge base with durable Run checkpoint support."""
        if not ctx.acquire_knowledge_tool_slot():
            return (
                "TOOL_CALL_LIMIT_REACHED: search_knowledge_base has already been "
                "called once in this turn. Use the existing retrieval result and "
                "provide the final answer directly."
            )

        outcome = runner.start(
            run_id=run_id,
            question=query,
            context=ctx,
            worker_id=worker_id,
            fencing_token=fencing_token,
        )
        pause = None
        if outcome.pause is not None:
            pause = {
                "run_id": outcome.pause.run_id,
                "checkpoint_id": outcome.pause.checkpoint_id,
                "interrupt_id": outcome.pause.interrupt_id,
                "hitl_token": outcome.pause.hitl_token,
                "prompt": outcome.pause.prompt,
                "options": outcome.pause.options,
                "route": outcome.pause.route,
                "retrieval_status": outcome.pause.retrieval_status,
            }
        return _render_rag_result(ctx, outcome.result, checkpoint_pause=pause)

    return search_knowledge_base
