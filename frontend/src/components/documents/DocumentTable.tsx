"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { Trash2, ExternalLink, Loader2, RotateCcw, FileText, CheckCircle2, XCircle } from "lucide-react";
import { cn, formatDate, formatBytes, capitalize } from "@/lib/utils";
import type { DocumentSummary, DocumentStatus, DocumentType } from "@/lib/types";
import { DOC_TYPE_COLORS } from "@/lib/types";
import { useDeleteDocument, useReprocessDocument } from "@/hooks/useDocuments";

interface Props {
  documents: DocumentSummary[];
}

const STATUS_STYLES: Record<DocumentStatus, string> = {
  uploaded: "bg-slate-100 text-slate-600 border-slate-200 dark:bg-gray-500/10 dark:text-gray-400 dark:border-gray-500/20",
  parsing: "bg-amber-50 text-amber-700 border-amber-200 dark:bg-amber-500/10 dark:text-amber-300 dark:border-amber-500/20 animate-pulse",
  routing: "bg-amber-50 text-amber-700 border-amber-200 dark:bg-amber-500/10 dark:text-amber-300 dark:border-amber-500/20 animate-pulse",
  chunking: "bg-amber-50 text-amber-700 border-amber-200 dark:bg-amber-500/10 dark:text-amber-300 dark:border-amber-500/20 animate-pulse",
  embedding: "bg-amber-50 text-amber-700 border-amber-200 dark:bg-amber-500/10 dark:text-amber-300 dark:border-amber-500/20 animate-pulse",
  storing: "bg-amber-50 text-amber-700 border-amber-200 dark:bg-amber-500/10 dark:text-amber-300 dark:border-amber-500/20 animate-pulse",
  completed: "bg-emerald-50 text-emerald-700 border-emerald-200 dark:bg-emerald-500/10 dark:text-emerald-400 dark:border-emerald-500/20",
  failed: "bg-red-50 text-red-700 border-red-200 dark:bg-red-500/10 dark:text-red-400 dark:border-red-500/20",
};

const PROCESSING_STATUSES: DocumentStatus[] = [
  "parsing", "routing", "chunking", "embedding", "storing",
];

