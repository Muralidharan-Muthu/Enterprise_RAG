export type DocumentType = "policy" | "financial" | "legal" | "entity" | "research";
export type DocumentStatus = "uploaded" | "parsing" | "routing" | "chunking" | "embedding" | "storing" | "completed" | "failed";
export type PipelineStage = "queued" | "parsing" | "images" | "routing" | "chunking" | "embedding" | "storing" | "graph" | "done" | "error";

export interface DocumentSummary {
  id: string;
  original_filename: string;
  document_type: DocumentType | null;
  document_subtype: string | null;
  status: DocumentStatus;
  page_count: number | null;
  word_count: number | null;
  router_confidence: number | null;
  doc_title: string | null;
  doc_summary: string | null;
  vector_chunks: number;
  table_count: number;
  clause_count: number;
  research_chunks: number;
  completed_at: string | null;
  created_at: string;
  error_message: string | null;
}

export interface DocumentDetail extends DocumentSummary {
  router_reasoning: string | null;
  doc_author: string | null;
  doc_date: string | null;
  has_tables: boolean;
  has_images: boolean;
  language_detected: string;
  doc_metadata: Record<string, unknown>;
  storage_path: string | null;
}

export interface DocumentListResponse {
  items: DocumentSummary[];
  total: number;
  page: number;
  pages: number;
  limit: number;
}

export interface JobStatus {
  job_id: string;
  document_id: string;
  current_stage: PipelineStage;
  stage_progress: number;
  total_chunks: number | null;
  processed_chunks: number;
  stage_timings: Record<string, number>;
  stage_detail: StageDetail;
  error_message: string | null;
  queued_at: string | null;
  started_at: string | null;
  completed_at: string | null;
  duration_seconds: number | null;
}

export interface UploadResponse {
  document_id: string;
  job_id: string;
  filename: string;
  status: string;
  message: string;
}

// ── Pipeline runs ─────────────────────────────────────────────────────────

export type PipelineSource = "local" | "gdrive" | "sharepoint";
export type PipelineRunStatus = "empty" | "running" | "completed" | "failed";

export interface PipelineCreateInput {
  name: string;
  description?: string;
  source?: PipelineSource;
  files: File[];
}

export interface PipelineFileResult {
  document_id: string;
  job_id: string | null;
  filename: string;
  status: "queued" | "failed";
  error: string | null;
}

export interface PipelineCreateResponse {
  pipeline_run_id: string;
  name: string;
  files_found: number;
  files_queued: number;
  files_failed: number;
  files: PipelineFileResult[];
}

export interface PipelineRunSummary {
  id: string;
  name: string;
  description: string | null;
  source: PipelineSource;
  domain?: string | null;
  sub_domain?: string | null;
  category?: string | null;
  sub_category?: string | null;
  files_found: number;
  files_processed: number;
  files_failed: number;
  status: PipelineRunStatus;
  started_at: string | null;
  created_at: string;
}

export interface PipelineRunListResponse {
  items: PipelineRunSummary[];
  total: number;
  page: number;
  pages: number;
  limit: number;
}

export interface PipelineDocumentDetail {
  document_id: string;
  job_id: string | null;
  original_filename: string;
  file_size_bytes: number;
  doc_status: DocumentStatus;
  document_type: DocumentType | null;
  router_confidence: number | null;
  doc_title: string | null;
  page_count: number | null;
  word_count: number | null;
  total_chunks: number | null;
  processed_chunks: number;
  vector_chunks: number;
  table_count: number;
  clause_count: number;
  research_chunks: number;
  error_stage: string | null;
  error_message: string | null;
  current_stage: PipelineStage | null;
  stage_progress: number;
  stage_timings: Record<string, number>;
  stage_detail: StageDetail;
  queued_at: string | null;
  started_at: string | null;
  completed_at: string | null;
  duration_seconds: number | null;
}

export interface PipelineDetail extends PipelineRunSummary {
  documents: PipelineDocumentDetail[];
}

// Granular per-stage detail (live). parsing.pages is the per-page workload map.
export interface StageDetailPage {
  page: number;
  images: number;
  blocks?: number;
  tables?: number;
  chars?: number;
  est_words?: number;
  done?: boolean;
  failed?: boolean;
}
export interface StageDetail {
  parsing?: {
    pages: StageDetailPage[];
    total_pages: number;
    pages_done?: number;
    phase?: "prescan" | "parsing" | "done";
  };
  [key: string]: unknown;
}

