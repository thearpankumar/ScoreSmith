"use client";

import { useCallback, useEffect, useRef, useState, type KeyboardEvent, type RefObject } from "react";
import { AlertTriangle, Check, Cloud, Globe, Loader2, Lock, RefreshCw, Waypoints } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  PROVIDERS,
  PROVIDER_LABELS,
  describeAvailability,
  describeLastChange,
  describeStatus,
  modelRows,
  radioKeyTarget,
  selectedProvider,
  tabStopProvider,
  webSearchLabel,
  withProvider,
  type ProviderStatus,
} from "@/lib/ai-provider";
import {
  ProviderLockedError,
  getAiProvider,
  setAiProvider,
  type AiProviderSettings,
  type ProviderName,
} from "@/lib/settings-client";
import { timeAgo } from "@/lib/time";
import { ChatModelsEditor } from "./ChatModelsEditor";
import { cn, formatDateTime } from "@/lib/utils";

const PROVIDER_ICONS: Record<ProviderName, typeof Cloud> = { bedrock: Cloud, openrouter: Waypoints };
const NAV_KEYS = new Set(["ArrowRight", "ArrowDown", "ArrowLeft", "ArrowUp", "Home", "End"]);

/**
 * Administrator setting: which LLM provider (AWS Bedrock or OpenRouter) the whole product uses for new chat turns and
 * evaluations. A provider whose key / credentials are missing on the server is locked. Choosing saves immediately
 * (optimistic, rolled back on error); the backend re-checks the admin role and the lock on every call.
 */
export function AiProviderCard() {
  const [settings, setSettings] = useState<AiProviderSettings | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const optionRefs = useRef<Partial<Record<ProviderName, HTMLButtonElement | null>>>({});
  // Bumped by every load and save: a response from an older one (e.g. the silent reload after a 409, overtaken by
  // the next save) must not overwrite newer state.
  const generation = useRef(0);

  const load = useCallback(async ({ signal, silent = false }: { signal?: AbortSignal; silent?: boolean } = {}) => {
    const mine = ++generation.current;
    if (!silent) setLoadError(null);
    try {
      const next = await getAiProvider(signal);
      if (signal?.aborted || mine !== generation.current) return;
      setSettings(next);
    } catch (err) {
      if (signal?.aborted || silent || mine !== generation.current) return;
      setLoadError(err instanceof Error && err.message ? err.message : "Please try again.");
    }
  }, []);

  useEffect(() => {
    const ac = new AbortController();
    void load({ signal: ac.signal });
    return () => ac.abort();
  }, [load]);

  // The inline "Saved" confirmation is brief.
  useEffect(() => {
    if (!saved) return;
    const t = setTimeout(() => setSaved(false), 2500);
    return () => clearTimeout(t);
  }, [saved]);

  async function choose(provider: ProviderName) {
    if (!settings || saving || !settings.available[provider] || provider === selectedProvider(settings)) return;
    const previous = settings;
    generation.current += 1;
    setSettings(withProvider(previous, provider));
    setSaving(true);
    setSaved(false);
    setSaveError(null);
    try {
      setSettings(await setAiProvider(provider));
      setSaved(true);
    } catch (err) {
      setSettings(previous);
      setSaveError(
        err instanceof Error && err.message ? err.message : "Could not save the AI provider. Please try again.",
      );
      // 409: the server no longer has that provider's key; refresh the lock states behind the error.
      if (err instanceof ProviderLockedError) void load({ silent: true });
    } finally {
      setSaving(false);
    }
  }

  function onOptionKeyDown(e: KeyboardEvent<HTMLButtonElement>, from: ProviderName) {
    if (!NAV_KEYS.has(e.key)) return;
    e.preventDefault();
    if (!settings || saving) return;
    const target = radioKeyTarget(e.key, from, settings);
    if (!target) return;
    optionRefs.current[target]?.focus();
    void choose(target);
  }

  const status = settings ? describeStatus(settings) : null;

  return (
    <section aria-labelledby="ai-provider-heading" className="flex flex-col gap-5">
      <header className="flex flex-wrap items-start justify-between gap-x-4 gap-y-2">
        <div className="min-w-0 flex-1 basis-60">
          <h2 id="ai-provider-heading" className="text-lg font-semibold text-ink">
            AI provider
          </h2>
          <p id="ai-provider-description" className="mt-1 text-sm text-ink-muted">
            Applies product-wide to new chat turns and evaluations. Runs already in progress finish on the provider they
            started with.
          </p>
        </div>
        {status && <StatusBadge status={status} />}
      </header>

      {!settings ? (
        loadError ? (
          <div role="alert" className="flex flex-col items-start gap-3">
            <p className="flex items-start gap-1.5 text-sm text-[var(--rag-poor)]">
              <AlertTriangle className="mt-0.5 size-4 shrink-0" aria-hidden />
              <span className="min-w-0 break-words">Could not load the AI provider settings. {loadError}</span>
            </p>
            <Button type="button" variant="outline" onClick={() => void load()}>
              <RefreshCw aria-hidden /> Retry
            </Button>
          </div>
        ) : (
          <LoadingSkeleton />
        )
      ) : (
        <ProviderBody
          settings={settings}
          saving={saving}
          saved={saved}
          saveError={saveError}
          optionRefs={optionRefs}
          onChoose={(p) => void choose(p)}
          onKeyDown={onOptionKeyDown}
          onSettings={setSettings}
        />
      )}
    </section>
  );
}

