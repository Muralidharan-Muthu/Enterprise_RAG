"use client";

import { CheckCircle2, Circle, Loader2, XCircle, FileText } from "lucide-react";
import { cn, formatBytes } from "@/lib/utils";
import { useJobStatus } from "@/hooks/useJobStatus";
import type { PipelineStage } from "@/lib/types";
import { DOC_TYPE_COLORS, PIPELINE_STAGES } from "@/lib/types";

const STAGE_LABELS: Record<PipelineStage, string> = {
  queued: "Queued",
  parsing: "Parsing PDF",
  images: "Analysing images",
  routing: "Classifying document",
  chunking: "Chunking text",
  embedding: "Generating embeddings",
  storing: "Storing to database",
  graph: "Linking knowledge graph",
  done: "Complete",
  error: "Failed",
};

interface Props {
  jobId: string;
  filename: string;
  fileSize: number;
  onComplete?: (documentId: string) => void;
}

export function UploadProgress({ jobId, filename, fileSize, onComplete }: Props) {
  const { data: job, isLoading } = useJobStatus(jobId);

  if (job?.current_stage === "done" && onComplete && job.document_id) {
    onComplete(job.document_id);
  }

  const currentStageIdx = job
    ? PIPELINE_STAGES.indexOf(job.current_stage)
    : 0;

  const isError = job?.current_stage === "error";
  const isDone = job?.current_stage === "done";

  return (
    <div className="bg-white dark:bg-gray-900 rounded-xl border border-gray-200 dark:border-gray-800 p-5 space-y-4">
      {/* File info */}
      <div className="flex items-start gap-3">
        <FileText className="h-5 w-5 text-gray-400 dark:text-gray-500 mt-0.5 flex-shrink-0" />
        <div className="min-w-0 flex-1">
          <p className="text-sm font-medium text-gray-900 dark:text-gray-100 truncate">{filename}</p>
          <p className="text-xs text-gray-500 dark:text-gray-400">{formatBytes(fileSize)}</p>
        </div>
        {isDone && job?.document_id && (
          <span className="text-xs text-green-600 dark:text-green-300 font-medium">Done</span>
        )}
        {isError && (
          <span className="text-xs text-red-600 dark:text-red-300 font-medium">Failed</span>
        )}
      </div>

      {/* Pipeline stages */}
      <div className="space-y-2">
        {(["parsing", "routing", "chunking", "embedding", "storing"] as PipelineStage[]).map(
          (stage, idx) => {
            const stageIdx = PIPELINE_STAGES.indexOf(stage);
            const isPast = currentStageIdx > stageIdx;
            const isCurrent = job?.current_stage === stage;
            const isPending = currentStageIdx < stageIdx && !isError;

            return (
              <div key={stage} className="flex items-center gap-3">
                <div className="flex-shrink-0">
                  {isError && isCurrent ? (
                    <XCircle className="h-4 w-4 text-red-500" />
                  ) : isPast || isDone ? (
                    <CheckCircle2 className="h-4 w-4 text-green-500" />
                  ) : isCurrent ? (
                    <Loader2 className="h-4 w-4 text-blue-500 animate-spin" />
                  ) : (
                    <Circle className="h-4 w-4 text-gray-300" />
                  )}
                </div>
                <span
                  className={cn(
                    "text-sm",
                    isPast || isDone
                      ? "text-gray-700 dark:text-gray-300"
                      : isCurrent
                      ? "text-blue-700 dark:text-blue-300 font-medium"
                      : "text-gray-400 dark:text-gray-500"
                  )}
                >
                  {STAGE_LABELS[stage]}
                  {isCurrent && job?.stage_progress > 0 && ` (${job.stage_progress}%)`}
                </span>

                {/* Routing result badge */}
                {stage === "routing" && (isPast || isDone) && job && (
                  <span className="ml-auto text-xs">
                    {/* Document type shown after routing is done — from document detail */}
                  </span>
                )}

                {/* Chunk count */}
                {stage === "chunking" && (isPast || isDone) && job?.total_chunks && (
                  <span className="ml-auto text-xs text-gray-500 dark:text-gray-400">
                    {job.total_chunks} chunks
                  </span>
                )}
              </div>
            );
          }
        )}
      </div>

      {/* Error message */}
      {isError && job?.error_message && (
        <p className="text-xs text-red-600 dark:text-red-300 bg-red-50 dark:bg-red-950 rounded p-2">
          {job.error_message}
        </p>
      )}

      {/* Timing */}
      {isDone && job?.duration_seconds && (
        <p className="text-xs text-gray-400 dark:text-gray-500">
          Completed in {job.duration_seconds.toFixed(1)}s
        </p>
      )}
    </div>
  );
}
