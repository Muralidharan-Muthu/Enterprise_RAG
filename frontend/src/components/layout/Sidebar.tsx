"use client";

import { useState } from "react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import {
  Upload,
  FileText,
  MessageSquare,
  Database,
  Workflow,
  PanelLeftClose,
  PanelLeftOpen,
  Loader2,
  LogOut,
  ExternalLink,
  type LucideIcon,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { ThemeToggle } from "./ThemeToggle";
import { useRunningPipelineCount } from "@/hooks/useDocuments";
import { useAuth } from "@/lib/auth";

interface NavItem {
  href: string;
  label: string;
  icon: LucideIcon;
}

const navItems: NavItem[] = [
  { href: "/upload", label: "Upload & Ingest", icon: Upload },
  { href: "/pipelines", label: "Pipelines", icon: Workflow },
  { href: "/documents", label: "Document Store", icon: FileText },
  { href: "/query", label: "AI Chat & Query", icon: MessageSquare },
];

export function Sidebar() {
  const pathname = usePathname();
  const [collapsed, setCollapsed] = useState(false);
  const runningPipelines = useRunningPipelineCount();
  const { user, logout } = useAuth();

  return (
    <aside
      className={cn(
        "flex-shrink-0 bg-white dark:bg-[#18181b] border-r border-slate-200/90 dark:border-white/[0.08] flex flex-col overflow-hidden transition-[width] duration-300 ease-in-out z-20 select-none",
        collapsed ? "w-16" : "w-64"
      )}
    >
      {/* ── Brand Header ────────────────────────────────────────────── */}
      <div
        className={cn(
          "h-16 flex-shrink-0 border-b border-slate-200/80 dark:border-white/[0.08] flex items-center justify-between",
          collapsed ? "px-3 justify-center" : "px-4"
        )}
      >
        <Link
          href="/"
          className={cn(
            "flex items-center gap-3 transition-opacity duration-200 overflow-hidden",
            collapsed && "w-0 opacity-0 pointer-events-none"
          )}
        >
          <div className="w-9 h-9 rounded-xl bg-gradient-to-tr from-indigo-600 via-indigo-500 to-violet-500 flex items-center justify-center shadow-md shadow-indigo-500/20 flex-shrink-0">
            <Database className="h-5 w-5 text-white" />
          </div>
          <div className="whitespace-nowrap">
            <div className="flex items-center gap-1.5">
              <span className="text-sm font-bold tracking-tight text-slate-900 dark:text-zinc-100">Enterprise RAG</span>
            </div>
            <span className="text-[10px] font-semibold text-indigo-600 dark:text-indigo-400">Document Intelligence</span>
          </div>
        </Link>

        {collapsed && (
          <div className="w-9 h-9 rounded-xl bg-gradient-to-tr from-indigo-600 to-violet-500 flex items-center justify-center shadow-md shadow-indigo-500/20 flex-shrink-0">
            <Database className="h-5 w-5 text-white" />
          </div>
        )}

        <button
          type="button"
          onClick={() => setCollapsed((c) => !c)}
          aria-label={collapsed ? "Expand sidebar" : "Collapse sidebar"}
          className={cn(
            "h-7 w-7 flex items-center justify-center text-slate-400 hover:text-slate-700 dark:text-zinc-400 dark:hover:text-white hover:bg-slate-100 dark:hover:bg-white/[0.06] rounded-lg transition-colors",
            collapsed && "hidden"
          )}
        >
          <PanelLeftClose className="h-4 w-4" />
        </button>
      </div>

      {collapsed && (
        <div className="pt-2 flex justify-center">
          <button
            type="button"
            onClick={() => setCollapsed(false)}
            aria-label="Expand sidebar"
            className="h-7 w-7 flex items-center justify-center text-slate-400 hover:text-slate-700 dark:text-zinc-400 dark:hover:text-white hover:bg-slate-100 dark:hover:bg-white/[0.06] rounded-lg transition-colors"
          >
            <PanelLeftOpen className="h-4 w-4" />
          </button>
        </div>
      )}

      {/* ── Main Navigation ─────────────────────────────────────────── */}
      <div className="px-3 pt-4 pb-2">
        {!collapsed && (
          <div className="px-3 pb-2 text-[10px] font-bold uppercase tracking-wider text-slate-400 dark:text-zinc-500">
            Navigation
          </div>
        )}
        <nav className="space-y-1">
          {navItems.map(({ href, label, icon: Icon }) => {
            const active = pathname === href || pathname.startsWith(href + "/");
            const running = href === "/pipelines" ? runningPipelines : 0;

            return (
              <Link
                key={href}
                href={href}
                title={collapsed ? label : undefined}
                className={cn(
                  "relative flex items-center rounded-xl text-sm font-medium transition-all duration-200 group",
                  collapsed ? "justify-center h-10 w-10 mx-auto px-0" : "px-3.5 py-2.5 gap-3",
                  active
                    ? "bg-indigo-50 dark:bg-indigo-500/15 text-indigo-700 dark:text-indigo-300 border border-indigo-200/80 dark:border-indigo-500/30 font-semibold shadow-sm"
                    : "text-slate-600 hover:text-slate-900 hover:bg-slate-100/80 dark:text-zinc-400 dark:hover:text-zinc-200 dark:hover:bg-white/[0.04]"
                )}
              >
                {/* Active Indicator Strip */}
                {active && !collapsed && (
                  <span className="absolute left-0 top-1/2 -translate-y-1/2 w-1 h-5 bg-indigo-600 dark:bg-indigo-400 rounded-r-full" />
                )}

                <Icon
                  className={cn(
                    "h-4 w-4 flex-shrink-0 transition-colors",
                    active
                      ? "text-indigo-600 dark:text-indigo-400"
                      : "text-slate-400 group-hover:text-slate-700 dark:text-zinc-400 dark:group-hover:text-zinc-200"
                  )}
                />

                {!collapsed && (
                  <span className="whitespace-nowrap flex-1 text-xs sm:text-sm">{label}</span>
                )}

                {/* Live count / spinner badge */}
                {running > 0 && !collapsed && (
                  <span className="flex items-center gap-1 px-2 py-0.5 rounded-full text-[10px] font-bold bg-indigo-100 text-indigo-700 dark:bg-indigo-500/20 dark:text-indigo-300 border border-indigo-200 dark:border-indigo-500/30 animate-pulse">
                    <Loader2 className="h-2.5 w-2.5 animate-spin" />
                    {running}
                  </span>
                )}

                {running > 0 && collapsed && (
                  <span className="absolute -top-0.5 -right-0.5 w-4 h-4 rounded-full bg-indigo-600 text-white text-[9px] font-bold flex items-center justify-center ring-2 ring-white dark:ring-[#18181b]">
                    {running}
                  </span>
                )}
              </Link>
            );
          })}
        </nav>
      </div>

      {/* ── Landing Page Quick Link ──────────────────────────────────── */}
      <div className="flex-1" />

      {/* ── Workspace & Theme Area ──────────────────────────────── */}
      <div className="p-3 border-t border-slate-200/80 dark:border-white/[0.08] bg-slate-50/50 dark:bg-white/[0.01]">
        {!collapsed ? (
          <div className="space-y-2">
            <div className="flex items-center gap-3 p-2 rounded-xl bg-white dark:bg-white/[0.03] border border-slate-200/80 dark:border-white/[0.06] shadow-sm dark:shadow-none">
              <div className="w-8 h-8 rounded-lg bg-gradient-to-tr from-indigo-500 to-violet-600 flex items-center justify-center text-white font-bold text-xs shadow">
                EA
              </div>
              <div className="flex-1 min-w-0">
                <p className="text-xs font-semibold text-slate-900 dark:text-zinc-100 truncate">
                  Enterprise Workspace
                </p>
                <p className="text-[10px] text-emerald-600 dark:text-emerald-400 font-medium truncate flex items-center gap-1">
                  <span className="w-1.5 h-1.5 rounded-full bg-emerald-500 animate-pulse" />
                  Active Cluster
                </p>
              </div>
            </div>

            <div className="flex items-center justify-between pt-1">
              <span className="text-[11px] text-slate-400 dark:text-zinc-500 font-medium">Appearance</span>
              <ThemeToggle compact={false} />
            </div>
          </div>
        ) : (
          <div className="flex flex-col items-center gap-2">
            <ThemeToggle compact={true} />
          </div>
        )}
      </div>
    </aside>
  );
}
