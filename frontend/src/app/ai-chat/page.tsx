"use client";

import { useEffect, useRef, useState } from "react";
import { Send, Sparkles, Trash2, Bot, User } from "lucide-react";
import { aiChat, type AiChatResponse } from "@/lib/api";

type Msg = { role: "user" | "assistant"; content: string };

const SUGGESTIONS = [
  "Most profitable channel in the last week?",
  "Which channels are the most consistent?",
  "Most consistent channel with the highest number of calls?",
  "Best win rate over 3 months with at least 20 decided calls?",
  "Which chain has the most calls across all channels?",
];

export default function AiChatPage() {
  const [messages, setMessages] = useState<Msg[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [banner, setBanner] = useState<string | null>(null);
  const bottomRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, busy]);

  const send = async (text: string) => {
    const question = text.trim();
    if (!question || busy) return;
    const next: Msg[] = [...messages, { role: "user", content: question }];
    setMessages(next);
    setInput("");
    setBanner(null);
    setBusy(true);
    try {
      const res: AiChatResponse = await aiChat(next);
      if (res.success && res.reply) {
        setMessages((m) => [...m, { role: "assistant", content: res.reply! }]);
      } else {
        // Expected failure paths (no key / expired sub / rate limit /
        // provider down): keep the user's question visible, explain plainly,
        // and let them retry. The rest of the app is untouched by this.
        setBanner(res.message || "The assistant couldn't answer. Try again.");
      }
    } finally {
      setBusy(false);
      inputRef.current?.focus();
    }
  };

  return (
    <div className="mx-auto flex h-[calc(100vh-8rem)] max-w-3xl flex-col">
      {/* Header */}
      <div className="mb-4 flex items-center gap-3">
        <div className="flex h-10 w-10 items-center justify-center rounded-full bg-[var(--accent-teal)]/15">
          <Sparkles className="h-5 w-5 text-[var(--accent-teal)]" />
        </div>
        <div>
          <h1 className="text-2xl font-bold text-[var(--text-primary)]">AI Chat</h1>
          <p className="text-xs text-[var(--text-muted)]">
            Ask about your tracked channels — answers come straight from the KOLfi database
          </p>
        </div>
        {messages.length > 0 && (
          <button
            onClick={() => {
              setMessages([]);
              setBanner(null);
            }}
            className="ml-auto flex items-center gap-1.5 rounded-lg border border-[var(--border-subtle)] px-3 py-1.5 text-xs text-[var(--text-secondary)] transition-colors hover:border-[var(--border-hover)] hover:text-[var(--text-primary)]"
            title="Clear conversation"
          >
            <Trash2 className="h-3.5 w-3.5" /> New chat
          </button>
        )}
      </div>

      {/* Error/config banner — never blocks the page, never a crash screen */}
      {banner && (
        <div className="mb-3 rounded-lg border border-red-500/30 bg-red-500/10 px-4 py-3 text-sm text-red-400">
          {banner}
          {messages.length > 0 && (
            <button
              onClick={() => void send(messages[messages.length - 1].content)}
              className="ml-3 underline decoration-dotted"
            >
              retry
            </button>
          )}
        </div>
      )}

      {/* Messages */}
      <div className="flex-1 space-y-4 overflow-y-auto rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-card)] p-4">
        {messages.length === 0 && !busy ? (
          <div className="flex h-full flex-col items-center justify-center gap-4 text-center">
            <p className="text-sm text-[var(--text-muted)]">
              Try one of these:
            </p>
            <div className="flex max-w-md flex-col gap-2">
              {SUGGESTIONS.map((s) => (
                <button
                  key={s}
                  onClick={() => void send(s)}
                  className="rounded-lg border border-[var(--border-subtle)] bg-transparent px-4 py-2 text-left text-sm text-[var(--text-secondary)] transition-colors hover:border-[var(--accent-teal)]/50 hover:text-[var(--text-primary)]"
                >
                  {s}
                </button>
              ))}
            </div>
          </div>
        ) : (
          <>
            {messages.map((m, i) => (
              <div key={i} className={`flex gap-3 ${m.role === "user" ? "justify-end" : ""}`}>
                {m.role === "assistant" && (
                  <div className="mt-0.5 flex h-7 w-7 flex-shrink-0 items-center justify-center rounded-full bg-[var(--accent-teal)]/15">
                    <Bot className="h-4 w-4 text-[var(--accent-teal)]" />
                  </div>
                )}
                <div
                  className={`max-w-[80%] whitespace-pre-wrap rounded-xl px-4 py-2.5 text-sm leading-relaxed ${
                    m.role === "user"
                      ? "bg-[var(--accent-teal)]/15 text-[var(--text-primary)]"
                      : "bg-[var(--bg-hover)] text-[var(--text-secondary)]"
                  }`}
                >
                  {m.content}
                </div>
                {m.role === "user" && (
                  <div className="mt-0.5 flex h-7 w-7 flex-shrink-0 items-center justify-center rounded-full bg-[var(--accent-gold)]/15">
                    <User className="h-4 w-4 text-[var(--accent-gold)]" />
                  </div>
                )}
              </div>
            ))}
            {busy && (
              <div className="flex gap-3">
                <div className="mt-0.5 flex h-7 w-7 flex-shrink-0 items-center justify-center rounded-full bg-[var(--accent-teal)]/15">
                  <Bot className="h-4 w-4 text-[var(--accent-teal)]" />
                </div>
                <div className="flex items-center gap-1.5 rounded-xl bg-[var(--bg-hover)] px-4 py-3">
                  <span className="h-1.5 w-1.5 animate-bounce rounded-full bg-[var(--text-muted)] [animation-delay:0ms]" />
                  <span className="h-1.5 w-1.5 animate-bounce rounded-full bg-[var(--text-muted)] [animation-delay:150ms]" />
                  <span className="h-1.5 w-1.5 animate-bounce rounded-full bg-[var(--text-muted)] [animation-delay:300ms]" />
                </div>
              </div>
            )}
          </>
        )}
        <div ref={bottomRef} />
      </div>

      {/* Composer — compact single line, matches adjacent buttons */}
      <form
        onSubmit={(e) => {
          e.preventDefault();
          void send(input);
        }}
        className="mt-3 flex items-center gap-2"
      >
        <input
          ref={inputRef}
          value={input}
          onChange={(e) => setInput(e.target.value)}
          placeholder="Ask about channels, win rates, consistency…"
          className="h-10 flex-1 rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-card)] px-4 text-sm text-[var(--text-primary)] placeholder-[var(--text-muted)] focus:border-[var(--accent-teal)] focus:outline-none"
        />
        <button
          type="submit"
          disabled={busy || !input.trim()}
          className="flex h-10 items-center gap-2 rounded-lg bg-[var(--accent-teal)] px-4 text-sm font-semibold text-black transition-opacity disabled:opacity-40"
        >
          <Send className="h-4 w-4" /> Send
        </button>
      </form>
    </div>
  );
}
