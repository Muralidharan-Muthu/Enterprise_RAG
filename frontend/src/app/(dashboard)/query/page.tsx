"use client";

import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import rehypeRaw from "rehype-raw";
import {
  Send,
  Loader2,
  Bot,
  User,
  FileText,
  Trash2,
  MessageSquare,
  ChevronDown,
  ChevronRight,
  Clock,
  Zap,
  ExternalLink,
  AlertCircle,
  Info,
  Shield,
  BookOpen,
  PanelLeft,
  Plus,
  Copy,
  Check,
  RotateCcw,
  Pencil,
  Sparkles,
  Cpu,
  Square,
  Network,
  Pin,
} from "lucide-react";
import { apiClient } from "@/lib/api-client";
import type {
  CitationItem,
  ChatSession,
  ChatMessageRecord,
  DocumentType,
} from "@/lib/types";
import { cn } from "@/lib/utils";

// ── Local types ────────────────────────────────────────────────────────────────

interface SynthesisInfo {
  model: string;
  maxTokens: number;
  chunksUsed: number;
  storesSearched: string[];
  // GraphRAG (Neo4j) involvement for this answer — "none" means the plain
  // vector/store retrieval path answered it with no graph traversal at all;
  // "local"/"global" mean the Neo4j entity graph was queried server-side.
  // graphExpanded is how many extra chunks the graph search added to the pool
  // beyond what plain vector search already found (0 = graph ran but found
  // nothing new). Surfaced separately from storesSearched because a
  // graph-sourced chunk still reports its underlying Postgres store type
  // (e.g. "vector") — storesSearched alone can't tell you whether Neo4j
  // contributed anything.
  graphMode?: "none" | "local" | "global" | string;
  graphExpanded?: number;
}

interface UserMessage {
  id?: string;
  role: "user";
  content: string;
  timestamp?: string;
  is_pinned?: boolean;
}

interface ConfidenceComponent {
  label: string;
  score: number;
  weight: number;
  detail?: {
    top_chunk_score?: number;
    top3_mean_score?: number;
    weights?: { top_chunk: number; top3_mean: number };
    score?: number;
  } | null;
}

interface ConfidenceBreakdown {
  method: "blended" | "retrieval_only" | "chunk_average" | string;
  final: number;
  components: ConfidenceComponent[];
}

interface AssistantMessage {
  id?: string;
  role: "assistant";
  content: string;
  timestamp?: string;
  confidence: number;
  confidenceBreakdown?: ConfidenceBreakdown | null;
  citations: CitationItem[];
  processingTime: number;
  storesSearched: string[];
  totalRetrieved: number;
  notes: string | null;
  isError?: boolean;
  is_pinned?: boolean;
  synthesisInfo?: SynthesisInfo;
  // Per-stage latency breakdown from app.core.tracing (QueryResponse.timings) —
  // live-response-only, like synthesisInfo, since chat_messages has no timings
  // column: history reloaded from the DB won't have this.
  timings?: Record<string, number> | null;
  // Sourced from retrieval_stats.graph_mode / graph_expanded, present on both the
  // streaming "done" event and the plain JSON fallback response — unlike
  // synthesisInfo (SSE-only), this is available regardless of which path answered.
  graphMode?: "none" | "local" | "global" | string;
  graphExpanded?: number;
  // Agentic RAG stats (PR #28) — present only when AGENTIC_RAG_ENABLED and the
  // backend returns them; the UI block is guarded on its presence.
  agenticStats?: {
    loops?: number;
    raven?: { reframed?: string | null } | null;
    [key: string]: unknown;
  } | null;
}

type ChatMessage = UserMessage | AssistantMessage;

function formatChatTimestamp(timestamp?: string): string {
  if (!timestamp) return "";
  const date = new Date(timestamp);
  if (Number.isNaN(date.getTime())) return "";
  return new Intl.DateTimeFormat("en-IN", {
    timeZone: "Asia/Kolkata",
    day: "2-digit",
    month: "short",
    year: "numeric",
    hour: "numeric",
    minute: "2-digit",
    second: "2-digit",
    hour12: true,
  }).format(date);
}

type TraceState = "pending" | "running" | "complete" | "skipped" | "error";

interface QueryTraceStep {
  id: string;
  label: string;
  detail: string;
  state: TraceState;
  chunks?: QueryTraceChunk[];
  reframedQuery?: string | null;
  subQueries?: string[];
}

interface QueryTraceChunk {
  chunkId?: string;
  filename: string;
  storeType: string;
  relevanceScore?: number;
  text: string;
}

const createQueryTrace = (query: string): QueryTraceStep[] => [
  { id: "query", label: "User query", detail: query, state: "complete" },
  { id: "graph-router", label: "GraphRAG router", detail: "Checking graph availability and query intent", state: "running" },
  { id: "global-search", label: "Global community search", detail: "Neo4j community summaries", state: "pending" },
  { id: "raven", label: "RAVEN query planner", detail: "Reframing the query for retrieval", state: "pending" },
  { id: "retrieve", label: "Hybrid retrieval", detail: "Searching indexed document stores", state: "running" },
  { id: "local-graph", label: "Local graph traversal", detail: "Expanding matching entities in Neo4j", state: "pending" },
  { id: "rerank", label: "Cross-encoder reranker", detail: "Ranking the most relevant evidence", state: "pending" },
  { id: "structured", label: "Structured table query", detail: "Checking for an exact table answer", state: "pending" },
  { id: "synthesize", label: "LLM synthesis", detail: "Building a cited answer", state: "pending" },
  { id: "response", label: "Final response", detail: "Preparing answer and citations", state: "pending" },
];

function toTraceChunks(raw: unknown): QueryTraceChunk[] {
  if (!Array.isArray(raw)) return [];
  return raw.map((chunk) => {
    const item = (chunk ?? {}) as Record<string, unknown>;
    return {
      chunkId: typeof item.chunk_id === "string" ? item.chunk_id : typeof item.chunkId === "string" ? item.chunkId : undefined,
      filename: String(item.document_filename ?? item.filename ?? "Document"),
      storeType: String(item.store_type ?? item.storeType ?? "document"),
      relevanceScore: typeof item.relevance_score === "number" ? item.relevance_score : item.relevanceScore as number | undefined,
      text: String(item.text ?? item.chunk_text ?? "").slice(0, 1200),
    };
  }).filter((chunk) => chunk.text.length > 0);
}

function traceStoreLabel(store: string): string {
  const labels: Record<string, string> = {
    vector: "Document",
    clause: "Legal clauses",
    research: "Research",
    table: "Tables",
    image: "Images",
    graph: "Neo4j graph",
  };
  return labels[store] ?? store;
}

function traceStoreSummary(chunks: QueryTraceChunk[]): string {
  const stores = Array.from(new Set(chunks.map((chunk) => traceStoreLabel(chunk.storeType))));
  return stores.length ? stores.join(", ") : "no store details";
}

function traceStoreNames(stores: unknown): string {
  if (!Array.isArray(stores) || stores.length === 0) return "no store details";
  return Array.from(new Set(stores.map((store) => traceStoreLabel(String(store))))).join(", ");
}

// ── Constants ──────────────────────────────────────────────────────────────────

const STORE_META: Record<string, { label: string; color: string; icon: typeof BookOpen; title: string }> = {
  vector:   { label: "Document", title: "Semantic vector search on document text",     color: "bg-blue-500/10 text-blue-400 border-blue-500/20",      icon: Zap },
  clause:   { label: "Legal",    title: "Legal clause store",                           color: "bg-purple-500/10 text-purple-400 border-purple-500/20", icon: Shield },
  research: { label: "Research", title: "Research & academic content store",            color: "bg-amber-500/10 text-amber-400 border-amber-500/20",   icon: BookOpen },
  table:    { label: "Table",    title: "Structured table data store",                  color: "bg-emerald-500/10 text-emerald-400 border-emerald-500/20", icon: FileText },
  image:    { label: "Image",    title: "Image & figure store",                         color: "bg-pink-500/10 text-pink-400 border-pink-500/20",      icon: FileText },
  graph:    { label: "Graph",    title: "Neo4j knowledge graph (entity/community reasoning)", color: "bg-cyan-500/10 text-cyan-400 border-cyan-500/20", icon: Network },
};

// GraphRAG routing mode (retrieval_stats.graph_mode / route_graphrag()) —
// "local" walked the Neo4j graph for entities named in the question and
// merged individually-cited chunks; "global" synthesized from community
// summaries (map-reduce over the graph, no per-chunk citations); "none"
// means the graph wasn't consulted at all for this query. Always shown so
// it's clear which mode answered, not just surfaced when graph was used.
function graphModeMeta(mode?: string | null): { label: string; title: string; textClass: string; badgeClass: string } {
  if (mode === "local") {
    return {
      label: "Graph: Local",
      title: "This answer's retrieval queried the Neo4j knowledge graph for entities named in the question — entity-based expansion, chunks are individually cited",
      textClass: "text-cyan-400",
      badgeClass: STORE_META.graph.color,
    };
  }
  if (mode === "global") {
    return {
      label: "Graph: Global",
      title: "This answer was synthesized from Neo4j community summaries (map-reduce over the knowledge graph) — not grounded in individually cited chunks",
      textClass: "text-indigo-400",
      badgeClass: "bg-indigo-500/10 text-indigo-400 border-indigo-500/20",
    };
  }
  return {
    label: "Graph: None",
    title: "This answer's retrieval did not use the Neo4j knowledge graph",
    textClass: "text-gray-400 dark:text-gray-500",
    badgeClass: "bg-gray-500/10 text-gray-400 dark:text-gray-500 border-gray-500/20",
  };
}

// ── Helpers ────────────────────────────────────────────────────────────────────

function groupSources(
  citations: CitationItem[]
): { filename: string; pages: number[]; pdfUrl: string | null; imageUrl: string | null; storeType: string; relevanceScore: number }[] {
  const map = new Map<string, { pages: Set<number>; pdfUrl: string | null; imageUrl: string | null; storeType: string; maxScore: number }>();
  citations.forEach((c) => {
    if (!map.has(c.filename)) {
      map.set(c.filename, { pages: new Set(), pdfUrl: null, imageUrl: null, storeType: c.store_type, maxScore: c.relevance_score });
    }
    const entry = map.get(c.filename)!;
    if (c.page_number != null) entry.pages.add(c.page_number);
    if (c.page_number_end != null) entry.pages.add(c.page_number_end);
    if (c.pdf_url && !entry.pdfUrl) entry.pdfUrl = c.pdf_url;
    if (c.store_type === "image" && c.image_url && !entry.imageUrl) entry.imageUrl = c.image_url;
    if (c.relevance_score > entry.maxScore) entry.maxScore = c.relevance_score;
  });
  return Array.from(map.entries()).map(([filename, { pages, pdfUrl, imageUrl, storeType, maxScore }]) => ({
    filename,
    pages: Array.from(pages).sort((a, b) => a - b),
    pdfUrl,
    imageUrl,
    storeType,
    relevanceScore: maxScore,
  }));
}

function confidenceInfo(c: number) {
  if (c >= 0.8) return { label: "High", color: "text-emerald-400", bg: "bg-emerald-500" };
  if (c >= 0.6) return { label: "Good", color: "text-blue-400",    bg: "bg-blue-500" };
  if (c >= 0.4) return { label: "Moderate", color: "text-amber-400", bg: "bg-amber-500" };
  return { label: "Low", color: "text-gray-400", bg: "bg-gray-500" };
}

// Compact "Retrieval XX%xYY%wt + Groq XX%×YY%wt" line shown inline under the
// confidence bar so the weighting is always visible in the chat response — no
// click/hover required. Returns null when there's nothing meaningful to show
// (e.g. confidence_breakdown missing for an older/restored message that
// predates this field).
function confidenceWeightSummary(breakdown?: ConfidenceBreakdown | null): string | null {
  if (!breakdown || !breakdown.components?.length) return null;
  const pct = (n: number) => Math.round(n * 100);

  if (breakdown.method === "blended") {
    const [retrieval, groq] = breakdown.components;
    if (!retrieval || !groq) return null;
    return `Retrieval ${pct(retrieval.score)}%×${pct(retrieval.weight)}%wt + Groq ${pct(groq.score)}%×${pct(groq.weight)}%wt`;
  }

  if (breakdown.method === "retrieval_only") {
    const detail = breakdown.components[0]?.detail;
    if (detail?.top_chunk_score != null && detail?.top3_mean_score != null && detail.weights) {
      return `Best chunk ${pct(detail.top_chunk_score)}%×${pct(detail.weights.top_chunk)}%wt + Top-3 avg ${pct(detail.top3_mean_score)}%×${pct(detail.weights.top3_mean)}%wt`;
    }
    return null;
  }

  if (breakdown.method === "chunk_average") {
    return breakdown.components[0]?.label ?? null;
  }

  return null;
}

function formatSessionDate(iso: string) {
  const d = new Date(iso);
  const now = new Date();
  const IST = { timeZone: "Asia/Kolkata" } as const;
  const diffDays = Math.floor(
    (new Date(now.toLocaleString("en-IN", IST)).getTime() -
      new Date(d.toLocaleString("en-IN", IST)).getTime()) /
      (1000 * 60 * 60 * 24)
  );
  if (diffDays === 0) return d.toLocaleTimeString("en-IN", { hour: "2-digit", minute: "2-digit", hour12: true, ...IST });
  if (diffDays === 1) return "Yesterday";
  if (diffDays < 7) return d.toLocaleDateString("en-IN", { weekday: "short", ...IST });
  return d.toLocaleDateString("en-IN", { month: "short", day: "numeric", ...IST });
}

