import type { RunArtifactState, RunTimelineItem } from '@/events/runEventReducer';

export type { RunEventType, RunEventV1 } from './generated/run-event-v1';

export interface RetrievedChunk {
  filename: string;
  file_type?: string;
  page_number?: number;
  rrf_rank?: number;
  rerank_score?: number | null;
  text?: string;
  chunk_id: string;
  parent_chunk_id?: string;
  root_chunk_id?: string;
  chunk_level?: number;
  chunk_idx?: number;
  document_id: string;
  document_version_id: string;
  section_id: string;
  index_version: string;
  content_hash: string;
  merged_from_children?: boolean;
  merged_child_count?: number;
}

export interface RetrievalTargetTrace {
  collection_name: string;
  required: boolean;
  mode: 'hybrid' | 'dense_fallback' | 'missing_optional';
  hit_count: number;
}

export interface RagTraceFields {
  tool_used?: boolean;
  tool_name?: string;
  query?: string;
  retrieval_stage?: string;
  route?: string;
  retrieval_status?: string;
  retrieval_outcome?: 'ANSWERABLE' | 'NO_KNOWLEDGE' | 'INSUFFICIENT_EVIDENCE';
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
  deduplicated_recall_count?: number | null;
  retrieval_index_id?: string;
  retrieval_target_count?: number;
  retrieval_required_target_count?: number;
  retrieval_optional_target_count?: number;
  retrieval_optional_missing_count?: number;
  retrieval_target_results?: RetrievalTargetTrace[];
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
  rerank_error_code?: string;
  rerank_retryable?: boolean | null;
  rerank_attempts?: number;
  rerank_fallback_applied?: boolean | null;
  rerank_timeout_seconds?: number;
  rerank_min_score?: number;
  rerank_threshold_applied?: boolean | null;
  rerank_skip_reason?: string | null;
  rerank_candidate_count?: number;
  rerank_candidate_limit?: number;
  rerank_candidate_limit_applied?: boolean | null;
  rerank_payload_characters?: number;
  rerank_document_character_limit?: number;
  rerank_total_character_limit?: number;
  rerank_truncated_document_count?: number;
  post_rerank_count?: number;
  post_threshold_count?: number;
  retrieval_empty?: boolean;
  retrieval_degraded_code?: string;
  provider_error_code?: string;
  provider_error_stage?: string;
  coverage_gap_codes?: string[];
  coverage_gap_questions?: string[];
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

export type RagSubTrace = RagTraceFields;

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
  runId?: string;
  hitlToken?: string;
  checkpointId?: string;
  prompt: string;
  options?: string[];
  route?: 'clarify' | 'scope_select' | string;
  retrieval_status?: string;
  original_question?: string;
}

export interface Message {
  id?: number;
  runId?: string;
  sequence?: number;
  status?: string;
  text: string;
  isUser: boolean;
  isThinking?: boolean;
  thinkingStartedAt?: number;
  isHitlRequest?: boolean;
  isHitlAnswer?: boolean;
  hitlPrompt?: string;
  hitlOptions?: string[];
  hitlResumeText?: string;
  skillName?: string | null;
  skillVersion?: string | null;
  ragTrace?: RagTrace | null;
  ragSteps?: RagStep[];
  runTimeline?: RunTimelineItem[];
  artifacts?: RunArtifactState[];
  _groupedSteps?: GroupedRagStep[];
}
