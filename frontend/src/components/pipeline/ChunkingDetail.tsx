"use client";

import { useEffect, useRef, useState } from "react";
import { FileText, Table2, Scale, Loader2, ChevronDown, ChevronLeft, ChevronRight } from "lucide-react";
import { cn } from "@/lib/utils";
import { useDocumentChunks } from "@/hooks/useDocuments";
import type { PipelineDocumentDetail, ChunkStore } from "@/lib/types";

// ── Props ───────────────────────────────────────────────────────────────────

interface Props {
  doc: PipelineDocumentDetail;
}

// ── Small helpers ────────────────────────────────────────────────────────────

function Pill({ children, className }: { children: React.ReactNode; className?: string }) {
  return (
    <span
      className={cn(
        "inline-block rounded-full border px-2 py-0.5 text-[10px] font-medium leading-tight",
        className,
      )}
    >
      {children}
    </span>
  );
}

function RiskBadge({ risk }: { risk: string | null }) {
  const map: Record<string, string> = {
    high: "border-red-300 bg-red-50 text-red-700 dark:border-red-800 dark:bg-red-950 dark:text-red-300",
    medium:
      "border-amber-300 bg-amber-50 text-amber-700 dark:border-amber-800 dark:bg-amber-950 dark:text-amber-300",
    low: "border-green-300 bg-green-50 text-green-700 dark:border-green-800 dark:bg-green-950 dark:text-green-300",
  };
  const cls =
    risk && map[risk]
      ? map[risk]
      : "border-gray-200 bg-gray-100 text-gray-500 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-400";
  return <Pill className={cls}>{risk ?? "—"}</Pill>;
}

// Pretty-print structured_content: JSON gets indented, plain text passes through.
function formatStructured(sc: string): string {
  if (!sc) return "";
  const trimmed = sc.trim();
  try {
    return JSON.stringify(JSON.parse(trimmed), null, 2);
  } catch {
    return trimmed;
  }
}

// Parse the VLM structured_content JSON into a {title, headers, rows} table.
// Returns null when it isn't table-shaped (caller falls back to pretty JSON).
function parseStructuredTable(
  sc: string
): { title: string | null; headers: string[]; rows: string[][] } | null {
  if (!sc) return null;
  let fenced = sc.trim().replace(/^```(?:json)?/i, "").replace(/```$/, "").trim();
  let obj: unknown;
  try {
    obj = JSON.parse(fenced);
  } catch {
    return null;
  }
  if (!obj || typeof obj !== "object" || Array.isArray(obj)) return null;
  const o = obj as Record<string, unknown>;
  const rawRows = o["rows"];
  if (!Array.isArray(rawRows)) return null;
  const headers = Array.isArray(o["headers"]) ? (o["headers"] as unknown[]).map(String) : [];
  const rows = rawRows.map((r) =>
    Array.isArray(r) ? r.map((c) => (c == null ? "" : String(c))) : [String(r)]
  );
  if (headers.length === 0 && rows.length === 0) return null;
  const title = typeof o["title"] === "string" ? (o["title"] as string) : null;
  return { title, headers, rows };
}

