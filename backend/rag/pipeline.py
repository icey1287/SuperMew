from typing import Annotated, Any, Literal, TypedDict, List, Optional
import operator
import os
from langchain.chat_models import init_chat_model
from langgraph.graph import StateGraph, END
from langgraph.types import Send
from pydantic import BaseModel, Field

from backend.chat.request_context import ChatRequestContext
from backend.rag.utils import (
    RETRIEVAL_TOP_K,
    retrieve_documents,
    step_back_expand,
    generate_hypothetical_document,
    dedupe_documents,
    retrieval_trace_fields,
    merge_retrieval_trace,
)

API_KEY = os.getenv("ARK_API_KEY")
MODEL = os.getenv("MODEL")
BASE_URL = os.getenv("BASE_URL")
GRADE_MODEL = os.getenv("GRADE_MODEL", "gpt-4.1")
FAST_MODEL = os.getenv("FAST_MODEL") or MODEL

_grader_model = None
_router_model = None
_complexity_model = None


def _get_grader_model():
    global _grader_model
    if not API_KEY or not GRADE_MODEL:
        return None
    if _grader_model is None:
        _grader_model = init_chat_model(
            model=GRADE_MODEL,
            model_provider="openai",
            api_key=API_KEY,
            base_url=BASE_URL,
            temperature=0,
            stream_usage=True,
        )
    return _grader_model


def _get_router_model():
    global _router_model
    if not API_KEY or not MODEL:
        return None
    if _router_model is None:
        _router_model = init_chat_model(
            model=MODEL,
            model_provider="openai",
            api_key=API_KEY,
            base_url=BASE_URL,
            temperature=0,
            stream_usage=True,
        )
    return _router_model


def _get_complexity_model():
    """FAST_MODEL 用于问题复杂度分类和子问题分解。"""
    global _complexity_model
    if not API_KEY or not FAST_MODEL:
        return None
    if _complexity_model is None:
        _complexity_model = init_chat_model(
            model=FAST_MODEL,
            model_provider="openai",
            api_key=API_KEY,
            base_url=BASE_URL,
            temperature=0,
            stream_usage=True,
        )
    return _complexity_model


EVIDENCE_GRADE_PROMPT = (
    "你是 RAG 证据评分器。请只根据检索片段判断它们是否足以回答用户问题，"
    "不要补充片段里没有的信息。\n\n"
    "用户问题：\n{question}\n\n"
    "检索片段：\n{context}\n\n"
    "请按以下规则给出结构化结果：\n"
    "- relevance: none 表示主题不相关；weak 表示主题接近但证据弱；strong 表示主题明确相关。\n"
    "- answerability: none 表示不能回答；partial 表示有部分线索但不足以给确定答案；"
    "sufficient 表示片段能直接或组合支撑答案。\n"
    "- ambiguity: missing_slot 表示缺少角色名、版本、文件类型、模块名、产品线等关键条件；"
    "multiple_candidates 表示多个候选方向都可能相关；none 表示无明显歧义。\n"
    "- route 只能选择：answer、rewrite、clarify、scope_select、no_knowledge。\n"
    "  answer: relevance=strong 且 answerability=sufficient。\n"
    "  rewrite: 有相关信号，但像是问法、别名或泛化程度导致证据不足。\n"
    "  clarify: 缺少关键条件，需要用户补充。\n"
    "  scope_select: 多个候选方向都相关，需要用户选择。\n"
    "  no_knowledge: 无召回或主题不相关。\n"
    "- 如果 route 是 clarify 或 scope_select，请给 hitl_prompt；如果能列出选项，请给 hitl_options。"
)


class EvidenceGrade(BaseModel):
    """结构化证据评分：同时判断相关性、可回答性与下一步路由。"""

    relevance: Literal["none", "weak", "strong"] = Field(
        description="检索片段与问题的主题相关性"
    )
    answerability: Literal["none", "partial", "sufficient"] = Field(
        description="检索片段是否足以回答问题"
    )
    ambiguity: Literal["none", "missing_slot", "multiple_candidates"] = Field(
        default="none",
        description="问题是否缺条件或存在多个候选方向"
    )
    route: Literal["answer", "rewrite", "clarify", "scope_select", "no_knowledge"] = Field(
        description="下一步路由"
    )
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    missing_slots: List[str] = Field(default_factory=list)
    hitl_prompt: str = ""
    hitl_options: List[str] = Field(default_factory=list)
    reason: str = ""


class RewriteStrategy(BaseModel):
    """Choose a query expansion strategy."""

    strategy: Literal["step_back", "hyde", "complex"]


class ComplexityResult(BaseModel):
    """问题复杂度分类结果。"""

    complexity: Literal["simple", "complex"] = Field(
        description="问题复杂度：'simple' 为简单问题，'complex' 为复杂问题"
    )
    reason: str = Field(default="", description="分类理由")


