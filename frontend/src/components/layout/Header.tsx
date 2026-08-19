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
  ShieldCheck,
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
    <header className="h-16 bg-[#0c1222]/90 backdrop-blur-xl border-b border-white/[0.07] flex items-center justify-between px-6 flex-shrink-0 z-10">
      {/* ── Breadcrumb / Title ────────────────────────────────────────── */}
      <div className="flex items-center gap-2">
        <span className="text-xs font-semibold text-indigo-400/90 uppercase tracking-wider">
          {meta.section}
        </span>
        <ChevronRight className="w-3.5 h-3.5 text-gray-500" />
        <h1 className="text-sm font-bold text-white tracking-tight">
          {meta.title}
        </h1>
      </div>

      {/* ── System Health Bar ─────────────────────────────────────────── */}
      <div className="flex items-center gap-2.5">
        {health ? (
          <div className="flex items-center gap-2 bg-white/[0.03] border border-white/[0.06] px-3 py-1.5 rounded-xl">
            <HealthPill label="API" status={health.api} icon={Server} />
            <span className="text-gray-600 text-xs">•</span>
            <HealthPill label="Postgres" status={health.database} icon={Database} />
            <span className="text-gray-600 text-xs">•</span>
            <HealthPill
              label="Groq"
              status={health.groq_endpoint || health.gemma_endpoint}
              icon={Cpu}
            />
            <span className="text-gray-600 text-xs">•</span>
            <HealthPill label="Neo4j" status={health.neo4j} icon={Network} />
          </div>
        ) : isError ? (
          <div className="flex items-center gap-2 px-3 py-1 rounded-lg bg-red-500/10 border border-red-500/20 text-xs text-red-400">
            <span className="w-2 h-2 rounded-full bg-red-500 animate-ping" />
            Backend Disconnected
          </div>
        ) : (
          <div className="flex items-center gap-2 text-xs text-gray-500 animate-pulse">
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
  status,
  icon: Icon,
}: {
  label: string;
  status: string;
  icon: any;
}) {
  const ok = status === "ok";
  const inactive = status === "not_configured" || status === "disabled";

  return (
    <div
      className="flex items-center gap-1.5 text-xs text-gray-300 group cursor-default"
      title={`${label}: ${status}`}
    >
      <span
        className={cn(
          "w-2 h-2 rounded-full transition-all",
          ok
            ? "bg-emerald-400 shadow-[0_0_8px_rgba(52,211,153,0.6)]"
            : inactive
            ? "bg-gray-500"
            : "bg-red-400 shadow-[0_0_8px_rgba(248,113,113,0.6)]"
        )}
      />
      <span className="font-mono text-[11px] text-gray-400 group-hover:text-gray-200 transition-colors">
        {label}
      </span>
    </div>
  );
}
