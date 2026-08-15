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

/**
 * Compact drag-and-drop target matching the "Manage RAG Pipelines" design.
 * Backend ingestion accepts PDF, DOCX, PPTX, XLSX, HTML, and Markdown.
 * The dropzone mirrors this on the client side to catch obvious mismatches
 * before the request is sent — the backend remains the authoritative gate.
 */
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
              return `${r.file.name} — unsupported format (PDF, DOCX, PPTX, XLSX, HTML, MD)`;
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
      "application/vnd.openxmlformats-officedocument.wordprocessingml.document": [".docx"],
      "application/vnd.openxmlformats-officedocument.presentationml.presentation": [".pptx"],
      "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": [".xlsx"],
      "text/html": [".html", ".htm"],
      "text/markdown": [".md"],
      // Windows browsers often report .md files as text/plain instead of text/markdown
      "text/plain": [".md"],
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
          "border-2 border-dashed rounded-xl px-6 py-10 text-center cursor-pointer transition-colors",
          isDragActive
            ? "border-blue-400 bg-blue-50 dark:bg-blue-950"
            : "border-gray-300 dark:border-gray-700 bg-gray-50 dark:bg-gray-800/50 hover:border-blue-400 hover:bg-gray-100 dark:hover:bg-gray-700",
          disabled && "opacity-50 cursor-not-allowed pointer-events-none"
        )}
      >
        <input {...getInputProps()} />
        <div className="flex flex-col items-center gap-2">
          <Upload className="h-7 w-7 text-gray-400 dark:text-gray-500" />
          <p className="text-base font-semibold text-gray-700 dark:text-gray-300">
            {isDragActive
              ? "Drop files here"
              : "Drag & drop files, or click to browse"}
          </p>
          <p className="text-sm text-gray-400 dark:text-gray-500">
            PDF, DOCX, PPTX, XLSX, HTML, MD — up to {maxSizeMB} MB per file
          </p>
        </div>
      </div>

      {rejected.length > 0 && (
        <div className="flex items-start gap-2 rounded-lg bg-red-50 dark:bg-red-950 border border-red-200 dark:border-red-900 px-3 py-2 text-xs text-red-600 dark:text-red-300">
          <AlertCircle className="h-4 w-4 mt-0.5 flex-shrink-0" />
          <div className="space-y-0.5">
            <p className="font-medium">Some files were rejected (unsupported format or too large):</p>
            {rejected.map((m, i) => (
              <p key={i}>{m}</p>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
