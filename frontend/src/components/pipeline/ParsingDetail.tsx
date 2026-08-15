"use client";

import { FileText, Table2, Image as ImageIcon, BookOpen, Languages, Loader2, Layers, CheckCircle2, Clock, RotateCw } from "lucide-react";
import { cn } from "@/lib/utils";
import { usePageStats } from "@/hooks/useDocuments";
import type { PipelineDocumentDetail, StageDetailPage } from "@/lib/types";

function fmtTime(s: number): string {
  if (s < 0.05) return "0s";
  if (s < 60) return `${Math.round(s)}s`;
  const m = Math.floor(s / 60);
  return `${m}m ${Math.round(s % 60)}s`;
}
function wordsOf(p: StageDetailPage): number {
  return p.est_words ?? (p.chars != null ? Math.round(p.chars / 5) : 0);
}

function Metric({ icon, label, value, hint }: { icon: React.ReactNode; label: string; value: React.ReactNode; hint?: string }) {
  return (
    <div className="flex flex-col gap-0.5 px-3 py-2 bg-gray-50 dark:bg-gray-800/60 rounded border border-gray-100 dark:border-gray-700" title={hint}>
      <div className="flex items-center gap-1 text-[10px] text-gray-500 dark:text-gray-400 uppercase tracking-wider font-medium">{icon}{label}</div>
      <span className="text-base font-semibold text-gray-800 dark:text-gray-100 tabular-nums">{value}</span>
    </div>
  );
}

function Count({ n, faint }: { n: number | null | undefined; faint?: boolean }) {
  if (n == null) return <span className="text-gray-300 dark:text-gray-600">—</span>;
  if (n === 0) return <span className="text-gray-300 dark:text-gray-600 tabular-nums">0</span>;
  return <span className={cn("tabular-nums", faint ? "text-gray-400 dark:text-gray-500" : "font-medium text-gray-700 dark:text-gray-300")}>{n}</span>;
}

interface Props {
  doc: PipelineDocumentDetail;
  live?: boolean;
  elapsed?: number;
}

// Extended row type: placeholder slots for pages not yet received from backend
type PageRow = StageDetailPage & { placeholder?: boolean };

