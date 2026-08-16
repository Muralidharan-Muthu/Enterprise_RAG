"use client";

import { useParams } from "next/navigation";
import { useEffect, useRef, useState } from "react";
import Link from "next/link";
import {
  ArrowLeft, CheckCircle2, XCircle, Loader2, Clock,
  FileText, ChevronDown, AlertTriangle, ExternalLink,
} from "lucide-react";
import { usePipeline, useDocument, usePageStats } from "@/hooks/useDocuments";
import { ParsingDetail } from "@/components/pipeline/ParsingDetail";
import { ChunkingDetail } from "@/components/pipeline/ChunkingDetail";
import { ImagesDetail } from "@/components/pipeline/ImagesDetail";
import { cn, formatBytes, formatDate, capitalize } from "@/lib/utils";
import type {
  PipelineDocumentDetail, PipelineStage, PipelineRunStatus, DocumentType, PageStats,
} from "@/lib/types";
import { DOC_TYPE_COLORS } from "@/lib/types";

// Full backend stage order. "images" (after parsing) and "graph" (after
// storing) are OPTIONAL — the worker only emits them when the doc has images
// / Neo4j is reachable — so they are rendered only when they actually ran
// (have a timing) or are the live stage. Without them the UI used to go blank
// for the ~2min the worker spent captioning images, looking like a failure.
const STAGES: PipelineStage[] = ["queued", "parsing", "images", "routing", "chunking", "embedding", "storing", "graph", "done"];
const OPTIONAL_STAGES = new Set<string>(["images", "graph"]);
const STAGE_LABELS: Record<string, string> = {
  queued: "Queued", parsing: "Parsing", images: "Images", routing: "Routing",
  chunking: "Chunking", embedding: "Embedding", storing: "Storing", graph: "Graph", done: "Done",
};

// Expected durations (seconds) for the est-time label and fill %. Calibrated
// from real stage_timings on CPU: parsing 12–180s (OCR-bound), image
// captioning ~8s/image, others quick. These are typical-case anchors, NOT
// caps — when a stage runs past its estimate the card switches to an
// indeterminate "running" state with a live elapsed counter instead of a
// frozen 99% bar, so a slow-but-healthy stage never reads as stuck.
const STAGE_ESTIMATES: Record<string, number> = {
  queued: 1, parsing: 45, images: 60, routing: 3, chunking: 3, embedding: 4, storing: 8, graph: 3, done: 0,
};

// Per-stage colour identity for the COMPLETED state — each stage reads as a
// distinct hue (light + dark) so the flow looks like a coloured ladder instead
// of an undifferentiated wall of green. Full literal class strings so Tailwind's
// scanner picks them up. In-progress stays blue, error stays red, pending gray.
interface StageColor { border: string; text: string; fill: string; base: string; ring: string; pill: string; }
const STAGE_COLORS: Record<string, StageColor> = {
  queued: {
    border: "border-slate-200 dark:border-slate-700", text: "text-slate-600 dark:text-slate-300",
    fill: "bg-slate-100 dark:bg-slate-800", base: "bg-slate-50 dark:bg-slate-900/40",
    ring: "ring-slate-400 dark:ring-slate-600", pill: "bg-slate-200/60 dark:bg-slate-100/10",
  },
  parsing: {
    border: "border-emerald-200 dark:border-emerald-900", text: "text-emerald-700 dark:text-emerald-300",
    fill: "bg-emerald-100 dark:bg-emerald-950", base: "bg-emerald-50 dark:bg-emerald-950/40",
    ring: "ring-emerald-400 dark:ring-emerald-700", pill: "bg-emerald-200/50 dark:bg-emerald-400/15",
  },
  images: {
    border: "border-indigo-200 dark:border-indigo-900", text: "text-indigo-700 dark:text-indigo-300",
    fill: "bg-indigo-100 dark:bg-indigo-950", base: "bg-indigo-50 dark:bg-indigo-950/40",
    ring: "ring-indigo-400 dark:ring-indigo-700", pill: "bg-indigo-200/50 dark:bg-indigo-400/15",
  },
  routing: {
    border: "border-amber-200 dark:border-amber-900", text: "text-amber-700 dark:text-amber-300",
    fill: "bg-amber-100 dark:bg-amber-950", base: "bg-amber-50 dark:bg-amber-950/40",
    ring: "ring-amber-400 dark:ring-amber-700", pill: "bg-amber-200/50 dark:bg-amber-400/15",
  },
  chunking: {
    border: "border-purple-200 dark:border-purple-900", text: "text-purple-700 dark:text-purple-300",
    fill: "bg-purple-100 dark:bg-purple-950", base: "bg-purple-50 dark:bg-purple-950/40",
    ring: "ring-purple-400 dark:ring-purple-700", pill: "bg-purple-200/50 dark:bg-purple-400/15",
  },
  embedding: {
    border: "border-cyan-200 dark:border-cyan-900", text: "text-cyan-700 dark:text-cyan-300",
    fill: "bg-cyan-100 dark:bg-cyan-950", base: "bg-cyan-50 dark:bg-cyan-950/40",
    ring: "ring-cyan-400 dark:ring-cyan-700", pill: "bg-cyan-200/50 dark:bg-cyan-400/15",
  },
  storing: {
    border: "border-blue-200 dark:border-blue-900", text: "text-blue-700 dark:text-blue-300",
    fill: "bg-blue-100 dark:bg-blue-950", base: "bg-blue-50 dark:bg-blue-950/40",
    ring: "ring-blue-400 dark:ring-blue-700", pill: "bg-blue-200/50 dark:bg-blue-400/15",
  },
  graph: {
    border: "border-fuchsia-200 dark:border-fuchsia-900", text: "text-fuchsia-700 dark:text-fuchsia-300",
    fill: "bg-fuchsia-100 dark:bg-fuchsia-950", base: "bg-fuchsia-50 dark:bg-fuchsia-950/40",
    ring: "ring-fuchsia-400 dark:ring-fuchsia-700", pill: "bg-fuchsia-200/50 dark:bg-fuchsia-400/15",
  },
  done: {
    border: "border-green-200 dark:border-green-900", text: "text-green-700 dark:text-green-300",
    fill: "bg-green-100 dark:bg-green-950", base: "bg-green-50 dark:bg-green-950/40",
    ring: "ring-green-400 dark:ring-green-700", pill: "bg-green-200/50 dark:bg-green-400/15",
  },
};

