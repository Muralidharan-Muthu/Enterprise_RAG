"use client";

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  ChevronDown,
  ChevronRight,
  ChevronLeft,
  Loader2,
  Table2,
  Copy,
  Check,
} from "lucide-react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { cn } from "@/lib/utils";
import type { DocumentType } from "@/lib/types";

/** Per-semantic-type colour scheme — badge + left accent border, for fast visual scanning. */
const TYPE_STYLES: Record<string, { badge: string; border: string }> = {
  paragraph: {
    badge: "bg-blue-50 text-blue-700 dark:bg-blue-950 dark:text-blue-300",
    border: "border-l-blue-400 dark:border-l-blue-600",
  },
  image_analysis: {
    badge: "bg-violet-50 text-violet-700 dark:bg-violet-950 dark:text-violet-300",
    border: "border-l-violet-400 dark:border-l-violet-600",
  },
  table: {
    badge: "bg-emerald-50 text-emerald-700 dark:bg-emerald-950 dark:text-emerald-300",
    border: "border-l-emerald-400 dark:border-l-emerald-600",
  },
  heading: {
    badge: "bg-amber-50 text-amber-700 dark:bg-amber-950 dark:text-amber-300",
    border: "border-l-amber-400 dark:border-l-amber-600",
  },
  title: {
    badge: "bg-amber-50 text-amber-700 dark:bg-amber-950 dark:text-amber-300",
    border: "border-l-amber-400 dark:border-l-amber-600",
  },
  list: {
    badge: "bg-cyan-50 text-cyan-700 dark:bg-cyan-950 dark:text-cyan-300",
    border: "border-l-cyan-400 dark:border-l-cyan-600",
  },
  caption: {
    badge: "bg-pink-50 text-pink-700 dark:bg-pink-950 dark:text-pink-300",
    border: "border-l-pink-400 dark:border-l-pink-600",
  },
  finding: {
    badge: "bg-rose-50 text-rose-700 dark:bg-rose-950 dark:text-rose-300",
    border: "border-l-rose-400 dark:border-l-rose-600",
  },
};
const DEFAULT_STYLE = {
  badge: "bg-indigo-50 text-indigo-600 dark:bg-indigo-950 dark:text-indigo-300",
  border: "border-l-indigo-400 dark:border-l-indigo-600",
};
function typeStyle(t?: string) {
  return (t && TYPE_STYLES[t.toLowerCase()]) || DEFAULT_STYLE;
}

/** Copy-to-clipboard button with transient confirmation. */
function CopyButton({ text }: { text: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <button
      onClick={() => {
        navigator.clipboard.writeText(text).then(() => {
          setCopied(true);
          setTimeout(() => setCopied(false), 1500);
        });
      }}
      className="inline-flex items-center gap-1 text-xs text-gray-500 dark:text-gray-400 hover:text-gray-800 dark:hover:text-gray-200 transition-colors"
    >
      {copied ? (
        <>
          <Check className="h-3.5 w-3.5 text-green-500" /> Copied
        </>
      ) : (
        <>
          <Copy className="h-3.5 w-3.5" /> Copy
        </>
      )}
    </button>
  );
}

