"use client";

import { useState, useRef, useEffect } from "react";
import {
  Upload,
  RefreshCw,
  CheckCircle2,
  XCircle,
  Loader2,
  Clock,
  Trash2,
} from "lucide-react";
import Link from "next/link";
import { usePipelines, useDeletePipeline, useRenamePipeline, useClearAllPipelines } from "@/hooks/useDocuments";
import { cn, formatDate } from "@/lib/utils";
import type { PipelineRunSummary, PipelineSource } from "@/lib/types";

export default function PipelinesPage() {
  const { data: runs, isFetching, refetch } = usePipelines({ limit: 50 });
  const { mutate: deleteRun } = useDeletePipeline();
  const { mutate: renameRun } = useRenamePipeline();
  const { mutate: clearAll, isPending: clearingAll } = useClearAllPipelines();

  return (
    <div className="max-w-6xl mx-auto space-y-6">
      {/* ── Page header ───────────────────────────────────────────── */}
      <div className="flex items-start justify-between">
        <div>
          <h1 className="text-2xl font-bold text-gray-900 dark:text-gray-100">
            Pipelines
          </h1>
          <p className="text-sm text-gray-500 dark:text-gray-400 mt-1">
            Track the status and history of every ingestion run
          </p>
        </div>
      </div>

      {/* ── Pipeline Run History ──────────────────────────────────── */}
      <div className="bg-white dark:bg-gray-900 rounded-2xl border border-gray-200 dark:border-gray-800 shadow-sm">
        <div className="px-6 py-4 border-b border-gray-100 dark:border-gray-800 flex items-center justify-between">
          <h2 className="text-base font-semibold text-gray-900 dark:text-gray-100">
            Pipeline Run History
          </h2>
          <div className="flex items-center gap-3">
            <span className="text-xs font-medium text-gray-500 dark:text-gray-400 bg-gray-100 dark:bg-gray-800 rounded-full px-2.5 py-1">
              {runs?.total ?? 0} runs
            </span>
            {(runs?.total ?? 0) > 0 && (
              <button
                type="button"
                onClick={() => {
                  if (confirm("Clear all pipeline history? Documents and their chunks will be kept.")) {
                    clearAll();
                  }
                }}
                disabled={clearingAll}
                className="flex items-center gap-1.5 rounded-lg border border-red-200 dark:border-red-900 bg-red-50 dark:bg-red-950 px-3 py-1.5 text-xs font-medium text-red-600 dark:text-red-300 hover:bg-red-100 dark:hover:bg-red-900 disabled:opacity-50 transition-colors"
              >
                {clearingAll ? <Loader2 className="h-3 w-3 animate-spin" /> : <Trash2 className="h-3 w-3" />}
                Clear All
              </button>
            )}
            <button
              type="button"
              onClick={() => refetch()}
              className="text-gray-400 dark:text-gray-500 hover:text-gray-600"
            >
              <RefreshCw
                className={cn("h-4 w-4", isFetching && "animate-spin")}
              />
            </button>
          </div>
        </div>

        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="text-left text-xs font-semibold uppercase tracking-wide text-gray-400 dark:text-gray-500 border-b border-gray-100 dark:border-gray-800">
                <th className="px-6 py-3">Name</th>
                <th className="px-6 py-3">Source</th>
                <th className="px-6 py-3">Found</th>
                <th className="px-6 py-3">Processed</th>
                <th className="px-6 py-3">Failed</th>
                <th className="px-6 py-3">Status</th>
                <th className="px-6 py-3">Started</th>
                <th className="px-6 py-3"></th>
              </tr>
            </thead>
            <tbody>
              {(runs?.items ?? []).map((r) => (
                <RunRow key={r.id} run={r} onDelete={deleteRun} onRename={renameRun} />
              ))}
              {runs && runs.items.length === 0 && (
                <tr>
                  <td
                    colSpan={8}
                    className="px-6 py-10 text-center text-sm text-gray-400 dark:text-gray-500"
                  >
                    No pipeline runs yet. Upload files to get started.
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}

// ── Sub-components ────────────────────────────────────────────────────────

const SOURCE_LABELS: Record<PipelineSource, string> = {
  local: "Local",
  gdrive: "Google Drive",
  sharepoint: "SharePoint",
};

function RunRow({
  run,
  onDelete,
  onRename,
}: {
  run: PipelineRunSummary;
  onDelete: (id: string) => void;
  onRename: (args: { runId: string; name: string }) => void;
}) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(run.name);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (editing) inputRef.current?.select();
  }, [editing]);

  const commitRename = () => {
    const name = draft.trim();
    if (name && name !== run.name) onRename({ runId: run.id, name });
    setEditing(false);
  };
  const statusStyle: Record<PipelineRunSummary["status"], string> = {
    completed: "bg-green-50 dark:bg-green-950 text-green-700 dark:text-green-300 border-green-200 dark:border-green-900",
    failed: "bg-red-50 dark:bg-red-950 text-red-700 dark:text-red-300 border-red-200 dark:border-red-900",
    running: "bg-blue-50 dark:bg-blue-950 text-blue-700 dark:text-blue-300 border-blue-200 dark:border-blue-900",
    empty: "bg-gray-50 dark:bg-gray-800/50 text-gray-500 dark:text-gray-400 border-gray-200 dark:border-gray-800",
  };
  const statusLabel: Record<PipelineRunSummary["status"], string> = {
    completed: "Completed",
    failed: "Failed",
    running: "Processing",
    empty: "Empty",
  };
  const StatusIcon =
    run.status === "running"
      ? Loader2
      : run.status === "failed"
      ? XCircle
      : run.status === "empty"
      ? Clock
      : CheckCircle2;

  return (
    <tr className="border-b border-gray-50 dark:border-gray-800 hover:bg-gray-50 dark:hover:bg-gray-800">
      <td className="px-6 py-4 max-w-[220px]">
        {editing ? (
          <input
            ref={inputRef}
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onBlur={commitRename}
            onKeyDown={(e) => {
              if (e.key === "Enter") commitRename();
              if (e.key === "Escape") { setDraft(run.name); setEditing(false); }
            }}
            className="w-full rounded border border-blue-400 px-2 py-0.5 text-sm font-semibold text-gray-900 dark:text-gray-100 outline-none ring-1 ring-blue-400"
          />
        ) : (
          <div className="flex items-center gap-1 min-w-0">
            <Link
              href={`/upload/${run.id}`}
              className="font-semibold text-gray-900 dark:text-gray-100 truncate hover:text-blue-600 hover:underline"
            >
              {run.name}
            </Link>
            <button
              type="button"
              title="Rename"
              onClick={() => { setDraft(run.name); setEditing(true); }}
              className="flex-shrink-0 text-gray-300 hover:text-gray-500"
            >
              <svg className="h-3 w-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M15.232 5.232l3.536 3.536M9 13l6.586-6.586a2 2 0 012.828 2.828L11.828 15.828a2 2 0 01-1.414.586H9v-1.414a2 2 0 01.586-1.414z" />
              </svg>
            </button>
          </div>
        )}
      </td>
      <td className="px-6 py-4">
        <span className="inline-flex items-center gap-1.5 rounded-md border border-gray-200 dark:border-gray-800 bg-gray-50 dark:bg-gray-800/50 px-2 py-1 text-xs text-gray-600 dark:text-gray-300">
          <Upload className="h-3 w-3" />
          {SOURCE_LABELS[run.source] ?? run.source}
        </span>
      </td>
      <td className="px-6 py-4 text-orange-600 dark:text-orange-300">{run.files_found}</td>
      <td className="px-6 py-4 text-orange-600 dark:text-orange-300">{run.files_processed}</td>
      <td className="px-6 py-4 text-gray-700 dark:text-gray-300">{run.files_failed}</td>
      <td className="px-6 py-4">
        <span
          className={cn(
            "inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-xs font-medium",
            statusStyle[run.status]
          )}
        >
          <StatusIcon
            className={cn(
              "h-3 w-3",
              run.status === "running" && "animate-spin"
            )}
          />
          {statusLabel[run.status]}
        </span>
      </td>
      <td className="px-6 py-4 text-gray-500 dark:text-gray-400">
        {formatDate(run.started_at ?? run.created_at)}
      </td>
      <td className="px-6 py-4">
        <button
          type="button"
          onClick={() => {
            if (confirm(`Delete history for pipeline "${run.name}"? Documents will be kept.`)) {
              onDelete(run.id);
            }
          }}
          className="text-gray-400 dark:text-gray-500 hover:text-red-600 transition-colors"
          title="Delete pipeline run"
        >
          <Trash2 className="h-4 w-4" />
        </button>
      </td>
    </tr>
  );
}
