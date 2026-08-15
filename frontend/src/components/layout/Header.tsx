"use client";

import { useQuery } from "@tanstack/react-query";
import { apiClient } from "@/lib/api-client";
import { cn } from "@/lib/utils";

export function Header() {
  const { data: health, isError } = useQuery({
    queryKey: ["health"],
    queryFn: apiClient.getHealth,
    refetchInterval: 30_000,
    retry: 3,
    retryDelay: (attempt) => Math.min(1000 * 2 ** attempt, 8000),
  });

  return (
    <header className="h-14 bg-white dark:bg-gray-900 border-b border-gray-200 dark:border-gray-800 flex items-center justify-between px-6 flex-shrink-0">
      <h1 className="text-sm font-semibold text-gray-700 dark:text-gray-200">
        Enterprise Agentic RAG
      </h1>

      <div className="flex items-center gap-3 text-xs text-gray-500 dark:text-gray-400">
        {health ? (
          <>
            <StatusDot label="API" status={health.api} />
            <StatusDot label="DB" status={health.database} />
            <StatusDot label="Redis" status={health.redis} />
            <StatusDot label="Gemma" status={health.gemma_endpoint} />
            <StatusDot label="Neo4j" status={health.neo4j} />
          </>
        ) : isError ? (
          <span className="text-red-500">System offline</span>
        ) : (
          <span className="text-gray-400 dark:text-gray-500">Checking system status…</span>
        )}
      </div>
    </header>
  );
}

function StatusDot({ label, status }: { label: string; status: string }) {
  const ok = status === "ok";
  const inactive = status === "not_configured" || status === "disabled";
  return (
    <div className="flex items-center gap-1">
      <span
        className={cn(
          "h-1.5 w-1.5 rounded-full",
          ok ? "bg-green-500" : inactive ? "bg-gray-300 dark:bg-gray-600" : "bg-red-500"
        )}
      />
      <span>{label}</span>
    </div>
  );
}
