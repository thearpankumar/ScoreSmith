"use client";

import { useState } from "react";
import { HelpCircle } from "lucide-react";

import { GlassCard } from "@/components/design-system/GlassCard";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import type { ClarifyingQuestion } from "@/lib/types";

/**
 * Chip-based clarifying-question UI: 2-5 short suggested answers plus an
 * "Other…" free-text escape hatch. Per the plan, the assistant must NEVER
 * ask an open-ended clarifying question as plain prose — every clarifying
 * turn renders through this component instead.
 */
export function ClarifyingQuestionCard({
  question,
  onAnswer,
  disabled,
}: {
  question: ClarifyingQuestion;
  onAnswer: (answer: string) => void;
  disabled?: boolean;
}) {
  const [showOther, setShowOther] = useState(false);
  const [otherValue, setOtherValue] = useState("");

  return (
    <GlassCard elevation={2} className="ml-9 max-w-[85%] p-4">
      <div className="mb-3 flex items-center gap-2 text-xs font-semibold uppercase tracking-wide text-ink-muted">
        <HelpCircle className="size-3.5 text-lemon-ink" aria-hidden />
        Quick question
      </div>
      <p className="mb-3 text-sm text-ink">{question.question}</p>
      <div className="flex flex-wrap gap-2">
        {question.options.map((opt) => (
          <button
            key={opt.id}
            type="button"
            disabled={disabled}
            onClick={() => onAnswer(opt.label)}
            className="rounded-full border border-hairline bg-solid px-3.5 py-1.5 text-sm font-medium text-ink transition-colors hover:bg-lemon-soft disabled:pointer-events-none disabled:opacity-50 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--focus)]"
          >
            {opt.label}
          </button>
        ))}
        {question.allowOther && !showOther && (
          <button
            type="button"
            disabled={disabled}
            onClick={() => setShowOther(true)}
            className="rounded-full border border-dashed border-hairline px-3.5 py-1.5 text-sm font-medium text-ink-muted transition-colors hover:text-ink disabled:pointer-events-none disabled:opacity-50 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--focus)]"
          >
            Other…
          </button>
        )}
      </div>
      {showOther && (
        <form
          className="mt-3 flex gap-2"
          onSubmit={(e) => {
            e.preventDefault();
            if (otherValue.trim()) onAnswer(otherValue.trim());
          }}
        >
          <Input
            autoFocus
            value={otherValue}
            onChange={(e) => setOtherValue(e.target.value)}
            placeholder="Type your own answer…"
            disabled={disabled}
          />
          <Button type="submit" size="sm" disabled={disabled || !otherValue.trim()}>
            Send
          </Button>
        </form>
      )}
    </GlassCard>
  );
}