class SubQuestions(BaseModel):
    """复杂问题分解后的子问题列表。"""

    sub_questions: List[str] = Field(
        description="2-4 个独立子问题，每个聚焦原问题的一个方面",
        min_length=1,
        max_length=4,
    )


class RAGState(TypedDict):
    question: str
    query: str
    context: str
    docs: List[dict]
    route: Optional[str]
    retrieval_status: Optional[str]
    evidence_relevance: Optional[str]
    evidence_answerability: Optional[str]
    evidence_ambiguity: Optional[str]
    evidence_confidence: Optional[float]
    missing_slots: Optional[List[str]]
    hitl_prompt: Optional[str]
    hitl_options: Optional[List[str]]
    rewrite_count: int
    expansion_type: Optional[str]
    expanded_query: Optional[str]
    step_back_question: Optional[str]
    step_back_answer: Optional[str]
    hypothetical_doc: Optional[str]
    rag_trace: Optional[dict]
    # 复杂度路由新增字段
    complexity: Optional[str]
    complexity_reason: Optional[str]
    sub_questions: Optional[List[str]]
    is_sub_agent: bool
    sub_results: Annotated[List[dict], operator.add]
    request_context: ChatRequestContext
    rag_step_group: Optional[str]
    rag_step_group_label: Optional[str]


def _format_docs(docs: List[dict]) -> str:
    if not docs:
        return ""
    chunks = []
    for i, doc in enumerate(docs, 1):
        source = doc.get("filename", "Unknown")
        page = doc.get("page_number", "N/A")
        text = doc.get("text", "")
        chunks.append(f"[{i}] {source} (Page {page}):\n{text}")
    return "\n\n---\n\n".join(chunks)


def _copy_jsonable_doc(doc: dict) -> dict:
    """Keep resume snapshots small and JSON-safe."""
    allowed = {
        "filename",
        "page_number",
        "text",
        "score",
        "rrf_rank",
        "rerank_score",
        "chunk_id",
        "doc_id",
    }
    return {key: value for key, value in doc.items() if key in allowed}


def _copy_jsonable_docs(docs: List[dict] | None) -> List[dict]:
    return [_copy_jsonable_doc(doc) for doc in (docs or []) if isinstance(doc, dict)]


def _is_hitl_result(result: dict | None) -> bool:
    if not isinstance(result, dict):
        return False
    trace = result.get("rag_trace") or {}
    status = result.get("retrieval_status") or trace.get("retrieval_status")
    route = result.get("route") or trace.get("grade_route")
    return status in ("needs_clarification", "needs_scope_selection") or route in ("clarify", "scope_select")


def _build_hitl_resume_state(result: dict) -> dict:
    trace = result.get("rag_trace") or {}
    candidate_docs = (
        trace.get("hitl_candidate_chunks")
        or trace.get("initial_retrieved_chunks")
        or trace.get("retrieved_chunks")
        or []
    )
    return {
        "version": 1,
        "question": result.get("question") or trace.get("query") or "",
        "query": result.get("query") or trace.get("query") or "",
        "route": result.get("route") or trace.get("grade_route"),
        "retrieval_status": result.get("retrieval_status") or trace.get("retrieval_status"),
        "rewrite_count": int(result.get("rewrite_count") or trace.get("rewrite_count") or 0),
        "complexity": result.get("complexity") or trace.get("complexity"),
        "complexity_reason": result.get("complexity_reason") or trace.get("complexity_reason"),
        "sub_questions": result.get("sub_questions") or trace.get("sub_questions") or [],
        "candidate_docs": _copy_jsonable_docs(candidate_docs),
        "sub_traces": trace.get("sub_traces") or [],
        "rag_trace": {
            key: value
            for key, value in trace.items()
            if key not in {"request_context"}
        },
    }


def _refined_question_for_hitl(resume_state: dict, user_answer: str) -> str:
    question = resume_state.get("question") or resume_state.get("query") or ""
    answer = user_answer.strip()
    if not question:
        return answer
    if answer and answer in question:
        return question
    return f"{answer}：{question}" if answer else question


def _emit(state: RAGState, icon: str, label: str, detail: str = "") -> None:
    ctx = state["request_context"]
    ctx.emit_rag_step(
        icon,
        label,
        detail,
        group=state.get("rag_step_group"),
        group_label=state.get("rag_step_group_label"),
    )


def _initial_state(
    question: str,
    ctx: ChatRequestContext,
    *,
    is_sub_agent: bool = False,
    rag_step_group: Optional[str] = None,
    rag_step_group_label: Optional[str] = None,
) -> dict:
    return {
        "question": question,
        "query": question,
        "context": "",
        "docs": [],
        "route": None,
        "retrieval_status": None,
        "evidence_relevance": None,
        "evidence_answerability": None,
        "evidence_ambiguity": None,
        "evidence_confidence": None,
        "missing_slots": [],
        "hitl_prompt": "",
        "hitl_options": [],
        "rewrite_count": 0,
        "expansion_type": None,
        "expanded_query": None,
        "step_back_question": None,
        "step_back_answer": None,
        "hypothetical_doc": None,
        "rag_trace": None,
        "complexity": None,
        "complexity_reason": None,
        "sub_questions": None,
        "is_sub_agent": is_sub_agent,
        "sub_results": [],
        "request_context": ctx,
        "rag_step_group": rag_step_group,
        "rag_step_group_label": rag_step_group_label,
    }


