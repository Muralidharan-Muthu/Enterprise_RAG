"use client";

import { useQuery } from "@tanstack/react-query";
import { apiClient } from "@/lib/api-client";
import type { JobStatus } from "@/lib/types";

const TERMINAL_STAGES = new Set(["done", "error"]);

export function useJobStatus(jobId: string | null) {
  return useQuery<JobStatus>({
    queryKey: ["job", jobId],
    queryFn: () => apiClient.getJobStatus(jobId!),
    enabled: !!jobId,
    refetchInterval: (query) => {
      const stage = query.state.data?.current_stage;
      return stage && TERMINAL_STAGES.has(stage) ? false : 2000;
    },
  });
}
