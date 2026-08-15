"use client";

import { useState } from "react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { Upload, FileText, MessageSquare, Database, Workflow, PanelLeftClose, PanelLeftOpen, Loader2, type LucideIcon } from "lucide-react";
import { cn } from "@/lib/utils";
import { ThemeToggle } from "./ThemeToggle";
import { useRunningPipelineCount } from "@/hooks/useDocuments";

interface NavItem {
  href: string;
  label: string;
  icon: LucideIcon;
  comingSoon?: boolean;
}

const navItems: NavItem[] = [
  { href: "/upload", label: "Upload", icon: Upload },
  { href: "/pipelines", label: "Pipelines", icon: Workflow },
  { href: "/documents", label: "Documents", icon: FileText },
  { href: "/query", label: "Query", icon: MessageSquare },
];

export function Sidebar() {
  const pathname = usePathname();
  const [collapsed, setCollapsed] = useState(false);
  const runningPipelines = useRunningPipelineCount();

  return (
    <aside
      className={cn(
        "flex-shrink-0 bg-white dark:bg-gray-900 border-r border-gray-200 dark:border-gray-800 flex flex-col overflow-hidden transition-[width] duration-300 ease-in-out",
        collapsed ? "w-16" : "w-60"
      )}
    >
      {/* Logo + hamburger toggle */}
      <div
        className={cn(
          "h-[57px] flex-shrink-0 border-b border-gray-200 dark:border-gray-800 flex items-center gap-3",
          collapsed ? "justify-center px-0" : "px-5"
        )}
      >
        {/* Animated curvy panel toggle */}


        {/* Logo text — fades out when collapsed */}
        <div
          className={cn(
            "flex items-center gap-2 transition-all duration-200 ease-in-out",
            collapsed
              ? "w-0 opacity-0 pointer-events-none"
              : "w-auto opacity-100 delay-100"
          )}
        >
          <Database className="h-6 w-6 flex-shrink-0 text-blue-600 dark:text-blue-400" />
          <div className="whitespace-nowrap">
            <p className="text-sm font-bold text-gray-900 dark:text-gray-100">Multi-Store RAG Chatbot</p>
            <p className="text-xs text-gray-500 dark:text-gray-400">Enterprise Intelligence</p>
          </div>
        </div>

                <button
          type="button"
          onClick={() => setCollapsed((c) => !c)}
          aria-label={collapsed ? "Open sidebar" : "Close sidebar"}
          title={collapsed ? "Open sidebar" : "Close sidebar"}
          className="relative h-6 w-6 flex-shrink-0 text-gray-600 dark:text-gray-300 hover:text-blue-600 dark:hover:text-blue-400 hover:bg-gray-100 dark:hover:bg-gray-800 rounded-lg p-0.5 transition-colors active:scale-90"
        >
          <PanelLeftOpen
            className={cn(
              "absolute inset-0 m-auto h-5 w-5 transition-all duration-300 ease-in-out",
              collapsed ? "opacity-100 scale-100 rotate-0" : "opacity-0 scale-50 -rotate-90"
            )}
          />
          <PanelLeftClose
            className={cn(
              "absolute inset-0 m-auto h-5 w-5 transition-all duration-300 ease-in-out",
              collapsed ? "opacity-0 scale-50 rotate-90" : "opacity-100 scale-100 rotate-0"
            )}
          />
        </button>
      </div>
      

      {/* Navigation */}
      <nav className="flex-1 px-3 py-4 space-y-1">
        {navItems.map(({ href, label, icon: Icon, comingSoon }) => {
          const active = pathname === href || pathname.startsWith(href + "/");
          // Live "running" badge — only on Pipelines, only when ≥1 run is in flight.
          const running = href === "/pipelines" ? runningPipelines : 0;
          return (
            <Link
              key={href}
              href={comingSoon ? "#" : href}
              title={collapsed ? (running > 0 ? `${label} — ${running} running` : label) : undefined}
              className={cn(
                "relative flex items-center px-3 py-2 rounded-md text-sm font-medium transition-colors",
                collapsed ? "justify-center px-0 gap-0" : "gap-3",
                active
                  ? "bg-blue-50 dark:bg-blue-950 text-blue-700 dark:text-blue-300"
                  : "text-gray-600 dark:text-gray-400 hover:bg-gray-50 dark:hover:bg-gray-800 hover:text-gray-900 dark:hover:text-gray-100",
                comingSoon && "opacity-50 cursor-not-allowed"
              )}
            >
              <Icon className="h-4 w-4 flex-shrink-0" />
              {/* Collapsed: spinner+count overlay on the icon corner */}
              {collapsed && running > 0 && (
                <span className="absolute -top-0.5 -right-0.5 flex items-center justify-center min-w-[16px] h-4 px-1 rounded-full bg-blue-600 text-white text-[10px] font-bold tabular-nums shadow ring-2 ring-white dark:ring-gray-900">
                  {running}
                </span>
              )}
              {!collapsed && <span className="whitespace-nowrap">{label}</span>}
              {!collapsed && comingSoon && (
                <span className="ml-auto text-xs bg-gray-100 dark:bg-gray-800 text-gray-500 dark:text-gray-400 px-1.5 py-0.5 rounded">
                  Soon
                </span>
              )}
              {/* Open: spinning loader + count pill on the right */}
              {!collapsed && running > 0 && (
                <span className="ml-auto flex items-center gap-1.5 rounded-full bg-blue-50 dark:bg-blue-950 text-blue-600 dark:text-blue-300 pl-1.5 pr-2 py-0.5 text-xs font-semibold tabular-nums">
                  <Loader2 className="h-3 w-3 animate-spin" />
                  {running}
                </span>
              )}
            </Link>
          );
        })}
      </nav>

      {/* Theme toggle — full pill when open, single icon when collapsed */}
      <div
        className={cn(
          "py-3 border-t border-gray-200 dark:border-gray-800",
          collapsed ? "px-0 flex justify-center" : "px-3"
        )}
      >
        <ThemeToggle compact={collapsed} />
      </div>

    </aside>
  );
}
