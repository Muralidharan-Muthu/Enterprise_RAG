"use client";

import { useParams, useRouter } from "next/navigation";
import { ArrowLeft, FileText, Loader2, AlertCircle, Calendar, User, BookOpen } from "lucide-react";
import Link from "next/link";
import { useDocument } from "@/hooks/useDocuments";
import { ChunkViewer } from "@/components/documents/ChunkViewer";
import { cn, formatDate, capitalize } from "@/lib/utils";
import type { DocumentType } from "@/lib/types";
import { DOC_TYPE_COLORS } from "@/lib/types";

export default function DocumentDetailPage() {
  const params = useParams();
  const documentId = params.id as string;
  const { data: doc, isLoading, error } = useDocument(documentId);

  if (isLoading) {
    return (
      <div className="flex items-center justify-center py-24">
        <Loader2 className="h-6 w-6 animate-spin text-gray-400 dark:text-gray-300" />
      </div>
    );
  }

  if (error || !doc) {
    return (
      <div className="max-w-xl mx-auto py-16 text-center space-y-3">
        <AlertCircle className="h-8 w-8 text-red-400 mx-auto" />
        <p className="text-sm text-gray-600 dark:text-gray-300">Document not found or failed to load.</p>
        <Link href="/documents" className="text-sm text-blue-600 hover:underline">
          ← Back to Documents
        </Link>
      </div>
    );
  }

  const totalChunks =
    doc.vector_chunks + doc.table_count + doc.clause_count + doc.research_chunks;

  return (
    <div className="space-y-6">
      {/* Back nav */}
      <Link
        href="/documents"
        className="inline-flex items-center gap-1 text-sm text-gray-500 dark:text-gray-300 hover:text-gray-700 dark:text-gray-300"
      >
        <ArrowLeft className="h-3.5 w-3.5" />
        Documents
      </Link>

      {/* ── Row 1: document statistics  |  summary + reasoning (equal height) ── */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6 items-stretch">
        {/* Left: document metadata */}
        <div className="lg:col-span-1">
          <div className="bg-white dark:bg-[#202024]/80 rounded-2xl border border-slate-200 dark:border-white/[0.08] shadow-sm dark:shadow-xl p-5 space-y-4 h-full">
            {/* Filename */}
            <div className="flex items-start gap-3">
              <FileText className="h-5 w-5 text-gray-400 dark:text-gray-300 mt-0.5" />
              <div>
                <p className="text-sm font-semibold text-gray-900 dark:text-gray-100 break-all">
                  {doc.original_filename}
                </p>
                {doc.doc_title && doc.doc_title !== doc.original_filename && (
                  <p className="text-xs text-gray-500 dark:text-gray-300 mt-0.5">{doc.doc_title}</p>
                )}
              </div>
            </div>

            {/* Type badge */}
            {doc.document_type && (
              <div>
                <p className="text-xs text-gray-400 dark:text-gray-300 mb-1">Document Type</p>
                <div className="flex items-center gap-2">
                  <span
                    className={cn(
                      "inline-flex items-center px-2.5 py-1 rounded-full text-sm font-medium border",
                      DOC_TYPE_COLORS[doc.document_type as DocumentType]
                    )}
                  >
                    {capitalize(doc.document_type)}
                    {doc.document_subtype && ` · ${doc.document_subtype}`}
                  </span>
                </div>
                {doc.router_confidence !== null && (
                  <div className="mt-2">
                    <div className="flex justify-between text-xs text-gray-500 dark:text-gray-300 mb-1">
                      <span>Confidence</span>
                      <span>{(doc.router_confidence * 100).toFixed(0)}%</span>
                    </div>
                    <div className="h-1.5 bg-gray-100 dark:bg-gray-800 rounded-full overflow-hidden">
                      <div
                        className="h-full bg-blue-500 rounded-full"
                        style={{ width: `${doc.router_confidence * 100}%` }}
                      />
                    </div>
                  </div>
                )}
              </div>
            )}

            {/* Stats */}
            <div className="grid grid-cols-2 gap-3">
              <Stat label="Pages" value={doc.page_count ?? "—"} />
              <Stat label="Words" value={doc.word_count?.toLocaleString() ?? "—"} />
              <Stat label="Chunks" value={totalChunks} />
              <Stat label="Status" value={capitalize(doc.status)} />
            </div>

            {/* Author / Date */}
            {(doc.doc_author || doc.doc_date) && (
              <div className="space-y-1 text-xs text-gray-500 dark:text-gray-300">
                {doc.doc_author && (
                  <div className="flex items-center gap-1.5">
                    <User className="h-3.5 w-3.5" />
                    {doc.doc_author}
                  </div>
                )}
                {doc.doc_date && (
                  <div className="flex items-center gap-1.5">
                    <Calendar className="h-3.5 w-3.5" />
                    {doc.doc_date}
                  </div>
                )}
              </div>
            )}

            {/* Uploaded at */}
            <p className="text-xs text-gray-400 dark:text-gray-300">
              Uploaded {formatDate(doc.created_at)}
            </p>
          </div>
        </div>

        {/* Right: summary + AI classification reasoning */}
        <div className="lg:col-span-2 flex flex-col gap-4">
          {/* Summary */}
          {doc.doc_summary && (
            <div className="bg-white dark:bg-[#202024]/80 rounded-2xl border border-slate-200 dark:border-white/[0.08] shadow-sm dark:shadow-xl p-5 flex-1">
              <p className="text-xs font-medium text-gray-500 dark:text-gray-300 mb-2 flex items-center gap-1">
                <BookOpen className="h-3.5 w-3.5" />
                Summary
              </p>
              <p className="text-sm text-gray-700 dark:text-gray-300 leading-relaxed">{doc.doc_summary}</p>
            </div>
          )}

          {/* Router reasoning */}
          {doc.router_reasoning && (
            <div className="bg-gray-50 dark:bg-gray-800/50 rounded-xl border border-gray-200 dark:border-gray-800 p-4">
              <p className="text-xs font-medium text-gray-500 dark:text-gray-300 mb-1">AI Classification Reasoning</p>
              <p className="text-sm text-gray-600 dark:text-gray-300 leading-relaxed">{doc.router_reasoning}</p>
            </div>
          )}

          {/* Error */}
          {doc.error_message && (
            <div className="bg-red-50 rounded-xl border border-red-200 p-4 flex gap-2">
              <AlertCircle className="h-4 w-4 text-red-400 flex-shrink-0 mt-0.5" />
              <p className="text-xs text-red-700">{doc.error_message}</p>
            </div>
          )}
        </div>
      </div>

      {/* ── Row 2: full-width knowledge store content ─────────── */}
      <div className="bg-white dark:bg-[#202024]/80 rounded-2xl border border-slate-200 dark:border-white/[0.08] shadow-sm dark:shadow-xl p-5">
        <h2 className="text-sm font-semibold text-gray-900 dark:text-gray-100 mb-4">Knowledge Store Content</h2>
        {doc.status === "completed" ? (
          <ChunkViewer
            documentId={documentId}
            documentType={doc.document_type as DocumentType}
            vectorChunks={doc.vector_chunks}
            tableCount={doc.table_count}
            clauseCount={doc.clause_count}
            researchChunks={doc.research_chunks}
          />
        ) : doc.status === "failed" ? (
          <p className="text-sm text-gray-400 dark:text-gray-300 py-8 text-center">
            Ingestion failed — no content stored.
          </p>
        ) : (
          <div className="flex items-center gap-2 text-sm text-gray-400 dark:text-gray-300 py-8 justify-center">
            <Loader2 className="h-4 w-4 animate-spin" />
            Processing…
          </div>
        )}
      </div>
    </div>
  );
}

function Stat({ label, value }: { label: string; value: string | number }) {
  return (
    <div className="bg-gray-50 dark:bg-gray-800/50 rounded-lg p-3">
      <p className="text-xs text-gray-400 dark:text-gray-300">{label}</p>
      <p className="text-sm font-semibold text-gray-800 dark:text-gray-200 mt-0.5">{value}</p>
    </div>
  );
}
