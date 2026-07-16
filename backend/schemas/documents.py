from __future__ import annotations

from pydantic import BaseModel, Field


class DocumentInfo(BaseModel):
    filename: str
    file_type: str
    chunk_count: int
    document_id: str | None = None
    current_version_id: str | None = None
    pending_version_id: str | None = None
    version_number: int | None = None
    status: str = "pending"
    parent_chunk_count: int = 0
    size_bytes: int = 0
    uploaded_at: str | None = None
    build_fingerprint: str | None = None
    parser_version: str | None = None
    chunker_version: str | None = None
    embedding_model: str | None = None
    index_version: str | None = None
    vector_collection: str | None = None
    storage_layout: str | None = None
    error_code: str | None = None


class DocumentListResponse(BaseModel):
    documents: list[DocumentInfo]


class DocumentUploadResponse(BaseModel):
    filename: str
    chunks_processed: int
    message: str
    document_id: str | None = None
    document_version_id: str | None = None
    version_number: int | None = None
    published: bool = True
    reused_current: bool = False


class DocumentUploadStartResponse(BaseModel):
    job_id: str
    filename: str
    message: str
    document_id: str | None = None
    document_version_id: str | None = None
    version_number: int | None = None
    status: str = "pending"


class UploadStepInfo(BaseModel):
    key: str
    label: str
    percent: int
    status: str
    message: str = ""


class DocumentUploadJobResponse(BaseModel):
    job_id: str
    filename: str
    status: str
    current_step: str
    message: str
    document_id: str | None = None
    document_version_id: str | None = None
    cleanup_job_id: str | None = None
    dead_letter_job_ids: list[str] = Field(default_factory=list)
    total_chunks: int = 0
    processed_chunks: int = 0
    error: str | None = None
    attempts: int = 0
    max_attempts: int = 0
    execution_fence: int = 0
    next_retry_at: str | None = None
    created_at: str
    updated_at: str
    steps: list[UploadStepInfo]


class DocumentDeleteStartResponse(BaseModel):
    job_id: str
    filename: str
    message: str


class DocumentDeleteJobResponse(DocumentUploadJobResponse):
    pass


class DocumentDeleteResponse(BaseModel):
    filename: str
    job_id: str
    chunks_deleted: int
    message: str
    document_id: str | None = None
    status: str = "completed"
    cleanup_pending: bool = False
    error_code: str | None = None
