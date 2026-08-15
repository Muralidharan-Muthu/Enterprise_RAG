"use client";

import { useEffect, useState } from "react";
import { Moon, Sun } from "lucide-react";
import { cn } from "@/lib/utils";

type Theme = "light" | "dark";

function applyTheme(t: Theme) {
  document.documentElement.classList.toggle("dark", t === "dark");
}

export function ThemeToggle({ compact = false }: { compact?: boolean }) {
  const [theme, setTheme] = useState<Theme>("light");
  const [mounted, setMounted] = useState(false);

  useEffect(() => {
    const stored = localStorage.getItem("theme") as Theme | null;
    const initial: Theme =
      stored ??
      (window.matchMedia("(prefers-color-scheme: dark)").matches
        ? "dark"
        : "light");
    setTheme(initial);
    applyTheme(initial);
    setMounted(true);
  }, []);

  const set = (t: Theme) => {
    setTheme(t);
    applyTheme(t);
    localStorage.setItem("theme", t);
  };

  // Avoid hydration mismatch — render a stable placeholder until mounted
  const current = mounted ? theme : "light";

  // Compact single-icon toggle for the collapsed sidebar rail.
  if (compact) {
    const next: Theme = current === "dark" ? "light" : "dark";
    return (
      <button
        onClick={() => set(next)}
        aria-label={`Switch to ${next} mode`}
        title={`Switch to ${next} mode`}
        className="relative h-9 w-9 flex items-center justify-center rounded-lg bg-gray-100 dark:bg-gray-800 text-gray-600 dark:text-gray-300 hover:text-blue-600 dark:hover:text-blue-400 hover:bg-gray-200 dark:hover:bg-gray-700 transition-colors active:scale-90"
      >
        <Sun
          className={cn(
            "absolute h-4 w-4 transition-all duration-300 ease-in-out",
            current === "dark" ? "opacity-100 rotate-0 scale-100" : "opacity-0 -rotate-90 scale-50"
          )}
        />
        <Moon
          className={cn(
            "absolute h-4 w-4 transition-all duration-300 ease-in-out",
            current === "dark" ? "opacity-0 rotate-90 scale-50" : "opacity-100 rotate-0 scale-100"
          )}
        />
      </button>
    );
  }

  const Btn = ({ value, icon: Icon, label }: { value: Theme; icon: typeof Sun; label: string }) => (
    <button
      onClick={() => set(value)}
      aria-label={label}
      className={cn(
        "flex-1 flex items-center justify-center gap-1.5 py-1.5 rounded-md text-xs font-medium transition-colors",
        current === value
          ? "bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100 shadow-sm"
          : "text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-200"
      )}
    >
      <Icon className="h-3.5 w-3.5" />
      {label}
    </button>
  );

  return (
    <div className="flex items-center gap-1 p-1 rounded-lg bg-gray-100 dark:bg-gray-800">
      <Btn value="light" icon={Sun} label="Light" />
      <Btn value="dark" icon={Moon} label="Dark" />
    </div>
  );
}
