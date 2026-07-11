from typing import List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


class StrictSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ChatRequest(StrictSchema):
    message: str
    session_id: Optional[str] = "default_session"


class RetrievedChunk(StrictSchema):
    filename: str
    page_number: Optional[str | int] = None
    text: Optional[str] = None
    score: Optional[float] = None
    rrf_rank: Optional[int] = None
    rerank_score: Optional[float] = None


class RagTraceFields(StrictSchema):
    tool_used: Optional[bool] = None
    tool_name: Optional[str] = None
    query: Optional[str] = None
    rewrite_method: Optional[Literal["step_back", "hyde"]] = None
    rewritten_query: Optional[str] = None
    step_back_question: Optional[str] = None
    hyde_document: Optional[str] = None
    retrieval_stage: Optional[str] = None
    route: Optional[str] = None
    retrieval_status: Optional[str] = None
    evidence_relevance: Optional[str] = None
    evidence_answerability: Optional[str] = None
    evidence_ambiguity: Optional[str] = None
    evidence_confidence: Optional[float] = None
    evidence_reason: Optional[str] = None
    missing_slots: Optional[List[str]] = None
    hitl_prompt: Optional[str] = None
    hitl_options: Optional[List[str]] = None
    hitl_resumed: Optional[bool] = None
    hitl_answer: Optional[str] = None
    hitl_resume_strategy: Optional[str] = None
    hitl_resume_from_status: Optional[str] = None
    hitl_resume_from_route: Optional[str] = None
    hitl_targeted_retrieved_chunks: Optional[List[RetrievedChunk]] = None
    rerank_enabled: Optional[bool] = None
    rerank_applied: Optional[bool] = None
    rerank_model: Optional[str] = None
    rerank_endpoint: Optional[str] = None
    rerank_error: Optional[str] = None
    rerank_timeout_seconds: Optional[float] = None
    rerank_min_score: Optional[float] = None
    post_rerank_count: Optional[int] = None
    post_threshold_count: Optional[int] = None
    retrieval_empty: Optional[bool] = None
    retrieval_mode: Optional[str] = None
    retrieval_pipeline: Optional[str] = None
    candidate_k: Optional[int] = None
    candidate_k_source: Optional[str] = None
    candidate_k_config_error: Optional[str] = None
    retrieval_candidate_multiplier: Optional[int] = None
    retrieval_top_k: Optional[int] = None
    recall_count: Optional[int] = None
    post_merge_candidate_count: Optional[int] = None
    candidate_count: Optional[int] = None
    leaf_retrieve_level: Optional[int] = None
    auto_merge_enabled: Optional[bool] = None
    auto_merge_applied: Optional[bool] = None
    auto_merge_threshold: Optional[int] = None
    auto_merge_replaced_chunks: Optional[int] = None
    auto_merge_steps: Optional[int] = None
    retrieved_chunks: Optional[List[RetrievedChunk]] = None
    initial_retrieved_chunks: Optional[List[RetrievedChunk]] = None
    rewrite_retrieved_chunks: Optional[List[RetrievedChunk]] = None
    # 复杂度路由新增字段
    complexity: Optional[str] = None
    complexity_reason: Optional[str] = None
    sub_questions: Optional[List[str]] = None
    sub_agent_count: Optional[int] = None
    synthesis_merged_count: Optional[int] = None


class RagSubTrace(RagTraceFields):
    pass


class RagTrace(RagTraceFields):
    sub_traces: Optional[List[RagSubTrace]] = None


class HitlResumeState(StrictSchema):
    question: str = Field(min_length=1)
    route: Literal["clarify", "scope_select"]
    retrieval_status: Literal["needs_clarification", "needs_scope_selection"]
    rewrite_count: int = Field(default=0, ge=0)
    complexity: Optional[Literal["simple", "complex"]] = None
    complexity_reason: Optional[str] = None
    sub_questions: List[str] = Field(default_factory=list, max_length=4)


class PendingHitlState(StrictSchema):
    id: str = Field(min_length=1)
    original_question: str = Field(min_length=1)
    prompt: str = Field(min_length=1)
    options: List[str] = Field(default_factory=list)
    route: Literal["clarify", "scope_select"]
    retrieval_status: Literal["needs_clarification", "needs_scope_selection"]
    answers: List[str] = Field(default_factory=list)
    resume_state: HitlResumeState
    created_at: str


def _normalize_chunks(value) -> list[dict]:
    if not isinstance(value, list):
        return []
    fields = RetrievedChunk.model_fields
    return [
        RetrievedChunk.model_validate({key: item[key] for key in fields if key in item}).model_dump(
            exclude_none=True
        )
        for item in value
        if isinstance(item, dict) and item.get("filename")
    ]


def _normalize_trace_fields(trace: dict, fields: dict) -> dict:
    normalized = {key: trace[key] for key in fields if key in trace}
    for key in (
        "retrieved_chunks",
        "initial_retrieved_chunks",
        "rewrite_retrieved_chunks",
        "hitl_targeted_retrieved_chunks",
    ):
        if key in normalized:
            normalized[key] = _normalize_chunks(normalized[key])
    return normalized


def normalize_rag_sub_trace(trace: dict | None) -> Optional[dict]:
    if not isinstance(trace, dict) or not trace:
        return None
    normalized = _normalize_trace_fields(trace, RagSubTrace.model_fields)
    return RagSubTrace.model_validate(normalized).model_dump(exclude_none=True)


def normalize_rag_trace(trace: dict | None) -> Optional[dict]:
    if not isinstance(trace, dict) or not trace:
        return None
    normalized = _normalize_trace_fields(trace, RagTrace.model_fields)
    if "sub_traces" in normalized:
        sub_traces = normalized["sub_traces"] if isinstance(normalized["sub_traces"], list) else []
        normalized["sub_traces"] = [
            item
            for item in (
                normalize_rag_sub_trace(sub_trace)
                for sub_trace in sub_traces
                if isinstance(sub_trace, dict)
            )
            if item is not None
        ]
    return RagTrace.model_validate(normalized).model_dump(exclude_none=True)


class ChatResponse(StrictSchema):
    response: str
    rag_trace: Optional[RagTrace] = None


class MessageInfo(StrictSchema):
    type: str
    content: str
    timestamp: str
    rag_trace: Optional[RagTrace] = None


class SessionMessagesResponse(StrictSchema):
    messages: List[MessageInfo]


class SessionInfo(StrictSchema):
    session_id: str
    title: Optional[str] = None
    updated_at: str
    message_count: int


class SessionListResponse(StrictSchema):
    sessions: List[SessionInfo]


class SessionDeleteResponse(StrictSchema):
    session_id: str
    message: str