/** Strip markdown syntax for the collapsed one-line preview. */
function stripMarkdown(s: string): string {
  return s
    .replace(/`{1,3}/g, "")
    .replace(/[*_#>]+/g, "")
    .replace(/^\s*[-–]{3,}\s*$/gm, "")
    .replace(/\s+/g, " ")
    .trim();
}

/** Tailwind-styled element overrides so markdown renders as a clean document. */
const mdComponents = {
  h1: (p: any) => <h3 className="text-base font-semibold text-gray-900 dark:text-gray-100 mt-3 mb-1.5" {...p} />,
  h2: (p: any) => <h4 className="text-sm font-semibold text-gray-900 dark:text-gray-100 mt-3 mb-1.5" {...p} />,
  h3: (p: any) => <h4 className="text-sm font-semibold text-gray-800 dark:text-gray-200 mt-3 mb-1" {...p} />,
  h4: (p: any) => <h5 className="text-sm font-semibold text-gray-700 dark:text-gray-300 mt-2 mb-1" {...p} />,
  p: (p: any) => <p className="text-sm leading-relaxed text-gray-700 dark:text-gray-300 mb-2" {...p} />,
  ul: (p: any) => <ul className="list-disc pl-5 space-y-1 mb-2 text-sm text-gray-700 dark:text-gray-300" {...p} />,
  ol: (p: any) => <ol className="list-decimal pl-5 space-y-1 mb-2 text-sm text-gray-700 dark:text-gray-300" {...p} />,
  li: (p: any) => <li className="leading-relaxed" {...p} />,
  strong: (p: any) => <strong className="font-semibold text-gray-900 dark:text-gray-100" {...p} />,
  em: (p: any) => <em className="italic" {...p} />,
  hr: () => <hr className="my-3 border-gray-200 dark:border-gray-800" />,
  a: (p: any) => <a className="text-blue-600 underline" {...p} />,
  code: (p: any) => (
    <code className="bg-gray-100 dark:bg-gray-800 text-gray-800 dark:text-gray-200 px-1 py-0.5 rounded text-[13px] font-mono" {...p} />
  ),
  blockquote: (p: any) => (
    <blockquote className="border-l-2 border-gray-300 dark:border-gray-700 pl-3 text-gray-600 dark:text-gray-300 italic mb-2" {...p} />
  ),
  table: (p: any) => (
    <div className="overflow-auto my-2">
      <table className="min-w-full text-sm border-collapse border border-gray-300 dark:border-gray-700" {...p} />
    </div>
  ),
  th: (p: any) => (
    <th className="text-left font-semibold text-gray-700 dark:text-gray-300 px-3 py-2 border border-gray-300 dark:border-gray-700 bg-gray-100 dark:bg-gray-800" {...p} />
  ),
  td: (p: any) => <td className="px-3 py-2 border border-gray-300 dark:border-gray-700 text-gray-700 dark:text-gray-300 align-top" {...p} />,
};

type StoreTab = "vector" | "table" | "clause";

interface Props {
  documentId: string;
  documentType: DocumentType | null;
  vectorChunks: number;
  tableCount: number;
  clauseCount: number;
  researchChunks?: number;
}

/** Pretty-print structured_content: JSON gets indented, plain text passes through. */
function formatStructured(sc: string): string {
  if (!sc) return "";
  const trimmed = sc.trim();
  try {
    return JSON.stringify(JSON.parse(trimmed), null, 2);
  } catch {
    return trimmed;
  }
}

/** Parse VLM structured_content JSON into a {headers, rows} table; null if not table-shaped. */
function parseStructuredTable(
  sc: string
): { headers: string[]; rows: string[][] } | null {
  if (!sc) return null;
  const fenced = sc.trim().replace(/^```(?:json)?/i, "").replace(/```$/, "").trim();
  let obj: unknown;
  try {
    obj = JSON.parse(fenced);
  } catch {
    return null;
  }
  if (!obj || typeof obj !== "object" || Array.isArray(obj)) return null;
  const o = obj as Record<string, unknown>;
  if (!Array.isArray(o.rows)) return null;
  const headers = Array.isArray(o.headers) ? (o.headers as unknown[]).map(String) : [];
  const rows = (o.rows as unknown[]).map((r) =>
    Array.isArray(r) ? r.map((c) => (c == null ? "" : String(c))) : [String(r)]
  );
  if (headers.length === 0 && rows.length === 0) return null;
  return { headers, rows };
}

/** Parse a GFM pipe table (markdown_text) into headers + rows. */
function parseMarkdownTable(
  md: string
): { headers: string[]; rows: string[][] } | null {
  if (!md) return null;
  const lines = md
    .trim()
    .split("\n")
    .map((l) => l.trim())
    .filter(Boolean);
  if (lines.length === 0) return null;

  const splitRow = (l: string) =>
    l
      .replace(/^\|/, "")
      .replace(/\|$/, "")
      .split("|")
      .map((c) => c.trim());
  const isSep = (l: string) => /^[\s:|-]+$/.test(l) && l.includes("-");

  const headers = splitRow(lines[0]);
  let bodyStart = 1;
  if (lines[1] && isSep(lines[1])) bodyStart = 2;
  const rows = lines
    .slice(bodyStart)
    .filter((l) => !isSep(l))
    .map(splitRow);
  return { headers, rows };
}

export function ChunkViewer({
  documentId,
  documentType,
  vectorChunks,
  tableCount,
  clauseCount,
}: Props) {
  const tabs = [
    { id: "vector" as StoreTab, label: "Vector Chunks", count: vectorChunks },
    { id: "table" as StoreTab, label: "Tables", count: tableCount },
    { id: "clause" as StoreTab, label: "Clauses", count: clauseCount },
  ].filter((t) => t.count > 0);

  const [activeTab, setActiveTab] = useState<StoreTab>(tabs[0]?.id ?? "vector");
  const [page, setPage] = useState(1);
  const [expanded, setExpanded] = useState<Set<number>>(new Set());

  const PAGE_SIZE = 50;

  const { data, isLoading } = useQuery({
    queryKey: ["chunks", documentId, activeTab, page],
    queryFn: () =>
      fetch(
        `/api/v1/documents/${documentId}/chunks?store=${activeTab}&page=${page}&limit=${PAGE_SIZE}`
      ).then((r) => r.json()),
    enabled: tabs.length > 0,
  });

  if (tabs.length === 0) {
    return (
      <div className="text-sm text-gray-400 dark:text-gray-400 py-6 text-center">
        No chunks stored yet.
      </div>
    );
  }

  const toggleExpand = (idx: number) => {
    setExpanded((prev) => {
      const next = new Set(prev);
      next.has(idx) ? next.delete(idx) : next.add(idx);
      return next;
    });
  };

  const items = data?.items ?? [];

  return (
    <div className="space-y-4">
      {/* Tabs */}
      <div className="flex gap-1 border-b border-gray-200 dark:border-gray-800">
        {tabs.map((tab) => (
          <button
            key={tab.id}
            onClick={() => {
              setActiveTab(tab.id);
              setPage(1);
              setExpanded(new Set());
            }}
            className={cn(
              "px-4 py-2 text-sm font-medium border-b-2 -mb-px transition-colors",
              activeTab === tab.id
                ? "border-blue-500 text-blue-600 dark:text-blue-400"
                : "border-transparent text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-200"
            )}
          >
            {tab.label}
            <span className="ml-2 text-xs bg-gray-100 dark:bg-gray-800 text-gray-500 dark:text-gray-300 px-1.5 py-0.5 rounded-full">
              {tab.count}
            </span>
          </button>
        ))}
      </div>

      {/* Toolbar: count + expand/collapse all (text stores only) */}
      {!isLoading && activeTab !== "table" && items.length > 0 && (
        <div className="flex items-center justify-between mb-2 text-xs">
          <span className="text-gray-400 dark:text-gray-500">
            {items.length} chunk{items.length === 1 ? "" : "s"}
          </span>
          <div className="flex gap-3">
            <button
              onClick={() => setExpanded(new Set(items.map((_: any, i: number) => i)))}
              className="text-blue-600 dark:text-blue-400 hover:underline"
            >
              Expand all
            </button>
            <button
              onClick={() => setExpanded(new Set())}
              className="text-gray-500 dark:text-gray-400 hover:underline"
            >
              Collapse all
            </button>
          </div>
        </div>
      )}

      {/* Body — scrollable region */}
      <div className="max-h-[65vh] overflow-y-auto pr-1 -mr-1">
        {isLoading ? (
          <div className="flex justify-center py-8">
            <Loader2 className="h-5 w-5 animate-spin text-gray-400 dark:text-gray-400" />
          </div>
        ) : activeTab === "table" ? (
          <TableList items={items} />
        ) : (
          <TextChunkList
            items={items}
            expanded={expanded}
            toggleExpand={toggleExpand}
          />
        )}
      </div>

      {/* Pagination */}
      {!isLoading && (data?.total ?? 0) > PAGE_SIZE && (
        <div className="flex items-center justify-between pt-2 border-t border-gray-100 dark:border-gray-800 text-xs text-gray-500 dark:text-gray-400">
          <span>
            {(page - 1) * PAGE_SIZE + 1}–{Math.min(page * PAGE_SIZE, data.total)} of {data.total}
          </span>
          <div className="flex gap-1">
            <button
              disabled={page === 1}
              onClick={() => { setPage(page - 1); setExpanded(new Set()); }}
              className="inline-flex items-center gap-1 px-2 py-1 rounded border border-gray-200 dark:border-gray-700 disabled:opacity-40 hover:bg-gray-50 dark:hover:bg-gray-800 transition-colors"
            >
              <ChevronLeft className="h-3.5 w-3.5" /> Prev
            </button>
            <button
              disabled={page * PAGE_SIZE >= data.total}
              onClick={() => { setPage(page + 1); setExpanded(new Set()); }}
              className="inline-flex items-center gap-1 px-2 py-1 rounded border border-gray-200 dark:border-gray-700 disabled:opacity-40 hover:bg-gray-50 dark:hover:bg-gray-800 transition-colors"
            >
              Next <ChevronRight className="h-3.5 w-3.5" />
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

/* ── Text chunks (vector / clause / research) ─────────────────────────── */

function TextChunkList({
  items,
  expanded,
  toggleExpand,
}: {
  items: any[];
  expanded: Set<number>;
  toggleExpand: (idx: number) => void;
}) {
  return (
    <div className="space-y-2">
      {items.map((chunk, idx) => {
        const isOpen = expanded.has(idx);
        const text = chunk.chunk_text || chunk.clause_text || chunk.raw_text || "";
        const stripped = stripMarkdown(text);
        const preview = stripped.slice(0, 220) + (stripped.length > 220 ? "…" : "");
        const keywords: string[] = Array.isArray(chunk.keywords)
          ? chunk.keywords
          : [];
        const kind = chunk.semantic_type || chunk.chunk_type;
        const style = typeStyle(kind);
        return (
          <div
            key={chunk.id}
            className={cn(
              "border border-slate-200 dark:border-white/[0.08] border-l-4 rounded-xl overflow-hidden bg-white dark:bg-[#202024]",
              style.border
            )}
          >
            {/* Clickable header — meta + title only (no block content inside button) */}
            <button
              onClick={() => toggleExpand(idx)}
              className="w-full flex items-start gap-3 px-4 py-3 text-left hover:bg-gray-50 dark:hover:bg-gray-800/50 transition-colors"
            >
              {isOpen ? (
                <ChevronDown className="h-4 w-4 text-gray-400 dark:text-gray-400 mt-1 flex-shrink-0" />
              ) : (
                <ChevronRight className="h-4 w-4 text-gray-400 dark:text-gray-400 mt-1 flex-shrink-0" />
              )}
              <span className="flex-1 min-w-0">
                {/* Meta row */}
                <span className="flex flex-wrap items-center gap-2 mb-1.5">
                  <span className="text-xs font-mono text-gray-400 dark:text-gray-400">
                    #{chunk.chunk_index ?? chunk.clause_index ?? idx}
                  </span>
                  {chunk.page_number != null && (
                    <span className="text-xs text-gray-500 dark:text-gray-300 bg-gray-100 dark:bg-gray-800 px-1.5 py-0.5 rounded">
                      page {chunk.page_number}
                    </span>
                  )}
                  {kind && (
                    <span
                      className={cn(
                        "text-xs px-1.5 py-0.5 rounded capitalize font-medium",
                        style.badge
                      )}
                    >
                      {String(kind).replace(/_/g, " ")}
                    </span>
                  )}
                  {chunk.clause_type && (
                    <span className="text-xs bg-purple-100 text-purple-700 dark:bg-purple-950 dark:text-purple-300 px-1.5 py-0.5 rounded">
                      {chunk.clause_type}
                    </span>
                  )}
                  {chunk.risk_level && (
                    <span
                      className={cn(
                        "text-xs px-1.5 py-0.5 rounded",
                        chunk.risk_level === "high"
                          ? "bg-red-100 text-red-700 dark:bg-red-950 dark:text-red-300"
                          : chunk.risk_level === "medium"
                          ? "bg-yellow-100 text-yellow-700 dark:bg-yellow-950 dark:text-yellow-300"
                          : "bg-green-100 text-green-700 dark:bg-green-950 dark:text-green-300"
                      )}
                    >
                      {chunk.risk_level} risk
                    </span>
                  )}
                </span>
                {/* Section title */}
                {chunk.section_title && (
                  <span className="block text-sm font-semibold text-gray-800 dark:text-gray-200 mb-1">
                    {chunk.section_title}
                  </span>
                )}
                {/* Collapsed preview (clean, no markdown) */}
                {!isOpen && (
                  <span className="block text-sm leading-relaxed text-gray-700 dark:text-gray-300 break-words">
                    {preview}
                  </span>
                )}
              </span>
            </button>

            {/* Expanded body — rendered markdown, outside the button (block content) */}
            {isOpen && (
              <div className="px-4 pb-4 pl-11">
                {/* Toolbar: char count + copy */}
                <div className="flex items-center justify-between mb-2 pb-2 border-b border-gray-100 dark:border-gray-800">
                  <span className="text-[11px] text-gray-400 dark:text-gray-500">
                    {text.length.toLocaleString()} chars
                  </span>
                  <CopyButton text={text} />
                </div>
                <div className="break-words">
                  <ReactMarkdown remarkPlugins={[remarkGfm]} components={mdComponents}>
                    {text}
                  </ReactMarkdown>
                </div>
                {keywords.length > 0 && (
                  <div className="mt-3 flex flex-wrap gap-1.5">
                    {keywords.map((kw, i) => (
                      <span
                        key={i}
                        className="text-[11px] bg-gray-100 dark:bg-gray-800 text-gray-600 dark:text-gray-300 px-1.5 py-0.5 rounded-full"
                      >
                        {kw}
                      </span>
                    ))}
                  </div>
                )}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}

/* ── Tables ───────────────────────────────────────────────────────────── */

function TableList({ items }: { items: any[] }) {
  return (
    <div className="space-y-4">
      {items.map((t, idx) => {
        const parsed =
          parseMarkdownTable(t.markdown_text) ??
          (t.json_data?.rows
            ? { headers: [], rows: t.json_data.rows as string[][] }
            : null);

        return (
          <div
            key={t.id}
            className="border border-gray-200 dark:border-gray-800 rounded-lg overflow-hidden bg-white dark:bg-gray-900"
          >
            {/* Header */}
            <div className="flex flex-wrap items-center gap-2 px-4 py-2.5 border-b border-gray-100 dark:border-gray-800 bg-gray-50 dark:bg-gray-800/50">
              <Table2 className="h-4 w-4 text-gray-400 dark:text-gray-400" />
              <span className="text-xs font-mono text-gray-400 dark:text-gray-400">
                #{t.table_index ?? idx}
              </span>
              <span className="text-sm font-semibold text-gray-800 dark:text-gray-200">
                {t.table_title || "Table"}
              </span>
              {t.page_number != null && (
                <span className="text-xs text-gray-500 dark:text-gray-300 bg-white dark:bg-gray-900 border border-gray-200 dark:border-gray-800 px-1.5 py-0.5 rounded">
                  page {t.page_number}
                </span>
              )}
              {t.table_category && (
                <span className="text-xs bg-emerald-50 text-emerald-600 dark:bg-emerald-950 dark:text-emerald-300 px-1.5 py-0.5 rounded capitalize">
                  {t.table_category}
                </span>
              )}
              {(t.row_count != null || t.col_count != null) && (
                <span className="ml-auto text-xs text-gray-400 dark:text-gray-400">
                  {t.row_count ?? "?"} rows × {t.col_count ?? "?"} cols
                </span>
              )}
            </div>

            {/* Structured content (VLM) — primary, retrieval-ready extraction */}
            {t.structured_content ? (
              <div className="px-4 py-3 border-b border-gray-100 dark:border-gray-800">
                <div className="text-[11px] font-semibold uppercase tracking-wide text-emerald-600 dark:text-emerald-300 mb-1.5">
                  Structured content
                </div>
                {(() => {
                  const st = parseStructuredTable(t.structured_content);
                  return st ? (
                    <div className="overflow-x-auto">
                      <table className="min-w-full text-sm border-collapse border border-gray-300 dark:border-gray-700">
                        {st.headers.length > 0 && (
                          <thead>
                            <tr className="bg-gray-100 dark:bg-gray-800">
                              {st.headers.map((h, i) => (
                                <th
                                  key={i}
                                  className="text-left font-semibold text-gray-700 dark:text-gray-300 px-3 py-2 border border-gray-300 dark:border-gray-700 whitespace-nowrap"
                                >
                                  {h}
                                </th>
                              ))}
                            </tr>
                          </thead>
                        )}
                        <tbody>
                          {st.rows.map((row, ri) => (
                            <tr
                              key={ri}
                              className={ri % 2 === 0 ? "bg-white dark:bg-gray-900" : "bg-gray-50/60 dark:bg-gray-800/40"}
                            >
                              {row.map((cell, ci) => (
                                <td
                                  key={ci}
                                  className="px-3 py-2 border border-gray-300 dark:border-gray-700 text-gray-700 dark:text-gray-300 align-top"
                                >
                                  {cell}
                                </td>
                              ))}
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  ) : (
                    <pre className="text-xs text-gray-700 dark:text-gray-300 whitespace-pre-wrap break-words max-h-96 overflow-y-auto font-mono">
                      {formatStructured(t.structured_content)}
                    </pre>
                  );
                })()}
              </div>
            ) : null}

            {/* Raw extraction — secondary; collapsed when structured_content exists */}
            <details open={!t.structured_content} className="group">
              <summary className="cursor-pointer select-none px-4 py-2 text-xs font-medium text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-200">
                Raw table extraction
              </summary>
              {parsed && parsed.rows.length > 0 ? (
                <div className="overflow-x-auto">
                  <table className="min-w-full text-sm border-collapse border border-gray-300 dark:border-gray-700">
                    {parsed.headers.length > 0 && (
                      <thead className="sticky top-0 z-10">
                        <tr className="bg-gray-100 dark:bg-gray-800">
                          {parsed.headers.map((h, i) => (
                            <th
                              key={i}
                              className="text-left font-semibold text-gray-700 dark:text-gray-300 px-3 py-2 border border-gray-300 dark:border-gray-700 whitespace-nowrap"
                            >
                              {h}
                            </th>
                          ))}
                        </tr>
                      </thead>
                    )}
                    <tbody>
                      {parsed.rows.map((row, ri) => (
                        <tr
                          key={ri}
                          className={ri % 2 === 0 ? "bg-white dark:bg-gray-900" : "bg-gray-50/60 dark:bg-gray-800/40"}
                        >
                          {row.map((cell, ci) => (
                            <td
                              key={ci}
                              className="px-3 py-2 border border-gray-300 dark:border-gray-700 text-gray-700 dark:text-gray-300 align-top"
                            >
                              {cell}
                            </td>
                          ))}
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              ) : (
                <pre className="px-4 py-3 text-xs text-gray-600 dark:text-gray-300 whitespace-pre-wrap break-words">
                  {t.raw_text || "No table data."}
                </pre>
              )}
            </details>
          </div>
        );
      })}
    </div>
  );
}
