"use client";

import { motion, AnimatePresence } from "framer-motion";
import { X, ExternalLink } from "lucide-react";
import { useUIStore } from "@/store/uiStore";

export default function CallModal() {
  const { callModal, closeCallModal } = useUIStore();

  if (!callModal) return null;

  return (
    <AnimatePresence>
      <motion.div
        initial={{ opacity: 0 }}
        animate={{ opacity: 1 }}
        exit={{ opacity: 0 }}
        className="fixed inset-0 z-[100] flex items-center justify-center bg-black/80 p-4 backdrop-blur-sm"
        onClick={closeCallModal}
      >
        <motion.div
          initial={{ scale: 0.9, opacity: 0 }}
          animate={{ scale: 1, opacity: 1 }}
          exit={{ scale: 0.9, opacity: 0 }}
          onClick={(e) => e.stopPropagation()}
          className="card w-full max-w-md p-6"
        >
          {/* Header */}
          <div className="mb-4 flex items-center justify-between">
            <div className="flex items-center gap-3">
              <img
                src={callModal.channel_avatar}
                alt={callModal.channel_title}
                className="h-10 w-10 rounded-full"
              />
              <h3 className="text-lg font-semibold text-[var(--text-primary)]">
                {callModal.channel_title}
              </h3>
            </div>
            <button
              onClick={closeCallModal}
              className="rounded-lg p-2 text-[var(--text-muted)] transition-colors hover:bg-[var(--bg-card-hover)] hover:text-[var(--text-primary)]"
            >
              <X className="h-5 w-5" />
            </button>
          </div>

          {/* Message */}
          <div className="mb-4 rounded-lg bg-[var(--bg-primary)]/50 p-4">
            <p className="mb-2 font-mono text-xs text-[var(--text-secondary)] break-all">
              {callModal.token_address}
            </p>
            <p className="text-sm text-[var(--text-primary)]">
              {callModal.message_text}
            </p>
            <p className="mt-2 text-right text-xs text-[var(--text-muted)]">
              {callModal.timestamp}
            </p>
          </div>

          {/* CTA */}
          {callModal.telegram_url && (
            <a
              href={callModal.telegram_url}
              target="_blank"
              rel="noopener noreferrer"
              className="flex w-full items-center justify-center gap-2 rounded-lg border border-[var(--border-subtle)] py-3 text-sm font-medium text-[var(--text-secondary)] transition-colors hover:border-[var(--accent-teal)] hover:text-[var(--accent-teal)]"
            >
              <ExternalLink className="h-4 w-4" />
              View in Telegram
            </a>
          )}
        </motion.div>
      </motion.div>
    </AnimatePresence>
  );
}
