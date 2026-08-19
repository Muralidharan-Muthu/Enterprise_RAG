"use client";

import { useEffect, useState } from "react";
import { Moon, Sun } from "lucide-react";
import { cn } from "@/lib/utils";

type Theme = "light" | "dark";

function applyTheme(t: Theme) {
  if (t === "dark") {
    document.documentElement.classList.add("dark");
  } else {
    document.documentElement.classList.remove("dark");
  }
}

export function ThemeToggle({ compact = false }: { compact?: boolean }) {
  const [theme, setTheme] = useState<Theme>("dark");
  const [mounted, setMounted] = useState(false);

  useEffect(() => {
    const stored = localStorage.getItem("theme") as Theme | null;
    const initial: Theme = stored ?? "dark";
    setTheme(initial);
    applyTheme(initial);
    setMounted(true);
  }, []);

  const set = (t: Theme) => {
    setTheme(t);
    applyTheme(t);
    localStorage.setItem("theme", t);
  };

  const current = mounted ? theme : "dark";

  // Compact single-icon toggle for the collapsed sidebar rail
  if (compact) {
    const next: Theme = current === "dark" ? "light" : "dark";
    return (
      <button
        onClick={() => set(next)}
        aria-label={`Switch to ${next} theme`}
        title={`Switch to ${next} theme`}
        className="relative h-8 w-8 flex items-center justify-center rounded-xl bg-slate-100 dark:bg-white/[0.06] border border-slate-200 dark:border-white/[0.08] text-slate-700 dark:text-slate-200 hover:text-indigo-600 dark:hover:text-indigo-400 hover:bg-slate-200 dark:hover:bg-white/[0.1] transition-all active:scale-95 shadow-xs"
      >
        <Sun
          className={cn(
            "absolute h-4 w-4 transition-all duration-300 ease-in-out",
            current === "dark" ? "opacity-0 -rotate-90 scale-50" : "opacity-100 rotate-0 scale-100 text-amber-500"
          )}
        />
        <Moon
          className={cn(
            "absolute h-4 w-4 transition-all duration-300 ease-in-out",
            current === "dark" ? "opacity-100 rotate-0 scale-100 text-indigo-400" : "opacity-0 rotate-90 scale-50"
          )}
        />
      </button>
    );
  }

  return (
    <div className="flex items-center gap-1 p-1 rounded-xl bg-slate-100 dark:bg-white/[0.04] border border-slate-200 dark:border-white/[0.06]">
      <button
        type="button"
        onClick={() => set("light")}
        aria-label="Light mode"
        className={cn(
          "flex-1 flex items-center justify-center gap-1.5 py-1 px-2.5 rounded-lg text-xs font-semibold transition-all",
          current === "light"
            ? "bg-white text-slate-900 shadow-sm border border-slate-200/80"
            : "text-slate-500 hover:text-slate-800"
        )}
      >
        <Sun className="h-3.5 w-3.5 text-amber-500" />
        <span>Light</span>
      </button>

      <button
        type="button"
        onClick={() => set("dark")}
        aria-label="Dark mode"
        className={cn(
          "flex-1 flex items-center justify-center gap-1.5 py-1 px-2.5 rounded-lg text-xs font-semibold transition-all",
          current === "dark"
            ? "bg-indigo-600 text-white shadow-sm shadow-indigo-500/20"
            : "text-slate-400 hover:text-slate-200"
        )}
      >
        <Moon className="h-3.5 w-3.5" />
        <span>Dark</span>
      </button>
    </div>
  );
}
