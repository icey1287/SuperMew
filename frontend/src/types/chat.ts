export interface RetrievedChunk {
  filename: string;
  page_number?: number;
  rrf_rank?: number;
  rerank_score?: number | null;
  text?: string;
}

export interface RagTraceFields {
  tool_used?: boolean;
  tool_name?: string;
  query?: string;
  retrieval_stage?: string;
  route?: string;
  retrieval_status?: string;
  evidence_relevance?: string;
  evidence_answerability?: string;
  evidence_ambiguity?: string;
  evidence_confidence?: number | null;
  evidence_reason?: string;
  missing_slots?: string[];
  hitl_prompt?: string;
  hitl_options?: string[];
  hitl_resumed?: boolean;
  hitl_answer?: string;
  hitl_resume_strategy?: string;
  hitl_resume_from_status?: string;
  hitl_resume_from_route?: string;
  hitl_targeted_retrieved_chunks?: RetrievedChunk[];
  retrieval_pipeline?: string;
  retrieval_mode?: string;
  candidate_k?: number;
  candidate_k_config_error?: string;
  candidate_k_source?: string;
  retrieval_candidate_multiplier?: number;
  recall_count?: number | null;
  post_merge_candidate_count?: number | null;
  candidate_count?: number | null;
  retrieval_top_k?: number;
  retrieved_chunks?: RetrievedChunk[];
  leaf_retrieve_level?: number;
  auto_merge_enabled?: boolean | null;
  auto_merge_applied?: boolean | null;
  auto_merge_threshold?: number;
  auto_merge_replaced_chunks?: number;
  auto_merge_steps?: number;
  rerank_enabled?: boolean | null;
  rerank_applied?: boolean | null;
  rerank_model?: string;
  rerank_error?: string;
  rerank_timeout_seconds?: number;
  rerank_min_score?: number;
  post_rerank_count?: number;
  post_threshold_count?: number;
  retrieval_empty?: boolean;
  rewrite_method?: 'step_back' | 'hyde';
  step_back_question?: string;
  hyde_document?: string;
  rewritten_query?: string;
  complexity?: 'simple' | 'complex' | string;
  complexity_reason?: string;
  sub_questions?: string[];
  sub_agent_count?: number;
  synthesis_merged_count?: number;
  initial_retrieved_chunks?: RetrievedChunk[];
  rewrite_retrieved_chunks?: RetrievedChunk[];
}

export interface RagSubTrace extends RagTraceFields {}

export interface RagTrace extends RagTraceFields {
  sub_traces?: RagSubTrace[];
}

export interface RagStep {
  key?: string;
  group?: string | null;
  group_label?: string | null;
  label: string;
  icon?: string;
  detail?: string;
  status?: string;
  percent?: number;
  message?: string;
  elapsed_ms?: number;
  stage_elapsed_ms?: number;
}

export interface GroupedRagStep {
  group: string | null;
  label: string | null;
  steps: RagStep[];
  collapsed: boolean;
}

export interface HitlRequest {
  id?: string;
  prompt: string;
  options?: string[];
  route?: 'clarify' | 'scope_select' | string;
  retrieval_status?: string;
  original_question?: string;
}

export interface Message {
  text: string;
  isUser: boolean;
  isThinking?: boolean;
  thinkingStartedAt?: number;
  isHitlRequest?: boolean;
  isHitlAnswer?: boolean;
  hitlPrompt?: string;
  hitlOptions?: string[];
  hitlResumeText?: string;
  ragTrace?: RagTrace | null;
  ragSteps?: RagStep[];
  _groupedSteps?: GroupedRagStep[];
}

export interface ChatSession {
  session_id: string;
  title?: string;
  message_count: number;
  updated_at: string;
  isStreaming?: boolean;
}
