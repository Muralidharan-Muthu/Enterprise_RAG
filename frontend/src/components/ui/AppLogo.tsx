import React from "react";

export function AppLogoIcon({ className = "w-6 h-6" }: { className?: string }) {
  return (
    <svg
      viewBox="0 0 32 32"
      fill="none"
      xmlns="http://www.w3.org/2000/svg"
      className={className}
    >
      <defs>
        <linearGradient id="logo-grad-1" x1="2" y1="4" x2="30" y2="28" gradientUnits="userSpaceOnUse">
          <stop stopColor="#6366F1" />
          <stop offset="0.5" stopColor="#8B5CF6" />
          <stop offset="1" stopColor="#06B6D4" />
        </linearGradient>
        <linearGradient id="logo-glow" x1="8" y1="6" x2="24" y2="26" gradientUnits="userSpaceOnUse">
          <stop stopColor="#FFFFFF" stopOpacity="0.9" />
          <stop offset="1" stopColor="#E0E7FF" stopOpacity="0.4" />
        </linearGradient>
      </defs>

      {/* Outer Hex Shield */}
      <path
        d="M16 2L28 9V23L16 30L4 23V9L16 2Z"
        stroke="url(#logo-grad-1)"
        strokeWidth="2"
        strokeLinejoin="round"
        fill="none"
      />

      {/* Layer 1: Top Vector & Graph Conduits */}
      <path
        d="M16 8L24 12.5L16 17L8 12.5L16 8Z"
        fill="url(#logo-glow)"
        fillOpacity="0.25"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinejoin="round"
      />

      {/* Layer 2: Mid Financial Table Grid */}
      <path
        d="M8 17.5L16 22L24 17.5"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinecap="round"
        strokeLinejoin="round"
      />

      {/* Layer 3: Base Clause Store */}
      <path
        d="M8 22.5L16 27L24 22.5"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinecap="round"
        strokeLinejoin="round"
      />

      {/* Central Neural Node */}
      <circle cx="16" cy="14.5" r="2" fill="#38BDF8" />
    </svg>
  );
}

export function AppLogo({ className = "h-8" }: { className?: string }) {
  return (
    <div className={`flex items-center gap-2.5 ${className}`}>
      <div className="w-8 h-8 rounded-xl bg-gradient-to-tr from-indigo-600 to-violet-500 flex items-center justify-center p-1.5 shadow-md shadow-indigo-500/25">
        <AppLogoIcon className="w-full h-full text-white" />
      </div>
      <div className="flex flex-col leading-tight">
        <span className="font-bold text-base tracking-tight text-slate-900 dark:text-white">
          Enterprise RAG
        </span>
        <span className="text-[10px] font-medium text-indigo-600 dark:text-indigo-400">
          Document Intelligence Platform
        </span>
      </div>
    </div>
  );
}
