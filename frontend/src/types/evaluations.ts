export type RagEvaluationJobStatus =
  'queued' | 'running' | 'cancelling' | 'cancelled' | 'succeeded' | 'failed';

export type RagEvaluationCaseStatus = 'queued' | 'running' | 'completed' | 'failed' | 'cancelled';

export interface RagExpectedBehavior {
  complexity?: 'simple' | 'complex' | null;
  route?: string | null;
  outcome?: 'ANSWERABLE' | 'NO_KNOWLEDGE' | 'INSUFFICIENT_EVIDENCE' | null;
  hitl?: 'none' | 'clarify' | 'scope_select';
  acceptable_abstention?: boolean;
  hitl_resolution_success?: boolean | null;
  hitl_final_outcome?: string | null;
}

export interface RagGoldDocument {
  document_id?: string | null;
  canonical_name?: string | null;
}

export interface RagGoldChunk {
  chunk_id?: string | null;
  content_sha256?: string | null;
}

export interface RagEvaluationDatasetCase {
  id: string;
  tags?: string[];
  critical?: boolean;
  question: string;
  expected: RagExpectedBehavior;
  gold_documents?: RagGoldDocument[];
  gold_chunks?: RagGoldChunk[];
  reference_answer?: string | null;
  required_claims?: string[];
  conflicts?: string[];
  hitl_answers?: string[];
}

export interface RagEvaluationDataset {
  schema_version: 1;
  name: string;
  cases: RagEvaluationDatasetCase[];
}

export interface RagEvaluationDatasetRecord {
  id: string;
  name: string;
  fingerprint: string;
  case_count: number;
  dataset: RagEvaluationDataset;
  created_by: string | null;
  created_at: string;
  updated_at: string;
}

export interface RagMetricGate {
  metric: string;
  direction: 'higher_is_better' | 'lower_is_better';
  minimum?: number | null;
  maximum?: number | null;
  max_regression?: number;
  required?: boolean;
}

export interface RagEvaluationGatePolicy {
  schema_version?: 1;
  k_values?: number[];
  critical_no_regression?: boolean;
  required_provenance?: 'contract_smoke' | 'live_rag' | null;
  metric_gates?: RagMetricGate[];
}

export interface EvaluationModelSummary {
  profile_id: string;
  profile_version: number;
  display_name: string;
  provider: string;
  model_name: string;
  timeout_seconds: number;
  supports_stream: boolean;
  supports_structured_output: boolean;
}

export interface RagMetricResult {
  value: number | null;
  eligible_cases: number;
}

export interface RagGateResult {
  name: string;
  status: 'passed' | 'failed' | 'skipped';
  metric?: string | null;
  actual?: number | null;
  baseline?: number | null;
  threshold?: number | null;
  baseline_threshold?: number | null;
  detail: string;
}

export interface RagReportCase {
  case_id: string;
  critical: boolean;
  metrics: Record<string, number | null>;
  checks: Record<string, boolean | null>;
  provider_failed: boolean;
  provider_error_code?: string | null;
  provider_error_stage?: 'retrieval' | 'generation' | 'judge' | null;
  gold_chunk_count: number;
  matched_gold_chunk_count: number;
  passed: boolean;
}

export interface RagEvaluationReport {
  schema_version: 1;
  dataset_name: string;
  dataset_fingerprint: string;
  case_count: number;
  observation_count: number;
  metrics: Record<string, RagMetricResult>;
  slices: Record<string, { case_count: number; metrics: Record<string, RagMetricResult> }>;
  unavailable_metrics: Record<string, string>;
  cases: RagReportCase[];
  gates: RagGateResult[];
  passed: boolean;
  metadata: Record<string, unknown>;
}

export interface RagEvaluationJob {
  id: string;
  dataset_id: string;
  dataset_name: string;
  dataset_fingerprint: string;
  baseline_job_id: string | null;
  status: RagEvaluationJobStatus;
  completed_cases: number;
  total_cases: number;
  progress: number;
  gate_policy: RagEvaluationGatePolicy;
  model_catalog_hash: string;
  models: Record<string, EvaluationModelSummary>;
  owner_worker_id: string | null;
  lease_expires_at: string | null;
  fencing_token: number;
  attempts: number;
  max_attempts: number;
  error_code: string | null;
  error: Record<string, unknown> | null;
  report: RagEvaluationReport | null;
  created_by: string | null;
  started_at: string | null;
  finished_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface RagJudgeMetrics {
  answer_correctness: number;
  groundedness: number;
  answer_relevance: number;
  completeness: number;
  context_relevance: number;
  unsupported_claim_rate: number;
  conflict_disclosure_rate: number;
  reason?: string;
}

export interface RagEvaluationObservation {
  case_id: string;
  complexity?: string | null;
  route?: string | null;
  outcome?: string | null;
  hitl?: string;
  rewrite_performed?: boolean;
  provider_error_code?: string | null;
  provider_error_stage?: 'retrieval' | 'generation' | 'judge' | null;
  duration_ms: number;
  judge?: RagJudgeMetrics | null;
}

export interface RagEvaluationCaseResult {
  id: string;
  job_id: string;
  case_id: string;
  position: number;
  status: RagEvaluationCaseStatus;
  question: string;
  generated_answer: string | null;
  judge_reason: string | null;
  observation: RagEvaluationObservation | null;
  judge: RagJudgeMetrics | null;
  metrics: Record<string, number | null>;
  checks: Record<string, boolean | null>;
  retrieved_identities: Array<Record<string, unknown>>;
  provider_error_code: string | null;
  provider_error_stage?: 'retrieval' | 'generation' | 'judge' | null;
  duration_ms: number | null;
  error_code: string | null;
  error: Record<string, unknown> | null;
  started_at: string | null;
  finished_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface RagEvaluationJobCreatePayload {
  dataset_id: string;
  baseline_job_id?: string | null;
  gate_policy?: RagEvaluationGatePolicy | null;
}