// Render a parsed structured_content table (headers + rows) as an HTML table.
function StructuredTable({
  headers,
  rows,
}: {
  headers: string[];
  rows: string[][];
}) {
  return (
    <div className="overflow-x-auto">
      <table className="min-w-full text-xs">
        {headers.length > 0 && (
          <thead>
            <tr className="border-b border-gray-200 dark:border-gray-700">
              {headers.map((h, i) => (
                <th
                  key={i}
                  className="px-2 py-1 text-left font-semibold text-gray-700 dark:text-gray-200 whitespace-nowrap"
                >
                  {h}
                </th>
              ))}
            </tr>
          </thead>
        )}
        <tbody>
          {rows.map((row, ri) => (
            <tr
              key={ri}
              className="border-b border-gray-100 dark:border-gray-800 last:border-0"
            >
              {row.map((cell, ci) => (
                <td
                  key={ci}
                  className="px-2 py-1 align-top text-gray-600 dark:text-gray-300"
                >
                  {cell}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// Parse a GitHub-flavour markdown table into rows of cells.
// Returns [headerRow, ...bodyRows] or [] on failure.
function parseMarkdownTable(md: string): string[][] {
  const lines = md
    .split("\n")
    .map((l) => l.trim())
    .filter((l) => l.startsWith("|") && l.endsWith("|"));

  if (lines.length < 2) return [];

  // Separator line: every cell is dashes/colons/spaces only
  const sepIdx = lines.findIndex((l) =>
    l
      .slice(1, -1)
      .split("|")
      .every((c) => /^[\s\-:]+$/.test(c)),
  );
  if (sepIdx < 1) return [];

  const headerLines = lines.slice(0, sepIdx);
  const bodyLines = lines.slice(sepIdx + 1);

  const parseRow = (line: string): string[] =>
    line
      .slice(1, -1) // drop leading/trailing |
      .split("|")
      .map((c) => c.trim());

  return [...headerLines.map(parseRow), ...bodyLines.map(parseRow)];
}

function MarkdownTable({ markdown, fallback }: { markdown: string; fallback: string }) {
  const rows = parseMarkdownTable(markdown);

  if (rows.length === 0) {
    return (
      <pre className="overflow-x-auto whitespace-pre-wrap rounded bg-gray-50 p-2 text-xs text-gray-700 dark:bg-gray-800 dark:text-gray-300">
        {fallback || "(no data)"}
      </pre>
    );
  }

  const [head, ...body] = rows;

  return (
    <div className="overflow-x-auto">
      <table className="min-w-full text-xs">
        <thead>
          <tr>
            {head.map((cell, i) => (
              <th
                key={i}
                className="border border-gray-200 bg-gray-50 px-2 py-1 font-semibold dark:border-gray-700 dark:bg-gray-800"
              >
                {cell}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {body.map((row, ri) => (
            <tr key={ri}>
              {row.map((cell, ci) => (
                <td
                  key={ci}
                  className="border border-gray-200 px-2 py-1 dark:border-gray-700"
                >
                  {cell}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// ── Expandable text card ─────────────────────────────────────────────────────

function ChunkCard({ children }: { children: React.ReactNode }) {
  return (
    <div className="rounded-lg border border-gray-100 bg-white px-3 py-2.5 dark:border-gray-800 dark:bg-gray-900">
      {children}
    </div>
  );
}

function ExpandableText({ text, idx, expanded, onToggle }: {
  text: string;
  idx: number;
  expanded: boolean;
  onToggle: (i: number) => void;
}) {
  return (
    <div>
      <p className={cn("text-xs text-gray-700 dark:text-gray-300", !expanded && "line-clamp-4")}>
        {text}
      </p>
      <button
        onClick={() => onToggle(idx)}
        className="mt-1 flex items-center gap-0.5 text-[10px] text-blue-600 hover:text-blue-700 dark:text-blue-400"
      >
        <ChevronDown
          size={10}
          className={cn("transition-transform", expanded && "rotate-180")}
        />
        {expanded ? "Show less" : "Show more"}
      </button>
    </div>
  );
}

// ── Store views ──────────────────────────────────────────────────────────────

function VectorList({ items }: { items: Record<string, unknown>[] }) {
  const [expanded, setExpanded] = useState<Set<number>>(new Set());
  const toggle = (i: number) =>
    setExpanded((prev) => {
      const next = new Set(prev);
      next.has(i) ? next.delete(i) : next.add(i);
      return next;
    });

  if (items.length === 0) return <Empty />;

  return (
    <div className="space-y-2">
      {items.map((item, i) => {
        const idx = Number(item["chunk_index"] ?? i);
        const text = String(item["chunk_text"] ?? "");
        const page = Number(item["page_number"] ?? 0);
        const section = item["section_title"] ? String(item["section_title"]) : null;
        const semType = item["semantic_type"] ? String(item["semantic_type"]) : null;
        const keywords = Array.isArray(item["keywords"])
          ? (item["keywords"] as string[])
          : [];

        return (
          <ChunkCard key={i}>
            <div className="mb-1 flex flex-wrap items-center gap-1.5">
              <span className="text-[10px] font-semibold text-gray-500 dark:text-gray-400">
                #{idx}
              </span>
              <Pill className="border-gray-200 bg-gray-100 text-gray-600 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-300">
                p.{page}
              </Pill>
              {section && (
                <span className="text-[10px] text-gray-500 dark:text-gray-400">{section}</span>
              )}
              {semType && (
                <Pill className="border-blue-200 bg-blue-50 text-blue-700 dark:border-blue-800 dark:bg-blue-950 dark:text-blue-300">
                  {semType}
                </Pill>
              )}
            </div>
            <ExpandableText
              text={text}
              idx={i}
              expanded={expanded.has(i)}
              onToggle={toggle}
            />
            {keywords.length > 0 && (
              <div className="mt-1.5 flex flex-wrap gap-1">
                {keywords.map((kw, ki) => (
                  <Pill
                    key={ki}
                    className="border-gray-200 bg-gray-50 text-gray-500 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-400"
                  >
                    {kw}
                  </Pill>
                ))}
              </div>
            )}
          </ChunkCard>
        );
      })}
    </div>
  );
}

function TableList({ items }: { items: Record<string, unknown>[] }) {
  if (items.length === 0) return <Empty />;

  return (
    <div className="space-y-3">
      {items.map((item, i) => {
        const tidx = Number(item["table_index"] ?? i);
        const title = item["table_title"] ? String(item["table_title"]) : `Table ${tidx}`;
        const page = Number(item["page_number"] ?? 0);
        const category = item["table_category"] ? String(item["table_category"]) : null;
        const rows = Number(item["row_count"] ?? 0);
        const cols = Number(item["col_count"] ?? 0);
        const md = String(item["markdown_text"] ?? "");
        const raw = String(item["raw_text"] ?? "");
        const sc = String(item["structured_content"] ?? "");

        return (
          <ChunkCard key={i}>
            <div className="mb-2 flex flex-wrap items-center gap-1.5">
              <span className="text-xs font-semibold text-gray-700 dark:text-gray-200">
                {title}
              </span>
              <Pill className="border-gray-200 bg-gray-100 text-gray-600 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-300">
                p.{page}
              </Pill>
              {category && (
                <Pill className="border-purple-200 bg-purple-50 text-purple-700 dark:border-purple-800 dark:bg-purple-950 dark:text-purple-300">
                  {category}
                </Pill>
              )}
              {rows > 0 && (
                <span className="text-[10px] text-gray-400 dark:text-gray-500">
                  {rows}×{cols}
                </span>
              )}
            </div>
            {/* Structured content (VLM) — primary, retrieval-ready extraction.
                Rendered as a real table when the JSON is table-shaped; only a
                non-table blob falls back to pretty JSON. */}
            {sc ? (
              <div className="mb-2">
                <div className="mb-1 text-[10px] font-semibold uppercase tracking-wide text-emerald-600 dark:text-emerald-300">
                  Structured content
                </div>
                {(() => {
                  const st = parseStructuredTable(sc);
                  return st ? (
                    <StructuredTable headers={st.headers} rows={st.rows} />
                  ) : (
                    <pre className="max-h-80 overflow-y-auto whitespace-pre-wrap break-words rounded bg-gray-50 p-2 font-mono text-[11px] text-gray-700 dark:bg-gray-800/50 dark:text-gray-300">
                      {formatStructured(sc)}
                    </pre>
                  );
                })()}
              </div>
            ) : null}
            {/* Raw table — secondary; collapsed when structured_content exists */}
            <details open={!sc}>
              <summary className="cursor-pointer select-none text-[11px] font-medium text-gray-500 hover:text-gray-700 dark:text-gray-400 dark:hover:text-gray-200">
                Raw table
              </summary>
              <div className="mt-1">
                <MarkdownTable markdown={md} fallback={raw} />
              </div>
            </details>
          </ChunkCard>
        );
      })}
    </div>
  );
}

function ClauseList({ items }: { items: Record<string, unknown>[] }) {
  const [expanded, setExpanded] = useState<Set<number>>(new Set());
  const toggle = (i: number) =>
    setExpanded((prev) => {
      const next = new Set(prev);
      next.has(i) ? next.delete(i) : next.add(i);
      return next;
    });

  if (items.length === 0) return <Empty />;

  return (
    <div className="space-y-2">
      {items.map((item, i) => {
        const num = item["clause_number"] ? String(item["clause_number"]) : `${i + 1}`;
        const ctitle = item["clause_title"] ? String(item["clause_title"]) : "";
        const ctype = item["clause_type"] ? String(item["clause_type"]) : null;
        const risk = item["risk_level"] ? String(item["risk_level"]) : null;
        const text = String(item["clause_text"] ?? "");
        const parties = Array.isArray(item["parties_mentioned"])
          ? (item["parties_mentioned"] as string[])
          : [];

        return (
          <ChunkCard key={i}>
            <div className="mb-1 flex flex-wrap items-center gap-1.5">
              <span className="text-[10px] font-semibold text-gray-500 dark:text-gray-400">
                §{num}
              </span>
              {ctitle && (
                <span className="text-xs font-medium text-gray-700 dark:text-gray-200">
                  {ctitle}
                </span>
              )}
              {ctype && (
                <Pill className="border-indigo-200 bg-indigo-50 text-indigo-700 dark:border-indigo-800 dark:bg-indigo-950 dark:text-indigo-300">
                  {ctype}
                </Pill>
              )}
              <RiskBadge risk={risk} />
            </div>
            {parties.length > 0 && (
              <div className="mb-1 flex flex-wrap gap-1">
                {parties.map((p, pi) => (
                  <Pill
                    key={pi}
                    className="border-gray-200 bg-gray-50 text-gray-500 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-400"
                  >
                    {p}
                  </Pill>
                ))}
              </div>
            )}
            <ExpandableText
              text={text}
              idx={i}
              expanded={expanded.has(i)}
              onToggle={toggle}
            />
          </ChunkCard>
        );
      })}
    </div>
  );
}

function Empty() {
  return (
    <p className="py-6 text-center text-xs text-gray-400 dark:text-gray-600">
      No chunks in this store yet.
    </p>
  );
}

// ── Main component ───────────────────────────────────────────────────────────

export function ChunkingDetail({ doc }: Props) {
  // Determine available tabs
  const tabDefs: { store: ChunkStore; label: string; count: number; Icon: React.ElementType }[] = [
    { store: "vector", label: "Text", count: doc.vector_chunks, Icon: FileText },
    { store: "table", label: "Tables", count: doc.table_count, Icon: Table2 },
    { store: "clause", label: "Clauses", count: doc.clause_count, Icon: Scale },
  ];

  const availableTabs = tabDefs.filter((t) => t.count > 0);
  const tabs = availableTabs.length > 0 ? availableTabs : [tabDefs[0]];

  const [activeStore, setActiveStore] = useState<ChunkStore>(tabs[0].store);
  const [page, setPage] = useState(1);
  const PAGE_SIZE = 50;

  // Poll the active store while the doc is still processing so chunks/tables stream
  // in; the storing stage writes them right before completion.
  const isProcessing = doc.doc_status !== "completed" && doc.doc_status !== "failed";
  const { data, isLoading, refetch } = useDocumentChunks(
    doc.document_id,
    activeStore,
    true,
    isProcessing ? 1500 : false,
    page,
    PAGE_SIZE,
  );
  const items = data?.items ?? [];
  const total = data?.total ?? 0;

  // One final refetch when processing finishes — polling stops the instant the run
  // goes terminal, so the last-written chunks/tables would otherwise only appear
  // after a manual page reload.
  const wasProcessing = useRef(isProcessing);
  useEffect(() => {
    if (wasProcessing.current && !isProcessing) refetch();
    wasProcessing.current = isProcessing;
  }, [isProcessing, refetch]);

  return (
    <div className="flex flex-col">
      {/* Header */}
      <div className="flex items-center justify-between border-b border-gray-100 bg-gray-50 px-4 py-3 dark:border-gray-800 dark:bg-gray-800/60">
        <span className="text-xs font-semibold uppercase tracking-wider text-gray-600 dark:text-gray-300">
          Chunking
        </span>
        <span className="text-[10px] text-gray-400 dark:text-gray-500">
          {doc.total_chunks ?? 0} units &bull; {doc.vector_chunks} text, {doc.table_count} tables,{" "}
          {doc.clause_count} clauses
        </span>
      </div>

      {/* Body */}
      <div className="px-4 py-3 space-y-3">
        {/* Tab row */}
        <div className="flex flex-wrap gap-1.5">
          {tabs.map(({ store, label, count, Icon }) => {
            const active = store === activeStore;
            return (
              <button
                key={store}
                onClick={() => { setActiveStore(store); setPage(1); }}
                className={cn(
                  "flex items-center gap-1 rounded-full border px-2.5 py-1 text-xs transition-colors",
                  active
                    ? "border-blue-200 bg-blue-50 text-blue-700 dark:border-blue-900 dark:bg-blue-950 dark:text-blue-300"
                    : "border-gray-200 bg-white text-gray-500 hover:border-gray-300 dark:border-gray-800 dark:bg-gray-900 dark:text-gray-400",
                )}
              >
                <Icon size={11} />
                {label}
                {count > 0 && <span className="opacity-75">({count})</span>}
              </button>
            );
          })}
        </div>

        {/* Content */}
        <div className="overflow-y-auto" style={{ maxHeight: "420px" }}>
          {isLoading ? (
            <div className="flex items-center justify-center gap-2 py-10 text-xs text-gray-400 dark:text-gray-500">
              <Loader2 size={14} className="animate-spin" />
              Loading chunks…
            </div>
          ) : activeStore === "vector" ? (
            <VectorList items={items} />
          ) : activeStore === "table" ? (
            <TableList items={items} />
          ) : (
            <ClauseList items={items} />
          )}
        </div>

        {/* Pagination */}
        {!isLoading && total > PAGE_SIZE && (
          <div className="flex items-center justify-between pt-2 border-t border-gray-100 dark:border-gray-800 text-[10px] text-gray-500 dark:text-gray-400">
            <span>
              {(page - 1) * PAGE_SIZE + 1}–{Math.min(page * PAGE_SIZE, total)} of {total}
            </span>
            <div className="flex gap-1">
              <button
                disabled={page === 1}
                onClick={() => setPage(page - 1)}
                className="inline-flex items-center gap-0.5 px-2 py-0.5 rounded border border-gray-200 dark:border-gray-700 disabled:opacity-40 hover:bg-gray-50 dark:hover:bg-gray-800 transition-colors"
              >
                <ChevronLeft size={10} /> Prev
              </button>
              <button
                disabled={page * PAGE_SIZE >= total}
                onClick={() => setPage(page + 1)}
                className="inline-flex items-center gap-0.5 px-2 py-0.5 rounded border border-gray-200 dark:border-gray-700 disabled:opacity-40 hover:bg-gray-50 dark:hover:bg-gray-800 transition-colors"
              >
                Next <ChevronRight size={10} />
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

export default ChunkingDetail;