def retrieve_initial(state: RAGState) -> RAGState:
    query = state["question"]
    _emit(state, "🔍", "正在检索知识库...", "初始检索")
    retrieved = retrieve_documents(query, top_k=RETRIEVAL_TOP_K)
    results = retrieved.get("docs", [])
    retrieve_meta = retrieved.get("meta", {})
    context = _format_docs(results)
    _emit(
        state,
        "🧱",
        "三级分块检索",
        (
            f"叶子层 L{retrieve_meta.get('leaf_retrieve_level', 3)} 召回，"
            f"候选 {retrieve_meta.get('candidate_k', 0)}"
        ),
    )
    _emit(
        state,
        "🧩",
        "Auto-merging 合并",
        (
            f"启用: {bool(retrieve_meta.get('auto_merge_enabled'))}，"
            f"应用: {bool(retrieve_meta.get('auto_merge_applied'))}，"
            f"替换片段: {retrieve_meta.get('auto_merge_replaced_chunks', 0)}"
        ),
    )
    _emit(state, "✅", f"检索完成，找到 {len(results)} 个片段", f"模式: {retrieve_meta.get('retrieval_mode', 'hybrid')}")
    if not results:
        _emit(state, "⚠️", "无可用片段，将进入证据评分短路判断")
    rag_trace = {
        "tool_used": True,
        "tool_name": "search_knowledge_base",
        "query": query,
        "expanded_query": query,
        "retrieved_chunks": results,
        "initial_retrieved_chunks": results,
        "retrieval_stage": "initial",
        **retrieval_trace_fields(retrieve_meta),
    }
    return {
        "query": query,
        "docs": results,
        "context": context,
        "rag_trace": rag_trace,
    }


def _route_after_initial(state: RAGState) -> Literal["grade_documents"]:
    return "grade_documents"


def _route_after_grade(state: RAGState) -> Literal["rewrite_question", "end"]:
    if state.get("route") == "rewrite":
        return "rewrite_question"
    return "end"


def _retrieval_status_for_route(route: str, grade: EvidenceGrade) -> str:
    if route == "answer":
        if grade.answerability == "partial":
            return "partial"
        return "answerable"
    if route == "rewrite":
        return "needs_rewrite"
    if route == "clarify":
        return "needs_clarification"
    if route == "scope_select":
        return "needs_scope_selection"
    return "no_knowledge"


def _default_hitl_prompt(route: str, grade: EvidenceGrade) -> str:
    if grade.hitl_prompt:
        return grade.hitl_prompt
    if route == "scope_select":
        return "我在知识库中找到了多个可能相关的方向。你想问的是哪一个？"
    if grade.missing_slots:
        return "我找到了相关内容，但还缺少关键信息：" + "、".join(grade.missing_slots)
    return "我找到了相关内容，但证据不足以确定答案。请补充一下你具体想问的条件。"


def _grade_for_no_docs() -> EvidenceGrade:
    return EvidenceGrade(
        relevance="none",
        answerability="none",
        ambiguity="none",
        route="no_knowledge",
        confidence=1.0,
        reason="no_retrieved_documents",
    )


def _fallback_grade_for_docs() -> EvidenceGrade:
    return EvidenceGrade(
        relevance="strong",
        answerability="sufficient",
        ambiguity="none",
        route="answer",
        confidence=0.5,
        reason="grader_unavailable",
    )


def _normalize_grade_route(grade: EvidenceGrade, state: RAGState) -> str:
    docs = state.get("docs") or []
    rewrite_count = int(state.get("rewrite_count") or 0)
    is_sub_agent = bool(state.get("is_sub_agent"))
    route = grade.route

    if not docs or grade.relevance == "none":
        return "no_knowledge"

    if grade.ambiguity == "missing_slot":
        return "clarify"
    if grade.ambiguity == "multiple_candidates":
        return "scope_select"

    answer_is_supported = grade.relevance == "strong" and grade.answerability == "sufficient"
    if route == "answer" and answer_is_supported:
        return "answer"

    # 子问题不做二次纠错。partial 证据交给 synthesis 合并，完全不可回答则停止。
    if is_sub_agent:
        if grade.answerability in ("partial", "sufficient"):
            return "answer"
        return "no_knowledge"

    if route == "rewrite" and rewrite_count < 1:
        return "rewrite"

    if route == "rewrite" and rewrite_count >= 1:
        if grade.answerability == "partial":
            return "clarify"
        return "no_knowledge"

    if grade.answerability == "partial":
        if rewrite_count < 1:
            return "rewrite"
        return "clarify"

    if answer_is_supported:
        return "answer"

    return "no_knowledge"


