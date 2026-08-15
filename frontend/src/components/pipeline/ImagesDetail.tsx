"use client";

import { useEffect, useRef, useState } from "react";
import {
  Image,
  ImageOff,
  ScanText,
  ChevronDown,
  Loader2,
  Zap,
  Filter,
  Eye,
  SkipForward,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { useDocumentImages } from "@/hooks/useDocuments";
import type { PipelineDocumentDetail } from "@/lib/types";
import type { DocumentImage } from "@/lib/types";

// ── ImageThumb ──────────────────────────────────────────────────────────────

interface ImageThumbProps {
  src: string | null;
  alt: string;
}

function ImageThumb({ src, alt }: ImageThumbProps) {
  const [errored, setErrored] = useState(false);

  if (!src || errored) {
    return (
      <div className="w-full h-40 flex flex-col items-center justify-center gap-1 bg-gray-50 dark:bg-gray-800 rounded-t-lg">
        <ImageOff className="h-6 w-6 text-gray-300 dark:text-gray-600" />
        <span className="text-[10px] text-gray-400 dark:text-gray-500">
          preview unavailable
        </span>
      </div>
    );
  }

  return (
    // eslint-disable-next-line @next/next/no-img-element
    <img
      src={src}
      alt={alt}
      loading="lazy"
      onError={() => setErrored(true)}
      className="w-full h-40 object-contain bg-gray-50 dark:bg-gray-800 rounded-t-lg"
    />
  );
}

// ── Text helpers ──────────────────────────────────────────────────────────────

// The vision model sometimes returns the whole analysis as a fenced JSON blob
// (```json {"caption": "...", "ocr_text": "..."} ```) stored verbatim in `caption`.
// Pull the human fields back out so we never dump raw JSON at the user.
function unescape(s: string): string {
  return s
    .replace(/\\n/g, "\n")
    .replace(/\\t/g, " ")
    .replace(/\\"/g, '"')
    .replace(/\\\\/g, "\\")
    .trim();
}

function parseImageMeta(rawCaption: string, rawOcr: string | null) {
  let caption = (rawCaption ?? "").trim();
  let ocr = (rawOcr ?? "").trim();

  // Strip ```json … ``` (or plain ``` … ```) fences before attempting a parse.
  const fence = caption.match(/^```(?:json)?\s*([\s\S]*?)\s*```?$/i);
  const candidate = (fence ? fence[1] : caption).trim();

  let parsed = false;
  // 1) Try strict JSON first (well-formed responses).
  if (candidate.startsWith("{") || candidate.startsWith("[")) {
    try {
      const obj = JSON.parse(candidate);
      if (obj && typeof obj === "object" && !Array.isArray(obj)) {
        if (typeof obj.caption === "string") caption = obj.caption.trim();
        if (typeof obj.ocr_text === "string" && !ocr) ocr = obj.ocr_text.trim();
        parsed = true;
      }
    } catch {
      /* fall through to lenient parse */
    }
  }

  // 2) Lenient regex parse for the model's frequent malformed pseudo-JSON,
  //    e.g. {caption: A digital cert 'X' ..., ocr_text: "FOUNDATIONS\n..."}
  //    where keys/values may be unquoted and contain stray apostrophes.
  if (!parsed && /["']?caption["']?\s*:/.test(candidate)) {
    const capMatch = candidate.match(
      /["']?caption["']?\s*:\s*["']?([\s\S]*?)["']?\s*,\s*["']?ocr_text["']?\s*:/i
    );
    const ocrMatch = candidate.match(
      /["']?ocr_text["']?\s*:\s*["']?([\s\S]*?)["']?\s*}?\s*$/i
    );
    if (capMatch) caption = unescape(capMatch[1]);
    if (ocrMatch && !rawOcr) ocr = unescape(ocrMatch[1]);
  } else if (parsed) {
    caption = unescape(caption);
    ocr = unescape(ocr);
  }

  return { caption: caption.trim(), ocr: ocr.trim() };
}

// OCR of repeating layouts (borders, watermarks) yields the same phrase 100×.
// Collapse any run of a 1–4 word unit repeated ≥3 times into "unit ×N".
function condenseRepeats(text: string): string {
  const tokens = text.split(/\s+/).filter(Boolean);
  const out: string[] = [];
  let i = 0;
  while (i < tokens.length) {
    let bestW = 0;
    let bestCount = 0;
    for (let w = 1; w <= 4 && i + w <= tokens.length; w++) {
      const unit = tokens.slice(i, i + w).join(" ");
      let count = 1;
      while (
        i + (count + 1) * w <= tokens.length &&
        tokens.slice(i + count * w, i + (count + 1) * w).join(" ") === unit
      ) {
        count++;
      }
      if (count >= 3 && count * w > bestCount * bestW) {
        bestW = w;
        bestCount = count;
      }
    }
    if (bestW > 0) {
      out.push(`${tokens.slice(i, i + bestW).join(" ")} ×${bestCount}`);
      i += bestW * bestCount;
    } else {
      out.push(tokens[i]);
      i++;
    }
  }
  return out.join(" ");
}

// Bold quoted spans ('…' / "…") so titles/named entities stand out in the caption.
function renderHighlighted(text: string) {
  const parts = text.split(/('[^']+'|"[^"]+")/g);
  return parts.map((part, idx) =>
    /^['"][\s\S]+['"]$/.test(part) ? (
      <strong key={idx} className="font-semibold text-gray-800 dark:text-gray-100">
        {part.slice(1, -1)}
      </strong>
    ) : (
      <span key={idx}>{part}</span>
    )
  );
}

// ── CaptionBlock ────────────────────────────────────────────────────────────

interface CaptionBlockProps {
  caption: string;
}

function CaptionBlock({ caption }: CaptionBlockProps) {
  const [expanded, setExpanded] = useState(false);
  const isLong = caption.length > 160;

  return (
    <div>
      <span className="block text-[9px] font-semibold uppercase tracking-wider text-gray-400 dark:text-gray-500 mb-0.5">
        Description
      </span>
      <p
        className={cn(
          "text-xs text-gray-600 dark:text-gray-300 leading-relaxed",
          !expanded && isLong && "line-clamp-3"
        )}
      >
        {renderHighlighted(caption)}
      </p>
      {isLong && (
        <button
          onClick={() => setExpanded((v) => !v)}
          className="mt-0.5 text-[10px] text-blue-500 hover:text-blue-600 dark:text-blue-400"
        >
          {expanded ? "Show less" : "Show more"}
        </button>
      )}
    </div>
  );
}

// ── OcrBlock ────────────────────────────────────────────────────────────────

interface OcrBlockProps {
  text: string;
}

function OcrBlock({ text }: OcrBlockProps) {
  const [open, setOpen] = useState(false);
  const condensed = condenseRepeats(text);

  return (
    <div className="mt-2">
      <button
        onClick={() => setOpen((v) => !v)}
        className="flex items-center gap-1 text-[10px] text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-200"
      >
        <ScanText className="h-3 w-3" />
        OCR text
        <ChevronDown
          className={cn("h-3 w-3 transition-transform", open && "rotate-180")}
        />
      </button>
      {open && (
        <pre className="mt-1 text-[11px] text-gray-500 dark:text-gray-400 bg-gray-50 dark:bg-gray-800/60 rounded p-2 max-h-40 overflow-y-auto whitespace-pre-wrap break-words font-mono">
          {condensed}
        </pre>
      )}
    </div>
  );
}

// ── ImageCard ───────────────────────────────────────────────────────────────

interface ImageCardProps {
  item: DocumentImage;
}

// Status chip styling per processing_status value.
const STATUS_STYLE: Record<
  DocumentImage["processing_status"],
  { label: string; className: string }
> = {
  VLM_PROCESSED: {
    label: "VLM",
    className:
      "bg-green-50 dark:bg-green-950 text-green-600 dark:text-green-300 border-green-100 dark:border-green-900",
  },
  OCR_ONLY: {
    label: "OCR only",
    className:
      "bg-amber-50 dark:bg-amber-950 text-amber-600 dark:text-amber-300 border-amber-100 dark:border-amber-900",
  },
  SKIPPED: {
    label: "Skipped",
    className:
      "bg-gray-50 dark:bg-gray-800 text-gray-500 dark:text-gray-400 border-gray-200 dark:border-gray-700",
  },
};

function ImageCard({ item }: ImageCardProps) {
  const {
    image_index,
    page_number,
    width,
    height,
    image_url,
    processing_status,
    skip_reason,
    filter_stage,
    image_type,
  } = item;
  const { caption, ocr } = parseImageMeta(item.caption, item.ocr_text);
  const statusStyle = STATUS_STYLE[processing_status];
  const isVlmProcessed = processing_status === "VLM_PROCESSED";

  return (
    <div className="rounded-lg border border-gray-100 dark:border-gray-800 bg-white dark:bg-gray-900 overflow-hidden">
      <ImageThumb src={image_url} alt={caption || `Image ${image_index}`} />

      <div className="p-2 space-y-1.5">
        {/* Meta row */}
        <div className="flex flex-wrap items-center gap-1">
          <span
            className={cn(
              "text-[10px] rounded px-1 border",
              "bg-blue-50 dark:bg-blue-950 text-blue-600 dark:text-blue-300 border-blue-100 dark:border-blue-900"
            )}
          >
            {page_number != null ? `Page ${page_number}` : "Page —"}
          </span>
          {width != null && height != null && (
            <span className="text-[10px] rounded px-1 border border-gray-100 dark:border-gray-800 text-gray-500 dark:text-gray-400">
              {width}×{height}
            </span>
          )}
          <span className="text-[10px] rounded px-1 border border-gray-100 dark:border-gray-800 text-gray-500 dark:text-gray-400">
            #{image_index}
          </span>
          {statusStyle && (
            <span className={cn("text-[10px] rounded px-1 border", statusStyle.className)}>
              {statusStyle.label}
            </span>
          )}
          {image_type && (
            <span className="text-[10px] rounded px-1 border border-purple-100 dark:border-purple-900 bg-purple-50 dark:bg-purple-950 text-purple-500 dark:text-purple-300">
              {image_type}
            </span>
          )}
        </div>

        {/* Caption / skip reason */}
        {isVlmProcessed ? (
          caption ? (
            <CaptionBlock caption={caption} />
          ) : (
            <p className="text-xs text-gray-400 dark:text-gray-600 italic">No caption</p>
          )
        ) : (
          <div>
            <p className="text-xs text-gray-500 dark:text-gray-400 italic">
              {skip_reason || "Skipped before VLM analysis"}
            </p>
            {filter_stage && (
              <span className="text-[10px] text-gray-400 dark:text-gray-600">
                via {filter_stage}
              </span>
            )}
          </div>
        )}

        {/* OCR disclosure */}
        {ocr ? <OcrBlock text={ocr} /> : null}
      </div>
    </div>
  );
}

// ── ImagesDetail (main export) ──────────────────────────────────────────────

interface Props {
  doc: PipelineDocumentDetail;
}

export function ImagesDetail({ doc }: Props) {
  // Poll while the doc is still processing — NOT only during the exact "images"
  // stage. The worker stores figures one at a time during the images stage, so
  // we want each one to surface the instant it lands. Polling across the whole
  // processing window means it streams in even if the panel is opened slightly
  // before/after the images stage (e.g. while a later stage is running).
  const isProcessing =
    doc.doc_status !== "completed" && doc.doc_status !== "failed";
  // The images stage specifically is "live" — drives the analyzing badge and the
  // "more images processing…" footer (more figures are still expected).
  const isLive = doc.current_stage === "images" && isProcessing;
  const { data, isLoading, refetch } = useDocumentImages(
    doc.document_id,
    isProcessing ? 1200 : false,
  );

  // One final refetch when processing finishes: polling stops the instant the run
  // goes terminal, but the storing stage adds late rows (table crops #20000+ and
  // stored_in flips), so without this the last images only appear on a manual reload.
  const wasProcessing = useRef(isProcessing);
  useEffect(() => {
    if (wasProcessing.current && !isProcessing) refetch();
    wasProcessing.current = isProcessing;
  }, [isProcessing, refetch]);

  const total = data?.total ?? 0;
  const progress = Math.round(doc.stage_progress ?? 0);
  const metrics = data?.metrics;

  return (
    <div>
      {/* Header */}
      <div className="flex items-center justify-between px-4 py-3 border-b border-gray-100 dark:border-gray-800 bg-gray-50 dark:bg-gray-800/60">
        <div className="flex items-center gap-2">
          {/* eslint-disable-next-line jsx-a11y/alt-text */}
          <Image className="h-3.5 w-3.5 text-gray-500 dark:text-gray-400" aria-hidden="true" />
          <span className="text-xs font-semibold text-gray-600 dark:text-gray-300 uppercase tracking-wider">
            Image Analysis
          </span>
        </div>
        <div className="flex items-center gap-1.5">
          {isLive && (
            <span className="flex items-center gap-1 text-[10px] rounded px-1.5 border border-indigo-200 dark:border-indigo-900 bg-indigo-50 dark:bg-indigo-950 text-indigo-600 dark:text-indigo-300">
              <Loader2 className="h-2.5 w-2.5 animate-spin" />
              analyzing {progress}%
            </span>
          )}
          {data != null && (
            <span className="text-[10px] rounded px-1.5 border border-gray-200 dark:border-gray-700 text-gray-500 dark:text-gray-400">
              {total} {total === 1 ? "image" : "images"}
            </span>
          )}
        </div>
      </div>

      {/* Body */}
      <div className="px-4 py-3 space-y-3">
        <p className="text-[11px] text-gray-500 dark:text-gray-400">
          Each figure is cropped and pre-filtered to skip decorative or junk
          images (icons, logos, blanks, duplicates); the rest are OCR&apos;d
          and/or analyzed by Gemma-4 vision into retrieval-ready structured
          content, embedded (BGE-1024) and saved to image_store — appearing
          here as each one finishes.
        </p>

        {/* Metrics summary bar */}
        {metrics && metrics.total > 0 && (
          <div className="rounded-lg border border-gray-100 dark:border-gray-800 bg-gray-50/60 dark:bg-gray-800/40 px-2.5 py-2 space-y-1.5">
            <div className="flex flex-wrap items-center gap-1.5">
              <span className="flex items-center gap-1 text-[10px] rounded px-1.5 border border-gray-200 dark:border-gray-700 text-gray-500 dark:text-gray-400">
                Total {metrics.total}
              </span>
              <span className="flex items-center gap-1 text-[10px] rounded px-1.5 border bg-green-50 dark:bg-green-950 text-green-600 dark:text-green-300 border-green-100 dark:border-green-900">
                <Eye className="h-2.5 w-2.5" />
                VLM {metrics.vlm_processed}
              </span>
              {metrics.ocr_only > 0 && (
                <span className="flex items-center gap-1 text-[10px] rounded px-1.5 border bg-amber-50 dark:bg-amber-950 text-amber-600 dark:text-amber-300 border-amber-100 dark:border-amber-900">
                  <ScanText className="h-2.5 w-2.5" />
                  OCR only {metrics.ocr_only}
                </span>
              )}
              <span className="flex items-center gap-1 text-[10px] rounded px-1.5 border bg-gray-100 dark:bg-gray-800 text-gray-500 dark:text-gray-400 border-gray-200 dark:border-gray-700">
                <SkipForward className="h-2.5 w-2.5" />
                Skipped {metrics.skipped}
              </span>
              <span className="flex items-center gap-1 text-[10px] rounded px-1.5 border bg-indigo-50 dark:bg-indigo-950 text-indigo-600 dark:text-indigo-300 border-indigo-200 dark:border-indigo-900 font-semibold">
                <Zap className="h-2.5 w-2.5" />
                {metrics.vlm_avoided_pct}% VLM avoided
              </span>
            </div>
            {Object.keys(metrics.by_stage).length > 0 && (
              <div className="flex items-center gap-1 text-[10px] text-gray-400 dark:text-gray-500">
                <Filter className="h-2.5 w-2.5 shrink-0" />
                <span>
                  decided at —{" "}
                  {Object.entries(metrics.by_stage)
                    .map(([stage, count]) => `${stage} ×${count}`)
                    .join(" · ")}
                </span>
              </div>
            )}
            {Object.keys(metrics.by_type).length > 0 && (
              <div className="text-[10px] text-gray-400 dark:text-gray-500 pl-[15px]">
                {Object.entries(metrics.by_type)
                  .map(([type, count]) => `${type} ×${count}`)
                  .join(" · ")}
              </div>
            )}
          </div>
        )}

        {/* First load, nothing yet */}
        {isLoading && total === 0 && (
          <div className="flex items-center justify-center gap-2 py-10 text-sm text-gray-500 dark:text-gray-400">
            <Loader2 className="h-4 w-4 animate-spin" />
            Loading images…
          </div>
        )}

        {/* Live but no rows stored yet — don't claim "none extracted" */}
        {!isLoading && isLive && total === 0 && (
          <div className="flex items-center justify-center gap-2 py-10 text-sm text-indigo-600 dark:text-indigo-300">
            <Loader2 className="h-4 w-4 animate-spin" />
            Analyzing images… {progress}%
          </div>
        )}

        {/* Terminal state, genuinely no images */}
        {!isLoading && !isLive && data && total === 0 && (
          <p className="py-6 text-center text-sm text-gray-400 dark:text-gray-500">
            No images were extracted from this document.
          </p>
        )}

        {total > 0 && (
          <div className="overflow-y-auto" style={{ maxHeight: "460px" }}>
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
              {data!.items.map((item) => (
                <ImageCard key={item.image_index} item={item} />
              ))}
            </div>
            {isLive && (
              <div className="flex items-center justify-center gap-2 py-3 text-xs text-gray-400 dark:text-gray-500">
                <Loader2 className="h-3 w-3 animate-spin" />
                more images processing…
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

export default ImagesDetail;