// ── Markdown → plain text (for Copy) ───────────────────────────────────────────
// The chat answer is stored as markdown with [n] citation markers. The Copy
// button should yield clean prose: no **, #, `, list/table syntax, and no [n]
// citation numbers — just what the user reads, paste-ready.
function markdownToPlainText(md: string): string {
  let t = md;
  t = t.replace(/[ \t]*\[\d+\]/g, "");                                        // citation refs
  t = t.replace(/```[a-zA-Z0-9]*\r?\n?([\s\S]*?)```/g, "$1");                // fenced code → body
  t = t.replace(/!\[([^\]]*)\]\([^)]*\)/g, "$1");                             // images → alt
  t = t.replace(/\[([^\]]+)\]\([^)]*\)/g, "$1");                             // links → text
  t = t.replace(/^#{1,6}\s+/gm, "");                                          // headings
  t = t.replace(/^\s*>\s?/gm, "");                                            // blockquotes
  t = t.replace(/\*\*\*([\s\S]+?)\*\*\*/g, "$1");                            // bold+italic
  t = t.replace(/___?([\s\S]+?)___?/g, "$1");
  t = t.replace(/\*\*([\s\S]+?)\*\*/g, "$1");                                // bold
  t = t.replace(/__([\s\S]+?)__/g, "$1");
  t = t.replace(/\*([\s\S]+?)\*/g, "$1");                                     // italic
  t = t.replace(/_([\s\S]+?)_/g, "$1");
  t = t.replace(/~~([\s\S]+?)~~/g, "$1");                                     // strikethrough
  t = t.replace(/`([^`]+)`/g, "$1");                                          // inline code
  t = t.replace(/^\s*[-*+]\s+/gm, "• ");                                      // unordered list
  t = t.replace(/^\s*\d+\.\s+/gm, (m, off, str) => {                         // ordered list → n.
    const num = str.slice(0, off).match(/^\s*\d+\.\s+/gm)?.length ?? 0;
    return `${num + 1}. `;
  });
  t = t.replace(/^[-*_]{3,}\s*$/gm, "─────");                                // HR
  t = t.replace(/^\s*\|[\s:|-]+\|?\s*$/gm, "");                              // table separator
  t = t.replace(/^\s*\|(.+)\|\s*$/gm, (_m, row) =>                           // table rows
    row.split("|").map((c: string) => c.trim()).filter(Boolean).join("  "));
  t = t.replace(/\n{3,}/g, "\n\n");
  return t.trim();
}

function markdownToHtml(md: string): string {
  // Escape existing HTML entities first so we don't double-encode.
  let h = md
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");

  h = h.replace(/[ \t]*\[\d+\]/g, "");                                        // strip citation refs

  // Code blocks must be handled before inline code / bold to avoid false matches.
  h = h.replace(/```[a-zA-Z0-9]*\r?\n?([\s\S]*?)```/g,
    (_m, body) => `<pre><code>${body.trim()}</code></pre>`);
  h = h.replace(/`([^`]+)`/g, "<code>$1</code>");                             // inline code

  // Images → alt; Links → anchor.
  h = h.replace(/!\[([^\]]*)\]\(([^)]*)\)/g, "$1");
  h = h.replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<a href="$2">$1</a>');

  // Headings (longest first to avoid ### matching #).
  h = h.replace(/^###### (.+)$/gm, "<h6>$1</h6>");
  h = h.replace(/^##### (.+)$/gm,  "<h5>$1</h5>");
  h = h.replace(/^#### (.+)$/gm,   "<h4>$1</h4>");
  h = h.replace(/^### (.+)$/gm,    "<h3>$1</h3>");
  h = h.replace(/^## (.+)$/gm,     "<h2>$1</h2>");
  h = h.replace(/^# (.+)$/gm,      "<h1>$1</h1>");

  // Blockquotes.
  h = h.replace(/^&gt;\s?(.+)$/gm, "<blockquote>$1</blockquote>");

  // Bold+italic before bold/italic alone.
  h = h.replace(/\*\*\*([\s\S]+?)\*\*\*/g, "<strong><em>$1</em></strong>");
  h = h.replace(/___([\s\S]+?)___/g,        "<strong><em>$1</em></strong>");
  h = h.replace(/\*\*([\s\S]+?)\*\*/g,      "<strong>$1</strong>");
  h = h.replace(/__([\s\S]+?)__/g,          "<strong>$1</strong>");
  h = h.replace(/\*([\s\S]+?)\*/g,          "<em>$1</em>");
  h = h.replace(/_([\s\S]+?)_/g,            "<em>$1</em>");
  h = h.replace(/~~([\s\S]+?)~~/g,          "<s>$1</s>");

  // Horizontal rules.
  h = h.replace(/^[-*_]{3,}\s*$/gm, "<hr>");

  // Lists — convert markers then wrap consecutive <li> in <ul>/<ol>.
  h = h.replace(/^[ \t]*[-*+] (.+)$/gm,    "<li>$1</li>");
  h = h.replace(/^[ \t]*(\d+)\. (.+)$/gm,  "<li>$2</li>");
  h = h.replace(/(<li>[\s\S]+?<\/li>)(\n|$)/g, "$1\n");
  h = h.replace(/((?:<li>.*\n?)+)/g, (m) => `<ul>${m}</ul>`);

  // Tables — remove separator rows, convert data rows.
  h = h.replace(/^\|?[\s:|-]+\|?\s*\n/gm, "");
  h = h.replace(/^\|(.+)\|\s*$/gm, (_m, row) => {
    const cells = row.split("|").map((c: string) => c.trim()).filter(Boolean);
    return "<tr>" + cells.map((c: string) => `<td style="padding:2px 8px">${c}</td>`).join("") + "</tr>";
  });
  h = h.replace(/((?:<tr>.*\n?)+)/g, (m) =>
    `<table border="1" cellspacing="0" cellpadding="0" style="border-collapse:collapse">${m}</table>`);

  // Paragraphs — split on blank lines; skip block-level elements.
  const blocks = h.split(/\n{2,}/);
  h = blocks.map((block) => {
    const b = block.trim();
    if (!b) return "";
    if (/^<(h[1-6]|ul|ol|pre|table|blockquote|hr)[\s>]/.test(b)) return b;
    return `<p>${b.replace(/\n/g, "<br>")}</p>`;
  }).filter(Boolean).join("\n");

  return h;
}

// ── Markdown: answer ───────────────────────────────────────────────────────────

function highlightCitations(children: ReactNode): ReactNode {
  if (typeof children === "string") {
    const parts = children.split(/(\[\d+\])/g);
    if (parts.length === 1) return children;
    return parts.map((part, i) => {
      const match = part.match(/^\[(\d+)\]$/);
      if (match) {
        return (
          <span
            key={i}
            className="inline-flex items-center justify-center w-5 h-5 text-[10px] font-bold bg-blue-500/20 text-blue-300 rounded-full mx-0.5 align-text-top border border-blue-500/30"
            title={`Citation ${match[1]}`}
          >
            {match[1]}
          </span>
        );
      }
      return part;
    });
  }
  if (Array.isArray(children)) {
    return children.map((child, i) => <span key={i}>{highlightCitations(child)}</span>);
  }
  return children;
}

function RenderedAnswer({ content, isError }: { content: string; isError?: boolean }) {
  if (isError) {
    return (
      <div className="flex items-start gap-2 text-red-400">
        <AlertCircle className="h-4 w-4 mt-0.5 flex-shrink-0" />
        <p className="text-sm leading-relaxed">{content}</p>
      </div>
    );
  }
  return (
    <div className="prose prose-sm dark:prose-invert prose-p:leading-relaxed prose-li:marker:text-blue-500 prose-a:text-blue-400 max-w-none">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        rehypePlugins={[rehypeRaw]}
        components={{
          p: ({ children }) => <p>{highlightCitations(children)}</p>,
          li: ({ children }) => <li>{highlightCitations(children)}</li>,
          u: ({ children }) => (
            <u className="underline decoration-blue-500 decoration-2 underline-offset-2 bg-blue-500/10 px-0.5 rounded-sm">
              {children}
            </u>
          ),
          table: ({ children }) => (
            <div className="overflow-x-auto my-4 rounded-lg border border-gray-200 dark:border-gray-700/50 bg-gray-50 dark:bg-gray-800/30">
              <table className="min-w-full m-0">{children}</table>
            </div>
          ),
          th: ({ children }) => (
            <th className="px-4 py-2 border-b border-gray-200 dark:border-gray-700/50 bg-gray-100 dark:bg-gray-800/50">
              {children}
            </th>
          ),
          td: ({ children }) => (
            <td className="px-4 py-2 border-b border-gray-200/80 dark:border-gray-800/50">{children}</td>
          ),
        }}
      >
        {content}
      </ReactMarkdown>
    </div>
  );
}

// ── Markdown: citation ─────────────────────────────────────────────────────────

function CitationMarkdown({ content }: { content: string }) {
  const displayContent = content.length > 2000 ? content.slice(0, 2000) + "…" : content;
  return (
    <div className="prose-citation text-xs text-gray-700 dark:text-gray-300 leading-relaxed bg-gray-100/80 dark:bg-gray-900/40 rounded-md p-3 max-h-64 overflow-y-auto">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        rehypePlugins={[rehypeRaw]}
        components={{
          h1: ({ children }) => <strong className="block text-gray-800 dark:text-gray-200 mt-2 mb-1">{children}</strong>,
          h2: ({ children }) => <strong className="block text-gray-800 dark:text-gray-200 mt-2 mb-1">{children}</strong>,
          h3: ({ children }) => <strong className="block text-gray-800 dark:text-gray-200 mt-2 mb-1">{children}</strong>,
          h4: ({ children }) => <strong className="block text-gray-800 dark:text-gray-200 mt-2 mb-1">{children}</strong>,
          p: ({ children }) => <p className="mb-2 last:mb-0">{children}</p>,
          ul: ({ children }) => (
            <ul className="space-y-1 mb-2 ml-4 list-disc text-gray-500 dark:text-gray-400 marker:text-gray-400 dark:marker:text-gray-500">
              {children}
            </ul>
          ),
          ol: ({ children }) => (
            <ol className="space-y-1 mb-2 ml-4 list-decimal text-gray-500 dark:text-gray-400 marker:text-gray-400 dark:marker:text-gray-500">
              {children}
            </ol>
          ),
          li: ({ children }) => <li className="text-gray-700 dark:text-gray-300 pl-1">{children}</li>,
          strong: ({ children }) => <strong className="font-semibold text-gray-800 dark:text-gray-200">{children}</strong>,
          em: ({ children }) => <em className="italic text-gray-500 dark:text-gray-400">{children}</em>,
          u: ({ children }) => <u className="underline decoration-blue-500/50 decoration-2 underline-offset-2">{children}</u>,
          code: ({ children, className }) => {
            if (className) {
              return (
                <code className="block bg-gray-200/80 dark:bg-gray-950/50 rounded p-2 text-[11px] text-gray-600 dark:text-gray-400 overflow-x-auto my-2 font-mono">
                  {children}
                </code>
              );
            }
            return (
              <code className="bg-gray-200/80 dark:bg-gray-800/50 text-gray-700 dark:text-gray-300 px-1 py-0.5 rounded text-[11px] font-mono">
                {children}
              </code>
            );
          },
          table: ({ children }) => (
            <div className="overflow-x-auto my-2 rounded border border-gray-200 dark:border-gray-700/50">
              <table className="min-w-full text-[11px]">{children}</table>
            </div>
          ),
          thead: ({ children }) => <thead className="bg-gray-100 dark:bg-gray-800/50">{children}</thead>,
          th: ({ children }) => (
            <th className="px-2 py-1.5 text-left text-gray-700 dark:text-gray-300 font-medium border-b border-gray-200 dark:border-gray-700/50 whitespace-nowrap">
              {children}
            </th>
          ),
          td: ({ children }) => (
            <td className="px-2 py-1.5 text-gray-600 dark:text-gray-400 border-b border-gray-200/60 dark:border-gray-800/30 min-w-[100px]">
              {children}
            </td>
          ),
        }}
      >
        {displayContent}
      </ReactMarkdown>
    </div>
  );
}

// ── Source chip with inline citation detail ────────────────────────────────────
// Replaces the old CitationPanel dropdown. Clicking a PDF/source chip toggles
// an inline panel that shows all retrieved chunks for that file, with page
// numbers, store badge, relevance score, chunk text, and a direct PDF link.

function CopyChunkButton({ text }: { text: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <button
      type="button"
      title="Copy chunk text"
      onClick={(e) => {
        e.stopPropagation();
        navigator.clipboard.writeText(text);
        setCopied(true);
        setTimeout(() => setCopied(false), 2000);
      }}
      className="p-1 rounded text-gray-400 hover:text-blue-500 hover:bg-blue-500/10 transition-colors flex items-center justify-center flex-shrink-0"
    >
      {copied ? <Check className="h-3 w-3 text-green-500" /> : <Copy className="h-3 w-3" />}
    </button>
  );
}

interface SourceChipProps {
  filename: string;
  pages: number[];
  pdfUrl: string | null;
  imageUrl: string | null;
  storeType: string;
  relevanceScore: number;
  citations: CitationItem[]; // all citations for this filename
}

function SourceChipWithDetail({
  filename,
  pages,
  pdfUrl,
  imageUrl,
  storeType,
  relevanceScore,
  citations,
}: SourceChipProps) {
  const [open, setOpen] = useState(false);
  const isImageSource = storeType === "image" && imageUrl;
  const truncatedName = filename.length > 35 ? filename.slice(0, 32) + "…" : filename;
  const fileCitations = citations.filter((c) => c.filename === filename);

  return (
    <div className="relative">
      {/* Chip button */}
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        title={`${filename} — click to view source excerpts`}
        className={cn(
          "inline-flex items-center gap-1.5 text-[11px] rounded-lg px-2.5 py-1.5 border transition-all group cursor-pointer select-none",
          open
            ? isImageSource
              ? "bg-pink-500/15 text-pink-300 border-pink-500/40"
              : "bg-blue-500/15 text-blue-300 border-blue-500/40 dark:bg-blue-500/15 dark:text-blue-300"
            : isImageSource
              ? "bg-pink-500/5 text-gray-300 hover:text-pink-300 border-pink-700/30 hover:border-pink-500/40"
              : "bg-gray-100 dark:bg-gray-800/50 text-gray-600 dark:text-gray-300 hover:text-blue-600 dark:hover:text-blue-300 border-gray-200 dark:border-gray-700/40 hover:border-blue-400/50 dark:hover:border-blue-500/30"
        )}
      >
        <FileText
          className={cn(
            "h-3 w-3 flex-shrink-0",
            open
              ? isImageSource ? "text-pink-400" : "text-blue-400"
              : isImageSource ? "text-pink-500/60 group-hover:text-pink-400" : "text-gray-500 group-hover:text-blue-400"
          )}
        />
        <span className="font-medium truncate">{truncatedName}</span>
        {pages.length > 0 && (
          <span className="text-gray-400 dark:text-gray-500 text-[10px]">
            p.{pages.join(", ")}
          </span>
        )}
        {open
          ? <ChevronDown className={cn("h-2.5 w-2.5", isImageSource ? "text-pink-400" : "text-blue-400")} />
          : <ChevronRight className={cn("h-2.5 w-2.5", isImageSource ? "text-pink-500/50 group-hover:text-pink-400" : "text-gray-600 group-hover:text-blue-400")} />
        }
      </button>

      {/* Inline detail panel */}
      {open && (
        <div className="mt-2 rounded-xl border border-blue-500/20 bg-gray-50 dark:bg-gray-900/60 shadow-lg overflow-hidden backdrop-blur-sm">
          {/* Panel header */}
          <div className="flex items-center gap-2 px-3 py-2 border-b border-gray-200 dark:border-gray-700/40 bg-gray-100/80 dark:bg-gray-800/50">
            <FileText className="h-3.5 w-3.5 text-blue-400 flex-shrink-0" />
            <span className="text-xs font-semibold text-gray-800 dark:text-gray-100 truncate flex-1">{filename}</span>
            {pages.length > 0 && (
              <span className="text-[10px] font-mono text-blue-400 bg-blue-500/10 border border-blue-500/20 rounded px-1.5 py-0.5 flex-shrink-0">
                Page{pages.length > 1 ? "s" : ""} {pages.join(", ")}
              </span>
            )}
            {/* Open PDF button in header */}
            {!isImageSource && pdfUrl && (
              <a
                href={citationPdfUrl(pdfUrl, pages[0]) ?? undefined}
                target="_blank"
                rel="noopener noreferrer"
                title="Open PDF at source page"
                className="ml-auto inline-flex flex-shrink-0 items-center gap-1 rounded-md border border-blue-500/30 bg-blue-500/10 px-2 py-1 text-[10px] text-blue-400 hover:bg-blue-500/20 hover:text-blue-300 transition-colors"
                onClick={(e) => e.stopPropagation()}
              >
                <ExternalLink className="h-2.5 w-2.5" />
                Open PDF
              </a>
            )}
            {isImageSource && imageUrl && (
              <a
                href={imageUrl}
                target="_blank"
                rel="noopener noreferrer"
                title="View image"
                className="ml-auto inline-flex flex-shrink-0 items-center gap-1 rounded-md border border-pink-500/30 bg-pink-500/10 px-2 py-1 text-[10px] text-pink-400 hover:bg-pink-500/20 hover:text-pink-300 transition-colors"
                onClick={(e) => e.stopPropagation()}
              >
                <ExternalLink className="h-2.5 w-2.5" />
                View image
              </a>
            )}
          </div>

          {/* Citations for this file */}
          <div className="divide-y divide-gray-200/60 dark:divide-gray-700/30 max-h-96 overflow-y-auto">
            {fileCitations.map((c, idx) => {
              const storeMeta = STORE_META[c.store_type] ?? STORE_META.vector;
              const StoreIcon = storeMeta.icon;
              return (
                <div key={idx} className="px-3 py-3">
                  {/* Chunk metadata row */}
                  <div className="flex items-center flex-wrap gap-1.5 mb-2">
                    {/* Index badge */}
                    <span className="inline-flex items-center justify-center w-4.5 h-4.5 text-[10px] font-bold bg-blue-500/20 text-blue-300 rounded-full border border-blue-500/30">
                      {idx + 1}
                    </span>
                    {/* Page badge */}
                    {c.page_number != null && (
                      <span className="inline-flex items-center gap-1 text-[10px] font-mono px-1.5 py-0.5 rounded-md bg-indigo-500/10 text-indigo-400 border border-indigo-500/20">
                        <BookOpen className="h-2.5 w-2.5" />
                        Page{c.page_number_end != null && c.page_number_end !== c.page_number ? "s" : ""} {c.page_number}
                        {c.page_number_end != null && c.page_number_end !== c.page_number ? `-${c.page_number_end}` : ""}
                      </span>
                    )}
                    {/* Store badge */}
                    <span className={cn("inline-flex items-center gap-1 text-[10px] px-1.5 py-0.5 rounded-full border", storeMeta.color)}>
                      <StoreIcon className="h-2.5 w-2.5" />
                      {storeMeta.label}
                    </span>
                    {/* Relevance */}
                    <span className="text-[10px] text-gray-400 dark:text-gray-500 font-mono">
                      {Math.round(c.relevance_score * 100)}% match
                    </span>
                    {/* Section */}
                    {c.section_title && (
                      <span className="text-[10px] px-1.5 py-0.5 rounded bg-gray-200/80 dark:bg-gray-700/50 text-gray-600 dark:text-gray-400">
                        §{c.section_title}
                      </span>
                    )}
                    {c.clause_type && (
                      <span className="text-[10px] px-1.5 py-0.5 rounded bg-purple-500/10 text-purple-400 border border-purple-500/20">
                        {c.clause_type}
                      </span>
                    )}
                    {c.risk_level && (
                      <span className={cn(
                        "text-[10px] px-1.5 py-0.5 rounded border",
                        c.risk_level.toLowerCase() === "high"
                          ? "bg-red-500/10 text-red-400 border-red-500/20"
                          : c.risk_level.toLowerCase() === "medium"
                          ? "bg-amber-500/10 text-amber-400 border-amber-500/20"
                          : "bg-green-500/10 text-green-400 border-green-500/20"
                      )}>
                        Risk: {c.risk_level}
                      </span>
                    )}
                    {/* Per-citation PDF link */}
                    <div className="ml-auto flex items-center gap-1.5">
                      <CopyChunkButton text={c.chunk_text} />
                      {citationPdfUrl(c.pdf_url, c.page_number) && (
                        <a
                          href={citationPdfUrl(c.pdf_url, c.page_number) ?? undefined}
                          target="_blank"
                          rel="noopener noreferrer"
                          className="inline-flex items-center gap-1 text-[10px] text-blue-400 hover:text-blue-300 transition-colors bg-blue-500/5 px-2 py-1 rounded border border-blue-500/20"
                          onClick={(e) => e.stopPropagation()}
                        >
                          <ExternalLink className="h-2.5 w-2.5" />
                          p.{c.page_number}
                        </a>
                      )}
                    </div>
                  </div>

                  {/* Chunk text */}
                  <CitationMarkdown content={c.chunk_text} />

                  {/* Table markdown if present */}
                  {c.table_markdown && (
                    <div className="mt-2">
                      <CitationMarkdown content={c.table_markdown} />
                    </div>
                  )}

                  {/* Image preview for image-store citations */}
                  {c.store_type === "image" && c.image_url && (
                    <div className="mt-2 space-y-1">
                      <a
                        href={c.image_url}
                        target="_blank"
                        rel="noopener noreferrer"
                        title="Open image in new tab"
                        className="block w-fit"
                        onClick={(e) => e.stopPropagation()}
                      >
                        {/* eslint-disable-next-line @next/next/no-img-element */}
                        <img
                          src={c.image_url}
                          alt={c.caption || "Referenced image"}
                          className="max-h-40 max-w-full rounded-lg border border-gray-700/50 object-contain bg-gray-900/40 hover:opacity-80 hover:border-pink-500/40 transition-all cursor-pointer"
                          onError={(e) => {
                            (e.currentTarget as HTMLImageElement).style.display = "none";
                          }}
                        />
                      </a>
                    </div>
                  )}

                  {/* Developer Debug Info */}
                  <details className="mt-3 text-[10px] text-gray-500 dark:text-gray-400 border-t border-gray-200/50 dark:border-gray-700/30 pt-2 group/debug">
                    <summary className="cursor-pointer hover:text-gray-700 dark:hover:text-gray-300 select-none flex items-center gap-1">
                      <ChevronRight className="h-3 w-3 transition-transform group-open/debug:rotate-90" />
                      Developer Debug Info
                    </summary>
                    <div className="mt-2 pl-4 space-y-1 font-mono break-all bg-gray-100/50 dark:bg-gray-800/30 p-2 rounded-md">
                      <div><span className="font-semibold text-gray-600 dark:text-gray-300">Document ID:</span> {c.document_id}</div>
                      {c.chunk_type && <div><span className="font-semibold text-gray-600 dark:text-gray-300">Chunk Type:</span> {c.chunk_type}</div>}
                      {c.source_doi && <div><span className="font-semibold text-gray-600 dark:text-gray-300">Source DOI:</span> {c.source_doi}</div>}
                      <div><span className="font-semibold text-gray-600 dark:text-gray-300">Raw Score:</span> {c.relevance_score}</div>
                      {(c as any).bbox && <div><span className="font-semibold text-gray-600 dark:text-gray-300">Bounding Box:</span> {JSON.stringify((c as any).bbox)}</div>}
                    </div>
                  </details>
                </div>
              );
            })}
          </div>
        </div>
      )}
    </div>
  );
}

// ── Recent chats sidebar ───────────────────────────────────────────────────────

function RecentChatsSidebar({
  sessions,
  currentSessionId,
  isLoading,
  onNewChat,
  onSessionClick,
  onDeleteSession,
}: {
  sessions: ChatSession[];
  currentSessionId: string | null;
  isLoading: boolean;
  onNewChat: () => void;
  onSessionClick: (id: string) => void;
  onDeleteSession: (id: string) => Promise<void>;
}) {
  const [deletingId, setDeletingId] = useState<string | null>(null);

  const handleDelete = async (e: React.MouseEvent, id: string) => {
    e.stopPropagation();
    setDeletingId(id);
    await onDeleteSession(id);
    setDeletingId(null);
  };

  return (
    <div className="h-full flex flex-col bg-white dark:bg-[#202024] border border-slate-200 dark:border-white/[0.08] rounded-2xl overflow-hidden w-64 shadow-xs dark:shadow-xl transition-colors">
      {/* Header */}
      <div className="flex-shrink-0 px-4 py-3 border-b border-slate-200 dark:border-white/[0.08] flex items-center gap-2">
        <span className="text-sm font-semibold text-slate-800 dark:text-gray-100">Recent Chats</span>
      </div>

      {/* New chat button */}
      <div className="flex-shrink-0 px-3 py-2 border-b border-slate-100 dark:border-white/[0.06]">
        <button
          type="button"
          onClick={onNewChat}
          className="w-full flex items-center gap-2 px-3 py-2 text-sm font-medium text-indigo-600 dark:text-indigo-400 bg-indigo-50 dark:bg-indigo-500/10 border border-indigo-200 dark:border-indigo-500/20 rounded-xl hover:bg-indigo-100 dark:hover:bg-indigo-500/20 transition-all"
        >
          <Plus className="h-3.5 w-3.5" />
          New Chat
        </button>
      </div>

      {/* Sessions list */}
      <div className="flex-1 overflow-y-auto py-1">
        {isLoading && (
          <div className="flex items-center justify-center py-8">
            <Loader2 className="h-4 w-4 animate-spin text-gray-300 dark:text-gray-600" />
          </div>
        )}
        {!isLoading && sessions.length === 0 && (
          <div className="flex flex-col items-center justify-center py-10 px-4 text-center">
            <MessageSquare className="h-6 w-6 text-gray-300 dark:text-gray-600 mb-2" />
            <p className="text-xs text-gray-400 dark:text-gray-600">No chats yet</p>
            <p className="text-[10px] text-gray-300 dark:text-gray-700 mt-0.5">Start asking a question</p>
          </div>
        )}
        {sessions.map((session) => (
          <div key={session.id} className="px-2 py-0.5">
            <button
              type="button"
              onClick={() => onSessionClick(session.id)}
              className={cn(
                "w-full text-left px-3 py-2.5 rounded-xl transition-all group flex items-start gap-2",
                session.id === currentSessionId
                  ? "bg-indigo-50 dark:bg-indigo-500/15 border border-indigo-200 dark:border-indigo-500/30"
                  : "hover:bg-slate-100 dark:hover:bg-white/[0.04] border border-transparent"
              )}
            >
              <div className="flex-1 min-w-0">
                <p
                  className={cn(
                    "text-xs font-medium truncate leading-relaxed",
                    session.id === currentSessionId
                      ? "text-indigo-700 dark:text-indigo-300 font-semibold"
                      : "text-slate-700 dark:text-slate-300"
                  )}
                >
                  {session.title}
                </p>
                <div className="flex items-center gap-1.5 mt-0.5">
                  <span className="text-[10px] text-gray-400 dark:text-gray-500">
                    {formatSessionDate(session.updated_at)}
                  </span>
                  {session.message_count > 0 && (
                    <span className="text-[10px] text-gray-300 dark:text-gray-600">
                      · {Math.ceil(session.message_count / 2)} turn
                      {Math.ceil(session.message_count / 2) !== 1 ? "s" : ""}
                    </span>
                  )}
                </div>
              </div>

              <button
                type="button"
                onClick={(e) => handleDelete(e, session.id)}
                disabled={deletingId === session.id}
                className="flex-shrink-0 opacity-0 group-hover:opacity-100 p-1 rounded hover:bg-red-100 dark:hover:bg-red-500/10 hover:text-red-500 dark:hover:text-red-400 text-gray-400 dark:text-gray-600 transition-all disabled:opacity-50"
                title="Delete chat"
              >
                {deletingId === session.id ? (
                  <Loader2 className="h-3 w-3 animate-spin" />
                ) : (
                  <Trash2 className="h-3 w-3" />
                )}
              </button>
            </button>
          </div>
        ))}
      </div>
    </div>
  );
}

// Model names are fetched from /health at runtime so this panel always
// reflects the actual backend config instead of hardcoded stale strings.
function buildPipelineStages(embeddingModel: string, rerankerName: string) {
  return [
    {
      id: "embed",
      label: "Embedding query",
      model: embeddingModel || "BGE-large-en-v1.5",
      detail: "Converting question to 1024-dim BGE vector",
      estimatedMs: 300,
    },
    {
      id: "retrieve",
      label: "Searching documents",
      model: "pgvector · HNSW",
      detail: "Parallel HNSW vector search across knowledge stores",
      estimatedMs: 1200,
    },
    {
      id: "rerank",
      label: "Re-ranking results",
      model: rerankerName || "MiniLM-L-6-v2",
      detail: `Cross-encoder scoring · ${rerankerName || "MiniLM-L-6-v2"}`,
      estimatedMs: 400,
    },
    {
      id: "synthesize",
      label: "Groq LLM synthesizing",
      model: "",
      detail: "Building answer from retrieved context",
      estimatedMs: Infinity,
    },
  ];
}

// ── Query pipeline timeline ───────────────────────────────────────────────────

function QueryTimeline({ synthesisInfo, embeddingModel, rerankerName }: {
  synthesisInfo: SynthesisInfo | null;
  embeddingModel: string;
  rerankerName: string;
}) {
  const PIPELINE_STAGES = buildPipelineStages(embeddingModel, rerankerName);
  const [activeIdx, setActiveIdx] = useState(0);
  const [elapsedMs, setElapsedMs] = useState(0);
  const startRef = useRef(Date.now());

  // SSE per-stage events are only emitted when AGENTIC_RAG_ENABLED and are not
  // yet plumbed into this component, so activeStageId has no source today. Keep
  // it null → the time-estimate animation below drives the active stage (the
  // documented fallback). When stage events are wired, pass activeStageId in as
  // a prop and this logic activates automatically.
  const activeStageId: string | null = null;
  const sseIdx = activeStageId != null
    ? PIPELINE_STAGES.findIndex((s) => s.id === activeStageId)
    : -1;
  const resolvedActiveIdx = sseIdx >= 0 ? sseIdx : activeIdx;

  useEffect(() => {
    // Only run the time-estimate animation when no SSE stage events are arriving.
    if (activeStageId != null) return;
    const id = setInterval(() => {
      const ms = Date.now() - startRef.current;
      setElapsedMs(ms);
      let cumulative = 0;
      for (let i = 0; i < PIPELINE_STAGES.length - 1; i++) {
        cumulative += PIPELINE_STAGES[i].estimatedMs;
        if (ms >= cumulative) setActiveIdx(i + 1);
        else break;
      }
    }, 80);
    return () => clearInterval(id);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const preSynthMs = PIPELINE_STAGES.slice(0, -1).reduce((s, st) => s + st.estimatedMs, 0);
  const synthElapsed = resolvedActiveIdx === PIPELINE_STAGES.length - 1
    ? Math.max(0, elapsedMs - preSynthMs)
    : 0;

  return (
    <div className="py-1 min-w-[260px]">
      <p className="text-[10px] font-semibold text-gray-400 dark:text-gray-500 uppercase tracking-wider mb-3">
        Processing pipeline
      </p>
      <div className="space-y-0">
        {PIPELINE_STAGES.map((stage, idx) => {
          const isDone = idx < resolvedActiveIdx;
          const isActive = idx === resolvedActiveIdx;

          return (
            <div key={stage.id} className="flex items-start gap-3">
              {/* Icon + connector line */}
              <div className="flex flex-col items-center">
                <div className={cn(
                  "flex-shrink-0 w-5 h-5 rounded-full flex items-center justify-center transition-all duration-300",
                  isDone
                    ? "bg-emerald-500/20 border border-emerald-500/40 text-emerald-400"
                    : isActive
                      ? "bg-blue-500/20 border border-blue-500/40 text-blue-400"
                      : "bg-gray-100 dark:bg-gray-800/50 border border-gray-200 dark:border-gray-700/40 text-gray-400 dark:text-gray-600"
                )}>
                  {isDone ? (
                    <Check className="h-2.5 w-2.5" />
                  ) : isActive ? (
                    <Loader2 className="h-2.5 w-2.5 animate-spin" />
                  ) : (
                    <div className="w-1.5 h-1.5 rounded-full bg-gray-300 dark:bg-gray-600" />
                  )}
                </div>
                {idx < PIPELINE_STAGES.length - 1 && (
                  <div className={cn(
                    "w-px h-6 mt-0.5 transition-colors duration-500",
                    isDone ? "bg-emerald-500/30" : "bg-gray-200 dark:bg-gray-700/40"
                  )} />
                )}
              </div>

              {/* Text */}
              <div className="flex-1 pt-0.5 pb-2">
                <div className="flex items-center gap-2">
                  <span className={cn(
                    "text-xs font-medium transition-colors duration-300",
                    isDone
                      ? "text-emerald-400"
                      : isActive
                        ? "text-gray-800 dark:text-gray-100"
                        : "text-gray-400 dark:text-gray-600"
                  )}>
                    {stage.label}
                  </span>
                  {isActive && stage.id === "synthesize" && synthElapsed > 800 && (
                    <span className="text-[10px] text-gray-400 dark:text-gray-500 font-mono tabular-nums">
                      {(synthElapsed / 1000).toFixed(1)}s
                    </span>
                  )}
                </div>
                {isActive && (
                  <>
                    <p className="text-[10px] text-gray-400 dark:text-gray-500 mt-0.5 leading-relaxed">
                      {stage.detail}
                    </p>
                    {stage.id === "synthesize" && synthesisInfo && (
                      <div className="flex flex-wrap gap-1 mt-1.5">
                        <span className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded bg-blue-500/10 border border-blue-500/20 text-[9px] font-mono text-blue-400">
                          <Cpu className="h-2 w-2" />
                          {synthesisInfo.model}
                        </span>
                        <span className="inline-flex items-center px-1.5 py-0.5 rounded bg-gray-500/10 border border-gray-500/20 text-[9px] font-mono text-gray-400">
                          {synthesisInfo.chunksUsed} ctx chunks
                        </span>
                        <span className="inline-flex items-center px-1.5 py-0.5 rounded bg-gray-500/10 border border-gray-500/20 text-[9px] font-mono text-gray-400">
                          {synthesisInfo.maxTokens} tok budget
                        </span>
                        {synthesisInfo.storesSearched.length > 0 && (
                          <span className="inline-flex items-center px-1.5 py-0.5 rounded bg-gray-500/10 border border-gray-500/20 text-[9px] font-mono text-gray-400">
                            {synthesisInfo.storesSearched.length} store{synthesisInfo.storesSearched.length !== 1 ? "s" : ""}
                          </span>
                        )}
                      </div>
                    )}
                  </>
                )}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

// ── Confidence badge (click to expand full weighted breakdown) ──────────────────

function QueryTraceSidebar({ steps, isPending }: { steps: QueryTraceStep[]; isPending: boolean }) {
  const [expandedSteps, setExpandedSteps] = useState<Record<string, boolean>>({});

  const toggleStep = (id: string) => {
    setExpandedSteps((current) => ({ ...current, [id]: !current[id] }));
  };
  if (steps.length === 0) return null;

  return (
    <aside className="hidden lg:flex w-64 flex-shrink-0 flex-col rounded-xl border border-gray-200 dark:border-gray-800 bg-white dark:bg-gray-900 shadow-xl overflow-hidden">
      <div className="px-4 py-3 border-b border-gray-200 dark:border-gray-800 flex items-center gap-2">
        <Network className="h-4 w-4 text-cyan-500" />
        <div className="min-w-0">
          <p className="text-sm font-semibold text-gray-900 dark:text-gray-100">Query pipeline</p>
          <p className="text-[10px] text-gray-500 dark:text-gray-400">
            {isPending ? "Live processing trace" : "Completed processing trace"}
          </p>
        </div>
        {isPending && <Loader2 className="ml-auto h-3.5 w-3.5 animate-spin text-blue-500" />}
      </div>

      <div className="flex-1 overflow-y-auto px-4 py-4">
        {steps.map((step, index) => {
          const isComplete = step.state === "complete";
          const isRunning = step.state === "running";
          const isSkipped = step.state === "skipped";
          const isError = step.state === "error";

          return (
            <div key={step.id} className="flex gap-3">
              <div className="flex flex-col items-center">
                <div className={cn(
                  "mt-0.5 flex h-5 w-5 flex-shrink-0 items-center justify-center rounded-full border",
                  isComplete ? "border-emerald-500/40 bg-emerald-500/15 text-emerald-500" :
                  isRunning ? "border-blue-500/40 bg-blue-500/15 text-blue-500" :
                  isError ? "border-red-500/40 bg-red-500/15 text-red-500" :
                  isSkipped ? "border-gray-200 dark:border-gray-700 bg-gray-100 dark:bg-gray-800 text-gray-400" :
                  "border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-900 text-gray-300 dark:text-gray-600"
                )}>
                  {isComplete ? <Check className="h-3 w-3" /> :
                   isRunning ? <Loader2 className="h-3 w-3 animate-spin" /> :
                   isError ? <AlertCircle className="h-3 w-3" /> :
                   <span className="h-1.5 w-1.5 rounded-full bg-current" />}
                </div>
                {index < steps.length - 1 && (
                  <div className={cn(
                    "my-1 h-7 w-px",
                    isComplete ? "bg-emerald-500/30" : "bg-gray-200 dark:bg-gray-800"
                  )} />
                )}
              </div>
              <div className="min-w-0 pb-3">
                <p className={cn(
                  "text-xs font-medium",
                  isComplete ? "text-emerald-600 dark:text-emerald-400" :
                  isRunning ? "text-gray-900 dark:text-gray-100" :
                  isError ? "text-red-600 dark:text-red-400" : "text-gray-400 dark:text-gray-600"
                )}>{step.label}</p>
                <p className={cn(
                  "mt-0.5 text-[10px] leading-relaxed",
                  isRunning ? "text-gray-600 dark:text-gray-400" : "text-gray-500 dark:text-gray-500"
                )}>{step.detail}</p>
                {((step.id === "raven" && step.state !== "skipped")
                  || step.id === "retrieve"
                  || step.id === "rerank"
                  || step.chunks?.length) ? (
                  <button
                    type="button"
                    onClick={() => toggleStep(step.id)}
                    className="mt-1 text-[10px] font-medium text-cyan-500 hover:text-cyan-400"
                  >
                    {expandedSteps[step.id] ? "Hide details" : (
                      step.id === "raven" ? "View reframed query" :
                      "View " + (step.chunks?.length ?? 0) + " chunk" + (step.chunks?.length === 1 ? "" : "s")
                    )}
                  </button>
                ) : null}
                {expandedSteps[step.id] && (
                  <div className="mt-2 space-y-1.5">
                    {step.id === "raven" && (
                      <div className="rounded-md border border-cyan-500/20 bg-cyan-500/5 p-2">
                        <p className="text-[9px] uppercase tracking-wide text-cyan-500/80">Reframed query</p>
                        <p className="mt-1 text-[10px] leading-relaxed text-gray-700 dark:text-gray-300">
                          {step.reframedQuery || "RAVEN used the original query without additional reframing."}
                        </p>
                        {!!step.subQueries?.length && (
                          <>
                            <p className="mt-2 text-[9px] uppercase tracking-wide text-cyan-500/80">Sub-queries</p>
                            <ul className="mt-1 space-y-1 text-[10px] text-gray-600 dark:text-gray-400">
                              {step.subQueries.map((subQuery, index) => <li key={index}>• {subQuery}</li>)}
                            </ul>
                          </>
                        )}
                      </div>
                    )}
                    {(step.id === "retrieve" || step.id === "rerank") && !!step.chunks?.length && (
                      <div className="space-y-1">
                        {step.id === "retrieve" && (
                          <p className="rounded-md border border-blue-500/20 bg-blue-500/5 px-2 py-1.5 text-[10px] text-blue-600 dark:text-blue-400">
                            Stores selected for hybrid retrieval: {traceStoreSummary(step.chunks)}
                          </p>
                        )}
                        <p className="rounded-md border border-cyan-500/20 bg-cyan-500/5 px-2 py-1.5 text-[10px] text-cyan-600 dark:text-cyan-400">
                          {step.id === "retrieve" ? "Retrieved from" : "Selected from"}: {traceStoreSummary(step.chunks)}
                        </p>
                      </div>
                    )}
                    {step.chunks?.map((chunk, chunkIndex) => (
                      <div key={chunk.chunkId ?? (step.id + "-" + chunkIndex)} className="rounded-md border border-gray-200 dark:border-gray-700/60 bg-gray-50 dark:bg-gray-800/50 p-2">
                        <div className="flex items-center justify-between gap-2">
                          <span className="truncate text-[9px] font-medium text-gray-700 dark:text-gray-300" title={chunk.filename}>{chunk.filename}</span>
                          <span className="flex-shrink-0 rounded bg-cyan-500/10 px-1 py-0.5 text-[9px] text-cyan-500">{traceStoreLabel(chunk.storeType)}</span>
                        </div>
                        <p className="mt-1 text-[10px] leading-relaxed text-gray-600 dark:text-gray-400">{chunk.text}</p>
                        {typeof chunk.relevanceScore === "number" && (
                          <p className="mt-1 text-[9px] text-gray-500">Relevance: {Math.round(chunk.relevanceScore * 100)}%</p>
                        )}
                      </div>
                    ))}
                    {(step.id === "retrieve" || step.id === "rerank") && !step.chunks?.length && (
                      <p className="rounded-md border border-gray-200 dark:border-gray-700/60 p-2 text-[10px] text-gray-500">
                        Chunk previews were not included in this response.
                      </p>
                    )}
                  </div>
                )}
              </div>
            </div>
          );
        })}
      </div>
    </aside>
  );
}

/** Ensure a source link opens the page represented by the citation. Some
 * older API responses contain a signed PDF URL without a fragment, while
 * grouped citations can otherwise retain the first citation's page. */
function citationPdfUrl(url: string | null | undefined, page?: number | null): string | null {
  if (!url) return null;
  if (page == null) return url;
  const withoutFragment = url.split("#", 1)[0];
  return `${withoutFragment}#page=${page}`;
}

function ConfidenceBadge({
  confidence,
  breakdown,
}: {
  confidence: number;
  breakdown?: ConfidenceBreakdown | null;
}) {
  const [expanded, setExpanded] = useState(false);
  const ci = confidenceInfo(confidence);
  const pct = Math.round(confidence * 100);
  const weightSummary = confidenceWeightSummary(breakdown);
  const pctOf = (n: number) => Math.round(n * 100);

  const methodNote =
    breakdown?.method === "blended"
      ? "Weighted blend of reranker relevance and Groq's self-rated confidence."
      : breakdown?.method === "retrieval_only"
      ? "Based on reranker relevance scores only — no Groq self-rating was available for this answer."
      : breakdown?.method === "chunk_average"
      ? "Simple average of the relevance scores across all cited chunks."
      : null;

  return (
    <div className="flex flex-col gap-0.5">
      <button
        type="button"
        onClick={() => breakdown && setExpanded((e) => !e)}
        disabled={!breakdown}
        className={cn("flex flex-col gap-0.5 text-left", breakdown ? "cursor-pointer group" : "cursor-default")}
        title={breakdown ? "Click for confidence details" : `Confidence: ${pct}% (${ci.label})`}
      >
        <div className="flex items-center gap-1.5">
          <div className="w-16 h-1.5 rounded-full bg-gray-200 dark:bg-gray-700/50 overflow-hidden">
            <div
              className={cn("h-full rounded-full transition-all", ci.bg)}
              style={{ width: `${pct}%` }}
            />
          </div>
          <span className={cn("text-[10px] font-medium", ci.color)}>
            {ci.label} confidence · {pct}%
          </span>
          {breakdown && (
            expanded
              ? <ChevronDown className="h-2.5 w-2.5 text-gray-400 group-hover:text-gray-300 transition-colors" />
              : <ChevronRight className="h-2.5 w-2.5 text-gray-400 group-hover:text-gray-300 transition-colors" />
          )}
        </div>
        {weightSummary && (
          <span className="text-[9px] font-mono text-gray-400 dark:text-gray-500 pl-0.5">
            {weightSummary}
          </span>
        )}
      </button>

      {expanded && breakdown && (
        <div className="ml-0.5 pl-2.5 border-l-2 border-gray-200 dark:border-gray-700/50 space-y-2 py-1 max-w-xs">
          {breakdown.components.map((comp, i) => (
            <div key={i} className="space-y-1">
              <div className="flex items-center justify-between gap-2 text-[10px]">
                <span className="text-gray-500 dark:text-gray-400">{comp.label}</span>
                <span className="font-mono text-gray-600 dark:text-gray-300">
                  {pctOf(comp.score)}% <span className="text-gray-400 dark:text-gray-600">× {pctOf(comp.weight)}% wt</span>
                </span>
              </div>
              <div className="w-full h-1 rounded-full bg-gray-200 dark:bg-gray-700/50 overflow-hidden">
                <div className="h-full rounded-full bg-blue-400/70 dark:bg-blue-500/60" style={{ width: `${pctOf(comp.score)}%` }} />
              </div>

              {/* Retrieval sub-components (best chunk vs top-3 mean) */}
              {comp.detail && comp.detail.top_chunk_score != null && (
                <div className="ml-2 pl-2 border-l border-gray-200/70 dark:border-gray-700/40 space-y-1 pt-0.5">
                  <div className="flex items-center justify-between gap-2 text-[9px] text-gray-400 dark:text-gray-500">
                    <span>Best-matching chunk</span>
                    <span className="font-mono">
                      {pctOf(comp.detail.top_chunk_score)}% × {pctOf(comp.detail.weights?.top_chunk ?? 0)}% wt
                    </span>
                  </div>
                  <div className="flex items-center justify-between gap-2 text-[9px] text-gray-400 dark:text-gray-500">
                    <span>Top-3 chunk average</span>
                    <span className="font-mono">
                      {pctOf(comp.detail.top3_mean_score ?? 0)}% × {pctOf(comp.detail.weights?.top3_mean ?? 0)}% wt
                    </span>
                  </div>
                </div>
              )}
            </div>
          ))}

          <div className="flex items-center justify-between text-[10px] pt-1.5 border-t border-gray-200/70 dark:border-gray-700/40">
            <span className="font-semibold text-gray-600 dark:text-gray-300">Final confidence</span>
            <span className={cn("font-mono font-semibold", ci.color)}>{pctOf(breakdown.final)}%</span>
          </div>

          {methodNote && (
            <p className="text-[9px] text-gray-400 dark:text-gray-500 italic leading-relaxed">
              {methodNote}
            </p>
          )}
        </div>
      )}
    </div>
  );
}

// ── Timings badge (per-stage latency breakdown, shown by default) ───────────────
// Sourced from app.core.tracing.stage() via QueryResponse.timings — raw keys are
// call-site names from query.py/retriever_service.py/hybrid_search_service.py,
// e.g. "store-query:vector", "hybrid-semantic", "kw-store-query:table",
// "structured-query", "synthesize". TIMING_LABELS translates the known ones to
// readable text; anything unrecognized falls back to a prettified raw key so new
// stage() call sites show up without needing a frontend change.

const TIMING_LABELS: Record<string, string> = {
  // agentic_pipeline.run() internally runs RAVEN (query reframing) -> a bounded
  // hybrid-retrieval loop -> SPYDER (sufficiency judge), looping hybrid+SPYDER
  // up to SPYDER_MAX_LOOPS times. None of those three are individually wrapped
  // in tracing.stage() (only emitted as SSE "stage" events for the UI's live
  // progress indicator), so this total can't be broken down further yet —
  // the label just documents what's inside it.
  "agentic-pipeline": "Agentic (RAVEN + Hybrid + SPYDER)",
  "retrieve": "Retrieve",
  "vector-store-fanout": "Vector fan-out",
  "keyword-store-fanout": "Keyword fan-out",
  "hybrid-semantic": "Semantic",
  "hybrid-keyword": "Keyword",
  "hybrid-rrf-fuse": "RRF fuse",
  "rank": "Rerank",
  "structured-query": "Table lookup",
  "synthesize": "Synthesis",
  "graphrag-route": "Graph route",
  "graphrag-global": "Graph global",
  "graphrag-local": "Graph local",
};

function timingLabel(stage: string): string {
  if (TIMING_LABELS[stage]) return TIMING_LABELS[stage];
  const m = stage.match(/^(store-query|kw-store-query):(.+)$/);
  if (m) return `${m[1] === "kw-store-query" ? "Keyword" : "Query"}: ${m[2]}`;
  return stage.replace(/[:_-]+/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

// Only the top-level pipeline phases are shown — "vector-store-fanout" /
// "store-query:*" / "keyword-store-fanout" / "kw-store-query:*" are sub-timings
// nested INSIDE "hybrid-semantic" / "hybrid-keyword" (see hybrid_search_service.py
// and retriever_service.py), so surfacing both is redundant: the child times
// already sum into the parent phase they ran under.
const PRIMARY_STAGES = new Set([
  "agentic-pipeline",
  "retrieve",
  "hybrid-semantic",
  "hybrid-keyword",
  "hybrid-rrf-fuse",
  "rank",
  "structured-query",
  "synthesize",
  "graphrag-route",
  "graphrag-global",
  "graphrag-local",
]);

// hybrid-semantic/keyword/rrf-fuse are timed by hybrid_search_service.py but
// invoked FROM INSIDE whichever of these wraps the call — "agentic-pipeline"
// (agentic_pipeline.run(), the default/AGENTIC_RAG_ENABLED path) or "retrieve"
// (the classic path's _classic_retrieve_fn()). Their durations are already
// counted inside the parent's elapsed time, so listing both at equal weight
// double-counts the same work — the panel groups them under whichever parent
// is present instead.
const HYBRID_CHILD_STAGES = ["hybrid-semantic", "hybrid-keyword", "hybrid-rrf-fuse"];
const HYBRID_PARENT_CANDIDATES = ["agentic-pipeline", "retrieve"];

function TimingsBadge({
  processingTime,
  timings,
}: {
  processingTime: number;
  timings?: Record<string, number> | null;
}) {
  const [collapsed, setCollapsed] = useState(false);
  const allEntries = timings ? Object.entries(timings).filter(([, v]) => typeof v === "number") : [];
  // Fall back to showing everything if none of the known primary keys are
  // present (e.g. a future stage() call site not yet in PRIMARY_STAGES) so the
  // panel never silently goes empty.
  const primaryEntries = allEntries.filter(([stage]) => PRIMARY_STAGES.has(stage));
  const entries = primaryEntries.length > 0 ? primaryEntries : allEntries;
  const hasBreakdown = entries.length > 0;

  const stageMap = new Map(entries);
  const parentStage = HYBRID_PARENT_CANDIDATES.find((p) => stageMap.has(p));
  const childStages = parentStage
    ? HYBRID_CHILD_STAGES.filter((c) => stageMap.has(c))
    : [];
  const childSet = new Set(childStages);
  // Top-level rows: everything that isn't a nested hybrid child. Summing just
  // these (not the children) reproduces the total processing time, since the
  // children's time is already inside their parent's duration.
  const topLevel = entries.filter(([stage]) => !childSet.has(stage));
  const topLevelSum = topLevel.reduce((sum, [, s]) => sum + s, 0);
  const maxStage = topLevel.length > 0 ? Math.max(...topLevel.map(([, v]) => v)) : 0;
  const maxChild = childStages.length > 0 ? Math.max(...childStages.map((c) => stageMap.get(c)!)) : 0;
  const expanded = hasBreakdown && !collapsed;

  return (
    <div className="flex flex-col gap-0.5 flex-1 min-w-0">
      <button
        type="button"
        onClick={() => hasBreakdown && setCollapsed((c) => !c)}
        disabled={!hasBreakdown}
        className={cn(
          "flex items-center gap-1 text-[10px] text-gray-500 dark:text-gray-500 pt-0.5 flex-shrink-0",
          hasBreakdown ? "cursor-pointer group" : "cursor-default"
        )}
        title={hasBreakdown ? "Click to toggle per-stage retrieval latency breakdown" : `Processing time: ${processingTime}s`}
      >
        <Clock className="h-2.5 w-2.5" />
        {processingTime}s
        {hasBreakdown && (
          expanded
            ? <ChevronDown className="h-2.5 w-2.5 text-gray-400 group-hover:text-gray-300 transition-colors" />
            : <ChevronRight className="h-2.5 w-2.5 text-gray-400 group-hover:text-gray-300 transition-colors" />
        )}
      </button>

      {expanded && (
        <div className="ml-0.5 pl-2.5 border-l-2 border-gray-200 dark:border-gray-700/50 py-1 w-full space-y-2">
          <div
            className="grid gap-x-4 gap-y-1.5"
            style={{ gridTemplateColumns: "repeat(auto-fit, minmax(170px, 1fr))" }}
          >
            {topLevel
              .sort(([, a], [, b]) => b - a)
              .map(([stage, seconds]) => (
                <div key={stage} className="space-y-0.5 min-w-0">
                  <div className="flex items-start justify-between gap-2 text-[10px]">
                    <span className="text-gray-500 dark:text-gray-400">{timingLabel(stage)}</span>
                    <span className="font-mono text-gray-600 dark:text-gray-300 flex-shrink-0">{seconds.toFixed(2)}s</span>
                  </div>
                  <div className="w-full h-1 rounded-full bg-gray-200 dark:bg-gray-700/50 overflow-hidden">
                    <div
                      className="h-full rounded-full bg-amber-400/70 dark:bg-amber-500/60"
                      style={{ width: `${maxStage > 0 ? (seconds / maxStage) * 100 : 0}%` }}
                    />
                  </div>
                </div>
              ))}
          </div>

          {childStages.length > 0 && (
            <div className="pl-2.5 border-l border-gray-200/70 dark:border-gray-700/40 space-y-1">
              <p className="text-[9px] text-gray-400 dark:text-gray-600 italic">
                within {timingLabel(parentStage!)} — already counted above, not additional time
              </p>
              <div
                className="grid gap-x-4 gap-y-1"
                style={{ gridTemplateColumns: "repeat(auto-fit, minmax(170px, 1fr))" }}
              >
                {childStages
                  .map((stage) => [stage, stageMap.get(stage)!] as [string, number])
                  .sort(([, a], [, b]) => b - a)
                  .map(([stage, seconds]) => (
                    <div key={stage} className="space-y-0.5 min-w-0">
                      <div className="flex items-start justify-between gap-2 text-[9px]">
                        <span className="text-gray-400 dark:text-gray-500">{timingLabel(stage)}</span>
                        <span className="font-mono text-gray-500 dark:text-gray-400 flex-shrink-0">{seconds.toFixed(2)}s</span>
                      </div>
                      <div className="w-full h-1 rounded-full bg-gray-200/70 dark:bg-gray-700/30 overflow-hidden">
                        <div
                          className="h-full rounded-full bg-gray-400/50 dark:bg-gray-500/40"
                          style={{ width: `${maxChild > 0 ? (seconds / maxChild) * 100 : 0}%` }}
                        />
                      </div>
                    </div>
                  ))}
              </div>
            </div>
          )}

          <div className="flex items-center justify-between text-[10px] pt-1.5 border-t border-gray-200/70 dark:border-gray-700/40">
            <span className="font-semibold text-gray-600 dark:text-gray-300">Total (top-level phases)</span>
            <span className="font-mono font-semibold text-gray-600 dark:text-gray-300">{topLevelSum.toFixed(2)}s</span>
          </div>
        </div>
      )}
    </div>
  );
}

// ── Document chat ──────────────────────────────────────────────────────────────

interface DocumentChatProps {
  sessionId: string | null;
  initialMessages: ChatMessage[];
  resetSignal: number;
  onSessionCreated: (id: string, title: string) => void;
  onMessageSaved: () => void;
  onTitleGenerated: (sessionId: string, title: string) => void;
  selectedTypes: DocumentType[];
  sidebarOpen: boolean;
  onToggleSidebar: () => void;
  isSwitchingSession: boolean;
  embeddingModel: string;
  rerankerName: string;
}

function DocumentChat({
  sessionId,
  initialMessages,
  resetSignal,
  onSessionCreated,
  onMessageSaved,
  onTitleGenerated,
  selectedTypes,
  sidebarOpen,
  onToggleSidebar,
  isSwitchingSession,
  embeddingModel,
  rerankerName,
}: DocumentChatProps) {
  const [messages, setMessages] = useState<ChatMessage[]>(initialMessages);
  const [input, setInput] = useState("");
  const [saveError, setSaveError] = useState<string | null>(null);
  const [copiedIdx, setCopiedIdx] = useState<number | null>(null);
  const [editingIdx, setEditingIdx] = useState<number | null>(null);
  const [editText, setEditText] = useState("");
  const [isPending, setIsPending] = useState(false);
  const [streamingText, setStreamingText] = useState<string | null>(null);
  const [synthesisInfo, setSynthesisInfo] = useState<SynthesisInfo | null>(null);
  const [queryTrace, setQueryTrace] = useState<QueryTraceStep[]>([]);
  const [traceOpen, setTraceOpen] = useState(false);
  const abortRef = useRef<AbortController | null>(null);

  const togglePin = async (idx: number) => {
    const msg = messages[idx];
    if (!msg || msg.role !== "user") return;

    const isNowPinned = !msg.is_pinned;
    
    // Optimistic update
    setMessages(prev => {
      const next = [...prev];
      next[idx] = { ...next[idx], is_pinned: isNowPinned };
      return next;
    });

    if (msg.id && sessionId) {
      try {
        await apiClient.toggleMessagePin(sessionId, msg.id, isNowPinned);
      } catch (err) {
        console.error("Failed to toggle pin:", err);
        // Revert on failure
        setMessages(prev => {
          const next = [...prev];
          next[idx] = { ...next[idx], is_pinned: !isNowPinned };
          return next;
        });
      }
    }
  };

  const updateTrace = useCallback((
    id: string,
    state: TraceState,
    detail?: string,
    extra?: Partial<Pick<QueryTraceStep, "chunks" | "reframedQuery" | "subQueries">>,
  ) => {
    setQueryTrace((steps) => steps.map((step) =>
      step.id === id
        ? {
            ...step,
            state,
            detail: detail ?? step.detail,
            ...extra,
            // Preserve the complete retrieved/selected preview emitted earlier.
            // A later done event may only contain the final citation subset.
            ...(step.chunks?.length && extra?.chunks ? { chunks: step.chunks } : {}),
          }
        : step
    ));
  }, []);

  // Abort any in-flight stream on unmount
  useEffect(() => () => { abortRef.current?.abort(); }, []);

  // Switching sessions / starting a new chat used to remount this component via a
  // React `key`, which destroyed any in-flight query mutation (its onSuccess —
  // and the DB save inside it — never fired, so the answer was lost). Instead we
  // stay mounted and resync state when the parent bumps `resetSignal`.
  const skipFirstReset = useRef(true);
  useEffect(() => {
    if (skipFirstReset.current) {
      skipFirstReset.current = false;
      return;
    }
    setMessages(initialMessages);
    setInput("");
    setSaveError(null);
    setEditingIdx(null);
    setEditText("");
    setSynthesisInfo(null);
    setQueryTrace([]);
    setTraceOpen(false);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [resetSignal]);

  // Live mirror of the active session so the async query result (which may resolve
  // after the user switched away) only renders into the view if it still belongs
  // to the session on screen. The DB save runs regardless of which view is active.
  const sessionIdRef = useRef(sessionId);
  useEffect(() => {
    sessionIdRef.current = sessionId;
  }, [sessionId]);

  const copyToClipboard = async (text: string, idx: number) => {
    const plain = markdownToPlainText(text);
    try {
      // Write both HTML (preserves bold/structure in rich editors) and plain text
      // (no markdown symbols for terminals / plain-text targets).
      await navigator.clipboard.write([
        new ClipboardItem({
          "text/html":  new Blob([markdownToHtml(text)], { type: "text/html" }),
          "text/plain": new Blob([plain],               { type: "text/plain" }),
        }),
      ]);
    } catch {
      // ClipboardItem not supported (Firefox < 127, some mobile) — fall back.
      await navigator.clipboard.writeText(plain);
    }
    setCopiedIdx(idx);
    setTimeout(() => setCopiedIdx(null), 2000);
  };

  const startEdit = (idx: number, content: string) => {
    setEditingIdx(idx);
    setEditText(content);
  };

  const handleStop = () => {
    abortRef.current?.abort();
  };

  const cancelEdit = () => {
    setEditingIdx(null);
    setEditText("");
  };

  const submitEdit = async (idx: number) => {
    const trimmed = editText.trim();
    if (!trimmed || isPending) return;
    const queryTimestamp = new Date().toISOString();
    setMessages((prev) => [
      ...prev.slice(0, idx),
      { role: "user" as const, content: trimmed, timestamp: queryTimestamp },
    ]);
    setEditingIdx(null);
    setEditText("");
    let sid = sessionId;
    if (!sid) {
      try {
        const sess = await apiClient.createChatSession({ title: trimmed.slice(0, 60) });
        sid = sess.id;
        onSessionCreated(sess.id, sess.title);
      } catch (err) {
        console.error("[chat] failed to create session:", err);
      }
    }
    await executeQuery(trimmed, sid, false);
  };

  const bottomRef = useRef<HTMLDivElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    const restored = textareaRef.current?.value;
    if (restored && !input) setInput(restored);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  useEffect(() => {
    const el = textareaRef.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 128)}px`;
  }, [input]);

  // Core streaming fetch — called by both send() and submitEdit()
  const executeQuery = async (q: string, sid: string | null, isNewSession: boolean) => {
    setIsPending(true);
    setStreamingText("");
    setSynthesisInfo(null);
    setQueryTrace(createQueryTrace(q));
    setTraceOpen(true);
    abortRef.current = new AbortController();

    let fullText = "";
    // doneData holds the final SSE metadata event — shape is unknown at compile time
    let doneData: any = null; // NOSONAR: intentional any — SSE payload shape varies
    // Track synthesis metadata locally — state updates don't reflect inside this
    // async closure, so we need a plain variable to carry it into AssistantMessage.
    let localSynthesisInfo: SynthesisInfo | null = null;

    // Shared request body for both the streaming and the non-streaming fallback.
    const body = JSON.stringify({
      query: q,
      document_types: selectedTypes.length > 0 ? selectedTypes : undefined,
      top_k: 3,
      use_reranker: true,
    });

    try {
      const resp = await fetch("/api/v1/query/stream", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body,
        signal: abortRef.current.signal,
      });

      // Stream endpoint missing (e.g. backend not restarted after it was added)
      // → fall back to the non-streaming /query endpoint so chat still works.
      if (resp.status === 404) {
        const fb = await fetch("/api/v1/query", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body,
          signal: abortRef.current.signal,
        });
        if (!fb.ok) throw new Error(`HTTP ${fb.status}`);
        const json = await fb.json();
        fullText = json.answer ?? "";
        setStreamingText(fullText);
        doneData = {
          confidence: json.confidence,
          confidence_breakdown: json.confidence_breakdown,
          citations: json.citations,
          processing_time_seconds: json.processing_time_seconds,
          retrieval_stats: json.retrieval_stats,
          notes: json.notes,
          timings: json.timings,
        };
        const graphMode = json.retrieval_stats?.graph_mode ?? "none";
        const fallbackChunks = toTraceChunks(json.citations);
        updateTrace("graph-router", "complete", "Route selected: " + graphMode);
        updateTrace("global-search", graphMode === "global" ? "complete" : "skipped");
        updateTrace("local-graph", graphMode === "local" ? "complete" : "skipped");
        updateTrace("raven", "skipped", "Classic retrieval path");
        updateTrace("retrieve", "complete", String(json.retrieval_stats?.total_retrieved ?? 0) + " chunks retrieved from " + traceStoreSummary(fallbackChunks), { chunks: fallbackChunks });
        updateTrace("rerank", "complete", String(json.retrieval_stats?.after_reranking ?? 0) + " chunks selected from " + traceStoreSummary(fallbackChunks), { chunks: fallbackChunks });
        updateTrace("structured", "complete", json.structured_result ? "Exact table result found" : "No structured shortcut used");
        updateTrace("synthesize", "complete");
        updateTrace("response", "complete");
      } else {
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);

        const reader = resp.body!.getReader();
        const decoder = new TextDecoder();
        let buffer = "";

      // eslint-disable-next-line no-constant-condition
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split("\n");
        buffer = lines.pop() ?? "";
        for (const line of lines) {
          if (!line.startsWith("data: ")) continue;
          const data = line.slice(6).trim();
          if (data === "[DONE]") break;
          if (!data) continue;
          try {
            const event = JSON.parse(data);
            if (event.type === "token") {
              fullText += event.text;
              setStreamingText(fullText);
            } else if (event.type === "status") {
              if (event.stage === "ranking") {
                updateTrace("retrieve", "complete");
                updateTrace("rerank", "running", event.message ?? "Ranking results");
              } else if (event.stage === "graph-traversal") {
                updateTrace("local-graph", "running", event.message ?? "Traversing entity relationships");
              } else if (event.stage === "graph-global") {
                updateTrace("global-search", "running", event.message ?? "Searching graph communities");
              }
              } else if (event.type === "stage") {
              if (event.stage === "graphrag_route") {
                const mode = event.detail?.mode ?? "none";
                updateTrace("graph-router", "complete", "Route selected: " + mode);
                updateTrace("global-search", mode === "global" ? "running" : "skipped");
                updateTrace("local-graph", mode === "local" ? "pending" : "skipped");
              } else if (event.stage === "graphrag_local") {
                const expanded = event.detail?.expanded ?? 0;
                updateTrace("local-graph", "complete", String(expanded) + " additional graph chunk" + (expanded === 1 ? "" : "s") + " added");
              } else if (event.stage === "retrieved") {
                const chunks = toTraceChunks(event.detail?.chunks);
                updateTrace("retrieve", "complete", chunks.length + " chunks retrieved from " + traceStoreSummary(chunks), { chunks });
              } else if (event.stage === "selected") {
                const chunks = toTraceChunks(event.detail?.chunks);
                updateTrace("rerank", "complete", chunks.length + " chunks selected from " + traceStoreSummary(chunks), { chunks });
              } else if (event.stage === "hybrid") {
                updateTrace("retrieve", "running", "Hybrid retrieval, pass " + String(event.detail?.loop ?? 1));
              } else if (event.stage === "raven") {
                const reframed = event.detail?.reframed ?? event.detail?.reframed_query;
                updateTrace(
                  "raven",
                  reframed ? "complete" : "running",
                  reframed ? "Query reframed for retrieval" : "RAVEN is planning the retrieval query",
                  { reframedQuery: reframed ?? null, subQueries: event.detail?.sub_queries ?? [] }
                );
              } else if (event.stage === "raven_result") {
                updateTrace(
                  "raven",
                  "complete",
                  "Query reframed for retrieval",
                  {
                    reframedQuery: event.detail?.reframed ?? event.detail?.reframed_query ?? null,
                    subQueries: event.detail?.sub_queries ?? [],
                  }
                );
              } else if (event.stage === "spyder") {
                updateTrace("rerank", "running", "SPYDER is checking answer sufficiency");
              }
            } else if (event.type === "synthesis_start") {
              updateTrace("retrieve", "complete");
              updateTrace("rerank", "complete");
              updateTrace("structured", "complete");
              updateTrace("synthesize", "running", "Building answer from selected evidence");
              localSynthesisInfo = {
                model: event.model ?? "Groq",
                maxTokens: event.max_tokens ?? 0,
                chunksUsed: event.chunks_used ?? 0,
                storesSearched: event.stores_searched ?? [],
                graphMode: event.graph_mode ?? "none",
                graphExpanded: event.graph_expanded ?? 0,
              };
              setSynthesisInfo(localSynthesisInfo);
            } else if (event.type === "done") {
              doneData = event;
              const mode = event.retrieval_stats?.graph_mode ?? "none";
              const finalChunkPreviews = toTraceChunks(event.citations);
              updateTrace("graph-router", "complete", "Route selected: " + mode);
              updateTrace("global-search", mode === "global" ? "complete" : "skipped");
              updateTrace("local-graph", mode === "local" ? "complete" : "skipped");
              if (event.agentic_stats?.raven) {
                const raven = event.agentic_stats.raven;
                const reframed = raven.reframed ?? raven.reframed_query ?? null;
                updateTrace(
                  "raven",
                  "complete",
                  reframed ? "Query reframed for retrieval" : "RAVEN used the original query",
                  { reframedQuery: reframed, subQueries: raven.sub_queries ?? [] }
                );
              } else {
                updateTrace(
                  "raven",
                  "skipped",
                  event.structured_result
                    ? "Structured table shortcut (RAVEN bypassed)"
                    : event.agentic_stats
                      ? "Global graph path"
                      : "Classic retrieval path"
                );
              }
              updateTrace(
                "retrieve",
                "complete",
                String(event.retrieval_stats?.total_retrieved ?? 0) + " chunks retrieved from " + traceStoreNames(event.retrieval_stats?.stores_searched),
                { chunks: finalChunkPreviews }
              );
              updateTrace("rerank", "complete", String(event.retrieval_stats?.after_reranking ?? 0) + " chunks selected from " + traceStoreSummary(finalChunkPreviews), { chunks: finalChunkPreviews });
              updateTrace("structured", "complete", event.structured_result ? "Exact table result found" : "No structured shortcut used");
              updateTrace("synthesize", "complete");
              updateTrace("response", "complete", String(event.citations?.length ?? 0) + " citations prepared");
            } else if (event.type === "error") {
              updateTrace("response", "error", event.message ?? "Query processing failed");
            }
          } catch { /* ignore malformed SSE lines */ }
        }
      }
      }  // close else block

      const assistantMsg: AssistantMessage = {
        role: "assistant",
        content: fullText || "No response received.",
        timestamp: new Date().toISOString(),
        confidence: doneData?.confidence ?? 0,
        confidenceBreakdown: doneData?.confidence_breakdown ?? null,
        citations: doneData?.citations ?? [],
        processingTime: doneData?.processing_time_seconds ?? 0,
        storesSearched: doneData?.retrieval_stats?.stores_searched ?? [],
        totalRetrieved: doneData?.retrieval_stats?.total_retrieved ?? 0,
        notes: doneData?.notes ?? null,
        synthesisInfo: localSynthesisInfo ?? undefined,
        timings: doneData?.timings ?? null,
        graphMode: doneData?.retrieval_stats?.graph_mode ?? "none",
        graphExpanded: doneData?.retrieval_stats?.graph_expanded ?? 0,
      };

      if (sid === sessionIdRef.current) {
        setMessages((prev) => [...prev, assistantMsg]);
      }

      if (sid) {
        const activeSid = sid;
        (async () => {
          try {
            const userRes = await apiClient.addChatMessage(activeSid, { role: "user", content: q });
            const asstRes = await apiClient.addChatMessage(activeSid, {
              role: "assistant",
              content: assistantMsg.content,
              confidence: assistantMsg.confidence,
              processing_time: assistantMsg.processingTime,
              stores_searched: assistantMsg.storesSearched,
              notes: assistantMsg.notes ?? undefined,
              citations: assistantMsg.citations,
            });
            
            // Update local messages with their database IDs so they can be pinned
            setMessages(prev => prev.map(m => {
              if (m.role === "user" && m.content === q && !m.id) return { ...m, id: userRes.id };
              if (m.role === "assistant" && m.content === assistantMsg.content && !m.id) return { ...m, id: asstRes.id };
              return m;
            }));

            onMessageSaved();
            if (isNewSession) {
              apiClient.generateChatTitle(activeSid)
                .then(({ title }) => onTitleGenerated(activeSid, title))
                .catch(console.error);
            }
          } catch (err) {
            console.error("[chat] failed to save messages:", err);
          }
        })();
      }
    } catch (err: unknown) {
      if (err instanceof DOMException && err.name === "AbortError") {
        if (sid === sessionIdRef.current && fullText) {
          setMessages((prev) => [
            ...prev,
            {
              role: "assistant",
              content: fullText,
              timestamp: new Date().toISOString(),
              confidence: 0,
              citations: [],
              processingTime: 0,
              storesSearched: [],
              totalRetrieved: 0,
              notes: "Stopped by user.",
            },
          ]);
        }
        return;
      }
      if (sid === sessionIdRef.current) {
        setMessages((prev) => [
          ...prev,
          {
            role: "assistant",
            content: "Sorry, something went wrong. Please try again.",
            timestamp: new Date().toISOString(),
            confidence: 0,
            citations: [],
            processingTime: 0,
            storesSearched: [],
            totalRetrieved: 0,
            notes: err instanceof Error ? err.message : String(err),
            isError: true,
          },
        ]);
      }
    } finally {
      setStreamingText(null);
      setIsPending(false);
    }
  };

  const send = async (query: string) => {
    const q = query.trim();
    if (!q || isPending) return;
    setMessages((prev) => [...prev, { role: "user", content: q, timestamp: new Date().toISOString() }]);
    setInput("");

    let sid = sessionId;
    let isNewSession = false;
    if (!sid) {
      try {
        const sess = await apiClient.createChatSession({ title: q.slice(0, 60) });
        sid = sess.id;
        isNewSession = true;
        setSaveError(null);
        onSessionCreated(sess.id, sess.title);
      } catch (err) {
        console.error("[chat] failed to create session:", err);
        setSaveError("Chat history unavailable — your message sent but won't be saved.");
      }
    }

    await executeQuery(q, sid, isNewSession);
  };

  const currentText = () => textareaRef.current?.value || input;

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    send(currentText());
  };

  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      send(currentText());
    }
  };

  const turnCount = Math.ceil(messages.length / 2);

  if (isSwitchingSession) {
    return (
      <div className="h-full flex items-center justify-center bg-white dark:bg-[#202024] rounded-2xl border border-slate-200 dark:border-white/[0.08]">
        <div className="flex flex-col items-center gap-3">
          <Loader2 className="h-6 w-6 animate-spin text-indigo-500" />
          <p className="text-sm text-slate-500 dark:text-zinc-400">Loading chat…</p>
        </div>
      </div>
    );
  }

  return (
    <div className="h-full flex min-w-0 gap-3">
    <div className="min-w-0 flex-1 flex flex-col bg-white dark:bg-[#202024] rounded-2xl border border-slate-200 dark:border-white/[0.08] shadow-sm dark:shadow-2xl overflow-hidden transition-colors">
      {/* ── Header ── */}
      <div className="flex-shrink-0 border-b border-slate-200 dark:border-white/[0.08] bg-white/90 dark:bg-[#202024]/90 backdrop-blur-md px-5 py-3.5 flex items-center justify-between gap-4">
        <div className="flex items-center gap-2.5">
          {/* Sidebar toggle */}
          <button
            type="button"
            onClick={onToggleSidebar}
            title={sidebarOpen ? "Hide chat history" : "Show chat history"}
            className={cn(
              "p-1.5 rounded-lg transition-all",
              sidebarOpen
                ? "bg-blue-100 dark:bg-blue-500/20 text-blue-600 dark:text-blue-400"
                : "text-gray-400 dark:text-gray-500 hover:text-gray-700 dark:hover:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-800"
            )}
          >
            <PanelLeft className="h-4 w-4" />
          </button>

          <div className="w-7 h-7 rounded-lg bg-gradient-to-br from-blue-500 to-indigo-600 flex items-center justify-center shadow-lg shadow-blue-500/20">
            <MessageSquare className="h-3.5 w-3.5 text-white" />
          </div>
          <div>
            <span className="text-sm font-semibold text-gray-900 dark:text-gray-100">
              Document Chat
            </span>
            {turnCount > 0 && (
              <span className="text-xs text-gray-500 ml-2">
                {turnCount} turn{turnCount !== 1 ? "s" : ""}
              </span>
            )}
          </div>
        </div>

        <div className="flex items-center gap-2 flex-wrap justify-end">
          {queryTrace.length > 0 && (
            <button
              type="button"
              onClick={() => setTraceOpen((open) => !open)}
              title={traceOpen ? "Hide query pipeline" : "View query pipeline"}
              aria-label={traceOpen ? "Hide query pipeline" : "View query pipeline"}
              className={cn(
                "inline-flex items-center gap-1.5 rounded-lg border px-2.5 py-1.5 text-xs transition-all",
                traceOpen
                  ? "border-cyan-500/30 bg-cyan-500/10 text-cyan-500"
                  : "border-gray-200 bg-gray-100 text-gray-500 hover:border-cyan-500/30 hover:text-cyan-500 dark:border-gray-700 dark:bg-gray-800/60 dark:text-gray-400"
              )}
            >
              <Network className="h-3.5 w-3.5" />
              {traceOpen ? "Hide pipeline" : "View pipeline"}
            </button>
          )}
        </div>
      </div>

      {/* ── Messages ── */}
      <div className="flex-1 overflow-y-auto px-5 py-5 space-y-5">
        {messages.length === 0 && !isPending && (
          <div className="h-full flex flex-col items-center justify-center text-center py-8 max-w-2xl mx-auto">
            <div className="w-14 h-14 bg-gradient-to-br from-indigo-500/20 to-violet-600/20 rounded-2xl flex items-center justify-center mb-4 border border-indigo-500/20 shadow-lg shadow-indigo-500/10">
              <Sparkles className="h-7 w-7 text-indigo-500 dark:text-indigo-400" />
            </div>
            <h2 className="text-base sm:text-lg font-bold text-slate-900 dark:text-gray-100 mb-1">
              Query Multi-Store Document Intelligence
            </h2>
            <p className="text-xs sm:text-sm text-slate-500 dark:text-gray-400 max-w-md mb-6">
              Ask anything across financial statements, legal contracts, M&amp;A corporate structures, and policies.
            </p>

            <div className="grid grid-cols-1 sm:grid-cols-2 gap-2.5 w-full text-left">
              <button
                type="button"
                onClick={() => send("What was the total revenue, EBITDA, and YoY growth across all business segments in FY24?")}
                className="p-3 rounded-xl bg-slate-50 dark:bg-white/[0.03] border border-slate-200/80 dark:border-white/[0.08] hover:border-emerald-500/50 dark:hover:border-emerald-500/40 hover:bg-emerald-50/40 dark:hover:bg-emerald-950/20 transition-all text-left group"
              >
                <div className="text-[11px] font-semibold text-emerald-700 dark:text-emerald-300 mb-0.5 flex items-center gap-1.5">
                  <span className="w-1.5 h-1.5 rounded-full bg-emerald-500" />
                  Financial &amp; Tabular Query
                </div>
                <div className="text-xs text-slate-600 dark:text-slate-400 line-clamp-2 group-hover:text-slate-900 dark:group-hover:text-slate-200">
                  &quot;What was the total revenue, EBITDA, and YoY growth across all segments in FY24?&quot;
                </div>
              </button>

              <button
                type="button"
                onClick={() => send("Show all clauses with HIGH or CRITICAL risk ratings and summarize the termination terms.")}
                className="p-3 rounded-xl bg-slate-50 dark:bg-white/[0.03] border border-slate-200/80 dark:border-white/[0.08] hover:border-amber-500/50 dark:hover:border-amber-500/40 hover:bg-amber-50/40 dark:hover:bg-amber-950/20 transition-all text-left group"
              >
                <div className="text-[11px] font-semibold text-amber-700 dark:text-amber-300 mb-0.5 flex items-center gap-1.5">
                  <span className="w-1.5 h-1.5 rounded-full bg-amber-500" />
                  Legal Clause &amp; Risk Analyzer
                </div>
                <div className="text-xs text-slate-600 dark:text-slate-400 line-clamp-2 group-hover:text-slate-900 dark:group-hover:text-slate-200">
                  &quot;Show all clauses with HIGH or CRITICAL risk ratings and summarize the termination terms.&quot;
                </div>
              </button>

              <button
                type="button"
                onClick={() => send("Trace the relationships, subsidiaries, and corporate board linkages mentioned across documents.")}
                className="p-3 rounded-xl bg-slate-50 dark:bg-white/[0.03] border border-slate-200/80 dark:border-white/[0.08] hover:border-violet-500/50 dark:hover:border-violet-500/40 hover:bg-violet-50/40 dark:hover:bg-violet-950/20 transition-all text-left group"
              >
                <div className="text-[11px] font-semibold text-violet-700 dark:text-violet-300 mb-0.5 flex items-center gap-1.5">
                  <span className="w-1.5 h-1.5 rounded-full bg-violet-500" />
                  Neo4j GraphRAG Multi-Hop
                </div>
                <div className="text-xs text-slate-600 dark:text-slate-400 line-clamp-2 group-hover:text-slate-900 dark:group-hover:text-slate-200">
                  &quot;Trace the relationships, subsidiaries, and corporate board linkages mentioned across documents.&quot;
                </div>
              </button>

              <button
                type="button"
                onClick={() => send("What are the key compliance rules, sustainability targets, and corporate policies?")}
                className="p-3 rounded-xl bg-slate-50 dark:bg-white/[0.03] border border-slate-200/80 dark:border-white/[0.08] hover:border-blue-500/50 dark:hover:border-blue-500/40 hover:bg-blue-50/40 dark:hover:bg-blue-950/20 transition-all text-left group"
              >
                <div className="text-[11px] font-semibold text-blue-700 dark:text-blue-300 mb-0.5 flex items-center gap-1.5">
                  <span className="w-1.5 h-1.5 rounded-full bg-blue-500" />
                  Semantic Policy &amp; SOP Search
                </div>
                <div className="text-xs text-slate-600 dark:text-slate-400 line-clamp-2 group-hover:text-slate-900 dark:group-hover:text-slate-200">
                  &quot;What are the key compliance rules, sustainability targets, and corporate policies?&quot;
                </div>
              </button>
            </div>
          </div>
        )}

        {messages.map((msg, msgIdx) =>
          msg.role === "user" ? (
            /* User bubble */
            <div key={msgIdx} className="flex items-start justify-end gap-2.5 group/umsg">
              {editingIdx === msgIdx ? (
                /* ── Edit mode ── */
                <div className="max-w-[80%] w-full space-y-2">
                  <textarea
                    value={editText}
                    onChange={(e) => setEditText(e.target.value)}
                    onKeyDown={(e) => {
                      if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); submitEdit(msgIdx); }
                      if (e.key === "Escape") cancelEdit();
                    }}
                    autoFocus
                    rows={Math.min(editText.split("\n").length + 1, 8)}
                    className="w-full text-sm bg-white dark:bg-gray-800 border-2 border-blue-400 dark:border-blue-500 rounded-xl px-4 py-3 focus:ring-2 focus:ring-blue-500/30 outline-none resize-none text-gray-900 dark:text-gray-100 leading-relaxed shadow-sm"
                  />
                  <div className="flex justify-end gap-2">
                    <button
                      type="button"
                      onClick={cancelEdit}
                      className="px-3 py-1.5 text-xs text-gray-600 dark:text-gray-400 bg-gray-100 dark:bg-gray-800 border border-gray-300 dark:border-gray-700 rounded-lg hover:bg-gray-200 dark:hover:bg-gray-700 transition-all"
                    >
                      Cancel
                    </button>
                    <button
                      type="button"
                      onClick={() => submitEdit(msgIdx)}
                      disabled={!editText.trim() || isPending}
                      className="px-3 py-1.5 text-xs font-medium text-white bg-blue-600 rounded-lg hover:bg-blue-700 disabled:opacity-40 disabled:cursor-not-allowed transition-all flex items-center gap-1.5"
                    >
                      <Send className="h-3 w-3" />
                      Send
                    </button>
                  </div>
                </div>
              ) : (
                /* ── Normal mode ── */
                <div className="relative max-w-[72%]">
                  {/* Hover actions: edit + copy */}
                  <div className="absolute top-1 -left-[6rem] opacity-0 group-hover/umsg:opacity-100 transition-opacity flex gap-1">
                    <button
                      type="button"
                      onClick={() => togglePin(msgIdx)}
                      className={cn(
                        "p-1.5 rounded-lg border shadow-sm transition-all",
                        msg.is_pinned
                          ? "bg-red-50 border-red-200 text-red-500 hover:bg-red-100 dark:bg-red-900/30 dark:border-red-800 dark:text-red-400"
                          : "bg-white dark:bg-gray-800 border-gray-200 dark:border-gray-700 hover:bg-gray-50 dark:hover:bg-gray-700 text-gray-400 dark:text-gray-500 hover:text-gray-700 dark:hover:text-gray-200"
                      )}
                      title="Pin message to mark as incorrect/verify"
                    >
                      <Pin className="h-3 w-3" />
                    </button>
                    <button
                      type="button"
                      onClick={() => startEdit(msgIdx, msg.content)}
                      className="p-1.5 rounded-lg bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 shadow-sm hover:bg-gray-50 dark:hover:bg-gray-700 text-gray-400 dark:text-gray-500 hover:text-gray-700 dark:hover:text-gray-200 transition-all"
                      title="Edit message"
                    >
                      <Pencil className="h-3 w-3" />
                    </button>
                    <button
                      type="button"
                      onClick={() => copyToClipboard(msg.content, msgIdx)}
                      className="p-1.5 rounded-lg bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 shadow-sm hover:bg-gray-50 dark:hover:bg-gray-700 text-gray-400 dark:text-gray-500 hover:text-gray-700 dark:hover:text-gray-200 transition-all"
                      title="Copy message"
                    >
                      {copiedIdx === msgIdx
                        ? <Check className="h-3 w-3 text-green-500" />
                        : <Copy className="h-3 w-3" />}
                    </button>
                  </div>
                  <div
                    className={cn(
                      "text-white text-sm px-4 py-3 rounded-2xl rounded-tr-md whitespace-pre-wrap leading-relaxed shadow-lg",
                      msg.is_pinned
                        ? "bg-red-500 shadow-red-500/10"
                        : "bg-blue-600 shadow-blue-600/10"
                    )}
                  >
                    {msg.content}
                  </div>
                  {msg.timestamp && (
                    <p className="mt-1 text-right text-[10px] text-gray-400 dark:text-gray-600">
                      {formatChatTimestamp(msg.timestamp)} IST
                    </p>
                  )}
                </div>
              )}
              <div className="flex-shrink-0 w-8 h-8 bg-gray-200 dark:bg-gray-700 rounded-xl flex items-center justify-center">
                <User className="h-4 w-4 text-gray-600 dark:text-gray-300" />
              </div>
            </div>
          ) : (
            /* Assistant bubble */
            <div key={msgIdx} className="flex items-start gap-2.5">
              <div className="flex-shrink-0 w-8 h-8 bg-gradient-to-br from-blue-500 to-indigo-600 rounded-xl flex items-center justify-center shadow-lg shadow-blue-500/20">
                <Bot className="h-4 w-4 text-white" />
              </div>

              <div className="max-w-[80%] min-w-[300px] space-y-2">
                {/* Answer card */}
                <div className="bg-gray-50 dark:bg-gray-800/40 border border-gray-200 dark:border-gray-700/40 rounded-2xl rounded-tl-md overflow-hidden backdrop-blur-sm">
                  <div className="px-5 py-4">
                    <RenderedAnswer content={msg.content} isError={msg.isError} />
                    {msg.timestamp && (
                      <p className="mt-2 text-[10px] text-gray-400 dark:text-gray-600">
                        {formatChatTimestamp(msg.timestamp)} IST
                      </p>
                    )}

                    {msg.notes && msg.notes !== "null" && !msg.isError && (
                      <div className="mt-3 flex items-start gap-2 text-xs text-gray-500 bg-gray-100/80 dark:bg-gray-800/40 rounded-lg px-3 py-2 border border-gray-200 dark:border-gray-700/30">
                        <Info className="h-3 w-3 mt-0.5 flex-shrink-0 text-gray-400 dark:text-gray-600" />
                        <span className="italic leading-relaxed">{msg.notes}</span>
                      </div>
                    )}
                    {msg.notes && msg.isError && (
                      <p className="mt-2 text-xs text-red-400/70 italic">{msg.notes}</p>
                    )}
                  </div>

                  {/* Stats bar */}
                  {!msg.isError && (
                    <div className="px-5 py-2.5 bg-gray-100/80 dark:bg-gray-900/40 border-t border-gray-200 dark:border-gray-700/30 space-y-2">
                      {/* Model attribution */}
                      {msg.synthesisInfo && (
                        <div className="flex items-center flex-wrap gap-x-2 gap-y-1 pb-2 border-b border-gray-200/70 dark:border-gray-700/30">

                          <div className="flex items-center gap-1">
                            <Sparkles className="h-2.5 w-2.5 text-blue-400" />
                            <span className="text-[10px] font-mono font-semibold text-blue-400">{msg.synthesisInfo.model}</span>
                          </div>
                          <span className="text-[10px] text-gray-300 dark:text-gray-600 select-none">·</span>
                          <span className="text-[10px] text-gray-400 dark:text-gray-500">{msg.synthesisInfo.chunksUsed} chunks synthesized</span>
                          <span className="text-[10px] text-gray-300 dark:text-gray-600 select-none">·</span>
                          <span className="text-[10px] text-gray-400 dark:text-gray-500">{msg.synthesisInfo.maxTokens} tok budget</span>
                          {msg.synthesisInfo.storesSearched.length > 0 && (
                            <>
                              <span className="text-[10px] text-gray-300 dark:text-gray-600 select-none">·</span>
                              <span className="text-[10px] text-gray-400 dark:text-gray-500">
                                {msg.synthesisInfo.storesSearched.join(", ")}
                              </span>
                            </>
                          )}
                          {/* Always shown so the mode that answered (local/global/none) is explicit. */}
                          {(() => {
                            const gm = graphModeMeta(msg.synthesisInfo.graphMode);
                            return (
                              <>
                                <span className="text-[10px] text-gray-300 dark:text-gray-600 select-none">·</span>
                                <span className={cn("text-[10px] font-semibold", gm.textClass)} title={gm.title}>
                                  {gm.label}
                                  {msg.synthesisInfo.graphMode === "local" && typeof msg.synthesisInfo.graphExpanded === "number" &&
                                    ` (+${msg.synthesisInfo.graphExpanded})`}
                                </span>
                              </>
                            );
                          })()}
                        </div>
                      )}
                      <div className="flex items-start justify-between gap-3 flex-wrap">
                        <div className="flex items-start gap-3 flex-1 min-w-0">
                          <div className="flex-shrink-0">
                            <ConfidenceBadge confidence={msg.confidence} breakdown={msg.confidenceBreakdown} />
                          </div>

                          <TimingsBadge processingTime={msg.processingTime} timings={msg.timings} />
                        </div>

                        <div className="flex items-center gap-1 flex-shrink-0" title="Knowledge stores searched for this query">
                          {msg.storesSearched
                            .filter((s) => s !== "graph_communities")
                            .map((s) => {
                              const meta = STORE_META[s] ?? STORE_META.vector;
                              const Icon = meta.icon;
                              return (
                                <span
                                  key={s}
                                  title={meta.title}
                                  className={cn(
                                    "inline-flex items-center gap-1 text-[10px] px-1.5 py-0.5 rounded-md border cursor-default",
                                    meta.color
                                  )}
                                >
                                  <Icon className="h-2.5 w-2.5" />
                                  {meta.label}
                                </span>
                              );
                            })}

                          {/* Always shown for live responses so it's clear whether this answer used
                              the Neo4j graph (local entity expansion, global community synthesis) or
                              not. graph_mode isn't persisted to chat_messages (same gap as timings/
                              synthesisInfo — see their comments), so msg.graphMode is undefined for
                              reloaded history: hidden rather than falsely claiming "None" for an old
                              answer whose actual mode was never recorded. */}
                          {msg.graphMode !== undefined && (() => {
                            const gm = graphModeMeta(msg.graphMode);
                            return (
                              <span
                                title={gm.title}
                                className={cn(
                                  "inline-flex items-center gap-1 text-[10px] px-1.5 py-0.5 rounded-md border cursor-default",
                                  gm.badgeClass
                                )}
                              >
                                <Network className="h-2.5 w-2.5" />
                                {gm.label}
                                {msg.graphMode === "local" && typeof msg.graphExpanded === "number" && msg.graphExpanded > 0 &&
                                  ` +${msg.graphExpanded}`}
                              </span>
                            );
                          })()}

                          {/* Agentic stats badge — visible only when backend flag is on */}
                          {msg.agenticStats && (
                            <span
                              className="inline-flex items-center gap-1 text-[10px] px-1.5 py-0.5 rounded-md border bg-violet-500/10 text-violet-400 border-violet-500/20"
                              title={
                                msg.agenticStats.raven?.reframed
                                  ? `RAVEN reframed: "${msg.agenticStats.raven.reframed}"`
                                  : "Agentic RAG"
                              }
                            >
                              <Zap className="h-2.5 w-2.5" />
                              Agentic
                              {(msg.agenticStats.loops ?? 0) > 0 && (
                                <span className="opacity-70">×{msg.agenticStats.loops}</span>
                              )}
                            </span>
                          )}

                          {/* Copy response */}
                          <button
                            type="button"
                            onClick={() => copyToClipboard(msg.content, msgIdx)}
                            className="ml-1 p-1.5 rounded-lg text-gray-400 dark:text-gray-500 hover:text-gray-700 dark:hover:text-gray-200 hover:bg-gray-200 dark:hover:bg-gray-700/50 transition-all"
                            title="Copy response"
                          >
                            {copiedIdx === msgIdx
                              ? <Check className="h-3 w-3 text-green-500" />
                              : <Copy className="h-3 w-3" />}
                          </button>

                          {/* Regenerate (last assistant message only) */}
                          {msgIdx === messages.length - 1 && !isPending && (
                            <button
                              type="button"
                              onClick={() => {
                                const lastUser = [...messages].slice(0, msgIdx).reverse().find((m) => m.role === "user");
                                if (lastUser) send(lastUser.content);
                              }}
                              className="p-1.5 rounded-lg text-gray-400 dark:text-gray-500 hover:text-gray-700 dark:hover:text-gray-200 hover:bg-gray-200 dark:hover:bg-gray-700/50 transition-all"
                              title="Regenerate response"
                            >
                              <RotateCcw className="h-3 w-3" />
                            </button>
                          )}
                        </div>
                      </div>
                    </div>
                  )}
                </div>

                {/* Source documents */}
                {!msg.isError && msg.citations.length > 0 && (
                  <div className="space-y-2">
                    <div className="flex flex-col gap-2">
                      {groupSources(msg.citations).map(({ filename, pages, pdfUrl, imageUrl, storeType, relevanceScore }) => (
                        <SourceChipWithDetail
                          key={filename}
                          filename={filename}
                          pages={pages}
                          pdfUrl={pdfUrl}
                          imageUrl={imageUrl}
                          storeType={storeType}
                          relevanceScore={relevanceScore}
                          citations={msg.citations}
                        />
                      ))}
                    </div>
                  </div>
                )}
              </div>
            </div>
          )
        )}

        {/* Streaming bubble: animated dots while processing, then answer tokens.
            Detailed progress is shown in the query-pipeline sidebar. */}
        {streamingText !== null && (
          <div className="flex items-start gap-2.5">
            <div className="flex-shrink-0 w-8 h-8 bg-gradient-to-br from-blue-500 to-indigo-600 rounded-xl flex items-center justify-center shadow-lg shadow-blue-500/20">
              <Bot className="h-4 w-4 text-white" />
            </div>
            <div className="max-w-[80%] min-w-[300px]">
              <div className="bg-gray-50 dark:bg-gray-800/40 border border-gray-200 dark:border-gray-700/40 rounded-2xl rounded-tl-md overflow-hidden backdrop-blur-sm px-5 py-4">
                {streamingText.length > 0 ? (
                  <>
                    {synthesisInfo && (
                      <div className="flex items-center flex-wrap gap-x-2 gap-y-1 mb-3 pb-2.5 border-b border-gray-200 dark:border-gray-700/40">
                        <div className="flex items-center gap-1.5">
                          <Sparkles className="h-3 w-3 text-blue-400 flex-shrink-0" />
                          <span className="text-[10px] font-mono font-semibold text-blue-400">{synthesisInfo.model}</span>
                        </div>
                        <span className="text-[10px] text-gray-300 dark:text-gray-600 select-none">·</span>
                        <span className="text-[10px] text-gray-400 dark:text-gray-500">{synthesisInfo.chunksUsed} chunks in context</span>
                        <span className="text-[10px] text-gray-300 dark:text-gray-600 select-none">·</span>
                        <span className="text-[10px] text-gray-400 dark:text-gray-500">{synthesisInfo.maxTokens} token budget</span>
                        {synthesisInfo.storesSearched.length > 0 && (
                          <>
                            <span className="text-[10px] text-gray-300 dark:text-gray-600 select-none">·</span>
                            <span className="text-[10px] text-gray-400 dark:text-gray-500">
                              {synthesisInfo.storesSearched.join(", ")}
                            </span>
                          </>
                        )}
                        {/* Same always-shown mode indicator as the final message header. */}
                        {(() => {
                          const gm = graphModeMeta(synthesisInfo.graphMode);
                          return (
                            <>
                              <span className="text-[10px] text-gray-300 dark:text-gray-600 select-none">·</span>
                              <span className={cn("text-[10px] font-semibold", gm.textClass)} title={gm.title}>
                                {gm.label}
                                {synthesisInfo.graphMode === "local" && typeof synthesisInfo.graphExpanded === "number" &&
                                  ` (+${synthesisInfo.graphExpanded})`}
                              </span>
                            </>
                          );
                        })()}
                      </div>
                    )}
                    <RenderedAnswer content={streamingText} />
                    <span className="inline-block w-1.5 h-4 bg-blue-400 animate-pulse ml-0.5 rounded-sm align-middle" />
                  </>
                ) : (
                  <div className="flex items-center gap-1.5 py-1" aria-label="Processing">
                    <span className="h-2 w-2 rounded-full bg-blue-400 animate-bounce [animation-delay:-0.3s]" />
                    <span className="h-2 w-2 rounded-full bg-blue-400 animate-bounce [animation-delay:-0.15s]" />
                    <span className="h-2 w-2 rounded-full bg-blue-400 animate-bounce" />
                  </div>
                )}
              </div>
            </div>
          </div>
        )}

        <div ref={bottomRef} />
      </div>

      {/* ── Save error banner ── */}
      {saveError && (
        <div className="mx-5 mb-0 mt-2 rounded-lg bg-amber-50 dark:bg-amber-950/30 border border-amber-200 dark:border-amber-800 px-3 py-2 text-sm text-amber-700 dark:text-amber-300 flex items-center gap-2">
          <AlertCircle className="h-4 w-4 flex-shrink-0" />
          {saveError}
        </div>
      )}

      {/* ── Input ── */}
      <div className="flex-shrink-0 border-t border-slate-200 dark:border-white/[0.08] bg-white/95 dark:bg-[#202024]/95 backdrop-blur-md px-5 py-4">
        <form onSubmit={handleSubmit} className="flex items-end gap-3">
          <textarea
            ref={textareaRef}
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={handleKeyDown}
            placeholder="Ask about your documents… (Enter to send, Shift+Enter for new line)"
            rows={1}
            disabled={isPending}
            className="flex-1 resize-none text-sm bg-slate-50/80 dark:bg-[#18181b]/80 border border-slate-300 dark:border-white/[0.12] shadow-xs rounded-xl px-4 py-3 focus:ring-2 focus:ring-indigo-500/30 focus:border-indigo-500 outline-none disabled:opacity-50 overflow-y-auto leading-relaxed text-slate-900 dark:text-zinc-100 placeholder:text-slate-400 dark:placeholder:text-zinc-500 transition-all"
            style={{ minHeight: "46px", maxHeight: "128px" }}
          />
          {isPending ? (
            <button
              type="button"
              onClick={handleStop}
              title="Stop generating"
              className="flex-shrink-0 w-11 h-11 bg-slate-700 text-white rounded-xl hover:bg-slate-800 flex items-center justify-center transition-all shadow-md"
            >
              <Square className="h-4 w-4 fill-current" />
            </button>
          ) : (
            <button
              type="submit"
              disabled={!input.trim()}
              className="flex-shrink-0 w-11 h-11 bg-gradient-to-r from-indigo-600 to-violet-600 text-white rounded-xl hover:from-indigo-500 hover:to-violet-500 disabled:opacity-30 disabled:cursor-not-allowed flex items-center justify-center transition-all shadow-md shadow-indigo-500/20 disabled:shadow-none"
            >
              <Send className="h-4 w-4" />
            </button>
          )}
        </form>
      </div>
    </div>
    {traceOpen && <QueryTraceSidebar steps={queryTrace} isPending={isPending} />}
    </div>
  );
}

// ── Page ───────────────────────────────────────────────────────────────────────

export default function QueryPage() {
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [sessions, setSessions] = useState<ChatSession[]>([]);
  const [sessionsLoading, setSessionsLoading] = useState(false);
  const [currentSessionId, setCurrentSessionId] = useState<string | null>(null);
  const [loadedMessages, setLoadedMessages] = useState<ChatMessage[]>([]);
  const [selectedTypes] = useState<DocumentType[]>([]);
  const [switchingSession, setSwitchingSession] = useState(false);
  const [newChatKey, setNewChatKey] = useState(0);
  // Live model names fetched from the backend /health endpoint so the pipeline
  // display always shows what is actually configured, not a hardcoded string.
  const [embeddingModel, setEmbeddingModel] = useState("bge-large-en-v1.5");
  const [rerankerName, setRerankerName] = useState("ms-marco-MiniLM-L-6-v2");

  useEffect(() => {
    const API = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";
    fetch(`${API}/api/v1/health`)
      .then((r) => r.json())
      .then((d) => {
        if (d.embedding_model) setEmbeddingModel(d.embedding_model);
        if (d.reranker_name) setRerankerName(d.reranker_name);
      })
      .catch(() => { /* keep defaults on error */ });
  }, []);

  const loadSessions = useCallback(async () => {
    setSessionsLoading(true);
    try {
      const data = await apiClient.listChatSessions();
      setSessions(data);
    } catch (err) {
      console.error("[chat] failed to load sessions:", err);
    } finally {
      setSessionsLoading(false);
    }
  }, []);

  useEffect(() => {
    loadSessions();
  }, [loadSessions]);

  const handleNewChat = () => {
    setCurrentSessionId(null);
    setLoadedMessages([]);
    setNewChatKey((k) => k + 1);
  };

  const handleSwitchSession = async (sessionId: string) => {
    if (sessionId === currentSessionId) return;
    setSwitchingSession(true);
    try {
      const data = await apiClient.getChatSession(sessionId);
      const msgs: ChatMessage[] = (data.messages as ChatMessageRecord[]).map((m) => {
        if (m.role === "user") return { id: m.id, role: "user" as const, content: m.content, timestamp: m.created_at, is_pinned: m.is_pinned };
        return {
          id: m.id,
          role: "assistant" as const,
          content: m.content,
          timestamp: m.created_at,
          confidence: m.confidence ?? 0,
          citations: (m.citations as CitationItem[]) ?? [],
          processingTime: m.processing_time ?? 0,
          storesSearched: m.stores_searched ?? [],
          totalRetrieved: 0,
          notes: m.notes ?? null,
          is_pinned: m.is_pinned,
        };
      });
      setLoadedMessages(msgs);
      setCurrentSessionId(sessionId);
      setNewChatKey((k) => k + 1); // force DocumentChat remount with loaded messages
    } catch (err) {
      console.error("[chat] failed to switch session:", err);
    } finally {
      setSwitchingSession(false);
    }
  };

  const handleDeleteSession = async (sessionId: string) => {
    await apiClient.deleteChatSession(sessionId);
    setSessions((prev) => prev.filter((s) => s.id !== sessionId));
    if (currentSessionId === sessionId) {
      setCurrentSessionId(null);
      setLoadedMessages([]);
      setNewChatKey((k) => k + 1);
    }
  };

  const handleSessionCreated = (id: string, title: string) => {
    setCurrentSessionId(id);
    setSessions((prev) => [
      {
        id,
        title,
        message_count: 0,
        created_at: new Date().toISOString(),
        updated_at: new Date().toISOString(),
      },
      ...prev,
    ]);
  };

  const handleMessageSaved = useCallback(() => {
    apiClient.listChatSessions().then(setSessions).catch(console.error);
  }, []);

  const handleTitleGenerated = useCallback((sessionId: string, title: string) => {
    setSessions((prev) =>
      prev.map((s) => (s.id === sessionId ? { ...s, title } : s))
    );
  }, []);

  return (
    <div className="flex gap-3 h-full overflow-hidden">
      {/* Sidebar */}
      <div
        className={cn(
          "transition-all duration-300 flex-shrink-0 overflow-hidden",
          sidebarOpen ? "w-64 opacity-100" : "w-0 opacity-0 pointer-events-none"
        )}
      >
        <RecentChatsSidebar
          sessions={sessions}
          currentSessionId={currentSessionId}
          isLoading={sessionsLoading}
          onNewChat={handleNewChat}
          onSessionClick={handleSwitchSession}
          onDeleteSession={handleDeleteSession}
        />
      </div>

      {/* Chat panel */}
      <div className="flex-1 min-w-0">
        <DocumentChat
          resetSignal={newChatKey}
          sessionId={currentSessionId}
          initialMessages={loadedMessages}
          onSessionCreated={handleSessionCreated}
          onMessageSaved={handleMessageSaved}
          onTitleGenerated={handleTitleGenerated}
          selectedTypes={selectedTypes}
          sidebarOpen={sidebarOpen}
          onToggleSidebar={() => setSidebarOpen((o) => !o)}
          isSwitchingSession={switchingSession}
          embeddingModel={embeddingModel}
          rerankerName={rerankerName}
        />
      </div>
    </div>
  );
}