def _grade_update(grade: EvidenceGrade, route: str) -> dict:
    status = _retrieval_status_for_route(route, grade)
    hitl_prompt = _default_hitl_prompt(route, grade) if route in ("clarify", "scope_select") else ""
    return {
        "retrieval_status": status,
        "evidence_relevance": grade.relevance,
        "evidence_answerability": grade.answerability,
        "evidence_ambiguity": grade.ambiguity,
        "evidence_confidence": grade.confidence,
        "evidence_reason": grade.reason,
        "missing_slots": grade.missing_slots,
        "hitl_prompt": hitl_prompt,
        "hitl_options": grade.hitl_options,
        # Keep legacy trace fields for existing UI/history compatibility.
        "grade_score": f"{grade.relevance}/{grade.answerability}",
        "grade_route": route,
        "rewrite_needed": route == "rewrite",
    }


def grade_documents_node(state: RAGState) -> RAGState:
    _emit(state, "📊", "正在评估证据质量...")
    docs = state.get("docs") or []
    if not docs:
        grade = _grade_for_no_docs()
    else:
        grader = _get_grader_model()
        if not grader:
            grade = _fallback_grade_for_docs()
        else:
            question = state["question"]
            context = state.get("context", "")
            prompt = EVIDENCE_GRADE_PROMPT.format(question=question, context=context)
            try:
                grade = grader.with_structured_output(EvidenceGrade).invoke(
                    [{"role": "user", "content": prompt}]
                )
            except Exception:
                grade = _fallback_grade_for_docs()

    route = _normalize_grade_route(grade, state)
    grade_update = _grade_update(grade, route)
    rag_trace = state.get("rag_trace", {}) or {}
    rag_trace.update(grade_update)

    if route == "answer":
        if grade.answerability == "partial":
            _emit(state, "🟡", "保留部分相关证据", f"置信度: {grade.confidence:.2f}")
        else:
            _emit(state, "✅", "证据足够，返回检索片段", f"置信度: {grade.confidence:.2f}")
    elif route == "rewrite":
        _emit(state, "⚠️", "证据不足，将改写查询一次", f"置信度: {grade.confidence:.2f}")
    elif route in ("clarify", "scope_select"):
        _emit(state, "❓", "需要用户补充信息", grade_update["hitl_prompt"])
    else:
        _emit(state, "⛔", "知识库中未找到可用证据", grade.reason or "no_knowledge")

    update = {
        "route": route,
        "retrieval_status": grade_update["retrieval_status"],
        "evidence_relevance": grade.relevance,
        "evidence_answerability": grade.answerability,
        "evidence_ambiguity": grade.ambiguity,
        "evidence_confidence": grade.confidence,
        "missing_slots": grade.missing_slots,
        "hitl_prompt": grade_update["hitl_prompt"],
        "hitl_options": grade.hitl_options,
        "rag_trace": rag_trace,
    }

    if route in ("no_knowledge", "clarify", "scope_select"):
        if route in ("clarify", "scope_select") and docs:
            rag_trace["hitl_candidate_chunks"] = _copy_jsonable_docs(docs)
        rag_trace["retrieved_chunks"] = []
        update.update({"docs": [], "context": ""})

    return update


def rewrite_question_node(state: RAGState) -> RAGState:
    question = state["question"]
    _emit(state, "✏️", "正在重写查询...")

    rewrite_count = int(state.get("rewrite_count") or 0)
    if rewrite_count >= 1:
        rag_trace = state.get("rag_trace", {}) or {}
        rag_trace.update({
            "retrieval_status": "no_knowledge",
            "grade_route": "no_knowledge",
            "rewrite_needed": False,
            "evidence_reason": "rewrite_budget_exhausted",
        })
        _emit(state, "⛔", "改写预算已用完，停止检索")
        return {
            "route": "no_knowledge",
            "retrieval_status": "no_knowledge",
            "docs": [],
            "context": "",
            "rag_trace": rag_trace,
        }

    router = _get_router_model()
    strategy = "step_back"
    if router:
        prompt = (
            "请根据用户问题选择最合适的查询扩展策略，仅输出策略名。\n"
            "- step_back：包含具体名称、日期、代码等细节，需要先理解通用概念的问题。\n"
            "- hyde：模糊、概念性、需要解释或定义的问题。\n"
            "- complex：多步骤、需要分解或综合多种信息的复杂问题。\n"
            f"用户问题：{question}"
        )
        try:
            decision = router.with_structured_output(RewriteStrategy).invoke(
                [{"role": "user", "content": prompt}]
            )
            strategy = decision.strategy
        except Exception:
            strategy = "step_back"

    expanded_query = question
    step_back_question = ""
    step_back_answer = ""
    hypothetical_doc = ""

    if strategy in ("step_back", "complex"):
        _emit(state, "🧠", f"使用策略: {strategy}", "生成退步问题")
        step_back = step_back_expand(question)
        step_back_question = step_back.get("step_back_question", "")
        step_back_answer = step_back.get("step_back_answer", "")
        expanded_query = step_back.get("expanded_query", question)

    if strategy in ("hyde", "complex"):
        _emit(state, "📝", "HyDE 假设性文档生成中...")
        hypothetical_doc = generate_hypothetical_document(question)

    rag_trace = state.get("rag_trace", {}) or {}
    rag_trace.update({
        "rewrite_strategy": strategy,
        "rewrite_query": expanded_query,
        "grade_skipped": False,
        "rewrite_count": rewrite_count + 1,
    })

    return {
        "expansion_type": strategy,
        "expanded_query": expanded_query,
        "step_back_question": step_back_question,
        "step_back_answer": step_back_answer,
        "hypothetical_doc": hypothetical_doc,
        "rewrite_count": rewrite_count + 1,
        "rag_trace": rag_trace,
    }