// Adaptive estimate: once the pre-scan workload map is in (page + image
// counts), parsing/images scale with the REAL workload instead of a flat
// guess — a 15-image doc no longer reads "~45s" while actually taking ~2min.
// CPU rule-of-thumb: ~8s OCR per page, ~6s per image to render, ~8s per image
// to caption+embed in the images stage.
function estimateFor(stage: string, doc: PipelineDocumentDetail): number {
  const pages = doc.stage_detail?.parsing?.pages ?? [];
  const nPages = doc.page_count ?? doc.stage_detail?.parsing?.total_pages ?? pages.length;
  const nImages = pages.reduce((s, p) => s + (p.images || 0), 0);
  if (stage === "parsing" && nPages) return Math.max(15, Math.round(nPages * 8 + nImages * 6));
  if (stage === "images" && nImages) return Math.max(10, Math.round(nImages * 8));
  return STAGE_ESTIMATES[stage] ?? 5;
}

function fmtTime(seconds: number, live = false): string {
  if (seconds < 0.05) return "< 0.1s";
  if (live && seconds < 100) return `${seconds.toFixed(2)}s`;
  if (seconds < 10) return `${seconds.toFixed(1)}s`;
  return `${Math.round(seconds)}s`;
}

export default function PipelineDetailPage() {
  const { run_id } = useParams<{ run_id: string }>();
  const { data: run, isLoading, isError } = usePipeline(run_id);

  if (isLoading) {
    return (
      <div className="flex items-center justify-center h-64">
        <Loader2 className="h-6 w-6 animate-spin text-blue-500" />
      </div>
    );
  }

  if (isError || !run) {
    return (
      <div className="max-w-4xl mx-auto mt-16 text-center text-gray-500 dark:text-gray-400">
        <AlertTriangle className="h-8 w-8 mx-auto mb-2 text-red-400" />
        Pipeline run not found.
      </div>
    );
  }

  const totalDuration = run.documents.reduce(
    (sum, d) => sum + (d.duration_seconds ?? 0), 0
  );

  return (
    <div className="max-w-6xl mx-auto space-y-6">

      {/* ── Back + header ─────────────────────────────────────── */}
      <div className="flex items-start gap-4">
        <Link
          href="/upload"
          className="mt-1 text-gray-400 dark:text-gray-500 hover:text-gray-600 transition-colors"
        >
          <ArrowLeft className="h-5 w-5" />
        </Link>
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-3 flex-wrap">
            <h1 className="text-2xl font-bold text-gray-900 dark:text-gray-100 truncate">{run.name}</h1>
            <StatusBadge status={run.status as PipelineRunStatus} />
          </div>
          {run.description && (
            <p className="text-sm text-gray-500 dark:text-gray-400 mt-0.5">{run.description}</p>
          )}
          <div className="flex flex-wrap gap-x-4 gap-y-1 mt-2 text-xs text-gray-400 dark:text-gray-500">
            <span>Started {formatDate(run.started_at ?? run.created_at)}</span>
            <span className="capitalize">{run.source}</span>
          </div>
        </div>
      </div>

      {/* ── Summary cards ─────────────────────────────────────── */}
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-4">
        <StatCard label="Files Found" value={run.files_found} color="text-gray-700 dark:text-gray-300" />
        <StatCard label="Processed" value={run.files_processed} color="text-green-600 dark:text-green-300" />
        <StatCard label="Failed" value={run.files_failed} color="text-red-500" />
        <StatCard
          label="Total Duration"
          value={totalDuration > 0 ? `${totalDuration.toFixed(1)}s` : "—"}
          color="text-blue-600 dark:text-blue-300"
        />
      </div>

      {/* ── Documents ─────────────────────────────────────────── */}
      <div className="bg-white dark:bg-gray-900 rounded-2xl border border-gray-200 dark:border-gray-800 shadow-sm">
        <div className="px-6 py-4 border-b border-gray-100 dark:border-gray-800">
          <h2 className="text-base font-semibold text-gray-900 dark:text-gray-100">
            Documents ({run.documents.length})
          </h2>
        </div>
        <div className="divide-y divide-gray-50">
          {run.documents.length === 0 && (
            <p className="px-6 py-10 text-center text-sm text-gray-400 dark:text-gray-500">No documents in this run.</p>
          )}
          {run.documents.map((doc) => (
            <DocumentRow key={doc.document_id} doc={doc} />
          ))}
        </div>
      </div>
    </div>
  );
}

