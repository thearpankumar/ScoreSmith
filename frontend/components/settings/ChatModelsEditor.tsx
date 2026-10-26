"use client";

import { useEffect, useState } from "react";
import { AlertTriangle, Check, Loader2 } from "lucide-react";

import { Button } from "@/components/ui/button";
import { chatModelsDraftError, sameChatModels } from "@/lib/ai-provider";
import { setChatModels, type AiProviderSettings, type ChatModelsSetting } from "@/lib/settings-client";
import { cn } from "@/lib/utils";

/**
 * Administrator setting (OpenRouter only): which chat models users may pick in the chat, and the default. Only the
 * chat assistant uses them; the scorecard builder and evaluations keep their own models (listed under "Models used").
 */
export function ChatModelsEditor({
  value,
  onSaved,
}: {
  value: ChatModelsSetting;
  onSaved: (settings: AiProviderSettings) => void;
}) {
  const [enabled, setEnabled] = useState<string[]>(value.enabled);
  const [defaultModel, setDefaultModel] = useState<string | null>(value.default);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setEnabled(value.enabled);
    setDefaultModel(value.default);
  }, [value]);

  const draft = { options: value.options, enabled, default: defaultModel };
  const problem = chatModelsDraftError(draft);
  const changed = !sameChatModels(draft, value);

  function toggle(id: string) {
    setSaved(false);
    const next = enabled.includes(id) ? enabled.filter((m) => m !== id) : [...enabled, id];
    setEnabled(next);
    // A default that was just switched off moves to the first model still enabled.
    if (!defaultModel || !next.includes(defaultModel)) setDefaultModel(next[0] ?? null);
  }

  async function save() {
    if (problem || !defaultModel) return;
    setSaving(true);
    setError(null);
    try {
      onSaved(await setChatModels(enabled, defaultModel));
      setSaved(true);
    } catch (err) {
      setError(err instanceof Error && err.message ? err.message : "Could not save the chat models.");
    } finally {
      setSaving(false);
    }
  }

  return (
    <section aria-labelledby="chat-models-heading" className="flex flex-col gap-2">
      <div>
        <h3 id="chat-models-heading" className="text-sm font-semibold text-ink">
          Chat models
        </h3>
        <p className="text-xs text-ink-muted">
          Users pick one of the enabled models in the chat. Used for answering, web search and edits; building a
          scorecard and evaluations keep their own models.
        </p>
      </div>
      <ul className="flex flex-col divide-y divide-hairline rounded-lg border border-hairline">
        {value.options.map((option) => {
          const on = enabled.includes(option.id);
          return (
            <li key={option.id} className="flex flex-wrap items-center gap-x-3 gap-y-1 px-3 py-2 text-sm">
              <label className="flex min-w-0 flex-1 items-center gap-2">
                <input
                  type="checkbox"
                  checked={on}
                  disabled={saving}
                  onChange={() => toggle(option.id)}
                  aria-describedby={`chat-model-${option.id}-id`}
                />
                <span className="text-ink">{option.label}</span>
                <span id={`chat-model-${option.id}-id`} className="truncate font-mono text-xs text-ink-muted">
                  {option.id}
                </span>
              </label>
              <label className={cn("flex items-center gap-1.5 text-xs", on ? "text-ink" : "text-ink-muted")}>
                <input
                  type="radio"
                  name="chat-default-model"
                  checked={defaultModel === option.id}
                  disabled={!on || saving}
                  onChange={() => {
                    setSaved(false);
                    setDefaultModel(option.id);
                  }}
                />
                Default
              </label>
            </li>
          );
        })}
      </ul>
      <div className="flex flex-wrap items-center gap-2">
        <Button type="button" size="sm" onClick={() => void save()} disabled={!changed || !!problem || saving}>
          {saving ? <Loader2 className="size-3.5 animate-spin" aria-hidden /> : null}
          Save chat models
        </Button>
        {saved && !changed && (
          <span role="status" className="flex items-center gap-1 text-xs text-ink-muted">
            <Check className="size-3.5" aria-hidden /> Saved
          </span>
        )}
        {(problem || error) && (
          <span role="alert" className="flex items-center gap-1 text-xs text-[var(--rag-poor)]">
            <AlertTriangle className="size-3.5" aria-hidden /> {error ?? problem}
          </span>
        )}
      </div>
    </section>
  );
}
