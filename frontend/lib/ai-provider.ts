/**
 * Display logic for the admin "AI provider" setting (components/settings/AiProviderCard.tsx): which options are
 * locked and why, the status line, the "Models used" rows and the web-search / last-change lines.
 * Pure and dependency-free (type-only imports) so `node --test` can import it directly.
 */
import type {
  AiProviderSettings,
  ChatModelsSetting,
  ProviderModels,
  ProviderName,
  WebSearchBackend,
} from "./settings-client";

export const PROVIDERS: readonly ProviderName[] = ["bedrock", "openrouter"];

export const PROVIDER_LABELS: Record<ProviderName, string> = {
  bedrock: "AWS Bedrock",
  openrouter: "OpenRouter",
};

export const LOCK_REASONS: Record<ProviderName, string> = {
  bedrock: "No AWS Bedrock credentials are configured on the server.",
  openrouter: "OPENROUTER_API_KEY is not set on the server.",
};

export const UNAVAILABLE_MESSAGE = "AI is unavailable: no provider keys are configured.";

export interface ProviderAvailability {
  locked: boolean;
  reason: string | null;
}

/** Per provider: locked (no key / credentials on the server) and the reason to show under the option. */
export function describeAvailability(settings: AiProviderSettings): Record<ProviderName, ProviderAvailability> {
  const one = (p: ProviderName): ProviderAvailability =>
    settings.available[p] ? { locked: false, reason: null } : { locked: true, reason: LOCK_REASONS[p] };
  return { bedrock: one("bedrock"), openrouter: one("openrouter") };
}

export type ProviderStatus =
  /** `effective` is in use; `isDefault` when no administrator has chosen yet (the server picked it). */
  | { kind: "active"; effective: ProviderName; label: string; isDefault: boolean; message: string }
  /** The stored choice is locked, so the server falls back to `effective`. */
  | { kind: "fallback"; effective: ProviderName; stored: ProviderName; label: string; message: string }
  /** No provider has a key: AI features are off. */
  | { kind: "none"; message: string };

export function describeStatus(settings: AiProviderSettings): ProviderStatus {
  const effective = settings.effectiveProvider;
  if (!effective) return { kind: "none", message: UNAVAILABLE_MESSAGE };
  const label = PROVIDER_LABELS[effective];
  const stored = settings.provider;
  if (stored && stored !== effective) {
    const reason = settings.available[stored] ? "" : ` ${LOCK_REASONS[stored]}`;
    return {
      kind: "fallback",
      effective,
      stored,
      label,
      message: `The saved choice, ${PROVIDER_LABELS[stored]}, is unavailable.${reason} ${label} is used instead.`,
    };
  }
  return {
    kind: "active",
    effective,
    label,
    isDefault: stored === null,
    message: `New chat turns and evaluations use ${label}.`,
  };
}

/** The option shown as checked: the stored choice (even when locked), else whatever the server uses. */
export function selectedProvider(settings: AiProviderSettings): ProviderName | null {
  return settings.provider ?? settings.effectiveProvider;
}

/** Optimistic local state for a newly chosen (available) provider: stored and effective both become it. */
export function withProvider(settings: AiProviderSettings, provider: ProviderName): AiProviderSettings {
  return { ...settings, provider, effectiveProvider: provider };
}

/** The option a tab into the radio group lands on (roving tabindex): the checked one, else the first unlocked one. */
export function tabStopProvider(settings: AiProviderSettings): ProviderName {
  return selectedProvider(settings) ?? PROVIDERS.find((p) => settings.available[p]) ?? PROVIDERS[0];
}

/**
 * Radio-group keyboard navigation (WAI-ARIA radio pattern): Arrow keys move to the next / previous unlocked option
 * (wrapping), Home / End to the first / last unlocked one. Null for other keys or when every option is locked.
 */
export function radioKeyTarget(key: string, from: ProviderName, settings: AiProviderSettings): ProviderName | null {
  const enabled = PROVIDERS.filter((p) => settings.available[p]);
  if (!enabled.length) return null;
  if (key === "Home") return enabled[0];
  if (key === "End") return enabled[enabled.length - 1];
  const step = key === "ArrowRight" || key === "ArrowDown" ? 1 : key === "ArrowLeft" || key === "ArrowUp" ? -1 : 0;
  if (!step) return null;
  const n = PROVIDERS.length;
  let i = PROVIDERS.indexOf(from);
  do {
    i = (i + step + n) % n;
  } while (!settings.available[PROVIDERS[i]]);
  return PROVIDERS[i];
}

export type ModelRole = keyof ProviderModels;

export const ROLE_LABELS: Record<ModelRole, string> = {
  chat: "Chat (default)",
  builder: "Scorecard builder (KPI research)",
  judge: "Evaluation judge",
  master: "Evaluation scoring",
  embedding: "Embeddings",
  vision: "Image analysis",
  transcription: "Audio transcription",
  video: "Video frames",
};

// `chat` is not listed: the chat assistant's models are their own section (chat models), the builder has its row.
const ROLE_ORDER: readonly ModelRole[] = [
  "builder", "judge", "master", "embedding", "vision", "transcription", "video",
];

export interface ModelRow {
  role: ModelRole;
  label: string;
  /** First choice first; later ids are fallbacks. Never empty. */
  models: string[];
}

/** The "Models used" rows in a fixed order; roles without a model id are left out. */
export function modelRows(models: ProviderModels): ModelRow[] {
  const rows: ModelRow[] = [];
  for (const role of ROLE_ORDER) {
    const value = models[role];
    const ids = (Array.isArray(value) ? value : [value]).map((id) => id.trim()).filter(Boolean);
    if (ids.length) rows.push({ role, label: ROLE_LABELS[role], models: ids });
  }
  return rows;
}

const WEB_SEARCH_LABELS: Record<WebSearchBackend, string> = {
  agentcore: "AWS AgentCore",
  openrouter: "OpenRouter web search",
  none: "Not configured",
};

export function webSearchLabel(settings: AiProviderSettings): string {
  return WEB_SEARCH_LABELS[settings.webSearch.effective] ?? WEB_SEARCH_LABELS.none;
}

/** "Last changed by Jane, 5 min ago" (time formatted by the caller), or null when the setting was never changed. */
export function describeLastChange(settings: AiProviderSettings, formatTime: (iso: string) => string): string | null {
  if (!settings.updatedAt) return null;
  const when = formatTime(settings.updatedAt);
  const who = settings.updatedBy?.name?.trim();
  const tail = when ? `, ${when}` : "";
  return who ? `Last changed by ${who}${tail}` : `Last changed${when ? ` ${when}` : ""}`;
}

/** Why a chat-models draft cannot be saved (null when it can). */
export function chatModelsDraftError(draft: ChatModelsSetting): string | null {
  if (draft.enabled.length === 0) return "Enable at least one chat model.";
  if (!draft.default || !draft.enabled.includes(draft.default)) return "Pick a default among the enabled models.";
  return null;
}

/** Same enabled set (order-insensitive) and same default. */
export function sameChatModels(a: ChatModelsSetting, b: ChatModelsSetting): boolean {
  const setA = new Set(a.enabled);
  return a.default === b.default && setA.size === new Set(b.enabled).size && b.enabled.every((m) => setA.has(m));
}
