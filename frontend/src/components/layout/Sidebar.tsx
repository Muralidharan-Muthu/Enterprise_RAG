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
  User as UserIcon,
  Sparkles,
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
  badge?: string;
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
        "flex-shrink-0 bg-[#0c1222] border-r border-white/[0.07] flex flex-col overflow-hidden transition-[width] duration-300 ease-in-out z-20 select-none",
        collapsed ? "w-16" : "w-64"
      )}
    >
      {/* ── Brand Header ────────────────────────────────────────────── */}
      <div
        className={cn(
          "h-16 flex-shrink-0 border-b border-white/[0.07] flex items-center justify-between",
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
          <div className="w-9 h-9 rounded-xl bg-gradient-to-tr from-indigo-600 via-indigo-500 to-violet-500 flex items-center justify-center shadow-lg shadow-indigo-500/25 flex-shrink-0">
            <Database className="h-5 w-5 text-white" />
          </div>
          <div className="whitespace-nowrap">
            <div className="flex items-center gap-1.5">
              <span className="text-sm font-bold tracking-tight text-white">Multi-Store RAG</span>
            </div>
            <span className="text-[10px] font-medium text-indigo-400">Enterprise Workspace</span>
          </div>
        </Link>

        {collapsed && (
          <div className="w-9 h-9 rounded-xl bg-gradient-to-tr from-indigo-600 to-violet-500 flex items-center justify-center shadow-lg shadow-indigo-500/25 flex-shrink-0">
            <Database className="h-5 w-5 text-white" />
          </div>
        )}

        <button
          type="button"
          onClick={() => setCollapsed((c) => !c)}
          aria-label={collapsed ? "Expand sidebar" : "Collapse sidebar"}
          className={cn(
            "h-7 w-7 flex items-center justify-center text-gray-400 hover:text-white hover:bg-white/[0.06] rounded-lg transition-colors",
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
            className="h-7 w-7 flex items-center justify-center text-gray-400 hover:text-white hover:bg-white/[0.06] rounded-lg transition-colors"
          >
            <PanelLeftOpen className="h-4 w-4" />
          </button>
        </div>
      )}

      {/* ── Main Navigation ─────────────────────────────────────────── */}
      <div className="px-3 pt-4 pb-2">
        {!collapsed && (
          <div className="px-3 pb-2 text-[10px] font-bold uppercase tracking-wider text-gray-400">
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
                    ? "bg-gradient-to-r from-indigo-600/20 to-violet-600/10 text-white border border-indigo-500/30 shadow-lg shadow-indigo-500/10 font-semibold"
                    : "text-gray-400 hover:text-gray-200 hover:bg-white/[0.04]"
                )}
              >
                {/* Active Indicator Strip */}
                {active && !collapsed && (
                  <span className="absolute left-0 top-1/2 -translate-y-1/2 w-1 h-5 bg-gradient-to-b from-indigo-400 to-violet-500 rounded-r-full" />
                )}

                <Icon
                  className={cn(
                    "h-4 w-4 flex-shrink-0 transition-colors",
                    active
                      ? "text-indigo-400"
                      : "text-gray-400 group-hover:text-gray-200"
                  )}
                />

                {!collapsed && (
                  <span className="whitespace-nowrap flex-1 text-xs sm:text-sm">{label}</span>
                )}

                {/* Live count / spinner badge */}
                {running > 0 && !collapsed && (
                  <span className="flex items-center gap-1 px-2 py-0.5 rounded-full text-[10px] font-bold bg-indigo-500/20 text-indigo-300 border border-indigo-500/30 animate-pulse">
                    <Loader2 className="h-2.5 w-2.5 animate-spin" />
                    {running}
                  </span>
                )}

                {running > 0 && collapsed && (
                  <span className="absolute -top-0.5 -right-0.5 w-4 h-4 rounded-full bg-indigo-600 text-white text-[9px] font-bold flex items-center justify-center ring-2 ring-[#0c1222]">
                    {running}
                  </span>
                )}
              </Link>
            );
          })}
        </nav>
      </div>

      {/* ── Landing Page Quick Link ──────────────────────────────────── */}
      <div className="px-3 pt-2">
        <Link
          href="/"
          className={cn(
            "flex items-center rounded-xl text-xs font-medium text-gray-400 hover:text-indigo-300 hover:bg-white/[0.03] transition-all",
            collapsed ? "justify-center h-10 w-10 mx-auto px-0" : "px-3.5 py-2 gap-2"
          )}
          title={collapsed ? "Landing Page" : undefined}
        >
          <ExternalLink className="h-3.5 w-3.5 flex-shrink-0 text-gray-400" />
          {!collapsed && <span>Public Landing Page</span>}
        </Link>
      </div>

      <div className="flex-1" />

      {/* ── User Profile & Logout Area ──────────────────────────────── */}
      <div className="p-3 border-t border-white/[0.07] bg-white/[0.01]">
        {!collapsed ? (
          <div className="space-y-2">
            <div className="flex items-center gap-3 p-2 rounded-xl bg-white/[0.03] border border-white/[0.05]">
              <div className="w-8 h-8 rounded-lg bg-gradient-to-tr from-indigo-500 to-violet-600 flex items-center justify-center text-white font-bold text-xs shadow">
                {user?.username ? user.username.charAt(0).toUpperCase() : "U"}
              </div>
              <div className="flex-1 min-w-0">
                <p className="text-xs font-semibold text-white truncate">
                  {user?.username || "Logged In User"}
                </p>
                <p className="text-[10px] text-gray-400 truncate">
                  {user?.email || "user@rag.ai"}
                </p>
              </div>
            </div>

            <div className="flex items-center justify-between pt-1">
              <ThemeToggle compact={true} />
              <button
                type="button"
                onClick={logout}
                className="flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium text-red-400 hover:text-red-300 hover:bg-red-500/10 rounded-lg transition-colors"
                title="Log out"
              >
                <LogOut className="h-3.5 w-3.5" />
                <span>Logout</span>
              </button>
            </div>
          </div>
        ) : (
          <div className="flex flex-col items-center gap-2">
            <button
              type="button"
              onClick={logout}
              className="w-10 h-10 rounded-xl flex items-center justify-center text-red-400 hover:bg-red-500/10 transition-colors"
              title="Log out"
            >
              <LogOut className="h-4 w-4" />
            </button>
          </div>
        )}
      </div>
    </aside>
  );
}
