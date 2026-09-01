"use client";

import { useCallback, useState } from "react";
import { useDropzone, type FileRejection } from "react-dropzone";
import { Upload, AlertCircle } from "lucide-react";
import { cn } from "@/lib/utils";

interface Props {
  onFiles: (files: File[]) => void;
  disabled?: boolean;
  maxSizeMB?: number;
}

export function FileDropzone({ onFiles, disabled, maxSizeMB = 100 }: Props) {
  const [rejected, setRejected] = useState<string[]>([]);

  const onDrop = useCallback(
    (accepted: File[], rejections: FileRejection[]) => {
      if (accepted.length > 0) {
        setRejected([]);
        onFiles(accepted);
      }
      if (rejections.length > 0) {
        setRejected(
          rejections.map((r) => {
            const reason = r.errors[0];
            if (reason?.code === "file-too-large")
              return `${r.file.name} — exceeds ${maxSizeMB} MB`;
            if (reason?.code === "file-invalid-type")
              return `${r.file.name} — unsupported format (PDF only)`;
            return `${r.file.name} — ${reason?.message ?? "rejected"}`;
          })
        );
      }
    },
    [onFiles, maxSizeMB]
  );

  const { getRootProps, getInputProps, isDragActive } = useDropzone({
    onDrop,
    accept: {
      "application/pdf": [".pdf"],
    },
    maxSize: maxSizeMB * 1024 * 1024,
    disabled,
    multiple: true,
  });

  return (
    <div className="space-y-2">
      <div
        {...getRootProps()}
        className={cn(
          "border-2 border-dashed rounded-2xl px-6 py-10 text-center cursor-pointer transition-all",
          isDragActive
            ? "border-indigo-500 bg-indigo-50/80 dark:bg-indigo-950/40"
            : "border-slate-300 dark:border-slate-700/80 bg-slate-50/70 dark:bg-slate-900/40 hover:border-indigo-400 dark:hover:border-indigo-500 hover:bg-slate-100/80 dark:hover:bg-slate-800/50",
          disabled && "opacity-50 cursor-not-allowed pointer-events-none"
        )}
      >
        <input {...getInputProps()} />
        <div className="flex flex-col items-center gap-2.5">
          <div className="w-12 h-12 rounded-xl bg-indigo-50 dark:bg-indigo-500/10 border border-indigo-200/60 dark:border-indigo-500/20 flex items-center justify-center text-indigo-600 dark:text-indigo-400">
            <Upload className="h-6 w-6" />
          </div>
          <p className="text-sm font-semibold text-slate-800 dark:text-slate-200">
            {isDragActive
              ? "Drop PDF files here to upload"
              : "Drag & drop PDF files, or click to browse"}
          </p>
          <p className="text-xs text-slate-500 dark:text-slate-400">
            PDF only — up to {maxSizeMB} MB per file
          </p>
        </div>
      </div>

      {rejected.length > 0 && (
        <div className="flex items-start gap-2 rounded-xl bg-red-50 dark:bg-red-500/10 border border-red-200 dark:border-red-500/20 px-3.5 py-2.5 text-xs text-red-600 dark:text-red-300">
          <AlertCircle className="h-4 w-4 mt-0.5 flex-shrink-0" />
          <div className="space-y-0.5">
            <p className="font-medium">Some files were rejected:</p>
            {rejected.map((m, i) => (
              <p key={i}>{m}</p>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
