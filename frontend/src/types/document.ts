export interface DocumentItem {
  filename: string;
  file_type: string;
  chunk_count: number;
  document_id?: string;
  current_version_id?: string | null;
  pending_version_id?: string | null;
  version_number?: number | null;
  status?: string;
  parent_chunk_count?: number;
  size_bytes?: number;
  uploaded_at?: string | null;
  build_fingerprint?: string;
  parser_version?: string;
  chunker_version?: string;
  embedding_model?: string;
  index_version?: string;
  vector_collection?: string;
  storage_layout?: string;
  error_code?: string | null;
}

export type UploadStepStatus = 'pending' | 'running' | 'completed' | 'failed';

export interface UploadStep {
  key: string;
  label: string;
  percent: number;
  status: UploadStepStatus;
  message: string;
}

export type UploadJobStatus =
  | 'pending'
  | 'running'
  | 'retry_wait'
  | 'staged'
  | 'completed'
  | 'failed'
  | 'cancelled'
  | 'dead_letter';

export interface UploadJob {
  job_id: string;
  cleanup_job_id?: string | null;
  filename?: string;
  status: UploadJobStatus;
  current_step?: string;
  message: string;
  total_chunks?: number;
  processed_chunks?: number;
  error?: string | null;
  attempts?: number;
  max_attempts?: number;
  execution_fence?: number;
  next_retry_at?: string | null;
  created_at?: string;
  updated_at?: string;
  steps: UploadStep[];
}

export interface DeleteStep {
  key: string;
  label: string;
  percent: number;
  status: 'pending' | 'running' | 'completed' | 'failed';
  message: string;
}

export interface DeleteJob {
  job_id: string;
  cleanup_job_id?: string | null;
  filename: string;
  document_id?: string | null;
  document_version_id?: string | null;
  dead_letter_job_ids?: string[];
  status: DeleteJobStatus;
  current_step?: string;
  message: string;
  error?: string | null;
  created_at?: string;
  updated_at?: string;
  steps: DeleteStep[];
}

export type DeleteJobStatus = 'running' | 'completed' | 'failed' | 'cleanup_pending';

export interface ActiveDeleteJob {
  jobId?: string;
  documentId?: string | null;
  documentVersionId?: string | null;
  deadLetterJobIds?: string[];
  createdAt?: string;
  updatedAt?: string;
  status: DeleteJobStatus;
  message: string;
  collapsed: boolean;
  steps: DeleteStep[];
}