// ── DocumentRow ───────────────────────────────────────────────────────────────

function DocumentRow({ doc }: { doc: PipelineDocumentDetail }) {
  const [expanded, setExpanded] = useState(false);
  const isFailed = doc.doc_status === "failed";

  return (
    <div className="px-6 py-5 space-y-4">
      {/* File info row — click to expand/collapse detail panel */}
      <button
        className="w-full flex items-start gap-3 text-left group"
        onClick={() => setExpanded((p) => !p)}
      >
        <FileText className="h-5 w-5 text-gray-400 dark:text-gray-500 flex-shrink-0 mt-0.5" />
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 flex-wrap">
            <span className="font-medium text-gray-900 dark:text-gray-100 truncate">{doc.original_filename}</span>
            {doc.document_type && (
              <span className="text-xs px-2 py-0.5 rounded-full border border-gray-200 dark:border-gray-800 bg-gray-50 dark:bg-gray-800/50 text-gray-600 dark:text-gray-300 capitalize">
                {doc.document_type}
              </span>
            )}
            {doc.router_confidence != null && (
              <span className="text-xs text-gray-400 dark:text-gray-500">{(doc.router_confidence * 100).toFixed(0)}% confidence</span>
            )}
          </div>
          <div className="flex flex-wrap gap-x-4 text-xs text-gray-400 dark:text-gray-500 mt-0.5">
            <span>{formatBytes(doc.file_size_bytes)}</span>
            {doc.page_count != null && <span>{doc.page_count} pages</span>}
            {doc.word_count != null && <span>{doc.word_count.toLocaleString()} words</span>}
            {doc.doc_status === "completed" ? (
              <>
                {doc.vector_chunks > 0 && <span className="text-blue-500">{doc.vector_chunks} vectors</span>}
                {doc.table_count > 0 && <span className="text-green-600 dark:text-green-300">{doc.table_count} tables</span>}
                {doc.clause_count > 0 && <span className="text-purple-600 dark:text-purple-300">{doc.clause_count} clauses</span>}
                {doc.research_chunks > 0 && <span className="text-orange-500">{doc.research_chunks} chunks</span>}
                {doc.vector_chunks === 0 && doc.table_count === 0 && doc.clause_count === 0 && doc.research_chunks === 0 && (
                  <span>0 chunks</span>
                )}
              </>
            ) : (
              doc.total_chunks != null && doc.total_chunks > 0 && <span>{doc.total_chunks} chunks</span>
            )}
            {doc.duration_seconds != null && <span>{doc.duration_seconds.toFixed(1)}s total</span>}
          </div>
        </div>
        <DocStatusBadge status={doc.doc_status} />
        <ChevronDown
          className={cn(
            "h-4 w-4 text-gray-400 flex-shrink-0 transition-transform mt-0.5 group-hover:text-gray-600",
            expanded && "rotate-180"
          )}
        />
      </button>

      {/* Stage flow */}
      <StageFlow doc={doc} />

      {/* Expanded detail panel */}
      {expanded && <DocumentDetailPanel doc={doc} />}

      {/* Error message */}
      {isFailed && doc.error_message && (
        <div className="flex items-start gap-2 rounded-lg bg-red-50 dark:bg-red-950 border border-red-200 dark:border-red-900 px-3 py-2 text-xs text-red-700 dark:text-red-300">
          <AlertTriangle className="h-3.5 w-3.5 flex-shrink-0 mt-0.5" />
          <span><span className="font-semibold">{doc.error_stage}:</span> {doc.error_message}</span>
        </div>
      )}
    </div>
  );
}

// ── DocumentDetailPanel ───────────────────────────────────────────────────────

