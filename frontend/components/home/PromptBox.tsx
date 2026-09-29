"use client";

import { useRouter } from "next/navigation";
import { useState, type FormEvent } from "react";
import { ArrowRight, Sparkles } from "lucide-react";

import { GlassCard } from "@/components/design-system/GlassCard";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";

export function PromptBox() {
  const router = useRouter();
  const [value, setValue] = useState("");
  const [submitting, setSubmitting] = useState(false);

  function handleSubmit(e: FormEvent) {
    e.preventDefault();
    if (!value.trim() || submitting) return;
    setSubmitting(true);
    router.push(`/chat/new?prompt=${encodeURIComponent(value.trim())}`);
  }

  return (
    <GlassCard elevation={2} as="form" className="p-6" onSubmit={handleSubmit}>
      <div className="mb-3 flex items-center gap-2 text-sm font-medium text-ink-muted">
        <Sparkles className="size-4 text-lemon-ink" aria-hidden />
        Describe what you want to rate
      </div>
      <Textarea
        value={value}
        onChange={(e) => setValue(e.target.value)}
        placeholder="e.g. “I need a scorecard to grade how well our support team replies to refund emails.”"
        className="min-h-24 bg-solid"
        aria-label="Describe the scorecard you want to build"
      />
      <div className="mt-3 flex items-center justify-between">
        <p className="text-xs text-ink-muted">
          You&apos;ll be asked a few quick clarifying questions — no blank forms.
        </p>
        <Button type="submit" disabled={!value.trim() || submitting}>
          {submitting ? "Starting…" : "Start building"}
          <ArrowRight className="size-4" aria-hidden />
        </Button>
      </div>
    </GlassCard>
  );
}
