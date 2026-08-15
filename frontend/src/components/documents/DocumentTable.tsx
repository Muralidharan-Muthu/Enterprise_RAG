"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { Trash2, ExternalLink, Loader2, PlayCircle } from "lucide-react";
import { cn, formatDate, formatBytes, capitalize } from "@/lib/utils";
import type { DocumentSummary, DocumentStatus, DocumentType } from "@/lib/types";
import { DOC_TYPE_COLORS } from "@/lib/types";
import { useDeleteDocument, useReprocessDocument } from "@/hooks/useDocuments";

interface Props {
  documents: DocumentSummary[];
}

const STATUS_STYLES: Record<DocumentStatus, string> = {
  uploaded: "bg-gray-100 dark:bg-gray-800 text-gray-600 dark:text-gray-300",
  parsing: "bg-yellow-100 dark:bg-yellow-950 text-yellow-700 dark:text-yellow-300",
  routing: "bg-yellow-100 dark:bg-yellow-950 text-yellow-700 dark:text-yellow-300",
  chunking: "bg-yellow-100 dark:bg-yellow-950 text-yellow-700 dark:text-yellow-300",
  embedding: "bg-yellow-100 dark:bg-yellow-950 text-yellow-700 dark:text-yellow-300",
  storing: "bg-yellow-100 dark:bg-yellow-950 text-yellow-700 dark:text-yellow-300",
  completed: "bg-green-100 dark:bg-green-950 text-green-700 dark:text-green-300",
  failed: "bg-red-100 dark:bg-red-950 text-red-700 dark:text-red-300",
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
      <div className="text-center py-16 text-gray-400 dark:text-gray-500">
        <p className="text-sm">No documents yet. Upload a PDF to get started.</p>
      </div>
    );
  }

  return (
    <div className="overflow-x-auto rounded-xl border border-gray-200 dark:border-gray-800">
      <table className="min-w-full divide-y divide-gray-200">
        <thead className="bg-gray-50 dark:bg-gray-800/50">
          <tr>
            {["Document", "Type", "Status", "Chunks", "Pages", "Uploaded"].map((h) => (
              <th
                key={h}
                className="px-4 py-3 text-left text-xs font-medium text-gray-500 dark:text-gray-400 uppercase tracking-wide"
              >
                {h}
              </th>
            ))}
            <th className="px-4 py-3" />
          </tr>
        </thead>
        <tbody className="bg-white dark:bg-gray-900 divide-y divide-gray-100">
          {documents.map((doc) => {
            const isProcessing = PROCESSING_STATUSES.includes(doc.status);
            const totalChunks =
              doc.vector_chunks + doc.table_count + doc.clause_count + doc.research_chunks;

            return (
              <tr key={doc.id} className="hover:bg-gray-50 dark:hover:bg-gray-800 transition-colors">
                {/* Document name */}
                <td className="px-4 py-3 max-w-xs">
                  <Link
                    href={`/documents/${doc.id}`}
                    className="text-sm font-medium text-gray-900 dark:text-gray-100 hover:text-blue-600 flex items-center gap-1 truncate"
                  >
                    <span className="truncate">{doc.original_filename}</span>
                    <ExternalLink className="h-3 w-3 flex-shrink-0 opacity-50" />
                  </Link>
                  {doc.doc_title && doc.doc_title !== doc.original_filename && (
                    <p className="text-xs text-gray-400 dark:text-gray-500 truncate mt-0.5">{doc.doc_title}</p>
                  )}
                </td>

                {/* Type badge */}
                <td className="px-4 py-3">
                  {doc.document_type ? (
                    <span
                      className={cn(
                        "inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium border",
                        DOC_TYPE_COLORS[doc.document_type as DocumentType]
                      )}
                    >
                      {capitalize(doc.document_type)}
                    </span>
                  ) : (
                    <span className="text-xs text-gray-400 dark:text-gray-500">—</span>
                  )}
                </td>

                {/* Status badge */}
                <td className="px-4 py-3">
                  <span
                    className={cn(
                      "inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs font-medium",
                      STATUS_STYLES[doc.status]
                    )}
                  >
                    {isProcessing && (
                      <Loader2 className="h-3 w-3 animate-spin" />
                    )}
                    {capitalize(doc.status)}
                  </span>
                </td>

                {/* Chunks */}
                <td className="px-4 py-3 text-sm text-gray-600 dark:text-gray-300">
                  {doc.status === "completed" ? (
                    <div className="space-y-0.5">
                      {doc.vector_chunks > 0 && (
                        <div className="text-xs text-blue-600 dark:text-blue-300">{doc.vector_chunks} vectors</div>
                      )}
                      {doc.table_count > 0 && (
                        <div className="text-xs text-green-600 dark:text-green-300">{doc.table_count} tables</div>
                      )}
                      {doc.clause_count > 0 && (
                        <div className="text-xs text-purple-600 dark:text-purple-300">{doc.clause_count} clauses</div>
                      )}
                      {doc.research_chunks > 0 && (
                        <div className="text-xs text-orange-600 dark:text-orange-300">{doc.research_chunks} chunks</div>
                      )}
                    </div>
                  ) : (
                    <span className="text-gray-400 dark:text-gray-500">—</span>
                  )}
                </td>

                {/* Pages */}
                <td className="px-4 py-3 text-sm text-gray-600 dark:text-gray-300">
                  {doc.page_count ?? "—"}
                </td>

                {/* Uploaded */}
                <td className="px-4 py-3 text-xs text-gray-500 dark:text-gray-400 whitespace-nowrap">
                  {formatDate(doc.created_at)}
                </td>

                {/* Actions */}
                <td className="px-4 py-3 text-right">
                  <div className="flex items-center justify-end gap-1">
                    <button
                      onClick={() => reprocessDoc(doc.id)}
                      disabled={isReprocessing || isProcessing}
                      className="text-gray-400 dark:text-gray-500 hover:text-green-600 transition-colors p-1 disabled:opacity-40"
                      title="Re-process document"
                    >
                      {isReprocessing && reprocessingId === doc.id
                        ? <Loader2 className="h-4 w-4 animate-spin" />
                        : <PlayCircle className="h-4 w-4" />}
                    </button>
                    <button
                      onClick={() => {
                        if (confirm(`Delete "${doc.original_filename}"?`)) {
                          deleteDoc(doc.id);
                        }
                      }}
                      disabled={isDeleting}
                      className="text-gray-400 dark:text-gray-500 hover:text-red-500 transition-colors p-1"
                      title="Delete document"
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
  );
}
