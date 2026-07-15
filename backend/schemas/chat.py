import re
from typing import Any, List, Literal, Mapping, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ChatStreamPublicError(StrictSchema):
    code: str = Field(pattern=r"^[A-Z][A-Z0-9_]{0,63}$")
    message: str = Field(min_length=1)
    retryable: bool
    category: Optional[str] = None
    stage: Optional[str] = None
    provider: Optional[str] = None
    retry_after: Optional[float] = Field(default=None, ge=0)
    request_id: Optional[str] = None
    details: dict[str, Any] = Field(default_factory=dict)


def build_chat_stream_error_event(error: Mapping[str, Any]) -> dict[str, Any]:
    """Build the one legacy-SSE error envelope accepted by old and new clients."""

    public = ChatStreamPublicError.model_validate(dict(error))
    contract = public.model_dump()
    compatibility = {
        key: contract[key]
        for key in (
            "code",
            "message",
            "retryable",
            "category",
            "stage",
            "provider",
            "retry_after",
            "request_id",
        )
    }
    return {
        "type": "error",
        "error": contract,
        **compatibility,
        "error_code": public.code,
        "content": f"[{public.code}] {public.message}",
    }


class ChatRequest(StrictSchema):
    message: str
    session_id: Optional[str] = "default_session"


class RetrievedChunk(StrictSchema):
    filename: str
    file_type: Optional[str] = None
    page_number: Optional[str | int] = None
    text: Optional[str] = None
    score: Optional[float] = None
    rrf_rank: Optional[int] = None
    rerank_score: Optional[float] = None
    chunk_id: Optional[str] = None
    parent_chunk_id: Optional[str] = None
    root_chunk_id: Optional[str] = None
    chunk_level: Optional[int] = None
    chunk_idx: Optional[int] = None
    document_id: Optional[str] = None
    document_version_id: Optional[str] = None
    section_id: Optional[str] = None
    index_version: Optional[str] = None
    content_hash: Optional[str] = Field(
        default=None,
        pattern=r"^[0-9a-fA-F]{64}$",
    )
    merged_from_children: Optional[bool] = None
    merged_child_count: Optional[int] = Field(default=None, ge=0)

    @model_validator(mode="after")
    def require_versioned_manifest_hash(self):
        if self.document_version_id and self.content_hash is None:
            raise ValueError("versioned retrieved chunk requires content_hash")
        return self


class RetrievalTargetTrace(StrictSchema):
    collection_name: str = Field(
        min_length=1,
        max_length=160,
        pattern=r"^[A-Za-z_][A-Za-z0-9_]{0,159}$",
    )
    storage_layout: Literal["versioned", "legacy_filename"]
    required: bool
    mode: Literal["hybrid", "dense_fallback", "missing_optional"]
    hit_count: int = Field(ge=0)


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
    retrieval_outcome: Optional[
        Literal["ANSWERABLE", "NO_KNOWLEDGE", "INSUFFICIENT_EVIDENCE"]
    ] = None
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
    rerank_error_code: Optional[str] = None
    rerank_retryable: Optional[bool] = None
    rerank_attempts: Optional[int] = None
    rerank_fallback_applied: Optional[bool] = None
    rerank_timeout_seconds: Optional[float] = None
    rerank_min_score: Optional[float] = None
    rerank_threshold_applied: Optional[bool] = None
    rerank_skip_reason: Optional[str] = None
    rerank_candidate_count: Optional[int] = None
    rerank_candidate_limit: Optional[int] = None
    rerank_candidate_limit_applied: Optional[bool] = None
    rerank_payload_characters: Optional[int] = None
    rerank_document_character_limit: Optional[int] = None
    rerank_total_character_limit: Optional[int] = None
    rerank_truncated_document_count: Optional[int] = None
    post_rerank_count: Optional[int] = None
    post_threshold_count: Optional[int] = None
    retrieval_empty: Optional[bool] = None
    retrieval_degraded_code: Optional[str] = None
    provider_error_code: Optional[str] = None
    provider_error_stage: Optional[str] = None
    coverage_gap_codes: Optional[List[str]] = None
    coverage_gap_questions: Optional[List[str]] = None
    retrieval_mode: Optional[str] = None
    retrieval_pipeline: Optional[str] = None
    candidate_k: Optional[int] = None
    candidate_k_source: Optional[str] = None
    candidate_k_config_error: Optional[str] = None
    retrieval_candidate_multiplier: Optional[int] = None
    retrieval_top_k: Optional[int] = None
    recall_count: Optional[int] = None
    deduplicated_recall_count: Optional[int] = None
    retrieval_index_id: Optional[str] = Field(
        default=None, min_length=1, max_length=128
    )
    retrieval_target_count: Optional[int] = Field(default=None, ge=0)
    retrieval_required_target_count: Optional[int] = Field(default=None, ge=0)
    retrieval_optional_target_count: Optional[int] = Field(default=None, ge=0)
    retrieval_optional_missing_count: Optional[int] = Field(default=None, ge=0)
    retrieval_target_results: Optional[List[RetrievalTargetTrace]] = None
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
    checkpoint_thread_id: Optional[str] = None
    checkpoint_id: Optional[str] = None
    interrupt_id: Optional[str] = None


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
    normalized: list[dict] = []
    for item in value:
        if not isinstance(item, dict) or not item.get("filename"):
            continue
        chunk = {key: item[key] for key in fields if key in item}
        content_hash = chunk.get("content_hash")
        if content_hash is not None and (
            not isinstance(content_hash, str)
            or re.fullmatch(r"[0-9a-fA-F]{64}", content_hash) is None
        ):
            if chunk.get("document_version_id"):
                # Versioned artifacts promise manifest identity. Corruption must
                # fail closed instead of being persisted as an ambiguous trace.
                RetrievedChunk.model_validate(chunk)
            chunk.pop("content_hash", None)
        normalized.append(
            RetrievedChunk.model_validate(chunk).model_dump(exclude_none=True)
        )
    return normalized


def _normalize_retrieval_targets(value) -> list[dict]:
    if not isinstance(value, list):
        return []
    fields = RetrievalTargetTrace.model_fields
    return [
        RetrievalTargetTrace.model_validate(
            {key: item[key] for key in fields if key in item}
        ).model_dump()
        for item in value
        if isinstance(item, dict)
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
    if "retrieval_target_results" in normalized:
        normalized["retrieval_target_results"] = _normalize_retrieval_targets(
            normalized["retrieval_target_results"]
        )
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
        sub_traces = (
            normalized["sub_traces"]
            if isinstance(normalized["sub_traces"], list)
            else []
        )
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
    id: Optional[int] = None
    run_id: Optional[str] = None
    sequence: Optional[int] = None
    status: Optional[str] = None
    type: str
    content: str
    timestamp: str
    rag_trace: Optional[RagTrace] = None


class SessionMessagesResponse(StrictSchema):
    messages: List[MessageInfo]
    next_cursor: Optional[int] = None


class SessionInfo(StrictSchema):
    session_id: str
    title: Optional[str] = None
    updated_at: str
    message_count: int
    version: Optional[int] = None
    status: Optional[str] = None


class SessionListResponse(StrictSchema):
    sessions: List[SessionInfo]


class SessionDeleteResponse(StrictSchema):
    session_id: str
    message: str