def retrieve_expanded(state: RAGState) -> RAGState:
    strategy = state.get("expansion_type") or "step_back"
    _emit(state, "🔄", "使用扩展查询重新检索...", f"策略: {strategy}")
    results: List[dict] = []
    rerank_errors = []
    retrieval_trace: dict = {}

    if strategy in ("hyde", "complex"):
        hypothetical_doc = state.get("hypothetical_doc") or generate_hypothetical_document(state["question"])
        retrieved_hyde = retrieve_documents(hypothetical_doc, top_k=RETRIEVAL_TOP_K)
        results.extend(retrieved_hyde.get("docs", []))
        hyde_meta = retrieved_hyde.get("meta", {})
        _emit(
            state,
            "🧱",
            "HyDE 三级检索",
            (
                f"L{hyde_meta.get('leaf_retrieve_level', 3)} 召回，"
                f"候选 {hyde_meta.get('candidate_k', 0)}，"
                f"合并替换 {hyde_meta.get('auto_merge_replaced_chunks', 0)}"
            ),
        )
        if hyde_meta.get("rerank_error"):
            rerank_errors.append(f"hyde:{hyde_meta.get('rerank_error')}")
        retrieval_trace = merge_retrieval_trace(retrieval_trace, hyde_meta)

    if strategy in ("step_back", "complex"):
        expanded_query = state.get("expanded_query") or state["question"]
        retrieved_stepback = retrieve_documents(expanded_query, top_k=RETRIEVAL_TOP_K)
        results.extend(retrieved_stepback.get("docs", []))
        step_meta = retrieved_stepback.get("meta", {})
        _emit(
            state,
            "🧱",
            "Step-back 三级检索",
            (
                f"L{step_meta.get('leaf_retrieve_level', 3)} 召回，"
                f"候选 {step_meta.get('candidate_k', 0)}，"
                f"合并替换 {step_meta.get('auto_merge_replaced_chunks', 0)}"
            ),
        )
        if step_meta.get("rerank_error"):
            rerank_errors.append(f"step_back:{step_meta.get('rerank_error')}")
        retrieval_trace = merge_retrieval_trace(retrieval_trace, step_meta)

    deduped = dedupe_documents(results)

    # 扩展阶段可能合并了多路召回（如 hyde + step_back），
    # 这里统一重排展示名次，避免出现 1,2,3,4,5,4,5 这类重复名次。
    for idx, item in enumerate(deduped, 1):
        item["rrf_rank"] = idx

    context = _format_docs(deduped)
    _emit(state, "✅", f"扩展检索完成，共 {len(deduped)} 个片段")
    rag_trace = state.get("rag_trace", {}) or {}
    rag_trace.update({
        "expanded_query": state.get("expanded_query") or state["question"],
        "step_back_question": state.get("step_back_question", ""),
        "step_back_answer": state.get("step_back_answer", ""),
        "hypothetical_doc": state.get("hypothetical_doc", ""),
        "expansion_type": strategy,
        "retrieved_chunks": deduped,
        "expanded_retrieved_chunks": deduped,
        "retrieval_stage": "expanded",
        "rerank_error": "; ".join(rerank_errors) if rerank_errors else retrieval_trace.get("rerank_error"),
        **retrieval_trace,
    })
    return {"docs": deduped, "context": context, "rag_trace": rag_trace}


# ---------------------------------------------------------------------------
# 复杂度分类 & 子问题分解
# ---------------------------------------------------------------------------

COMPLEXITY_PROMPT = (
    "你是一个问题复杂度分类器。请判断用户问题的复杂度。\n\n"
    "【简单问题】：事实查询、定义查询、单一信息点查询、明确的 yes/no 问题、"
    "某个具体属性/参数/规格的查询。\n"
    "【复杂问题】：需要跨文档综合、多角度分析、比较对比、多步骤推理、"
    "需要综合多个信息源才能完整回答的问题。\n\n"
    "用户问题：{question}\n\n"
    "请输出分类结果。"
)