function ProviderBody({
  settings,
  saving,
  saved,
  saveError,
  optionRefs,
  onChoose,
  onKeyDown,
  onSettings,
}: {
  settings: AiProviderSettings;
  saving: boolean;
  saved: boolean;
  saveError: string | null;
  optionRefs: RefObject<Partial<Record<ProviderName, HTMLButtonElement | null>>>;
  onChoose: (p: ProviderName) => void;
  onKeyDown: (e: KeyboardEvent<HTMLButtonElement>, p: ProviderName) => void;
  onSettings: (settings: AiProviderSettings) => void;
}) {
  const status = describeStatus(settings);
  const availability = describeAvailability(settings);
  const selected = selectedProvider(settings);
  const tabStop = tabStopProvider(settings);
  const locked = PROVIDERS.filter((p) => availability[p].locked);
  const effective = settings.effectiveProvider;
  const rows = effective ? modelRows(settings.models[effective]) : [];
  const lastChange =
    describeLastChange(settings, timeAgo) ??
    (status.kind === "active" && status.isDefault ? "Not chosen yet: the server default is in use." : null);

  return (
    <div className="flex flex-col gap-5">
      <div className="flex flex-col gap-2">
        <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
          <div
            role="radiogroup"
            aria-labelledby="ai-provider-heading"
            aria-describedby="ai-provider-description"
            aria-disabled={saving || undefined}
            aria-busy={saving || undefined}
            className="glass-elev-1 flex h-11 w-full items-center rounded-full p-1 sm:inline-flex sm:w-auto"
          >
            {PROVIDERS.map((p) => {
              const checked = selected === p;
              const isLocked = availability[p].locked;
              const Icon = isLocked ? Lock : saving && checked ? Loader2 : PROVIDER_ICONS[p];
              return (
                <button
                  key={p}
                  ref={(el) => {
                    optionRefs.current[p] = el;
                  }}
                  type="button"
                  role="radio"
                  aria-checked={checked}
                  aria-disabled={isLocked || saving || undefined}
                  aria-describedby={isLocked ? `ai-provider-lock-${p}` : undefined}
                  tabIndex={p === tabStop ? 0 : -1}
                  onClick={() => onChoose(p)}
                  onKeyDown={(e) => onKeyDown(e, p)}
                  className={cn(
                    "flex h-full min-w-0 flex-1 items-center justify-center gap-1.5 rounded-full px-3 text-sm font-medium transition-colors sm:flex-none sm:px-4",
                    "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--focus)]",
                    checked && !isLocked && "bg-lemon font-semibold text-lemon-ink shadow-sm",
                    // The saved choice is locked (fallback in use): still marked, but not as the live provider.
                    checked && isLocked && "bg-lemon-soft font-semibold text-lemon-ink",
                    !checked && "text-ink-muted",
                    !checked && !isLocked && !saving && "hover:text-ink",
                    isLocked && "cursor-not-allowed",
                    isLocked && !checked && "opacity-60",
                    saving && "cursor-wait",
                  )}
                >
                  <Icon className={cn("size-4 shrink-0", Icon === Loader2 && "motion-safe:animate-spin")} aria-hidden />
                  <span className="truncate">{PROVIDER_LABELS[p]}</span>
                  {isLocked && <span className="sr-only">(locked)</span>}
                </button>
              );
            })}
          </div>
          <p role="status" aria-live="polite" className="text-sm">
            {saving ? (
              <span className="text-ink-muted">Saving…</span>
            ) : saved ? (
              <span className="flex items-center gap-1 text-[var(--rag-excellent)]">
                <Check className="size-4" aria-hidden />
                Saved
              </span>
            ) : null}
          </p>
        </div>

        {locked.length > 0 && (
          <ul className="flex flex-col gap-1 text-xs text-ink-muted">
            {locked.map((p) => (
              <li key={p} id={`ai-provider-lock-${p}`} className="flex items-start gap-1.5">
                <Lock className="mt-px size-3.5 shrink-0" aria-hidden />
                <span className="min-w-0 break-words">
                  <span className="font-medium text-ink">{PROVIDER_LABELS[p]}:</span> {availability[p].reason}
                </span>
              </li>
            ))}
          </ul>
        )}

        {saveError && (
          <p role="alert" className="flex items-start gap-1.5 text-sm text-[var(--rag-poor)]">
            <AlertTriangle className="mt-0.5 size-4 shrink-0" aria-hidden />
            <span className="min-w-0 break-words">{saveError}</span>
          </p>
        )}
      </div>

      {status.kind !== "active" && (
        <p
          className={cn(
            "flex items-start gap-2 rounded-lg border px-3 py-2 text-sm text-ink",
            status.kind === "none"
              ? "border-rag-poor/30 bg-rag-poor/[0.06]"
              : "border-rag-needs-improvement/50 bg-rag-needs-improvement/10",
          )}
        >
          <AlertTriangle
            className={cn("mt-0.5 size-4 shrink-0", status.kind === "none" ? "text-rag-poor" : "text-rag-weak")}
            aria-hidden
          />
          <span className="min-w-0 break-words">{status.message}</span>
        </p>
      )}

      {effective === "openrouter" && settings.chatModels.options.length > 0 && (
        <ChatModelsEditor value={settings.chatModels} onSaved={onSettings} />
      )}

      {effective && rows.length > 0 && (
        <section aria-labelledby="ai-provider-models" className="flex flex-col gap-1">
          <h3 id="ai-provider-models" className="text-sm font-semibold text-ink">
            Models used <span className="font-normal text-ink-muted">· {PROVIDER_LABELS[effective]}</span>
          </h3>
          <dl className="text-sm">
            {rows.map((row) => (
              <div
                key={row.role}
                // Two columns from `sm`; stacked on phones so the model ids keep most of the width.
                className="grid gap-0.5 border-b border-hairline py-2 last:border-b-0 sm:grid-cols-[minmax(0,2fr)_minmax(0,3fr)] sm:gap-3"
              >
                <dt className="text-ink-muted">{row.label}</dt>
                <dd className="min-w-0">
                  {row.models.map((id, i) => (
                    <span
                      key={`${i}-${id}`}
                      title={id}
                      className={cn(
                        "block truncate font-mono text-xs leading-5",
                        i === 0 ? "text-ink" : "text-ink-muted",
                      )}
                    >
                      {i > 0 && (
                        <>
                          <span aria-hidden className="mr-1 select-none">
                            ↳
                          </span>
                          <span className="sr-only">fallback: </span>
                        </>
                      )}
                      {id}
                    </span>
                  ))}
                </dd>
              </div>
            ))}
          </dl>
        </section>
      )}

      <div className="flex flex-col gap-1 border-t border-hairline pt-3 text-xs text-ink-muted">
        <p className="flex items-center gap-1.5">
          <Globe className="size-3.5 shrink-0" aria-hidden />
          <span>
            Web search: <span className="text-ink">{webSearchLabel(settings)}</span>
          </span>
        </p>
        {lastChange && <p title={settings.updatedAt ? formatDateTime(settings.updatedAt) : undefined}>{lastChange}</p>}
      </div>
    </div>
  );
}

function StatusBadge({ status }: { status: ProviderStatus }) {
  if (status.kind === "none") {
    return (
      <Badge variant="muted" className="shrink-0">
        AI unavailable
      </Badge>
    );
  }
  return (
    <Badge variant="soft" className="shrink-0">
      <span aria-hidden className="size-1.5 rounded-full bg-rag-excellent" />
      In use: {status.label}
    </Badge>
  );
}

function LoadingSkeleton() {
  return (
    <div aria-busy="true" className="flex flex-col gap-4">
      <span role="status" className="sr-only">
        Loading AI provider settings…
      </span>
      <div className="h-11 w-full rounded-full bg-black/[0.06] motion-safe:animate-pulse sm:w-72" />
      <div className="flex flex-col gap-2.5">
        {[88, 72, 80, 64].map((w) => (
          <div key={w} className="h-4 rounded bg-black/[0.06] motion-safe:animate-pulse" style={{ width: `${w}%` }} />
        ))}
      </div>
    </div>
  );
}
