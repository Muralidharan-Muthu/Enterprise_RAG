import type {
  AddChatMessageRequest,
  ChatSession,
  ChatSessionDetail,
  ChunksResponse,
  ChunkStore,
  DocumentDetail,
  DocumentImagesResponse,
  DocumentListResponse,
  DocumentSummary,
  HealthResponse,
  JobStatus,
  PageStats,
  PipelineCreateInput,
  PipelineCreateResponse,
  PipelineDetail,
  PipelineRunListResponse,
  QueryRequest,
  QueryResponse,
  UploadResponse,
} from "./types";
import { getAuthToken } from "./auth";

const BASE = typeof window !== "undefined" ? "" : (process.env.NEXT_PUBLIC_API_URL || "https://muralidharan007-multi-store-rag-backend.hf.space");

function _authHeaders(): Record<string, string> {
  const token = getAuthToken();
  return token ? { Authorization: `Bearer ${token}` } : {};
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    headers: { "Content-Type": "application/json", ..._authHeaders(), ...init?.headers },
    // Our polling hooks (useDocumentImages, usePipeline, etc.) hit the exact
    // same URL every 1-1.2s while a run is live. Without this, the browser's
    // HTTP cache can serve back the FIRST response it saw for that URL (e.g.
    // "0 images" from before any figure was stored) on every later poll,
    // since the backend sends no Cache-Control header telling it not to.
    // React Query's own refetch logic is irrelevant once that happens — the
    // fetch never reaches the network. Always bypass the HTTP cache.
    cache: "no-store",
    ...init,
  });
  if (!res.ok) {
    const body = await res.text();
    throw new Error(`API ${res.status}: ${body}`);
  }
  return res.json() as Promise<T>;
}

export const apiClient = {
  uploadDocument: async (file: File): Promise<UploadResponse> => {
    const form = new FormData();
    form.append("file", file);
    const res = await fetch(`${BASE}/api/v1/ingest/upload`, {
      method: "POST",
      headers: _authHeaders(),
      body: form,
    });
    if (!res.ok) {
      const body = await res.text();
      throw new Error(`Upload failed ${res.status}: ${body}`);
    }
    return res.json();
  },

  createPipeline: async (
    input: PipelineCreateInput
  ): Promise<PipelineCreateResponse> => {
    const form = new FormData();
    form.append("name", input.name);
    if (input.description) form.append("description", input.description);
    form.append("source", input.source ?? "local");
    for (const file of input.files) form.append("files", file);

    const res = await fetch(`${BASE}/api/v1/ingest/pipeline`, {
      method: "POST",
      headers: _authHeaders(),
      body: form,
    });
    if (!res.ok) {
      const body = await res.text();
      throw new Error(`Pipeline failed ${res.status}: ${body}`);
    }
    return res.json();
  },

  listPipelines: (params?: {
    page?: number;
    limit?: number;
  }): Promise<PipelineRunListResponse> => {
    const qs = new URLSearchParams();
    if (params?.page) qs.set("page", String(params.page));
    if (params?.limit) qs.set("limit", String(params.limit));
    return request<PipelineRunListResponse>(`/api/v1/ingest/pipelines?${qs}`);
  },

  getJobStatus: (jobId: string): Promise<JobStatus> =>
    request<JobStatus>(`/api/v1/ingest/status/${jobId}`),

  listDocuments: (params?: {
    page?: number;
    limit?: number;
    status?: string;
    document_type?: string;
  }): Promise<DocumentListResponse> => {
    const qs = new URLSearchParams();
    if (params?.page) qs.set("page", String(params.page));
    if (params?.limit) qs.set("limit", String(params.limit));
    if (params?.status) qs.set("status", params.status);
    if (params?.document_type) qs.set("document_type", params.document_type);
    return request<DocumentListResponse>(`/api/v1/documents?${qs}`);
  },

  getDocument: (id: string): Promise<DocumentDetail> =>
    request<DocumentDetail>(`/api/v1/documents/${id}`),

  getPageStats: (id: string): Promise<PageStats> =>
    request<PageStats>(`/api/v1/documents/${id}/page-stats`),

  getDocumentImages: (id: string): Promise<DocumentImagesResponse> =>
    request<DocumentImagesResponse>(`/api/v1/documents/${id}/images`),

  getDocumentChunks: (
    id: string,
    store: ChunkStore = "vector",
    page = 1,
    limit = 50,
  ): Promise<ChunksResponse> =>
    request<ChunksResponse>(
      `/api/v1/documents/${id}/chunks?store=${store}&page=${page}&limit=${limit}`,
    ),

  deleteDocument: (id: string): Promise<{ deleted: boolean }> =>
    request(`/api/v1/documents/${id}`, { method: "DELETE" }),

  reprocessDocument: (id: string): Promise<{ document_id: string; job_id: string; pipeline_run_id: string; status: string }> =>
    request(`/api/v1/ingest/documents/${id}/reprocess`, { method: "POST" }),

  deletePipeline: (runId: string): Promise<{ deleted: boolean; run_id: string; documents_deleted: number }> =>
    request(`/api/v1/ingest/pipelines/${runId}`, { method: "DELETE" }),

  getPipeline: (runId: string): Promise<PipelineDetail> =>
    request<PipelineDetail>(`/api/v1/ingest/pipelines/${runId}`),

  renamePipeline: (runId: string, name: string): Promise<{ run_id: string; name: string }> =>
    request(`/api/v1/ingest/pipelines/${runId}`, {
      method: "PATCH",
      body: JSON.stringify({ name }),
    }),

  clearAllPipelines: (): Promise<{ deleted: number }> =>
    request("/api/v1/ingest/pipelines", { method: "DELETE" }),

  getHealth: (): Promise<HealthResponse> =>
    request<HealthResponse>("/api/v1/health"),

  submitQuery: (body: QueryRequest): Promise<QueryResponse> =>
    request<QueryResponse>("/api/v1/query", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  // ── Chat sessions ──────────────────────────────────────────────────────────

  listChatSessions: (limit = 30): Promise<ChatSession[]> =>
    request<ChatSession[]>(`/api/v1/chats?limit=${limit}`),

  createChatSession: (body: { title?: string }): Promise<ChatSession> =>
    request<ChatSession>("/api/v1/chats", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  getChatSession: (sessionId: string): Promise<ChatSessionDetail> =>
    request<ChatSessionDetail>(`/api/v1/chats/${sessionId}`),

  addChatMessage: (
    sessionId: string,
    message: AddChatMessageRequest,
  ): Promise<{ id: string; created_at: string }> =>
    request(`/api/v1/chats/${sessionId}/messages`, {
      method: "POST",
      body: JSON.stringify(message),
    }),

  deleteChatSession: (sessionId: string): Promise<{ deleted: boolean }> =>
    request(`/api/v1/chats/${sessionId}`, { method: "DELETE" }),

  updateChatSession: (sessionId: string, title: string): Promise<{ id: string; title: string }> =>
    request(`/api/v1/chats/${sessionId}`, {
      method: "PATCH",
      body: JSON.stringify({ title }),
    }),

  generateChatTitle: (sessionId: string): Promise<{ title: string }> =>
    request(`/api/v1/chats/${sessionId}/generate-title`, { method: "POST" }),

  toggleMessagePin: (sessionId: string, messageId: string, isPinned: boolean): Promise<{ status: string; is_pinned: boolean }> =>
    request(`/api/v1/chats/${sessionId}/messages/${messageId}/pin`, {
      method: "PATCH",
      body: JSON.stringify({ is_pinned: isPinned }),
    }),
};