DECOMPOSE_PROMPT = (
    "请将以下复杂问题分解为 2-4 个独立的子问题。\n"
    "每个子问题应聚焦于原问题的一个明确方面，能独立通过知识库检索获得答案。\n"
    "子问题之间应覆盖原问题的核心维度，避免重叠。\n\n"
    "原问题：{question}\n\n"
    "请输出子问题列表。"
)


def classify_complexity(state: RAGState) -> RAGState:
    """使用 FAST_MODEL 判断问题复杂度。"""
    question = state["question"]
    _emit(state, "🧭", "正在分析问题复杂度...")

    model = _get_complexity_model()
    if not model:
        _emit(state, "⚠️", "复杂度模型不可用，默认简单问题")
        return {"complexity": "simple", "complexity_reason": "model_unavailable"}

    prompt = COMPLEXITY_PROMPT.format(question=question)
    try:
        result = model.with_structured_output(ComplexityResult).invoke(
            [{"role": "user", "content": prompt}]
        )
        complexity = (result.complexity or "simple").strip().lower()
        reason = (result.reason or "").strip()
        if complexity not in ("simple", "complex"):
            complexity = "simple"
    except Exception:
        complexity = "simple"
        reason = "classification_error"

    if complexity == "simple":
        _emit(state, "✅", "简单问题 → 走标准 RAG 流程", f"理由: {reason[:60]}")
    else:
        _emit(state, "🔀", "复杂问题 → 将分解为子问题并行检索", f"理由: {reason[:60]}")

    return {"complexity": complexity, "complexity_reason": reason}


def decompose_question(state: RAGState) -> RAGState:
    """将复杂问题分解为 2-4 个独立子问题。"""
    question = state["question"]
    _emit(state, "🧩", "正在分解复杂问题...")

    model = _get_complexity_model()
    if not model:
        _emit(state, "⚠️", "分解模型不可用，使用原始问题")
        return {"sub_questions": [question]}

    prompt = DECOMPOSE_PROMPT.format(question=question)
    try:
        result = model.with_structured_output(SubQuestions).invoke(
            [{"role": "user", "content": prompt}]
        )
        sub_qs = [sq.strip() for sq in (result.sub_questions or []) if sq.strip()]
        if not sub_qs:
            sub_qs = [question]
    except Exception:
        sub_qs = [question]

    for i, sq in enumerate(sub_qs, 1):
        _emit(state, "📌", f"子问题 {i}", f"{sq[:80]} 已加入并行检索")

    return {"sub_questions": sub_qs}


def _route_after_complexity(state: RAGState):
    """复杂度路由：simple 走原有流程，complex 先分解再并行检索。"""
    if state.get("complexity") == "complex":
        return "decompose_question"
    return "retrieve_initial"


def _fanout_sub_questions(state: RAGState):
    """将分解后的子问题通过 Send API 并行分发到 rag_sub_agent 子图。"""
    sub_qs = state.get("sub_questions") or []
    ctx = state["request_context"]
    if not sub_qs:
        # 分解失败，回退到原有流程
        return [Send("retrieve_initial", _initial_state(state["question"], ctx))]
    return [
        Send(
            "rag_sub_agent",
            _initial_state(
                sq,
                ctx,
                is_sub_agent=True,
                rag_step_group=f"子问题 {i}",
                rag_step_group_label=sq,
            ),
        )
        for i, sq in enumerate(sub_qs, 1)
    ]


