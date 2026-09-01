"use client";

import { useState, useCallback } from "react";
import { useRouter } from "next/navigation";
import { useQueryClient } from "@tanstack/react-query";
import {
  Upload,
  HardDrive,
  Globe,
  Play,
  X,
  FileText,
  CheckCircle2,
  XCircle,
  Loader2,
  Layers,
} from "lucide-react";
import { FileDropzone } from "@/components/upload/FileDropzone";
import { apiClient } from "@/lib/api-client";
import { cn, formatBytes } from "@/lib/utils";
import type { PipelineSource } from "@/lib/types";

interface StagedFile {
  id: string;
  file: File;
}

export default function UploadPage() {
  const qc = useQueryClient();
  const router = useRouter();

  const [source, setSource] = useState<PipelineSource>("local");
  const [pipelineName, setPipelineName] = useState("");
  const [description, setDescription] = useState("");

  const [staged, setStaged] = useState<StagedFile[]>([]);
  const [submitting, setSubmitting] = useState(false);
  const [result, setResult] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const addFiles = useCallback((files: File[]) => {
    setStaged((prev) => [
      ...files.map((file) => ({ id: crypto.randomUUID(), file })),
      ...prev,
    ]);
  }, []);

  const removeFile = (id: string) =>
    setStaged((prev) => prev.filter((f) => f.id !== id));

  const canSubmit =
    source === "local" &&
    pipelineName.trim().length > 0 &&
    staged.length > 0 &&
    !submitting;

  const handleSubmit = async () => {
    if (!canSubmit) return;
    setSubmitting(true);
    setResult(null);
    setError(null);
    try {
      const res = await apiClient.createPipeline({
        name: pipelineName.trim(),
        description: description.trim() || undefined,
        source,
        files: staged.map((f) => f.file),
      });
      setResult(
        `Pipeline "${res.name}" started — ${res.files_queued} queued for ingestion.`
      );
      setStaged([]);
      setPipelineName("");
      setDescription("");
      qc.invalidateQueries({ queryKey: ["pipelines"] });
      qc.invalidateQueries({ queryKey: ["documents"] });
      if (res.pipeline_run_id) {
        router.push(`/upload/${res.pipeline_run_id}`);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "Pipeline creation failed");
    } finally {
      setSubmitting(false);
    }
  };

  const TABS: { key: PipelineSource; label: string; icon: typeof Upload }[] = [
    { key: "local", label: "Local Upload", icon: Upload },
    { key: "gdrive", label: "Google Drive", icon: HardDrive },
    { key: "sharepoint", label: "SharePoint", icon: Globe },
  ];

  return (
    <div className="max-w-5xl mx-auto space-y-6">
      {/* ── Page Header ───────────────────────────────────────────── */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl sm:text-2xl font-extrabold text-slate-900 dark:text-white tracking-tight flex items-center gap-2.5">
            <Layers className="w-6 h-6 text-indigo-600 dark:text-indigo-400" />
            Ingestion Pipeline
          </h1>
          <p className="text-xs sm:text-sm text-slate-500 dark:text-slate-400 mt-1">
            Upload PDFs to parse, chunk, embed, and route across our specialized tabular, clause, vector, graph, and visual stores.
          </p>
        </div>
      </div>

      {/* ── Upload Card ───────────────────────────────────────────── */}
      <div className="bg-white dark:bg-[#202024]/80 backdrop-blur-xl rounded-2xl border border-slate-200 dark:border-white/[0.08] shadow-sm dark:shadow-2xl overflow-hidden transition-colors">
        {/* Source Selector Tabs */}
        <div className="px-6 py-4 border-b border-slate-200/80 dark:border-white/[0.07] bg-slate-50/70 dark:bg-white/[0.02] flex items-center justify-between">
          <div className="flex flex-wrap gap-2">
            {TABS.map(({ key, label, icon: Icon }) => (
              <button
                key={key}
                type="button"
                onClick={() => setSource(key)}
                className={cn(
                  "flex items-center gap-2 px-3.5 py-1.5 rounded-xl border text-xs font-semibold transition-all",
                  source === key
                    ? "border-indigo-500/50 bg-indigo-50 dark:bg-indigo-600/20 text-indigo-700 dark:text-indigo-300 shadow-sm"
                    : "border-slate-200 dark:border-white/[0.08] bg-white dark:bg-white/[0.02] text-slate-600 dark:text-slate-400 hover:bg-slate-100 dark:hover:bg-white/[0.06] hover:text-slate-900 dark:hover:text-slate-200"
                )}
              >
                <Icon className="h-3.5 w-3.5" />
                {label}
              </button>
            ))}
          </div>
          <span className="text-[11px] font-mono text-slate-400 dark:text-slate-400">
            PDF
          </span>
        </div>

        <div className="p-6 space-y-6">
          {source !== "local" && (
            <div className="rounded-xl bg-amber-50 dark:bg-amber-500/10 border border-amber-200 dark:border-amber-500/20 px-4 py-3 text-xs text-amber-700 dark:text-amber-300">
              {TABS.find((t) => t.key === source)?.label} connector coming soon. Use Local Upload to ingest files immediately.
            </div>
          )}

          {/* Form Inputs */}
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            <Field label="Pipeline Name" required>
              <input
                type="text"
                value={pipelineName}
                onChange={(e) => setPipelineName(e.target.value)}
                placeholder="e.g. FY2025 Reliance Annual Report"
                className="w-full rounded-xl bg-slate-50/50 dark:bg-slate-900/60 border border-slate-200 dark:border-white/[0.1] px-4 py-2.5 text-xs sm:text-sm text-slate-900 dark:text-white placeholder-slate-400 dark:placeholder-slate-500 focus:border-indigo-500 focus:ring-2 focus:ring-indigo-500/20 outline-none transition-all"
              />
            </Field>
            <Field label="Description">
              <input
                type="text"
                value={description}
                onChange={(e) => setDescription(e.target.value)}
                placeholder="Optional batch description"
                className="w-full rounded-xl bg-slate-50/50 dark:bg-slate-900/60 border border-slate-200 dark:border-white/[0.1] px-4 py-2.5 text-xs sm:text-sm text-slate-900 dark:text-white placeholder-slate-400 dark:placeholder-slate-500 focus:border-indigo-500 focus:ring-2 focus:ring-indigo-500/20 outline-none transition-all"
              />
            </Field>
          </div>

          {/* Dropzone */}
          <FileDropzone onFiles={addFiles} disabled={source !== "local"} />

          {/* Staged Files List */}
          {staged.length > 0 && (
            <div className="space-y-2">
              <div className="text-xs font-semibold text-slate-600 dark:text-slate-400 uppercase tracking-wider">
                Staged Files ({staged.length})
              </div>
              <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
                {staged.map((f) => (
                  <div
                    key={f.id}
                    className="flex items-center gap-3 rounded-xl bg-slate-50 dark:bg-white/[0.03] border border-slate-200 dark:border-white/[0.08] px-3.5 py-2.5 transition-all"
                  >
                    <div className="w-8 h-8 rounded-lg bg-indigo-100 dark:bg-indigo-500/15 flex items-center justify-center text-indigo-600 dark:text-indigo-400 flex-shrink-0">
                      <FileText className="h-4 w-4" />
                    </div>
                    <div className="min-w-0 flex-1">
                      <p className="text-xs font-medium text-slate-800 dark:text-slate-200 truncate">
                        {f.file.name}
                      </p>
                      <p className="text-[10px] text-slate-400 dark:text-slate-500 font-mono">
                        {formatBytes(f.file.size)}
                      </p>
                    </div>
                    <button
                      type="button"
                      onClick={() => removeFile(f.id)}
                      className="text-slate-400 hover:text-red-500 transition-colors p-1"
                    >
                      <X className="h-3.5 w-3.5" />
                    </button>
                  </div>
                ))}
              </div>
            </div>
          )}

          {/* Result / Error Banners */}
          {result && (
            <div className="flex items-start gap-2.5 rounded-xl bg-emerald-50 dark:bg-emerald-500/10 border border-emerald-200 dark:border-emerald-500/20 px-4 py-3 text-xs text-emerald-700 dark:text-emerald-300">
              <CheckCircle2 className="h-4 w-4 mt-0.5 flex-shrink-0 text-emerald-600 dark:text-emerald-400" />
              <span>{result}</span>
            </div>
          )}
          {error && (
            <div className="flex items-start gap-2.5 rounded-xl bg-red-50 dark:bg-red-500/10 border border-red-200 dark:border-red-500/20 px-4 py-3 text-xs text-red-600 dark:text-red-300">
              <XCircle className="h-4 w-4 mt-0.5 flex-shrink-0 text-red-600 dark:text-red-400" />
              <span>{error}</span>
            </div>
          )}

          {/* Submit Button */}
          <button
            type="button"
            onClick={handleSubmit}
            disabled={!canSubmit}
            className={cn(
              "w-full flex items-center justify-center gap-2 rounded-xl py-3 text-sm font-semibold text-white transition-all duration-200",
              canSubmit
                ? "bg-gradient-to-r from-indigo-600 via-indigo-500 to-violet-600 hover:from-indigo-500 hover:to-violet-500 shadow-md hover:shadow-lg shadow-indigo-500/20 cursor-pointer"
                : "bg-slate-200 dark:bg-white/[0.05] text-slate-400 dark:text-slate-500 border border-transparent dark:border-white/[0.05] cursor-not-allowed"
            )}
          >
            {submitting ? (
              <>
                <Loader2 className="h-4 w-4 animate-spin" />
                Ingesting Files into Stores...
              </>
            ) : (
              <>
                <Play className="h-4 w-4" />
                Execute Multi-Store Ingestion
              </>
            )}
          </button>
        </div>
      </div>
    </div>
  );
}

function Field({
  label,
  required,
  children,
}: {
  label: string;
  required?: boolean;
  children: React.ReactNode;
}) {
  return (
    <div className="space-y-1.5">
      <label className="block text-xs font-semibold text-slate-700 dark:text-slate-300">
        {label}
        {required && <span className="text-red-500 dark:text-red-400"> *</span>}
      </label>
      {children}
    </div>
  );
}
