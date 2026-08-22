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
  Workflow,
  Pencil,
} from "lucide-react";
import Link from "next/link";
import {
  usePipelines,
  useDeletePipeline,
  useRenamePipeline,
  useClearAllPipelines,
} from "@/hooks/useDocuments";
import { cn, formatDate } from "@/lib/utils";
import type { PipelineRunSummary, PipelineSource } from "@/lib/types";
import { ConfirmDialog } from "@/components/ui/ConfirmDialog";

export default function PipelinesPage() {
  const { data: runs, isFetching, refetch } = usePipelines({ limit: 50 });
  const { mutate: deleteRun, isPending: isDeletingRun } = useDeletePipeline();
  const { mutate: renameRun } = useRenamePipeline();
  const { mutate: clearAll, isPending: clearingAll } = useClearAllPipelines();

  const [clearAllOpen, setClearAllOpen] = useState(false);
  const [deleteRunTarget, setDeleteRunTarget] = useState<{ id: string; name: string } | null>(null);

  const handleConfirmClearAll = () => {
    clearAll(undefined, {
      onSettled: () => setClearAllOpen(false),
    });
  };

  const handleConfirmDeleteRun = () => {
    if (deleteRunTarget) {
      deleteRun(deleteRunTarget.id, {
        onSettled: () => setDeleteRunTarget(null),
      });
    }
  };

  return (
    <div className="max-w-6xl mx-auto space-y-6">
      {/* ── Page Header ───────────────────────────────────────────── */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl sm:text-2xl font-extrabold text-slate-900 dark:text-white tracking-tight flex items-center gap-2.5">
            <Workflow className="w-6 h-6 text-indigo-600 dark:text-indigo-400" />
            Pipelines History
          </h1>
          <p className="text-xs sm:text-sm text-slate-500 dark:text-slate-400 mt-1">
            Real-time tracking of ingestion stages across vector, table, clause, and graph stores.
          </p>
        </div>
      </div>

      {/* ── Table Card ────────────────────────────────────────────── */}
      <div className="bg-white dark:bg-[#202024]/80 backdrop-blur-xl rounded-2xl border border-slate-200 dark:border-white/[0.08] shadow-sm dark:shadow-2xl overflow-hidden transition-colors">
        <div className="px-6 py-4 border-b border-slate-200/80 dark:border-white/[0.07] bg-slate-50/70 dark:bg-white/[0.02] flex items-center justify-between">
          <div className="flex items-center gap-3">
            <h2 className="text-sm font-semibold text-slate-900 dark:text-white">Execution Logs</h2>
            <span className="text-[11px] font-mono font-medium text-indigo-700 dark:text-indigo-300 bg-indigo-50 dark:bg-indigo-500/15 border border-indigo-200 dark:border-indigo-500/30 rounded-full px-2.5 py-0.5">
              {runs?.total ?? 0} total
            </span>
          </div>

          <div className="flex items-center gap-2">
            {(runs?.total ?? 0) > 0 && (
              <button
                type="button"
                onClick={() => setClearAllOpen(true)}
                disabled={clearingAll}
                className="flex items-center gap-1.5 rounded-xl border border-red-200 dark:border-red-500/30 bg-red-50 dark:bg-red-500/10 px-3 py-1.5 text-xs font-medium text-red-600 dark:text-red-400 hover:bg-red-100 dark:hover:bg-red-500/20 disabled:opacity-50 transition-all"
              >
                {clearingAll ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Trash2 className="h-3.5 w-3.5" />}
                Clear History
              </button>
            )}
            <button
              type="button"
              onClick={() => refetch()}
              className="p-1.5 rounded-xl text-slate-400 dark:text-gray-400 hover:text-slate-700 dark:hover:text-white hover:bg-slate-100 dark:hover:bg-white/[0.06] transition-colors"
              title="Refresh runs"
            >
              <RefreshCw className={cn("h-4 w-4", isFetching && "animate-spin text-indigo-600 dark:text-indigo-400")} />
            </button>
          </div>
        </div>

        <div className="overflow-x-auto">
          <table className="w-full text-xs sm:text-sm">
            <thead>
              <tr className="text-left text-[11px] font-semibold uppercase tracking-wider text-slate-500 dark:text-slate-400 border-b border-slate-200/80 dark:border-white/[0.06] bg-slate-50/50 dark:bg-white/[0.01]">
                <th className="px-6 py-3.5">Pipeline Name</th>
                <th className="px-6 py-3.5">Source</th>
                <th className="px-6 py-3.5">Found</th>
                <th className="px-6 py-3.5">Processed</th>
                <th className="px-6 py-3.5">Failed</th>
                <th className="px-6 py-3.5">Status</th>
                <th className="px-6 py-3.5">Started</th>
                <th className="px-6 py-3.5"></th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100 dark:divide-white/[0.04]">
              {(runs?.items ?? []).map((r) => (
                <RunRow
                  key={r.id}
                  run={r}
                  onDelete={(id, name) => setDeleteRunTarget({ id, name })}
                  onRename={renameRun}
                />
              ))}
              {runs && runs.items.length === 0 && (
                <tr>
                  <td
                    colSpan={8}
                    className="px-6 py-12 text-center text-xs text-slate-400 dark:text-gray-500"
                  >
                    No pipeline runs recorded. Start by uploading documents.
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </div>

      {/* ── Clear All History Dialog ─────────────────────────────── */}
      <ConfirmDialog
        isOpen={clearAllOpen}
        onClose={() => setClearAllOpen(false)}
        onConfirm={handleConfirmClearAll}
        title="Clear All Pipeline History"
        description="Are you sure you want to clear all pipeline execution logs? Uploaded documents and their multi-store records will remain intact."
        confirmText="Clear History"
        cancelText="Cancel"
        variant="danger"
        isLoading={clearingAll}
      />

      {/* ── Delete Single Run Dialog ─────────────────────────────── */}
      <ConfirmDialog
        isOpen={!!deleteRunTarget}
        onClose={() => setDeleteRunTarget(null)}
        onConfirm={handleConfirmDeleteRun}
        title="Delete Pipeline Run"
        description={
          <span>
            Are you sure you want to delete the execution history for{" "}
            <strong className="font-semibold text-slate-900 dark:text-white">
              &ldquo;{deleteRunTarget?.name}&rdquo;
            </strong>
            ? Documents and stored records will be kept.
          </span>
        }
        confirmText="Delete"
        cancelText="Cancel"
        variant="danger"
        isLoading={isDeletingRun}
      />
    </div>
  );
}

const SOURCE_LABELS: Record<PipelineSource, string> = {
  local: "Local Disk",
  gdrive: "Google Drive",
  sharepoint: "SharePoint",
};

function RunRow({
  run,
  onDelete,
  onRename,
}: {
  run: PipelineRunSummary;
  onDelete: (id: string, name: string) => void;
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
    completed: "bg-emerald-50 text-emerald-700 border-emerald-200 dark:bg-emerald-500/10 dark:text-emerald-400 dark:border-emerald-500/20",
    failed: "bg-red-50 text-red-700 border-red-200 dark:bg-red-500/10 dark:text-red-400 dark:border-red-500/20",
    running: "bg-indigo-50 text-indigo-700 border-indigo-200 dark:bg-indigo-500/10 dark:text-indigo-300 dark:border-indigo-500/30 animate-pulse",
    empty: "bg-slate-100 text-slate-600 border-slate-200 dark:bg-gray-500/10 dark:text-gray-400 dark:border-gray-500/20",
  };

  const statusLabel: Record<PipelineRunSummary["status"], string> = {
    completed: "Completed",
    failed: "Failed",
    running: "Ingesting...",
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
    <tr className="hover:bg-slate-50/80 dark:hover:bg-white/[0.02] transition-colors group">
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
            className="w-full rounded-lg bg-white dark:bg-slate-900 border border-indigo-500 px-2.5 py-1 text-xs font-semibold text-slate-900 dark:text-white outline-none ring-2 ring-indigo-500/20"
          />
        ) : (
          <div className="flex items-center gap-2 min-w-0">
            <Link
              href={`/upload/${run.id}`}
              className="font-medium text-slate-800 dark:text-slate-200 truncate hover:text-indigo-600 dark:hover:text-indigo-400 transition-colors"
            >
              {run.name}
            </Link>
            <button
              type="button"
              title="Rename"
              onClick={() => { setDraft(run.name); setEditing(true); }}
              className="opacity-0 group-hover:opacity-100 text-slate-400 dark:text-slate-500 hover:text-indigo-600 dark:hover:text-indigo-300 transition-all p-0.5"
            >
              <Pencil className="h-3 w-3" />
            </button>
          </div>
        )}
      </td>

      <td className="px-6 py-4">
        <span className="inline-flex items-center gap-1.5 rounded-lg border border-slate-200 dark:border-white/[0.08] bg-slate-50 dark:bg-white/[0.02] px-2.5 py-1 text-[11px] text-slate-600 dark:text-slate-400">
          <Upload className="h-3 w-3" />
          {SOURCE_LABELS[run.source] ?? run.source}
        </span>
      </td>

      <td className="px-6 py-4 font-mono text-slate-700 dark:text-slate-300">{run.files_found}</td>
      <td className="px-6 py-4 font-mono text-emerald-600 dark:text-emerald-400">{run.files_processed}</td>
      <td className="px-6 py-4 font-mono text-red-600 dark:text-red-400">{run.files_failed}</td>

      <td className="px-6 py-4">
        <span
          className={cn(
            "inline-flex items-center gap-1.5 rounded-full border px-2.5 py-0.5 text-[11px] font-medium",
            statusStyle[run.status]
          )}
        >
          <StatusIcon
            className={cn("h-3 w-3", run.status === "running" && "animate-spin")}
          />
          {statusLabel[run.status]}
        </span>
      </td>

      <td className="px-6 py-4 text-slate-500 dark:text-slate-400 text-xs font-mono">
        {formatDate(run.started_at ?? run.created_at)}
      </td>

      <td className="px-6 py-4 text-right">
        <button
          type="button"
          onClick={() => onDelete(run.id, run.name)}
          className="text-slate-400 dark:text-slate-500 hover:text-red-600 dark:hover:text-red-400 transition-colors p-1"
          title="Delete pipeline run"
        >
          <Trash2 className="h-4 w-4" />
        </button>
      </td>
    </tr>
  );
}