def synthesis(state: RAGState) -> RAGState:
    """合并所有子 Agent 检索到的文档，去重排序后输出最终上下文。"""
    sub_results = state.get("sub_results", [])
    _emit(state, "🔬", f"正在合成 {len(sub_results)} 个子问题的检索结果...")

    all_docs: List[dict] = []
    for result in sub_results:
        status = result.get("retrieval_status")
        if status not in ("answerable", "partial"):
            continue
        docs = result.get("docs", [])
        all_docs.extend(docs)

    deduped = dedupe_documents(all_docs)
    for idx, item in enumerate(deduped, 1):
        item["rrf_rank"] = idx

    context = _format_docs(deduped)
    if deduped:
        _emit(state, "✅", f"合成完成，共 {len(deduped)} 个去重片段")
    else:
        _emit(state, "⛔", "所有子问题都没有可用证据")

    # 合并所有子 Agent 的 rag_trace
    sub_traces = []
    for result in sub_results:
        trace = result.get("rag_trace")
        if trace:
            sub_traces.append(trace)

    original_trace = state.get("rag_trace") or {}
    has_docs = bool(deduped)
    retrieval_status = "answerable" if has_docs else "no_knowledge"
    if has_docs and any(result.get("retrieval_status") == "partial" for result in sub_results):
        retrieval_status = "partial"
    hitl_traces = [
        trace for trace in sub_traces
        if trace.get("retrieval_status") in ("needs_clarification", "needs_scope_selection")
    ]
    hitl_route = None
    hitl_prompt = ""
    hitl_options: List[str] = []
    if not has_docs and hitl_traces:
        scope_trace = next(
            (trace for trace in hitl_traces if trace.get("retrieval_status") == "needs_scope_selection"),
            None,
        )
        chosen_trace = scope_trace or hitl_traces[0]
        retrieval_status = chosen_trace.get("retrieval_status") or "needs_clarification"
        hitl_route = "scope_select" if retrieval_status == "needs_scope_selection" else "clarify"
        prompts = [
            trace.get("hitl_prompt")
            for trace in hitl_traces
            if trace.get("hitl_prompt")
        ]
        hitl_prompt = "；".join(dict.fromkeys(prompts))
        for trace in hitl_traces:
            for option in trace.get("hitl_options") or []:
                if option not in hitl_options:
                    hitl_options.append(option)

    hitl_candidate_chunks: List[dict] = []
    if not has_docs and hitl_traces:
        for trace in hitl_traces:
            hitl_candidate_chunks.extend(trace.get("hitl_candidate_chunks") or [])

    rag_trace = {
        **original_trace,
        "tool_used": True,
        "tool_name": "search_knowledge_base",
        "query": state["question"],
        "expanded_query": state["question"],
        "retrieved_chunks": deduped,
        "retrieval_stage": "synthesis",
        "complexity": "complex",
        "complexity_reason": state.get("complexity_reason", ""),
        "sub_questions": state.get("sub_questions", []),
        "sub_agent_count": len(sub_results),
        "synthesis_merged_count": len(all_docs),
        "sub_traces": sub_traces,
        "retrieval_status": retrieval_status,
        "evidence_relevance": "strong" if has_docs else "none",
        "evidence_answerability": "partial" if retrieval_status == "partial" else ("sufficient" if has_docs else "none"),
        "evidence_confidence": None,
        "grade_route": "answer" if has_docs else (hitl_route or "no_knowledge"),
        "rewrite_needed": False,
        "hitl_prompt": hitl_prompt,
        "hitl_options": hitl_options,
        "hitl_candidate_chunks": _copy_jsonable_docs(hitl_candidate_chunks),
    }

    return {
        "docs": deduped,
        "context": context,
        "route": "answer" if has_docs else (hitl_route or "no_knowledge"),
        "retrieval_status": retrieval_status,
        "hitl_prompt": hitl_prompt,
        "hitl_options": hitl_options,
        "rag_trace": rag_trace,
    }


# ---------------------------------------------------------------------------
# 子 Agent RAG 子图（每个子问题独立运行完整 RAG 流程）
# ---------------------------------------------------------------------------

def build_rag_sub_agent_graph():
    """构建子 Agent RAG 子图：retrieve → grade → rewrite → retrieve_expanded。"""
    sub_graph = StateGraph(RAGState)
    sub_graph.add_node("retrieve_initial", retrieve_initial)
    sub_graph.add_node("grade_documents", grade_documents_node)
    sub_graph.add_node("rewrite_question", rewrite_question_node)
    sub_graph.add_node("retrieve_expanded", retrieve_expanded)

    sub_graph.set_entry_point("retrieve_initial")
    sub_graph.add_edge("retrieve_initial", "grade_documents")
    sub_graph.add_conditional_edges(
        "grade_documents",
        _route_after_grade,
        {
            "rewrite_question": "rewrite_question",
            "end": END,
        },
    )
    sub_graph.add_edge("rewrite_question", "retrieve_expanded")
    sub_graph.add_edge("retrieve_expanded", "grade_documents")
    return sub_graph.compile()


# 子 Agent 子图实例（模块级单例）
_rag_sub_agent_graph = build_rag_sub_agent_graph()


def rag_sub_agent(state: RAGState) -> RAGState:
    """包装子图，将子图结果封装为 sub_results 以便主图通过 reducer 合并。"""
    question = state.get("question", "")
    result = _rag_sub_agent_graph.invoke(state)
    trace = result.get("rag_trace") or {}
    return {
        "sub_results": [{
            "question": question,
            "docs": result.get("docs", []),
            "retrieval_status": result.get("retrieval_status") or trace.get("retrieval_status"),
            "route": result.get("route") or trace.get("grade_route"),
            "rag_trace": trace,
        }],
    }


# ---------------------------------------------------------------------------
# 主 RAG 图
# ---------------------------------------------------------------------------