function DocumentDetailPanel({ doc }: { doc: PipelineDocumentDetail }) {
  const { data: docDetail, isLoading } = useDocument(doc.document_id);
  const timings = doc.stage_timings ?? {};
  const completedStageSet = new Set(Object.keys(timings));
  const isCompleted = doc.doc_status === "completed";

  const hasParsingResult = completedStageSet.has("parsing") || isCompleted;
  const hasRoutingResult = (completedStageSet.has("routing") || isCompleted) && doc.document_type != null;
  const hasChunkingResult = (completedStageSet.has("chunking") || isCompleted) && doc.total_chunks != null;
  const hasEmbeddingResult = completedStageSet.has("embedding") || isCompleted;
  const hasStoringResult = isCompleted;

  if (!hasParsingResult) {
    return (
      <div className="rounded-xl bg-gray-50 dark:bg-gray-800/40 border border-gray-100 dark:border-gray-800 px-4 py-3 text-xs text-gray-400 dark:text-gray-500 text-center">
        No stage results yet — document is queued.
      </div>
    );
  }

  return (
    <div className="rounded-xl bg-gray-50 dark:bg-gray-800/40 border border-gray-100 dark:border-gray-800 overflow-hidden divide-y divide-gray-100 dark:divide-gray-800">

      {/* ── Parsing ── */}
      {hasParsingResult && (
        <div className="px-4 py-3 space-y-2">
          <SectionHeader label="Parsing" timing={timings.parsing} />
          <div className="flex flex-wrap gap-2">
            {doc.page_count != null && <Pill label="Pages" value={String(doc.page_count)} />}
            {doc.word_count != null && <Pill label="Words" value={doc.word_count.toLocaleString()} />}
            {isLoading ? (
              <Pill label="..." value="loading" dimmed />
            ) : (
              <>
                {docDetail?.has_tables != null && (
                  <Pill
                    label="Tables"
                    value={docDetail.has_tables ? "Detected" : "None"}
                    accent={docDetail.has_tables ? "green" : undefined}
                  />
                )}
                {docDetail?.has_images != null && (
                  <Pill
                    label="Images"
                    value={docDetail.has_images ? "Detected" : "None"}
                    accent={docDetail.has_images ? "blue" : undefined}
                  />
                )}
                {docDetail?.language_detected && (
                  <Pill label="Language" value={docDetail.language_detected.toUpperCase()} />
                )}
              </>
            )}
          </div>
        </div>
      )}

      {/* ── Classification / Routing ── */}
      {hasRoutingResult && (
        <div className="px-4 py-3 space-y-2">
          <SectionHeader label="Classification" timing={timings.routing} />
          <div className="flex items-center gap-3 flex-wrap">
            <span
              className={cn(
                "inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium border capitalize",
                DOC_TYPE_COLORS[doc.document_type as DocumentType]
              )}
            >
              {doc.document_type}
            </span>
            {doc.router_confidence != null && (
              <div className="flex items-center gap-2">
                <div className="w-24 h-1.5 bg-gray-200 dark:bg-gray-700 rounded-full overflow-hidden">
                  <div
                    className="h-full bg-blue-500 rounded-full"
                    style={{ width: `${doc.router_confidence * 100}%` }}
                  />
                </div>
                <span className="text-xs text-gray-600 dark:text-gray-300">
                  {(doc.router_confidence * 100).toFixed(0)}% confidence
                </span>
              </div>
            )}
          </div>
          {!isLoading && docDetail?.router_reasoning && (
            <p className="text-xs text-gray-500 dark:text-gray-400 leading-relaxed line-clamp-3 italic">
              &ldquo;{docDetail.router_reasoning}&rdquo;
            </p>
          )}
        </div>
      )}

      {/* ── Chunking ── */}
      {hasChunkingResult && (
        <div className="px-4 py-3 space-y-2">
          <SectionHeader label="Chunking" timing={timings.chunking} />
          <div className="flex flex-wrap gap-2">
            <Pill label="Chunks Created" value={String(doc.total_chunks)} />
          </div>
        </div>
      )}

      {/* ── Embedding ── */}
      {hasEmbeddingResult && (
        <div className="px-4 py-3 space-y-2">
          <SectionHeader label="Embedding" timing={timings.embedding} />
          <div className="flex flex-wrap gap-2">
            <Pill label="Model" value="BGE-large-en-v1.5" />
            <Pill label="Dimensions" value="1024" />
          </div>
        </div>
      )}

      {/* ── Knowledge Store ── */}
      {hasStoringResult && (
        <div className="px-4 py-3 space-y-2">
          <div className="flex items-center justify-between">
            <SectionHeader label="Knowledge Store" timing={timings.storing} />
            <Link
              href={`/documents/${doc.document_id}`}
              className="flex items-center gap-1 text-xs text-blue-600 dark:text-blue-400 hover:text-blue-700 dark:hover:text-blue-300 font-medium"
              onClick={(e) => e.stopPropagation()}
            >
              View document
              <ExternalLink className="h-3 w-3" />
            </Link>
          </div>
          <div className="flex flex-wrap gap-2">
            {doc.vector_chunks > 0 && (
              <Pill label="Vector Chunks" value={String(doc.vector_chunks)} accent="blue" />
            )}
            {doc.table_count > 0 && (
              <Pill label="Tables" value={String(doc.table_count)} accent="green" />
            )}
            {doc.clause_count > 0 && (
              <Pill label="Clauses" value={String(doc.clause_count)} accent="purple" />
            )}
            {doc.research_chunks > 0 && (
              <Pill label="Research Chunks" value={String(doc.research_chunks)} accent="orange" />
            )}
            {doc.vector_chunks === 0 && doc.table_count === 0 && doc.clause_count === 0 && doc.research_chunks === 0 && (
              <span className="text-xs text-gray-400 dark:text-gray-500">No chunks stored</span>
            )}
          </div>
          {!isLoading && docDetail?.doc_summary && (
            <p className="text-xs text-gray-500 dark:text-gray-400 leading-relaxed line-clamp-3 mt-1">
              {docDetail.doc_summary}
            </p>
          )}
        </div>
      )}
    </div>
  );
}

// ── StageFlow ─────────────────────────────────────────────────────────────────