export function DocumentTable({ documents }: Props) {
  const router = useRouter();
  const { mutate: deleteDoc, isPending: isDeleting } = useDeleteDocument();
  const { mutate: reprocessDoc, isPending: isReprocessing, variables: reprocessingId } = useReprocessDocument({
    onSuccess: (data) => router.push(`/upload/${data.pipeline_run_id}`),
  });

  if (documents.length === 0) {
    return (
      <div className="text-center py-16 text-slate-400 dark:text-gray-500 bg-white dark:bg-[#0f172a]/40 rounded-2xl border border-slate-200 dark:border-white/[0.06] shadow-xs">
        <FileText className="w-10 h-10 mx-auto text-slate-300 dark:text-gray-600 mb-3" />
        <p className="text-sm font-medium text-slate-600 dark:text-gray-400">No documents match the selected filters.</p>
      </div>
    );
  }

  return (
    <div className="bg-white dark:bg-[#202024]/80 backdrop-blur-xl rounded-2xl border border-slate-200 dark:border-white/[0.08] shadow-sm dark:shadow-2xl overflow-hidden transition-colors">
      <div className="overflow-x-auto">
        <table className="w-full text-xs sm:text-sm">
          <thead>
            <tr className="text-left text-[11px] font-semibold uppercase tracking-wider text-slate-500 dark:text-slate-400 border-b border-slate-200/80 dark:border-white/[0.06] bg-slate-50/50 dark:bg-white/[0.01]">
              <th className="px-6 py-3.5">Document</th>
              <th className="px-6 py-3.5">Store Partition</th>
              <th className="px-6 py-3.5">Status</th>
              <th className="px-6 py-3.5">Chunks</th>
              <th className="px-6 py-3.5">Pages</th>
              <th className="px-6 py-3.5">Ingested</th>
              <th className="px-6 py-3.5 text-right">Actions</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-100 dark:divide-white/[0.04]">
            {documents.map((doc) => {
              const isProcessing = PROCESSING_STATUSES.includes(doc.status);
              const totalChunks =
                (doc.vector_chunks ?? 0) + (doc.table_count ?? 0) + (doc.clause_count ?? 0);

              return (
                <tr key={doc.id} className="hover:bg-slate-50/80 dark:hover:bg-white/[0.02] transition-colors group">
                  {/* Document Name */}
                  <td className="px-6 py-4 max-w-xs">
                    <Link
                      href={`/documents/${doc.id}`}
                      className="font-medium text-slate-800 dark:text-slate-200 hover:text-indigo-600 dark:hover:text-indigo-400 flex items-center gap-1.5 truncate transition-colors"
                    >
                      <span className="truncate">{doc.original_filename}</span>
                      <ExternalLink className="h-3 w-3 flex-shrink-0 opacity-40 group-hover:opacity-100" />
                    </Link>
                    {doc.doc_title && doc.doc_title !== doc.original_filename && (
                      <p className="text-[11px] text-slate-400 dark:text-gray-400 truncate mt-0.5">{doc.doc_title}</p>
                    )}
                  </td>

                  {/* Document Type Badge */}
                  <td className="px-6 py-4">
                    {doc.document_type ? (
                      <span
                        className={cn(
                          "inline-flex items-center px-2.5 py-0.5 rounded-full text-[11px] font-medium border",
                          DOC_TYPE_COLORS[doc.document_type as DocumentType] || "bg-slate-100 text-slate-700 border-slate-200 dark:bg-gray-800 dark:text-gray-300 dark:border-gray-700"
                        )}
                      >
                        {capitalize(doc.document_type)}
                      </span>
                    ) : (
                      <span className="text-xs text-slate-400 dark:text-gray-500">—</span>
                    )}
                  </td>

                  {/* Status Badge */}
                  <td className="px-6 py-4">
                    <span
                      className={cn(
                        "inline-flex items-center gap-1.5 px-2.5 py-0.5 rounded-full text-[11px] font-medium border",
                        STATUS_STYLES[doc.status] || "bg-slate-100 text-slate-600 dark:bg-gray-800 dark:text-gray-400"
                      )}
                    >
                      {isProcessing ? (
                        <Loader2 className="h-3 w-3 animate-spin" />
                      ) : doc.status === "completed" ? (
                        <CheckCircle2 className="h-3 w-3" />
                      ) : (
                        <XCircle className="h-3 w-3" />
                      )}
                      {capitalize(doc.status)}
                    </span>
                  </td>

                  {/* Chunks */}
                  <td className="px-6 py-4 font-mono text-slate-700 dark:text-gray-300 text-xs">
                    {totalChunks > 0 ? (
                      <span title={`Vector: ${doc.vector_chunks}, Tables: ${doc.table_count}, Clauses: ${doc.clause_count}, Research: ${doc.research_chunks}`}>
                        {totalChunks}
                      </span>
                    ) : (
                      <span className="text-slate-400 dark:text-gray-500">—</span>
                    )}
                  </td>

                  {/* Pages */}
                  <td className="px-6 py-4 font-mono text-slate-700 dark:text-gray-300 text-xs">
                    {doc.page_count ?? "—"}
                  </td>

                  {/* Upload Date */}
                  <td className="px-6 py-4 text-slate-500 dark:text-gray-400 text-xs font-mono">
                    {formatDate(doc.created_at)}
                  </td>

                  {/* Actions */}
                  <td className="px-6 py-4 text-right">
                    <div className="flex items-center justify-end gap-2">
                      {doc.status === "failed" && (
                        <button
                          type="button"
                          onClick={() => reprocessDoc(doc.id)}
                          disabled={isReprocessing && reprocessingId === doc.id}
                          className="text-amber-600 dark:text-amber-400 hover:text-amber-700 dark:hover:text-amber-300 p-1 transition-colors"
                          title="Reprocess Document"
                        >
                          <RotateCcw className="h-4 w-4" />
                        </button>
                      )}
                      <button
                        type="button"
                        onClick={() => {
                          if (confirm(`Delete document "${doc.original_filename}"? All chunks and embeddings will be removed.`)) {
                            deleteDoc(doc.id);
                          }
                        }}
                        disabled={isDeleting}
                        className="text-slate-400 hover:text-red-600 dark:text-gray-500 dark:hover:text-red-400 p-1 transition-colors"
                        title="Delete Document"
                      >
                        <Trash2 className="h-4 w-4" />
                      </button>
                    </div>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}