def build_rag_graph():
    graph = StateGraph(RAGState)

    # 节点注册
    graph.add_node("classify_complexity", classify_complexity)
    graph.add_node("decompose_question", decompose_question)
    graph.add_node("retrieve_initial", retrieve_initial)
    graph.add_node("grade_documents", grade_documents_node)
    graph.add_node("rewrite_question", rewrite_question_node)
    graph.add_node("retrieve_expanded", retrieve_expanded)
    graph.add_node("rag_sub_agent", rag_sub_agent)
    graph.add_node("synthesis", synthesis)

    # 入口：复杂度分类
    graph.set_entry_point("classify_complexity")

    # 复杂度路由：simple → 原有流程，complex → decompose_question
    graph.add_conditional_edges(
        "classify_complexity",
        _route_after_complexity,
        {
            "retrieve_initial": "retrieve_initial",
            "decompose_question": "decompose_question",
        },
    )

    # 分解节点 → 通过 Send API 并行分发到 rag_sub_agent
    graph.add_conditional_edges("decompose_question", _fanout_sub_questions)

    # 原有简单路径
    graph.add_edge("retrieve_initial", "grade_documents")
    graph.add_conditional_edges(
        "grade_documents",
        _route_after_grade,
        {
            "rewrite_question": "rewrite_question",
            "end": END,
        },
    )
    graph.add_edge("rewrite_question", "retrieve_expanded")
    graph.add_edge("retrieve_expanded", "grade_documents")

    # 并行子 Agent → 合成
    graph.add_edge("rag_sub_agent", "synthesis")
    graph.add_edge("synthesis", END)

    return graph.compile()


rag_graph = build_rag_graph()


def _state_from_resume(
    resume_state: dict,
    user_answer: str,
    ctx: ChatRequestContext,
) -> dict:
    refined_question = _refined_question_for_hitl(resume_state, user_answer)
    rag_trace = dict(resume_state.get("rag_trace") or {})
    rag_trace.update({
        "query": refined_question,
        "expanded_query": refined_question,
        "hitl_resumed": True,
        "hitl_answer": user_answer,
        "hitl_resume_from_status": resume_state.get("retrieval_status"),
        "hitl_resume_from_route": resume_state.get("route"),
    })
    state = _initial_state(refined_question, ctx)
    state.update({
        "query": refined_question,
        "rewrite_count": int(resume_state.get("rewrite_count") or 0),
        "complexity": resume_state.get("complexity"),
        "complexity_reason": resume_state.get("complexity_reason"),
        "sub_questions": resume_state.get("sub_questions") or [],
        "rag_trace": rag_trace,
    })
    return state


def _retrieve_resume_query(state: dict) -> dict:
    _emit(state, "🔎", "使用 HITL 补充进行针对性检索", "跳过复杂度判断与子问题分解")
    query = state["question"]
    retrieved = retrieve_documents(query, top_k=RETRIEVAL_TOP_K)
    results = retrieved.get("docs", [])
    retrieve_meta = retrieved.get("meta", {})
    context = _format_docs(results)
    _emit(
        state,
        "🧱",
        "HITL 三级分块检索",
        (
            f"叶子层 L{retrieve_meta.get('leaf_retrieve_level', 3)} 召回，"
            f"候选 {retrieve_meta.get('candidate_k', 0)}"
        ),
    )
    _emit(
        state,
        "🧩",
        "Auto-merging 合并",
        (
            f"启用: {bool(retrieve_meta.get('auto_merge_enabled'))}，"
            f"应用: {bool(retrieve_meta.get('auto_merge_applied'))}，"
            f"替换片段: {retrieve_meta.get('auto_merge_replaced_chunks', 0)}"
        ),
    )
    _emit(state, "✅", f"HITL 针对性检索完成，找到 {len(results)} 个片段", f"模式: {retrieve_meta.get('retrieval_mode', 'hybrid')}")
    rag_trace = state.get("rag_trace") or {}
    rag_trace.update({
        "tool_used": True,
        "tool_name": "search_knowledge_base",
        "query": query,
        "expanded_query": query,
        "retrieved_chunks": results,
        "hitl_targeted_retrieved_chunks": results,
        "hitl_resumed": True,
        "hitl_resume_strategy": "targeted_retrieval",
        "retrieval_stage": "hitl_targeted_retrieval",
        **retrieval_trace_fields(retrieve_meta),
    })
    state.update({
        "query": query,
        "docs": results,
        "context": context,
        "rag_trace": rag_trace,
    })
    state.update(grade_documents_node(state))
    return state


def resume_rag_from_hitl(
    resume_state: dict,
    user_answer: str,
    ctx: ChatRequestContext,
) -> dict:
    """Resume a paused RAG run from the HITL breakpoint without re-entering the main graph."""
    state = _state_from_resume(resume_state, user_answer, ctx)
    _emit(state, "▶️", "收到 HITL 补充，继续原 RAG 流程", user_answer)

    state = _retrieve_resume_query(state)
    if _is_hitl_result(state):
        state["hitl_resume_state"] = _build_hitl_resume_state(state)
    return state


def run_rag_graph(question: str, ctx: ChatRequestContext) -> dict:
    result = rag_graph.invoke(_initial_state(question, ctx))
    if _is_hitl_result(result):
        result["hitl_resume_state"] = _build_hitl_resume_state(result)
    return result
