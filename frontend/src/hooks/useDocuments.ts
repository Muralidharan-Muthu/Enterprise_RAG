"use client";

import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { apiClient } from "@/lib/api-client";
import type { ChunkStore } from "@/lib/types";

export function useDocuments(params?: {
  page?: number;
  limit?: number;
  status?: string;
  document_type?: string;
}) {
  return useQuery({
    queryKey: ["documents", params],
    queryFn: () => apiClient.listDocuments(params),
  });
}

export function usePipelines(params?: { page?: number; limit?: number }) {
  return useQuery({
    queryKey: ["pipelines", params],
    queryFn: () => apiClient.listPipelines(params),
    // Poll so the sidebar "running" badge and the list reflect in-flight runs.
    refetchInterval: 5000,
    refetchIntervalInBackground: true,
  });
}

// Count of pipeline runs currently processing — drives the sidebar spinner badge.
export function useRunningPipelineCount(): number {
  const { data } = usePipelines({ page: 1, limit: 50 });
  return (data?.items ?? []).filter((p) => p.status === "running").length;
}

export function usePipeline(runId: string | null) {
  return useQuery({
    queryKey: ["pipeline", runId],
    queryFn: () => apiClient.getPipeline(runId!),
    enabled: !!runId,
    retry: 3,
    retryDelay: (attemptIndex) => Math.min(1000 * 2 ** attemptIndex, 4000),
    // Poll smoothly every 2s while running without hammering single-process CPU
    refetchInterval: (query) => {
      const status = query.state.data?.status;
      return status === "running" ? 2000 : false;
    },
    refetchIntervalInBackground: true,
  });
}

export function useDocument(id: string | null) {
  return useQuery({
    queryKey: ["document", id],
    queryFn: () => apiClient.getDocument(id!),
    enabled: !!id,
  });
}

export function usePageStats(id: string | null) {
  return useQuery({
    queryKey: ["page-stats", id],
    queryFn: () => apiClient.getPageStats(id!),
    enabled: !!id,
  });
}

export function useDocumentImages(
  id: string | null,
  refetchInterval: number | false = false,
) {
  return useQuery({
    queryKey: ["document-images", id],
    queryFn: () => apiClient.getDocumentImages(id!),
    enabled: !!id,
    // Poll while the images stage is live so each figure appears the moment the
    // worker stores it, instead of all at once when the stage finishes.
    refetchInterval,
    refetchIntervalInBackground: true,
  });
}

export function useDocumentChunks(
  id: string | null,
  store: ChunkStore = "vector",
  enabled = true,
  refetchInterval: number | false = false,
  page = 1,
  limit = 50,
) {
  return useQuery({
    queryKey: ["document-chunks", id, store, page, limit],
    queryFn: () => apiClient.getDocumentChunks(id!, store, page, limit),
    enabled: !!id && enabled,
    refetchInterval,
    refetchIntervalInBackground: true,
  });
}

export function useDeleteDocument() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => apiClient.deleteDocument(id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["documents"] });
    },
  });
}

export function useReprocessDocument(options?: {
  onSuccess?: (data: { document_id: string; job_id: string; pipeline_run_id: string; status: string }) => void;
}) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => apiClient.reprocessDocument(id),
    onSuccess: (data) => {
      qc.invalidateQueries({ queryKey: ["documents"] });
      options?.onSuccess?.(data);
    },
  });
}

export function useDeletePipeline() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (runId: string) => apiClient.deletePipeline(runId),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["pipelines"] });
      qc.invalidateQueries({ queryKey: ["documents"] });
    },
  });
}

export function useRenamePipeline() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ runId, name }: { runId: string; name: string }) =>
      apiClient.renamePipeline(runId, name),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["pipelines"] });
    },
  });
}

export function useClearAllPipelines() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => apiClient.clearAllPipelines(),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["pipelines"] });
      qc.invalidateQueries({ queryKey: ["documents"] });
    },
  });
}