// ── Per-document drill-in: images + store chunks ────────────────────────────
export interface DocumentImage {
  image_index: number;
  page_number: number | null;
  bbox: { x1: number; y1: number; x2: number; y2: number } | null;
  width: number | null;
  height: number | null;
  caption: string;
  /** VLM structured extraction (primary knowledge column). Backend also aliases
   *  this to `caption` for backward compat with the existing image card. */
  structured_content: string;
  ocr_text: string;
  vlm_ocr_text: string;
  image_url: string | null;
  processing_status: "SKIPPED" | "OCR_ONLY" | "VLM_PROCESSED";
  skip_reason: string | null;
  filter_stage: string | null;
  image_type: string | null;
}
export interface ImageMetrics {
  total: number;
  vlm_processed: number;
  ocr_only: number;
  skipped: number;
  vlm_avoided_pct: number;
  by_stage: Record<string, number>;
  by_type: Record<string, number>;
}
export interface DocumentImagesResponse {
  items: DocumentImage[];
  total: number;
  metrics?: ImageMetrics; // optional for backward-compat during deploy
}

export type ChunkStore = "vector" | "table" | "clause";
export interface ChunksResponse {
  items: Record<string, unknown>[];
  total: number;
  page: number;
  pages: number;
  store: ChunkStore;
}

export interface HealthResponse {
  status: string;
  api: string;
  database: string;
  redis: string;
  groq_endpoint?: string;
  gemma_endpoint: string;
  neo4j: string;
  timestamp: string;
  groq_model?: string;
  gemma_model?: string;
}

export interface UploadItem {
  file: File;
  documentId: string | null;
  jobId: string | null;
  uploadProgress: number;  // 0-100 for HTTP upload
  error: string | null;
}

// Colour mappings for document type badges
export const DOC_TYPE_COLORS: Record<DocumentType, string> = {
  policy:
    "bg-blue-100 text-blue-800 border-blue-200 dark:bg-blue-950 dark:text-blue-300 dark:border-blue-900",
  financial:
    "bg-green-100 text-green-800 border-green-200 dark:bg-green-950 dark:text-green-300 dark:border-green-900",
  legal:
    "bg-purple-100 text-purple-800 border-purple-200 dark:bg-purple-950 dark:text-purple-300 dark:border-purple-900",
  entity:
    "bg-yellow-100 text-yellow-800 border-yellow-200 dark:bg-yellow-950 dark:text-yellow-300 dark:border-yellow-900",
  research:
    "bg-orange-100 text-orange-800 border-orange-200 dark:bg-orange-950 dark:text-orange-300 dark:border-orange-900",
};

export const PIPELINE_STAGES: PipelineStage[] = [
  "queued", "parsing", "images", "routing", "chunking", "embedding", "storing", "graph", "done",
];

export interface PageStats {
  total_pages: number;
  word_count: number;
  has_tables: boolean;
  has_images: boolean;
  language: string;
  pages: {
    page: number;
    chunks: number;
    tables: number;
    clauses: number;
    images: number;
    est_words: number;
  }[];
}

// ── Phase 2: Query types ──────────────────────────────────────────────────────

export interface QueryRequest {
  query: string;
  document_types?: DocumentType[];
  document_id?: string;
  top_k?: number;
  use_reranker?: boolean;
}

export interface CitationItem {
  document_id: string;
  filename: string;
  chunk_text: string;
  store_type: "vector" | "clause" | "research" | "table" | "image";
  relevance_score: number;
  page_number: number | null;
  /** Last page of a multi-page table window, when different from page_number.
   *  Null for single-page citations and non-table store types. */
  page_number_end: number | null;
  section_title: string | null;
  clause_type: string | null;
  risk_level: string | null;
  chunk_type: string | null;
  source_doi: string | null;
  table_markdown: string | null;
  image_url: string | null;
  caption: string | null;
  ocr_text: string | null;
  pdf_url: string | null;
  bbox: { x1: number; y1: number; x2: number; y2: number } | null;
}

export interface QueryResponse {
  answer: string;
  confidence: number;
  citations: CitationItem[];
  retrieval_stats: {
    total_retrieved: number;
    after_reranking: number;
    stores_searched: string[];
  };
  query: string;
  processing_time_seconds: number;
  notes: string | null;
  timings?: Record<string, number> | null;
}

// ── Chat session types ────────────────────────────────────────────────────────

export interface ChatSession {
  id: string;
  title: string;
  message_count: number;
  created_at: string;
  updated_at: string;
}

export interface ChatMessageRecord {
  id: string;
  role: "user" | "assistant";
  content: string;
  confidence?: number | null;
  processing_time?: number | null;
  stores_searched?: string[] | null;
  notes?: string | null;
  citations?: CitationItem[] | null;
  is_pinned?: boolean;
  created_at: string;
}

export interface ChatSessionDetail extends ChatSession {
  messages: ChatMessageRecord[];
}

export interface AddChatMessageRequest {
  role: "user" | "assistant";
  content: string;
  confidence?: number;
  processing_time?: number;
  stores_searched?: string[];
  notes?: string;
  citations?: CitationItem[];
  is_pinned?: boolean;
}
