from pydantic import BaseModel
from typing import Optional
from datetime import datetime


class UploadResponse(BaseModel):
    document_id: str
    job_id: str
    filename: str
    status: str = "queued"
    message: str = "Document queued for processing"


class JobStatusResponse(BaseModel):
    job_id: str
    document_id: str
    current_stage: str
    stage_progress: int
    total_chunks: Optional[int] = None
    processed_chunks: int = 0
    stage_timings: dict = {}
    stage_detail: dict = {}
    error_message: Optional[str] = None
    queued_at: Optional[datetime] = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    duration_seconds: Optional[float] = None


class DocumentSummary(BaseModel):
    id: str
    original_filename: str
    document_type: Optional[str] = None
    document_subtype: Optional[str] = None
    status: str
    page_count: Optional[int] = None
    word_count: Optional[int] = None
    router_confidence: Optional[float] = None
    doc_title: Optional[str] = None
    doc_summary: Optional[str] = None
    vector_chunks: int = 0
    table_count: int = 0
    clause_count: int = 0
    research_chunks: int = 0
    completed_at: Optional[datetime] = None
    created_at: datetime
    error_message: Optional[str] = None


class DocumentDetail(DocumentSummary):
    router_reasoning: Optional[str] = None
    doc_author: Optional[str] = None
    doc_date: Optional[str] = None
    has_tables: bool = False
    has_images: bool = False
    language_detected: str = "en"
    doc_metadata: dict = {}
    storage_path: Optional[str] = None


class DocumentListResponse(BaseModel):
    items: list[DocumentSummary]
    total: int
    page: int
    pages: int
    limit: int


class PipelineFileResult(BaseModel):
    document_id: str
    job_id: Optional[str] = None
    filename: str
    status: str           # 'queued' | 'failed'
    error: Optional[str] = None


class PipelineCreateResponse(BaseModel):
    pipeline_run_id: str
    name: str
    files_found: int
    files_queued: int
    files_failed: int
    files: list[PipelineFileResult]


class PipelineRunSummary(BaseModel):
    id: str
    name: str
    description: Optional[str] = None
    source: str
    domain: Optional[str] = None
    sub_domain: Optional[str] = None
    category: Optional[str] = None
    sub_category: Optional[str] = None
    files_found: int = 0
    files_processed: int = 0
    files_failed: int = 0
    status: str
    started_at: Optional[datetime] = None
    created_at: datetime


class PipelineRunListResponse(BaseModel):
    items: list[PipelineRunSummary]
    total: int
    page: int
    pages: int
    limit: int


class PipelineDocumentDetail(BaseModel):
    document_id: str
    job_id: Optional[str] = None
    original_filename: str
    file_size_bytes: int = 0
    doc_status: str
    document_type: Optional[str] = None
    router_confidence: Optional[float] = None
    doc_title: Optional[str] = None
    page_count: Optional[int] = None
    word_count: Optional[int] = None
    total_chunks: Optional[int] = None
    processed_chunks: int = 0
    error_stage: Optional[str] = None
    error_message: Optional[str] = None
    current_stage: Optional[str] = None
    stage_progress: int = 0
    stage_timings: dict = {}
    stage_detail: dict = {}
    vector_chunks: int = 0
    table_count: int = 0
    clause_count: int = 0
    research_chunks: int = 0
    queued_at: Optional[datetime] = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    duration_seconds: Optional[float] = None


class PipelineDetail(BaseModel):
    id: str
    name: str
    description: Optional[str] = None
    source: str
    domain: Optional[str] = None
    sub_domain: Optional[str] = None
    category: Optional[str] = None
    sub_category: Optional[str] = None
    status: str
    files_found: int = 0
    files_processed: int = 0
    files_failed: int = 0
    created_at: datetime
    started_at: Optional[datetime] = None
    documents: list[PipelineDocumentDetail] = []


class HealthResponse(BaseModel):
    status: str
    api: str
    database: str
    redis: str
    gemma_endpoint: str
    neo4j: str = "disabled"
    timestamp: datetime
    embedding_model: str = ""
    reranker_name: str = ""
    gemma_model: str = ""
