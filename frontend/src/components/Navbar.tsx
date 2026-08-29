"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { Crown, Eye, User } from "lucide-react";
import StrategyToggle from "./StrategyToggle";

const navItems = [
  { label: "Leaderboard", href: "/leaderboard" },
  { label: "Channels", href: "/" },
  { label: "Tokens", href: "/tokens" },
];

export default function Navbar() {
  const pathname = usePathname();

  return (
    <nav className="sticky top-0 z-50 border-b border-[var(--border-subtle)] bg-[var(--bg-primary)]/80 backdrop-blur-md">
      <div className="mx-auto flex h-16 max-w-7xl items-center justify-between px-6">
        {/* Logo */}
        <Link href="/" className="flex items-center gap-3">
          <Crown className="h-6 w-6 text-[var(--accent-gold)]" />
          <span className="text-xl font-bold tracking-tight">KOLfi</span>
        </Link>

        {/* Nav Links */}
        <div className="flex items-center gap-1">
          {navItems.map((item) => {
            const isActive = pathname === item.href;
            return (
              <Link
                key={item.href}
                href={item.href}
                className={`rounded-lg px-4 py-2 text-sm font-medium transition-colors ${
                  isActive
                    ? "bg-[var(--accent-teal-dim)] text-[var(--accent-teal)]"
                    : "text-[var(--text-secondary)] hover:bg-[var(--bg-card-hover)] hover:text-[var(--text-primary)]"
                }`}
              >
                {item.label}
              </Link>
            );
          })}
        </div>

        {/* Right side */}
        <div className="flex items-center gap-4">
          <button className="rounded-lg border border-[var(--border-subtle)] px-4 py-2 text-sm font-medium text-[var(--text-secondary)] transition-colors hover:border-[var(--border-hover)] hover:text-[var(--text-primary)]">
            + Submit KOL
          </button>
          <StrategyToggle />
          <div className="flex items-center gap-2">
            <Eye className="h-5 w-5 text-[var(--accent-cyan)]" />
            <div className="h-8 w-8 rounded-full bg-gradient-to-br from-purple-500 to-pink-500">
              <User className="h-full w-full p-1.5 text-white" />
            </div>
          </div>
        </div>
      </div>
    </nav>
  );
}
