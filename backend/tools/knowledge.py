import json
import re

from langchain_core.tools import tool

from backend.rag.evidence import agent_evidence_character_budget, pack_evidence
from backend.runs.request_context import RunRequestContext


def _render_rag_result(
    ctx: RunRequestContext,
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
    outcome = rag_trace.get("retrieval_outcome")
    coverage_gap_codes = [
        str(code)
        for code in (rag_trace.get("coverage_gap_codes") or [])
        if re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", str(code))
    ][:8]
    coverage_gap_questions = [
        " ".join(str(question).split())[:240]
        for question in (rag_trace.get("coverage_gap_questions") or [])
        if " ".join(str(question).split())
    ][:8]
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

    if not docs and (
        outcome == "INSUFFICIENT_EVIDENCE"
        or status == "insufficient_evidence"
        or route == "insufficient_evidence"
    ):
        return (
            "INSUFFICIENT_EVIDENCE: Retrieval completed only partially because one or "
            "more provider branches failed. Do not claim the knowledge base has no answer."
        )

    if not docs:
        return "No relevant documents found in the knowledge base."

    evidence = pack_evidence(
        docs,
        maximum_characters=agent_evidence_character_budget(),
    )
    budget_notice = ""
    if evidence.omitted_count or evidence.truncated_count:
        budget_notice = (
            "\nEVIDENCE_CONTEXT_NOTICE: Only the shown highest-ranked chunks fit the "
            "answer context. Use only these chunks."
        )
    chunks = "Retrieved Chunks:\n" + evidence.text + budget_notice
    if (
        outcome == "INSUFFICIENT_EVIDENCE"
        or status in {"partial", "insufficient_evidence"}
        or route == "insufficient_evidence"
        or coverage_gap_codes
        or coverage_gap_questions
    ):
        gap_line = (
            "\nCOVERAGE_GAPS: " + ", ".join(coverage_gap_codes)
            if coverage_gap_codes
            else ""
        )
        question_line = (
            "\nCOVERAGE_GAP_QUESTIONS: "
            + json.dumps(coverage_gap_questions, ensure_ascii=False)
            if coverage_gap_questions
            else ""
        )
        return (
            "PARTIAL_EVIDENCE: The chunks below cover only part of the question. "
            "Answer only supported parts and explicitly disclose the missing coverage."
            f"{gap_line}{question_line}\n{chunks}"
        )
    return chunks


def make_checkpointed_search_knowledge_base(
    ctx: RunRequestContext,
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
