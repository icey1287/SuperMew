from typing import Annotated, Literal, TypedDict, List, Optional
import operator
import os
from langchain.chat_models import init_chat_model
from langgraph.graph import StateGraph, END
from langgraph.types import Send
from pydantic import BaseModel, Field

from backend.chat.request_context import ChatRequestContext
from backend.rag.utils import (
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


GRADE_PROMPT = (
    "You are a grader assessing relevance of a retrieved document to a user question. \n "
    "Here is the retrieved document: \n\n {context} \n\n"
    "Here is the user question: {question} \n"
    "If the document contains keyword(s) or semantic meaning related to the user question, grade it as relevant. \n"
    "Give a binary score 'yes' or 'no' score to indicate whether the document is relevant to the question."
)


class GradeDocuments(BaseModel):
    """Grade documents using a binary score for relevance check."""

    binary_score: str = Field(
        description="Relevance score: 'yes' if relevant, or 'no' if not relevant"
    )


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


def _emit(state: RAGState, icon: str, label: str, detail: str = "") -> None:
    ctx = state["request_context"]
    ctx.emit_rag_step(
        icon,
        label,
        detail,
        group=state.get("rag_step_group"),
    )


def _initial_state(
    question: str,
    ctx: ChatRequestContext,
    *,
    is_sub_agent: bool = False,
    rag_step_group: Optional[str] = None,
) -> dict:
    return {
        "question": question,
        "query": question,
        "context": "",
        "docs": [],
        "route": None,
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
    }


def retrieve_initial(state: RAGState) -> RAGState:
    query = state["question"]
    _emit(state, "🔍", "正在检索知识库...", "初始检索")
    retrieved = retrieve_documents(query, top_k=5)
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
        _emit(state, "⚠️", "无可用片段，跳过评估并强制 step-back 扩展检索")
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


def _route_after_initial(state: RAGState) -> Literal["grade_documents", "rewrite_question"]:
    if not state.get("docs"):
        return "rewrite_question"
    return "grade_documents"


def grade_documents_node(state: RAGState) -> RAGState:
    grader = _get_grader_model()
    _emit(state, "📊", "正在评估文档相关性...")
    if not grader:
        grade_update = {
            "grade_score": "unknown",
            "grade_route": "rewrite_question",
            "rewrite_needed": True,
        }
        rag_trace = state.get("rag_trace", {}) or {}
        rag_trace.update(grade_update)
        return {"route": "rewrite_question", "rag_trace": rag_trace}
    question = state["question"]
    context = state.get("context", "")
    prompt = GRADE_PROMPT.format(question=question, context=context)
    response = grader.with_structured_output(GradeDocuments).invoke(
        [{"role": "user", "content": prompt}]
    )
    score = (response.binary_score or "").strip().lower()
    route = "generate_answer" if score == "yes" else "rewrite_question"
    if route == "generate_answer":
        _emit(state, "✅", "文档相关性评估通过", f"评分: {score}")
    else:
        _emit(state, "⚠️", "文档相关性不足，将重写查询", f"评分: {score}")
    grade_update = {
        "grade_score": score,
        "grade_route": route,
        "rewrite_needed": route == "rewrite_question",
    }
    rag_trace = state.get("rag_trace", {}) or {}
    rag_trace.update(grade_update)
    return {"route": route, "rag_trace": rag_trace}


def rewrite_question_node(state: RAGState) -> RAGState:
    question = state["question"]
    force_step_back = not state.get("docs")
    _emit(state, "✏️", "正在重写查询...")

    if force_step_back:
        strategy = "step_back"
    else:
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

    if not force_step_back and strategy in ("hyde", "complex"):
        _emit(state, "📝", "HyDE 假设性文档生成中...")
        hypothetical_doc = generate_hypothetical_document(question)

    rag_trace = state.get("rag_trace", {}) or {}
    rag_trace.update({
        "rewrite_strategy": strategy,
        "rewrite_query": expanded_query,
        "grade_skipped": force_step_back,
    })

    return {
        "expansion_type": strategy,
        "expanded_query": expanded_query,
        "step_back_question": step_back_question,
        "step_back_answer": step_back_answer,
        "hypothetical_doc": hypothetical_doc,
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
        retrieved_hyde = retrieve_documents(hypothetical_doc, top_k=5)
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
        retrieved_stepback = retrieve_documents(expanded_query, top_k=5)
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

    for i, _sq in enumerate(sub_qs, 1):
        _emit(state, "📌", f"子问题 {i}", "已加入并行检索")

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
        docs = result.get("docs", [])
        all_docs.extend(docs)

    deduped = dedupe_documents(all_docs)
    for idx, item in enumerate(deduped, 1):
        item["rrf_rank"] = idx

    context = _format_docs(deduped)
    _emit(state, "✅", f"合成完成，共 {len(deduped)} 个去重片段")

    # 合并所有子 Agent 的 rag_trace
    sub_traces = []
    for result in sub_results:
        trace = result.get("rag_trace")
        if trace:
            sub_traces.append(trace)

    original_trace = state.get("rag_trace") or {}
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
    }

    return {"docs": deduped, "context": context, "rag_trace": rag_trace}


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
    sub_graph.add_conditional_edges(
        "retrieve_initial",
        _route_after_initial,
        {
            "grade_documents": "grade_documents",
            "rewrite_question": "rewrite_question",
        },
    )
    sub_graph.add_conditional_edges(
        "grade_documents",
        lambda state: state.get("route"),
        {
            "generate_answer": END,
            "rewrite_question": "rewrite_question",
        },
    )
    sub_graph.add_edge("rewrite_question", "retrieve_expanded")
    sub_graph.add_edge("retrieve_expanded", END)
    return sub_graph.compile()


# 子 Agent 子图实例（模块级单例）
_rag_sub_agent_graph = build_rag_sub_agent_graph()


def rag_sub_agent(state: RAGState) -> RAGState:
    """包装子图，将子图结果封装为 sub_results 以便主图通过 reducer 合并。"""
    question = state.get("question", "")
    result = _rag_sub_agent_graph.invoke(state)
    return {
        "sub_results": [{
            "question": question,
            "docs": result.get("docs", []),
            "rag_trace": result.get("rag_trace"),
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
    graph.add_conditional_edges(
        "retrieve_initial",
        _route_after_initial,
        {
            "grade_documents": "grade_documents",
            "rewrite_question": "rewrite_question",
        },
    )
    graph.add_conditional_edges(
        "grade_documents",
        lambda state: state.get("route"),
        {
            "generate_answer": END,
            "rewrite_question": "rewrite_question",
        },
    )
    graph.add_edge("rewrite_question", "retrieve_expanded")
    graph.add_edge("retrieve_expanded", END)

    # 并行子 Agent → 合成
    graph.add_edge("rag_sub_agent", "synthesis")
    graph.add_edge("synthesis", END)

    return graph.compile()


rag_graph = build_rag_graph()


def run_rag_graph(question: str, ctx: ChatRequestContext) -> dict:
    return rag_graph.invoke(_initial_state(question, ctx))
