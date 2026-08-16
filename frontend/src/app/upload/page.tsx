"use client";

import { useState, useCallback } from "react";
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
        `Pipeline "${res.name}" started — ${res.files_queued} queued, ${res.files_failed} failed.`
      );
      setStaged([]);
      setPipelineName("");
      setDescription("");
      qc.invalidateQueries({ queryKey: ["pipelines"] });
      qc.invalidateQueries({ queryKey: ["documents"] });
    } catch (err) {
      setError(err instanceof Error ? err.message : "Pipeline failed");
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
    <div className="max-w-6xl mx-auto space-y-6">
      {/* ── Page header ───────────────────────────────────────────── */}
      <div className="flex items-start justify-between">
        <div>
          <h1 className="text-2xl font-bold text-gray-900 dark:text-gray-100">
            Manage RAG Pipelines
          </h1>
          <p className="text-sm text-gray-500 dark:text-gray-400 mt-1">
            Load documents into the vector database for AI-powered retrieval
          </p>
        </div>
        <span className="text-sm font-medium text-blue-600 dark:text-blue-300">Admin Only</span>
      </div>

      {/* ── Upload Files card ─────────────────────────────────────── */}
      <div className="bg-white dark:bg-gray-900 rounded-2xl border border-gray-200 dark:border-gray-800 shadow-sm">
        <div className="px-6 py-4 border-b border-gray-100 dark:border-gray-800">
          <h2 className="text-base font-semibold text-gray-900 dark:text-gray-100">Upload Files</h2>
        </div>

        <div className="p-6 space-y-5">
          {/* Source tabs */}
          <div className="flex flex-wrap gap-2">
            {TABS.map(({ key, label, icon: Icon }) => (
              <button
                key={key}
                type="button"
                onClick={() => setSource(key)}
                className={cn(
                  "flex items-center gap-2 px-4 py-2 rounded-lg border text-sm font-medium transition-colors",
                  source === key
                    ? "border-blue-400 bg-blue-50 dark:bg-blue-950 text-blue-700 dark:text-blue-300"
                    : "border-gray-200 dark:border-gray-800 bg-white dark:bg-gray-900 text-gray-600 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-800"
                )}
              >
                <Icon className="h-4 w-4" />
                {label}
              </button>
            ))}
          </div>

          {source !== "local" && (
            <div className="rounded-lg bg-amber-50 dark:bg-amber-950 border border-amber-200 dark:border-amber-900 px-4 py-3 text-sm text-amber-700 dark:text-amber-300">
              {TABS.find((t) => t.key === source)?.label} integration coming
              soon. Switch to Local Upload to load files now.
            </div>
          )}

          {/* Pipeline name + description */}
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            <Field label="Pipeline Name" required>
              <input
                type="text"
                value={pipelineName}
                onChange={(e) => setPipelineName(e.target.value)}
                placeholder="e.g. Sales Docs Q2 2026"
                className="w-full rounded-lg border border-gray-300 dark:border-gray-700 px-3 py-2 text-sm text-gray-900 dark:text-gray-100 placeholder-gray-400 focus:border-blue-400 focus:ring-1 focus:ring-blue-400 outline-none"
              />
            </Field>
            <Field label="Description">
              <input
                type="text"
                value={description}
                onChange={(e) => setDescription(e.target.value)}
                placeholder="Optional — describe this batch"
                className="w-full rounded-lg border border-gray-300 dark:border-gray-700 px-3 py-2 text-sm text-gray-900 dark:text-gray-100 placeholder-gray-400 focus:border-blue-400 focus:ring-1 focus:ring-blue-400 outline-none"
              />
            </Field>
          </div>

          {/* Dropzone */}
          <FileDropzone onFiles={addFiles} disabled={source !== "local"} />

          {/* Staged files */}
          {staged.length > 0 && (
            <div className="space-y-2">
              {staged.map((f) => (
                <div
                  key={f.id}
                  className="flex items-center gap-3 rounded-lg border border-gray-200 dark:border-gray-800 px-3 py-2"
                >
                  <FileText className="h-4 w-4 text-gray-400 dark:text-gray-500 flex-shrink-0" />
                  <div className="min-w-0 flex-1">
                    <p className="text-sm font-medium text-gray-800 dark:text-gray-200 truncate">
                      {f.file.name}
                    </p>
                    <p className="text-xs text-gray-400 dark:text-gray-500">
                      {formatBytes(f.file.size)}
                    </p>
                  </div>
                  <button
                    type="button"
                    onClick={() => removeFile(f.id)}
                    className="text-gray-400 dark:text-gray-500 hover:text-gray-600"
                  >
                    <X className="h-4 w-4" />
                  </button>
                </div>
              ))}
            </div>
          )}

          {/* Result / error banners */}
          {result && (
            <div className="flex items-start gap-2 rounded-lg bg-green-50 dark:bg-green-950 border border-green-200 dark:border-green-900 px-4 py-3 text-sm text-green-700 dark:text-green-300">
              <CheckCircle2 className="h-4 w-4 mt-0.5 flex-shrink-0" />
              {result}
            </div>
          )}
          {error && (
            <div className="flex items-start gap-2 rounded-lg bg-red-50 dark:bg-red-950 border border-red-200 dark:border-red-900 px-4 py-3 text-sm text-red-600 dark:text-red-300">
              <XCircle className="h-4 w-4 mt-0.5 flex-shrink-0" />
              {error}
            </div>
          )}

          {/* Submit */}
          <button
            type="button"
            onClick={handleSubmit}
            disabled={!canSubmit}
            className={cn(
              "w-full flex items-center justify-center gap-2 rounded-lg py-3 text-sm font-semibold text-white transition-colors",
              canSubmit
                ? "bg-blue-700 hover:bg-blue-800"
                : "bg-blue-700/50 cursor-not-allowed"
            )}
          >
            {submitting ? (
              <Loader2 className="h-4 w-4 animate-spin" />
            ) : (
              <Play className="h-4 w-4" />
            )}
            Load into Vector Database
          </button>
        </div>
      </div>
    </div>
  );
}

// ── Sub-components ────────────────────────────────────────────────────────

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
      <label className="block text-sm font-semibold text-gray-700 dark:text-gray-300">
        {label}
        {required && <span className="text-red-500"> *</span>}
      </label>
      {children}
    </div>
  );
}


