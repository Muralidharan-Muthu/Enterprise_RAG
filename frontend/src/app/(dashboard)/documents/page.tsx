"use client";

import { useState } from "react";
import { RefreshCw, Loader2, FileText } from "lucide-react";
import { useDocuments } from "@/hooks/useDocuments";
import { DocumentTable } from "@/components/documents/DocumentTable";
import { cn } from "@/lib/utils";

const TYPE_FILTERS = [
  { value: "", label: "All Document Types" },
  { value: "policy", label: "Policy Documents" },
  { value: "financial", label: "Financial Reports" },
  { value: "legal", label: "Legal Agreements" },
  { value: "research", label: "Research Papers" },
];

const STATUS_FILTERS = [
  { value: "", label: "All Statuses" },
  { value: "completed", label: "Completed" },
  { value: "failed", label: "Failed" },
];

export default function DocumentsPage() {
  const [typeFilter, setTypeFilter] = useState("");
  const [statusFilter, setStatusFilter] = useState("");
  const [page, setPage] = useState(1);

  const { data, isLoading, isFetching, refetch } = useDocuments({
    page,
    limit: 20,
    document_type: typeFilter || undefined,
    status: statusFilter || undefined,
  });

  return (
    <div className="max-w-6xl mx-auto space-y-6">
      {/* ── Page Header ───────────────────────────────────────────── */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl sm:text-2xl font-extrabold text-slate-900 dark:text-white tracking-tight flex items-center gap-2.5">
            <FileText className="w-6 h-6 text-indigo-600 dark:text-indigo-400" />
            Document Catalog
          </h1>
          <p className="text-xs sm:text-sm text-slate-500 dark:text-slate-400 mt-1">
            {data?.total ?? 0} document{data?.total !== 1 ? "s" : ""} indexed across vector, table, clause, and graph stores.
          </p>
        </div>
        <button
          onClick={() => refetch()}
          className="flex items-center gap-2 text-xs font-medium text-slate-700 dark:text-gray-300 hover:text-slate-900 dark:hover:text-white px-3.5 py-2 rounded-xl bg-white dark:bg-white/[0.04] border border-slate-200 dark:border-white/[0.08] hover:bg-slate-50 dark:hover:bg-white/[0.08] shadow-xs transition-all"
        >
          <RefreshCw className={cn("h-3.5 w-3.5", isFetching && "animate-spin text-indigo-600 dark:text-indigo-400")} />
          Refresh
        </button>
      </div>

      {/* ── Filter Toolbar ────────────────────────────────────────── */}
      <div className="flex items-center gap-3 flex-wrap">
        <div className="relative">
          <select
            value={typeFilter}
            onChange={(e) => { setTypeFilter(e.target.value); setPage(1); }}
            className="text-xs font-medium rounded-xl border border-slate-200 dark:border-white/[0.08] px-3.5 py-2 bg-white dark:bg-slate-900 text-slate-700 dark:text-slate-300 focus:outline-none focus:ring-2 focus:ring-indigo-500/30 focus:border-indigo-500 appearance-none pr-8 cursor-pointer shadow-xs"
          >
            {TYPE_FILTERS.map((f) => (
              <option key={f.value} value={f.value}>{f.label}</option>
            ))}
          </select>
        </div>

        <div className="relative">
          <select
            value={statusFilter}
            onChange={(e) => { setStatusFilter(e.target.value); setPage(1); }}
            className="text-xs font-medium rounded-xl border border-slate-200 dark:border-white/[0.08] px-3.5 py-2 bg-white dark:bg-slate-900 text-slate-700 dark:text-slate-300 focus:outline-none focus:ring-2 focus:ring-indigo-500/30 focus:border-indigo-500 appearance-none pr-8 cursor-pointer shadow-xs"
          >
            {STATUS_FILTERS.map((f) => (
              <option key={f.value} value={f.value}>{f.label}</option>
            ))}
          </select>
        </div>
      </div>

      {/* ── Main Content / Table ──────────────────────────────────── */}
      {isLoading ? (
        <div className="flex items-center justify-center py-24">
          <Loader2 className="h-7 w-7 animate-spin text-indigo-600 dark:text-indigo-400" />
        </div>
      ) : (
        <div className="space-y-4">
          <DocumentTable documents={data?.items ?? []} />

          {/* Pagination */}
          {data && data.pages > 1 && (
            <div className="flex items-center justify-between text-xs text-slate-500 dark:text-slate-400 pt-2 px-1">
              <span>
                Page {data.page} of {data.pages}
              </span>
              <div className="flex gap-2">
                <button
                  disabled={page <= 1}
                  onClick={() => setPage((p) => p - 1)}
                  className="px-3.5 py-1.5 rounded-xl border border-slate-200 dark:border-white/[0.08] bg-white dark:bg-white/[0.02] hover:bg-slate-50 dark:hover:bg-white/[0.06] text-slate-700 dark:text-slate-300 disabled:opacity-30 disabled:cursor-not-allowed transition-all shadow-xs"
                >
                  Previous
                </button>
                <button
                  disabled={page >= data.pages}
                  onClick={() => setPage((p) => p + 1)}
                  className="px-3.5 py-1.5 rounded-xl border border-slate-200 dark:border-white/[0.08] bg-white dark:bg-white/[0.02] hover:bg-slate-50 dark:hover:bg-white/[0.06] text-slate-700 dark:text-slate-300 disabled:opacity-30 disabled:cursor-not-allowed transition-all shadow-xs"
                >
                  Next
                </button>
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
