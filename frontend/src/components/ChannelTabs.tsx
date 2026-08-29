"use client";

import { motion } from "framer-motion";
import type { ChannelTab } from "@/types";

interface ChannelTabsProps {
  value: ChannelTab;
  onChange: (t: ChannelTab) => void;
}

const tabs: ChannelTab[] = ["All", "Hot", "Consistent", "New"];

export default function ChannelTabs({ value, onChange }: ChannelTabsProps) {
  return (
    <div className="flex items-center gap-6 border-b border-[var(--border-subtle)]">
      {tabs.map((t) => (
        <button
          key={t}
          onClick={() => onChange(t)}
          className={`relative pb-3 text-sm font-medium transition-colors ${
            value === t
              ? "text-[var(--text-primary)]"
              : "text-[var(--text-muted)] hover:text-[var(--text-secondary)]"
          }`}
        >
          {t}
          {value === t && (
            <motion.div
              layoutId="activeTab"
              className="absolute bottom-0 left-0 right-0 h-0.5 bg-[var(--accent-teal)]"
            />
          )}
        </button>
      ))}
    </div>
  );
}
