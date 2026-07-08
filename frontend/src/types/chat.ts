export interface RetrievedChunk {
  filename: string;
  page_number?: number;
  rrf_rank?: number;
  rerank_score?: number | null;
  text?: string;
}

export interface RagTrace {
  tool_used?: boolean;
  tool_name?: string;
  retrieval_stage?: string;
  grade_score?: number;
  grade_route?: string;
  rewrite_needed?: boolean;
  rewrite_strategy?: string;
  rewrite_query?: string;
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
  expansion_type?: string;
  step_back_question?: string;
  expanded_query?: string;
  hypothetical_doc?: string;
  complexity?: 'simple' | 'complex' | string;
  complexity_reason?: string;
  sub_questions?: string[];
  sub_agent_count?: number;
  synthesis_merged_count?: number;
  sub_traces?: any[];
  initial_retrieved_chunks?: RetrievedChunk[];
  expanded_retrieved_chunks?: RetrievedChunk[];
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
}

export interface GroupedRagStep {
  group: string | null;
  label: string | null;
  steps: RagStep[];
  collapsed: boolean;
}

export interface Message {
  text: string;
  isUser: boolean;
  isThinking?: boolean;
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