export function ParsingDetail({ doc, live, elapsed }: Props) {
  const parsing = doc.stage_detail?.parsing;
  const pages: StageDetailPage[] = parsing?.pages ?? [];
  const hasStageDetail = pages.length > 0;
  const { data: pageStats } = usePageStats(!live && !hasStageDetail ? doc.document_id : null);

  const total = parsing?.total_pages ?? doc.page_count ?? pageStats?.total_pages ?? pages.length;
  const donePages = pages.filter((p) => p.done);
  const pagesDone = parsing?.pages_done ?? (live ? donePages.length : total);
  const pct = total ? Math.round((pagesDone / total) * 100) : 0;

  // running totals from finished pages (climb as pages complete)
  const sumWords = !live ? (doc.word_count ?? donePages.reduce((s, p) => s + wordsOf(p), 0))
                         : donePages.reduce((s, p) => s + wordsOf(p), 0);
  const sumTables = donePages.reduce((s, p) => s + (p.tables ?? 0), 0);
  const sumImages = donePages.reduce((s, p) => s + (p.images ?? 0), 0);
  const language = pageStats?.language ? pageStats.language.toUpperCase() : "EN";

  // ETA from real page rate
  let eta: string | null = null;
  if (live && elapsed && pagesDone > 0 && pagesDone < total) {
    const remaining = (total - pagesDone) * (elapsed / pagesDone);
    eta = fmtTime(remaining);
  }

  // Build rows:
  // - Live + total known: show ALL page slots immediately (parallel parsing).
  //   Known pages get real data; unknown slots get placeholder state.
  // - Non-live with stage_detail: use pages array directly.
  // - Non-live without stage_detail: fall back to pageStats.
  let rows: PageRow[];
  if (live && total > 0) {
    const pageMap = new Map(pages.map((p) => [p.page, p]));
    rows = Array.from({ length: total }, (_, i) => {
      const pg = i + 1;
      return pageMap.get(pg) ?? { page: pg, images: 0, done: false, placeholder: true };
    });
  } else if (hasStageDetail) {
    rows = pages;
  } else {
    rows = (pageStats?.pages ?? []).map((p) => ({
      page: p.page, images: p.images, tables: p.tables, est_words: p.est_words, done: true,
    }));
  }

  // max words from DONE pages only (pending pages have 0 words — don't let them skew scale)
  const maxWords = rows.filter((p) => p.done).reduce((m, p) => Math.max(m, wordsOf(p)), 0);

  return (
    <div className="text-sm">
      {/* header */}
      <div className="flex items-center justify-between px-4 py-3 border-b border-gray-100 dark:border-gray-800 bg-gray-50 dark:bg-gray-800/60">
        <div className="flex items-center gap-2">
          {live ? <Loader2 className="w-3.5 h-3.5 text-blue-500 animate-spin" /> : <FileText className="w-3.5 h-3.5 text-gray-500 dark:text-gray-400" />}
          <span className="text-xs font-semibold text-gray-600 dark:text-gray-300 uppercase tracking-wider">{live ? "Parsing — Live" : "Parsing Detail"}</span>
        </div>
        {live && elapsed != null && <span className="text-xs tabular-nums text-gray-400 dark:text-gray-500">{fmtTime(elapsed)}{eta ? ` · ~${eta} left` : ""}</span>}
      </div>

      {/* overall page progress (live) */}
      {live && total > 0 && (
        <div className="px-4 pt-3">
          <div className="flex justify-between text-[11px] text-gray-500 dark:text-gray-400 mb-1">
            <span className="font-medium text-gray-700 dark:text-gray-300">{pagesDone} of {total} pages parsed</span>
            <span className="tabular-nums">{pct}%</span>
          </div>
          <div className="w-full h-2 rounded bg-gray-100 dark:bg-gray-800 overflow-hidden">
            <div className="h-full bg-blue-500 rounded transition-all duration-500" style={{ width: `${pct}%` }} />
          </div>
        </div>
      )}

      {/* metric cards — Tables & Images are live COUNTS that climb as pages finish */}
      <div className="px-4 py-3 grid grid-cols-2 sm:grid-cols-5 gap-2">
        <Metric icon={<BookOpen className="w-3 h-3" />} label="Pages" value={total} />
        <Metric icon={<FileText className="w-3 h-3" />} label="Words" value={Number(sumWords).toLocaleString()} />
        <Metric icon={<Table2 className="w-3 h-3" />} label="Tables" value={sumTables} />
        <Metric icon={<ImageIcon className="w-3 h-3" />} label="Images" value={sumImages} />
        <Metric icon={<Languages className="w-3 h-3" />} label="Language" value={live ? "…" : language} />
      </div>

      {/* per-page table */}
      <div className="px-4 pb-3">
        <div className="text-[10px] text-gray-400 dark:text-gray-500 uppercase tracking-wider mb-1">
          Per-page breakdown
          {live && <span className="normal-case text-gray-400"> · fills in as each page completes</span>}
        </div>
        {rows.length === 0 ? (
          <div className="text-xs text-gray-400 dark:text-gray-500 py-2">No per-page data yet…</div>
        ) : (
          <div className="overflow-y-auto" style={{ maxHeight: "420px" }}>
            <table className="w-full text-xs border-separate" style={{ borderSpacing: 0 }}>
              <thead className="sticky top-0 bg-white dark:bg-gray-900">
                <tr className="text-[10px] uppercase tracking-wider text-gray-400 dark:text-gray-500 text-left">
                  <th className="py-1 pr-2 font-medium">Page</th>
                  <th className="py-1 pr-2 font-medium">Words</th>
                  <th className="py-1 px-2 font-medium text-center" title="Text blocks: paragraphs, headings and list items detected on the page">Blocks</th>
                  <th className="py-1 px-2 font-medium text-center">Tables</th>
                  <th className="py-1 px-2 font-medium text-center">Images</th>
                  <th className="py-1 pl-2 font-medium text-center">Status</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((p) => {
                  const row = p as PageRow;
                  const w = wordsOf(p);
                  const done = p.done === true;
                  // placeholder = slot exists but backend hasn't sent data yet (queued)
                  // inFlight    = backend sent the page object but it's still parsing (done=false, not placeholder)
                  const isPlaceholder = !done && !!row.placeholder;
                  const inFlight = !done && !isPlaceholder && live;

                  // bar: done → proportional green (word density vs max page)
                  //      inFlight → full-width blue pulse
                  //      placeholder → thin gray stub
                  const barPct = done && maxWords > 0
                    ? Math.max(4, Math.round((w / maxWords) * 100))
                    : 0;
                  const barPctLabel = done && maxWords > 0
                    ? `${Math.round((w / maxWords) * 100)}%`
                    : null;

                  return (
                    <tr
                      key={p.page}
                      className={cn(
                        "border-t border-gray-50 dark:border-gray-800/60 transition-all duration-500",
                        isPlaceholder && "opacity-35",
                        inFlight && "opacity-70"
                      )}
                    >
                      <td className="py-1.5 pr-2 tabular-nums text-gray-500 dark:text-gray-400 font-medium whitespace-nowrap">
                        Pg {p.page}
                      </td>
                      <td className="py-1.5 pr-2">
                        <div className="flex items-center gap-2">
                          {/* progress bar */}
                          <div className="w-16 h-2 rounded-full bg-gray-100 dark:bg-gray-700 overflow-hidden shrink-0">
                            {inFlight ? (
                              <div className="h-full w-full bg-blue-300 dark:bg-blue-800 animate-pulse rounded-full" />
                            ) : isPlaceholder ? (
                              <div className="h-full w-[6%] bg-gray-300 dark:bg-gray-600 rounded-full" />
                            ) : (
                              <div
                                className="h-full rounded-full transition-all duration-700 ease-out bg-emerald-400 dark:bg-emerald-500"
                                style={{ width: `${barPct}%` }}
                              />
                            )}
                          </div>
                          {/* % label — only when done */}
                          <span className="text-[10px] tabular-nums font-semibold w-8 shrink-0 text-right">
                            {done && barPctLabel
                              ? <span className="text-emerald-600 dark:text-emerald-400">{barPctLabel}</span>
                              : <span className="text-gray-300 dark:text-gray-600">—</span>
                            }
                          </span>
                          {/* word count */}
                          <span className={cn(
                            "tabular-nums text-xs",
                            done ? "text-gray-600 dark:text-gray-300" : "text-gray-400 dark:text-gray-500"
                          )}>
                            {done ? (w ? w.toLocaleString() : "0") : (inFlight ? "…" : "—")}
                          </span>
                        </div>
                      </td>
                      <td className="py-1.5 px-2 text-center">
                        <span className="inline-flex items-center gap-0.5 justify-center">
                          <Layers className="w-2.5 h-2.5 text-gray-400" />
                          <Count n={done ? (p.blocks ?? 0) : null} />
                        </span>
                      </td>
                      <td className="py-1.5 px-2 text-center">
                        <Count n={done ? (p.tables ?? 0) : null} />
                      </td>
                      <td className="py-1.5 px-2 text-center text-indigo-600 dark:text-indigo-300">
                        <Count n={done ? p.images : null} faint={!done} />
                      </td>
                      <td className="py-1.5 pl-2 text-center">
                        {done ? (
                          <CheckCircle2 className="w-3.5 h-3.5 text-emerald-500 inline" />
                        ) : inFlight ? (
                          <span className="inline-flex items-center gap-1 text-[10px] text-blue-400">
                            <RotateCw className="w-3 h-3 animate-spin" />parsing
                          </span>
                        ) : (
                          <span className="inline-flex items-center gap-1 text-[10px] text-gray-400 dark:text-gray-500">
                            <Clock className="w-3 h-3" />queued
                          </span>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
}

export default ParsingDetail;
