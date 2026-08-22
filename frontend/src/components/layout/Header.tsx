"use client";

import { usePathname } from "next/navigation";
import { useQuery } from "@tanstack/react-query";
import { apiClient } from "@/lib/api-client";
import { cn } from "@/lib/utils";
import {
  Activity,
  Server,
  Database,
  Cpu,
  Network,
  ChevronRight,
} from "lucide-react";

export function Header() {
  const pathname = usePathname();

  const { data: health, isError } = useQuery({
    queryKey: ["health"],
    queryFn: apiClient.getHealth,
    refetchInterval: 30_000,
    retry: 3,
    retryDelay: (attempt) => Math.min(1000 * 2 ** attempt, 8000),
  });

  const getPageMeta = (path: string) => {
    if (path.startsWith("/upload")) return { title: "Upload & Parsing Pipeline", section: "Ingestion" };
    if (path.startsWith("/pipelines")) return { title: "Pipeline Ingestion Monitor", section: "Orchestration" };
    if (path.startsWith("/documents")) return { title: "Multi-Store Document Catalog", section: "Data Stores" };
    if (path.startsWith("/query")) return { title: "Multi-Retriever Agentic Chat", section: "Intelligence" };
    return { title: "Enterprise Workspace", section: "Dashboard" };
  };

  const meta = getPageMeta(pathname);

  return (
    <header className="h-16 bg-white/90 dark:bg-[#18181b]/90 backdrop-blur-xl border-b border-slate-200/90 dark:border-white/[0.08] flex items-center justify-between px-6 flex-shrink-0 z-10 transition-colors duration-200">
      {/* ── Breadcrumb / Title ────────────────────────────────────────── */}
      <div className="flex items-center gap-2">
        <span className="text-xs font-semibold text-indigo-600 dark:text-indigo-400 uppercase tracking-wider">
          {meta.section}
        </span>
        <ChevronRight className="w-3.5 h-3.5 text-slate-400 dark:text-zinc-600" />
        <h1 className="text-sm font-bold text-slate-900 dark:text-zinc-100 tracking-tight">
          {meta.title}
        </h1>
      </div>

      {/* ── System Health Bar ─────────────────────────────────────────── */}
      <div className="flex items-center gap-2.5">
        {health ? (
          <div className="flex items-center gap-2 bg-slate-50 dark:bg-white/[0.03] border border-slate-200 dark:border-white/[0.06] px-3 py-1.5 rounded-xl shadow-xs">
            <HealthPill label="API" status={health.api} icon={Server} />
            <span className="text-slate-300 dark:text-zinc-700 text-xs">•</span>
            <HealthPill label="Postgres" status={health.database} icon={Database} />
            <span className="text-slate-300 dark:text-zinc-700 text-xs">•</span>
            <HealthPill
              label="Groq"
              status={health.groq_endpoint ?? "unknown"}
              icon={Cpu}
            />
            <span className="text-slate-300 dark:text-zinc-700 text-xs">•</span>
            <HealthPill label="Neo4j" status={health.neo4j ?? "unknown"} icon={Network} />
          </div>
        ) : isError ? (
          <div className="flex items-center gap-2 px-3 py-1 rounded-xl bg-red-50 dark:bg-red-500/10 border border-red-200 dark:border-red-500/20 text-xs text-red-600 dark:text-red-400">
            <span className="w-2 h-2 rounded-full bg-red-500 animate-ping" />
            Backend Disconnected
          </div>
        ) : (
          <div className="flex items-center gap-2 text-xs text-slate-400 dark:text-zinc-500 animate-pulse">
            <Activity className="w-3.5 h-3.5" />
            Connecting to cluster...
          </div>
        )}
      </div>
    </header>
  );
}

function HealthPill({
  label,
  status = "unknown",
  icon: _Icon,
}: {
  label: string;
  status?: string;
  icon: any;
}) {
  const ok = status === "ok";
  const inactive = status === "not_configured" || status === "disabled";

  return (
    <div
      className="flex items-center gap-1.5 text-xs text-slate-600 dark:text-zinc-300 group cursor-default"
      title={`${label}: ${status}`}
    >
      <span
        className={cn(
          "w-2 h-2 rounded-full transition-all",
          ok
            ? "bg-emerald-500 dark:bg-emerald-400 shadow-[0_0_8px_rgba(52,211,153,0.5)]"
            : inactive
            ? "bg-slate-300 dark:bg-zinc-600"
            : "bg-red-500 dark:bg-red-400 shadow-[0_0_8px_rgba(248,113,113,0.5)]"
        )}
      />
      <span className="font-mono text-[11px] text-slate-600 dark:text-zinc-400 group-hover:text-slate-900 dark:group-hover:text-zinc-200 transition-colors">
        {label}
      </span>
    </div>
  );
}
