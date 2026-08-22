"use client";

import React, { useEffect } from "react";
import * as Dialog from "@radix-ui/react-dialog";
import { AlertTriangle, Trash2, Info, Loader2, X } from "lucide-react";
import { cn } from "@/lib/utils";

export interface ConfirmDialogProps {
  isOpen: boolean;
  onClose: () => void;
  onConfirm: () => void | Promise<void>;
  title: string;
  description: React.ReactNode;
  confirmText?: string;
  cancelText?: string;
  variant?: "danger" | "warning" | "info";
  isLoading?: boolean;
}

export function ConfirmDialog({
  isOpen,
  onClose,
  onConfirm,
  title,
  description,
  confirmText = "Confirm",
  cancelText = "Cancel",
  variant = "danger",
  isLoading = false,
}: ConfirmDialogProps) {
  // Close on Escape key
  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape" && isOpen && !isLoading) {
        onClose();
      }
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [isOpen, isLoading, onClose]);

  const iconConfig = {
    danger: {
      icon: Trash2,
      bg: "bg-red-50 dark:bg-red-500/10 border-red-200 dark:border-red-500/20 text-red-600 dark:text-red-400",
      confirmBtn: "bg-red-600 hover:bg-red-700 text-white shadow-red-600/20 dark:shadow-red-900/30",
    },
    warning: {
      icon: AlertTriangle,
      bg: "bg-amber-50 dark:bg-amber-500/10 border-amber-200 dark:border-amber-500/20 text-amber-600 dark:text-amber-400",
      confirmBtn: "bg-amber-600 hover:bg-amber-700 text-white shadow-amber-600/20",
    },
    info: {
      icon: Info,
      bg: "bg-indigo-50 dark:bg-indigo-500/10 border-indigo-200 dark:border-indigo-500/20 text-indigo-600 dark:text-indigo-400",
      confirmBtn: "bg-indigo-600 hover:bg-indigo-700 text-white shadow-indigo-600/20",
    },
  }[variant];

  const IconComponent = iconConfig.icon;

  return (
    <Dialog.Root open={isOpen} onOpenChange={(open) => !open && !isLoading && onClose()}>
      <Dialog.Portal>
        {/* Backdrop */}
        <Dialog.Overlay className="fixed inset-0 z-50 bg-slate-950/60 dark:bg-black/80 backdrop-blur-sm animate-in fade-in duration-200" />

        {/* Modal Container */}
        <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
          <Dialog.Content
            className="w-full max-w-md bg-white dark:bg-[#18181b] border border-slate-200 dark:border-white/[0.1] rounded-2xl shadow-2xl overflow-hidden animate-in zoom-in-95 fade-in duration-200 focus:outline-none"
            onPointerDownOutside={(e) => {
              if (isLoading) e.preventDefault();
            }}
          >
            {/* Header / Body */}
            <div className="p-6">
              <div className="flex items-start gap-4">
                <div className={cn("p-3 rounded-2xl border flex-shrink-0", iconConfig.bg)}>
                  <IconComponent className="w-5 h-5" />
                </div>
                <div className="flex-1 min-w-0">
                  <div className="flex items-center justify-between gap-2">
                    <Dialog.Title className="text-base font-semibold text-slate-900 dark:text-white">
                      {title}
                    </Dialog.Title>
                    <button
                      type="button"
                      onClick={onClose}
                      disabled={isLoading}
                      className="text-slate-400 hover:text-slate-600 dark:text-gray-400 dark:hover:text-white rounded-lg p-1 transition-colors disabled:opacity-50"
                    >
                      <X className="w-4 h-4" />
                    </button>
                  </div>
                  <Dialog.Description className="mt-2 text-sm text-slate-600 dark:text-slate-300 leading-relaxed">
                    {description}
                  </Dialog.Description>
                </div>
              </div>
            </div>

            {/* Footer Action Buttons */}
            <div className="px-6 py-4 bg-slate-50/80 dark:bg-white/[0.02] border-t border-slate-200/80 dark:border-white/[0.06] flex items-center justify-end gap-3">
              <button
                type="button"
                onClick={onClose}
                disabled={isLoading}
                className="px-4 py-2 text-xs font-medium text-slate-700 dark:text-slate-300 hover:bg-slate-200/70 dark:hover:bg-white/[0.08] rounded-xl transition-all disabled:opacity-50"
              >
                {cancelText}
              </button>
              <button
                type="button"
                onClick={onConfirm}
                disabled={isLoading}
                className={cn(
                  "px-4 py-2 text-xs font-medium rounded-xl shadow-lg flex items-center gap-1.5 transition-all disabled:opacity-50",
                  iconConfig.confirmBtn
                )}
              >
                {isLoading && <Loader2 className="w-3.5 h-3.5 animate-spin" />}
                {confirmText}
              </button>
            </div>
          </Dialog.Content>
        </div>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