function StageFlow({ doc }: { doc: PipelineDocumentDetail }) {
  const isFailed = doc.doc_status === "failed";
  const isCompleted = doc.doc_status === "completed";
  const failedStage = doc.error_stage;
  const timings = doc.stage_timings ?? {};

  // Position in the canonical order. current_stage can be "error" (on failure)
  // or any future stage not in STAGES — clamp instead of letting indexOf(-1)
  // mark every stage pending (which read as "stuck").
  const rawIdx = STAGES.indexOf((doc.current_stage ?? "queued") as PipelineStage);
  let activeIdx: number;
  if (isCompleted) activeIdx = STAGES.length;
  else if (isFailed) activeIdx = STAGES.indexOf((failedStage ?? "queued") as PipelineStage);
  else activeIdx = rawIdx;
  if (activeIdx < 0) activeIdx = 0;

  // Optional stages (images/graph) only appear once they have actually run,
  // are the live stage, or are where a failure occurred.
  const displayedStages = STAGES.filter(
    (s) =>
      !OPTIONAL_STAGES.has(s) ||
      timings[s] != null ||
      s === doc.current_stage ||
      s === failedStage
  );

  const [elapsed, setElapsed] = useState(0);
  const [selectedStage, setSelectedStage] = useState<string | null>(null);
  // Once the user clicks any card we stop auto-following the live stage so we
  // never yank their view away from a stage they're inspecting.
  const userPinnedRef = useRef(false);

  // Fetch detail data lazily — only when a stage is opened
  const needsDoc = selectedStage !== null && selectedStage !== "parsing";
  const { data: docDetail, isLoading: docDetailLoading } = useDocument(
    needsDoc ? doc.document_id : null
  );
  // Parsing panel uses page-stats
  const { data: pageStats, isLoading: pageStatsLoading } = usePageStats(
    selectedStage === "parsing" ? doc.document_id : null
  );

  const wallStartRef = useRef(0);
  const initialElapsedRef = useRef(0);
  const prevStageRef = useRef<string | null>(null);
  const latestTimingsRef = useRef(doc.stage_timings);
  const latestStartedAtRef = useRef(doc.started_at);
  latestTimingsRef.current = doc.stage_timings;
  latestStartedAtRef.current = doc.started_at;

  useEffect(() => {
    if (!doc.current_stage || isCompleted || isFailed) {
      setElapsed(0);
      prevStageRef.current = null;
      return;
    }
    if (doc.current_stage !== prevStageRef.current) {
      prevStageRef.current = doc.current_stage;
      const completedSecs = Object.values(latestTimingsRef.current ?? {}).reduce((s, t) => s + t, 0);
      const pipelineStart = latestStartedAtRef.current
        ? new Date(latestStartedAtRef.current).getTime()
        : Date.now();
      const stageStartMs = pipelineStart + completedSecs * 1000;
      initialElapsedRef.current = Math.max(0, (Date.now() - stageStartMs) / 1000);
      wallStartRef.current = performance.now();
    }
    const id = setInterval(() => {
      const wallElapsed = (performance.now() - wallStartRef.current) / 1000;
      setElapsed(initialElapsedRef.current + wallElapsed);
    }, 50);
    return () => clearInterval(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [doc.current_stage, isCompleted, isFailed]);

  const isParsing = doc.current_stage === "parsing" && !isCompleted && !isFailed;

  // Live auto-follow: while the pipeline runs, keep the right-hand detail panel
  // pointed at the stage that's currently executing — so each stage's result
  // (and the per-page parsing table) streams in on the right as it happens
  // instead of only after the user clicks at the end. On completion, settle on
  // the final "storing" result. Stops the moment the user clicks a card.
  useEffect(() => {
    if (isFailed) {
      if (failedStage) { userPinnedRef.current = false; setSelectedStage(failedStage); }
      return;
    }
    // Completed: keep whatever the live follow last landed on. Don't force a
    // panel open on historical runs the user opens fresh (prev stays null).
    if (isCompleted) return;
    const cur = doc.current_stage;
    if (cur && cur !== "done" && displayedStages.includes(cur as PipelineStage)) {
      // A new stage just went LIVE. Resume auto-follow even if the user pinned an
      // earlier stage — a click pins only WITHIN the current stage; once the
      // pipeline advances we move the panel to the live stage so each stage's
      // streaming output (per-page parsing table, per-figure image grid) shows
      // up on its own. This effect only fires on an actual current_stage change,
      // so it never yanks the view mid-stage.
      userPinnedRef.current = false;
      setSelectedStage(cur);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [doc.current_stage, isCompleted, isFailed, failedStage]);

  // Auto-open parsing panel when actively parsing and user clicks
  const handleCardClick = (stage: string, clickable: boolean) => {
    if (!clickable && !(stage === "parsing" && isParsing)) return;
    userPinnedRef.current = true;
    setSelectedStage((prev) => (prev === stage ? null : stage));
  };

  return (
    <div className="flex gap-4 py-2 items-start">
      {/* ── Left: Stage cards column ── */}
      <div className="flex flex-col items-center gap-0 flex-shrink-0">
        {displayedStages.map((stage, dispIdx) => {
          const idx = STAGES.indexOf(stage);
          const timing = timings[stage];
          const estimate = estimateFor(stage, doc);
          const isError = isFailed && stage === failedStage;
          const isDone = (idx < activeIdx || isCompleted) && !isError;
          const isCurrent = stage === doc.current_stage && !isDone && !isFailed;
          const isPending = !isDone && !isCurrent && !isError;
          // parsing and images stream live detail (per-page table / per-figure
          // grid) while running, so they're inspectable mid-flight — not just
          // once done. Other stages only become clickable when complete.
          const isClickable =
            isDone || isError || ((stage === "parsing" || stage === "images") && isCurrent);
          const isExpanded = selectedStage === stage;
          const c = STAGE_COLORS[stage] ?? STAGE_COLORS.done;

          // Overrun: live stage has blown past its estimate with no real
          // progress signal → show an indeterminate "working" state rather
          // than a fake 99% that looks frozen.
          const overrun = isCurrent && doc.stage_progress <= 0 && estimate > 0 && elapsed > estimate;
          const fillPct = isDone
            ? 100
            : isCurrent
              ? (doc.stage_progress > 0
                ? doc.stage_progress
                : overrun ? 95 : estimate > 0 ? Math.min(95, (elapsed / estimate) * 100) : 50)
              : 0;

          const timingLabel = isDone && timing != null
            ? fmtTime(timing)
            : isCurrent
              ? (overrun ? `${fmtTime(elapsed, true)} · working…` : `${fmtTime(elapsed, true)} / ~${estimate}s`)
              : estimate > 0 ? `~${estimate}s est.` : null;

          const resultHint = isDone
            ? stage === "parsing" && doc.page_count != null
              ? `${doc.page_count} pg`
              : stage === "routing" && doc.document_type
                ? capitalize(doc.document_type)
                : stage === "chunking" && doc.total_chunks != null
                  ? `${doc.total_chunks} chunks`
                  : stage === "storing" && isCompleted
                    ? `${doc.vector_chunks + doc.table_count + doc.clause_count + doc.research_chunks} stored`
                    : null
            : null;

          return (
            <div key={stage} className="flex flex-col items-center w-72">
              <button
                className={cn(
                  "relative overflow-hidden rounded-xl w-full shadow-sm border transition-all",
                  isClickable ? "cursor-pointer hover:shadow-md" : "cursor-default",
                  isDone && !isError && cn(c.border, c.text),
                  isCurrent && !isFailed && "border-blue-300 dark:border-blue-900 text-blue-700 dark:text-blue-300",
                  isError && "border-red-200 dark:border-red-900 text-red-700 dark:text-red-300",
                  isPending && "border-gray-200 dark:border-gray-800 text-gray-400 dark:text-gray-500",
                  isExpanded && isDone && !isError && cn("ring-2", c.ring),
                  isExpanded && isCurrent && "ring-2 ring-blue-400 dark:ring-blue-700",
                  isExpanded && isError && "ring-2 ring-red-400 dark:ring-red-700",
                )}
                onClick={() => handleCardClick(stage, isClickable)}
                disabled={!isClickable}
              >
                {/* Fill sweep */}
                <div
                  className={cn(
                    "absolute inset-0 transition-all duration-500 ease-out",
                    isDone && !isError && c.fill,
                    isCurrent && !isFailed && "bg-blue-100 dark:bg-blue-950",
                    isCurrent && overrun && "animate-pulse",
                    isError && "bg-red-100 dark:bg-red-950",
                    isPending && "bg-gray-50 dark:bg-gray-800/50",
                  )}
                  style={{ width: `${fillPct}%` }}
                />
                <div
                  className={cn(
                    "absolute inset-0",
                    isDone && !isError && c.base,
                    isCurrent && !isFailed && "bg-blue-50/40 dark:bg-blue-950/30",
                    isError && "bg-red-50 dark:bg-red-950",
                    isPending && "bg-gray-50 dark:bg-gray-800/50",
                  )}
                />
                <div className="relative z-10 flex items-center gap-2.5 px-4 py-3.5">
                  <div className="flex-shrink-0">
                    {isDone && !isError && <CheckCircle2 className="h-4.5 w-4.5 h-[18px] w-[18px]" />}
                    {isCurrent && !isFailed && <Loader2 className="h-[18px] w-[18px] animate-spin" />}
                    {isError && <XCircle className="h-[18px] w-[18px]" />}
                    {isPending && <Clock className="h-[18px] w-[18px]" />}
                  </div>
                  <span className="text-sm font-semibold">{STAGE_LABELS[stage]}</span>
                  {resultHint && (
                    <span className={cn(
                      "text-xs font-medium rounded px-1.5 py-0.5 border border-current/25",
                      isDone && !isError ? c.pill : "bg-white/60 dark:bg-white/10"
                    )}>
                      {resultHint}
                    </span>
                  )}
                  {timingLabel && (
                    <span className="ml-auto text-xs font-normal opacity-60 tabular-nums whitespace-nowrap">
                      {timingLabel}
                    </span>
                  )}
                  {isClickable && (
                    <ChevronDown
                      className={cn(
                        "h-3.5 w-3.5 flex-shrink-0 opacity-50 transition-transform",
                        !timingLabel && "ml-auto",
                        isExpanded && "rotate-180",
                      )}
                    />
                  )}
                </div>
              </button>

              {dispIdx < displayedStages.length - 1 && (
                <div className="py-0.5">
                  <ChevronDown className="h-3.5 w-3.5 text-gray-300 dark:text-gray-600" />
                </div>
              )}
            </div>
          );
        })}
      </div>

      {/* ── Right: Detail panel ── */}
      {selectedStage && (
        <div className="flex-1 min-w-0 self-stretch">
          <div className="h-full rounded-xl border bg-white dark:bg-gray-900 shadow-sm overflow-hidden flex flex-col">
            <StageDetailContent
              stage={selectedStage}
              doc={doc}
              docDetail={docDetail}
              docDetailLoading={docDetailLoading}
              pageStats={pageStats}
              pageStatsLoading={pageStatsLoading}
              elapsed={elapsed}
              timing={doc.stage_timings?.[selectedStage]}
            />
          </div>
        </div>
      )}
    </div>
  );
}

// ── StageDetailContent ────────────────────────────────────────────────────────

type DocDetail = ReturnType<typeof useDocument>["data"];

function StageDetailContent({
  stage, doc, docDetail, docDetailLoading, pageStats, pageStatsLoading, elapsed, timing,
}: {
  stage: string;
  doc: PipelineDocumentDetail;
  docDetail: DocDetail;
  docDetailLoading: boolean;
  pageStats: PageStats | undefined;
  pageStatsLoading: boolean;
  elapsed: number;
  timing?: number;
}) {
  const isCompleted = doc.doc_status === "completed";
  const isParsing = doc.current_stage === "parsing" && !isCompleted && doc.doc_status !== "failed";

  const PanelHeader = ({ label }: { label: string }) => (
    <div className="flex items-center justify-between px-4 py-3 border-b border-gray-100 dark:border-gray-800 bg-gray-50 dark:bg-gray-800/60">
      <p className="text-xs font-semibold text-gray-600 dark:text-gray-300 uppercase tracking-wider">{label}</p>
      {timing != null && (
        <span className="text-xs text-gray-400 tabular-nums font-medium">{fmtTime(timing)}</span>
      )}
    </div>
  );

  // ── Parsing ──────────────────────────────────────────────────────────────────
  if (stage === "parsing") {
    return <ParsingDetail doc={doc} live={isParsing} elapsed={elapsed} />;
  }

  // ── Routing ──────────────────────────────────────────────────────────────────
  if (stage === "routing") {
    return (
      <>
        <PanelHeader label="Classification" />
        <div className="px-4 py-3 space-y-3">
          {doc.document_type && (
            <div className="flex items-center gap-3 flex-wrap">
              <span className={cn(
                "inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium border capitalize",
                DOC_TYPE_COLORS[doc.document_type as DocumentType]
              )}>
                {doc.document_type}
              </span>
              {doc.router_confidence != null && (
                <div className="flex items-center gap-2">
                  <div className="w-28 h-1.5 bg-gray-200 dark:bg-gray-700 rounded-full overflow-hidden">
                    <div className="h-full bg-blue-500 rounded-full" style={{ width: `${doc.router_confidence * 100}%` }} />
                  </div>
                  <span className="text-xs text-gray-600 dark:text-gray-400 tabular-nums">
                    {(doc.router_confidence * 100).toFixed(0)}% confidence
                  </span>
                </div>
              )}
            </div>
          )}
          {docDetailLoading ? (
            <p className="text-xs text-gray-400 dark:text-gray-500 italic">Loading reasoning…</p>
          ) : docDetail?.router_reasoning ? (
            <div className="rounded-lg bg-gray-50 dark:bg-gray-800/60 border border-gray-100 dark:border-gray-700 px-3 py-2.5">
              <p className="text-xs text-gray-500 dark:text-gray-400 leading-relaxed italic">
                &ldquo;{docDetail.router_reasoning}&rdquo;
              </p>
            </div>
          ) : null}
          {!doc.document_type && (
            <span className="text-xs text-gray-400 dark:text-gray-500">No classification details available</span>
          )}
        </div>
      </>
    );
  }

  // ── Chunking ─────────────────────────────────────────────────────────────────
  if (stage === "chunking") {
    return <ChunkingDetail doc={doc} />;
  }

  // ── Images ───────────────────────────────────────────────────────────────────
  if (stage === "images") {
    return <ImagesDetail doc={doc} />;
  }

  // ── Embedding ────────────────────────────────────────────────────────────────
  if (stage === "embedding") {
    return (
      <>
        <PanelHeader label="Embedding" />
        <div className="px-4 py-3 flex flex-wrap gap-2">
          <Pill label="Model" value="BGE-large-en-v1.5" />
          <Pill label="Dimensions" value="1024" />
        </div>
      </>
    );
  }

  // ── Storing ──────────────────────────────────────────────────────────────────
  if (stage === "storing") {
    return (
      <>
        <PanelHeader label="Knowledge Store" />
        <div className="px-4 py-3 space-y-2.5">
          <div className="flex justify-end">
            <Link
              href={`/documents/${doc.document_id}`}
              className="flex items-center gap-1 text-xs text-blue-600 dark:text-blue-400 hover:text-blue-700 dark:hover:text-blue-300 font-medium"
              onClick={(e) => e.stopPropagation()}
            >
              View document <ExternalLink className="h-3 w-3" />
            </Link>
          </div>
          <div className="flex flex-wrap gap-2">
            {doc.vector_chunks > 0 && <Pill label="Vector Chunks" value={String(doc.vector_chunks)} accent="blue" />}
            {doc.table_count > 0 && <Pill label="Tables" value={String(doc.table_count)} accent="green" />}
            {doc.clause_count > 0 && <Pill label="Clauses" value={String(doc.clause_count)} accent="purple" />}
            {doc.research_chunks > 0 && <Pill label="Research Chunks" value={String(doc.research_chunks)} accent="orange" />}
            {isCompleted && doc.vector_chunks === 0 && doc.table_count === 0 && doc.clause_count === 0 && doc.research_chunks === 0 && (
              <span className="text-xs text-gray-400 dark:text-gray-500">No chunks stored</span>
            )}
          </div>
          {!docDetailLoading && docDetail?.doc_summary && (
            <div className="rounded-lg bg-gray-50 dark:bg-gray-800/60 border border-gray-100 dark:border-gray-700 px-3 py-2.5 mt-1">
              <p className="text-xs text-gray-500 dark:text-gray-400 leading-relaxed line-clamp-4">
                {docDetail.doc_summary}
              </p>
            </div>
          )}
        </div>
      </>
    );
  }

  // ── Graph ────────────────────────────────────────────────────────────────────
  if (stage === "graph") {
    return (
      <>
        <PanelHeader label="Knowledge Graph" />
        <div className="px-4 py-3 space-y-2.5">
          <div className="flex flex-wrap gap-2">
            <Pill label="Store" value="Neo4j" accent="purple" />
            <Pill label="Links" value="Cross-document" accent="purple" />
          </div>
          <p className="text-xs text-gray-500 dark:text-gray-400 leading-relaxed">
            Entities (organisations, people, dates, monetary values) are extracted and
            linked into the multi-document graph, connecting this PDF to related ones.
            Optional — degrades gracefully when Neo4j is offline.
          </p>
        </div>
      </>
    );
  }

  return null;
}

// ── Small helpers ─────────────────────────────────────────────────────────────

function SectionHeader({ label, timing }: { label: string; timing?: number }) {
  return (
    <div className="flex items-center justify-between">
      <p className="text-xs font-semibold text-gray-500 dark:text-gray-400 uppercase tracking-wide">{label}</p>
      {timing != null && (
        <span className="text-xs text-gray-400 dark:text-gray-500 tabular-nums">{fmtTime(timing)}</span>
      )}
    </div>
  );
}

function Pill({
  label,
  value,
  accent,
  dimmed,
}: {
  label: string;
  value: string;
  accent?: "blue" | "green" | "purple" | "orange";
  dimmed?: boolean;
}) {
  const accentStyles: Record<string, string> = {
    blue: "bg-blue-50 dark:bg-blue-950/40 text-blue-700 dark:text-blue-300 border-blue-200 dark:border-blue-900",
    green: "bg-green-50 dark:bg-green-950/40 text-green-700 dark:text-green-300 border-green-200 dark:border-green-900",
    purple: "bg-purple-50 dark:bg-purple-950/40 text-purple-700 dark:text-purple-300 border-purple-200 dark:border-purple-900",
    orange: "bg-orange-50 dark:bg-orange-950/40 text-orange-700 dark:text-orange-300 border-orange-200 dark:border-orange-900",
  };
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1 px-2.5 py-1 rounded-lg border text-xs font-medium",
        accent
          ? accentStyles[accent]
          : dimmed
            ? "bg-gray-100 dark:bg-gray-800 text-gray-400 dark:text-gray-500 border-gray-200 dark:border-gray-700"
            : "bg-white dark:bg-gray-900 text-gray-700 dark:text-gray-200 border-gray-200 dark:border-gray-700"
      )}
    >
      <span className="text-gray-400 dark:text-gray-500 font-normal">{label}:</span>
      {value}
    </span>
  );
}

function StatCard({ label, value, color }: { label: string; value: number | string; color: string }) {
  return (
    <div className="bg-white dark:bg-gray-900 rounded-xl border border-gray-200 dark:border-gray-800 shadow-sm px-4 py-3">
      <p className="text-xs text-gray-400 dark:text-gray-500 font-medium">{label}</p>
      <p className={cn("text-2xl font-bold mt-0.5", color)}>{value}</p>
    </div>
  );
}

function StatusBadge({ status }: { status: PipelineRunStatus }) {
  const styles: Record<PipelineRunStatus, string> = {
    completed: "bg-green-50 dark:bg-green-950 text-green-700 dark:text-green-300 border-green-200 dark:border-green-900",
    failed: "bg-red-50 dark:bg-red-950 text-red-700 dark:text-red-300 border-red-200 dark:border-red-900",
    running: "bg-blue-50 dark:bg-blue-950 text-blue-700 dark:text-blue-300 border-blue-200 dark:border-blue-900",
    empty: "bg-gray-50 dark:bg-gray-800/50 text-gray-500 dark:text-gray-400 border-gray-200 dark:border-gray-800",
  };
  const labels: Record<PipelineRunStatus, string> = {
    completed: "Completed", failed: "Failed", running: "Processing", empty: "Empty",
  };
  return (
    <span className={cn("inline-flex items-center gap-1.5 rounded-full border px-2.5 py-0.5 text-xs font-medium", styles[status])}>
      {status === "running" && <Loader2 className="h-3 w-3 animate-spin" />}
      {labels[status]}
    </span>
  );
}

function DocStatusBadge({ status }: { status: string }) {
  const map: Record<string, string> = {
    completed: "text-green-600 dark:text-green-300", failed: "text-red-500",
    uploaded: "text-gray-400 dark:text-gray-500",
  };
  // Anything that isn't a terminal/idle state is in-flight. The backend emits
  // intermediate statuses (parsing/parsed/routed/chunked/embedded/storing…) —
  // treat them all as processing so the spinner doesn't stall mid-pipeline.
  const processing = !["completed", "failed", "uploaded"].includes(status);
  const colorClass = map[status] ?? (processing ? "text-blue-500" : "text-gray-400 dark:text-gray-500");
  return (
    <span className={cn("flex items-center gap-1 text-xs font-medium capitalize", colorClass)}>
      {processing && <Loader2 className="h-3 w-3 animate-spin" />}
      {status === "completed" && <CheckCircle2 className="h-3 w-3" />}
      {status === "failed" && <XCircle className="h-3 w-3" />}
      {status}
    </span>
  );
}
