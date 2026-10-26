// Pure logic for the chat composer: the model picker and the attachment limits it gets from
// `GET /api/v1/chat/config` (backend app/api/v1/chat.py::get_chat_config), and the small helpers the
// composer and message bubbles share. No runtime imports, so node:test loads it directly.

export type ChatProvider = "bedrock" | "openrouter";

export interface ChatModelOption {
  id: string;
  label: string;
  vendor: string;
}

export interface ChatAttachmentLimits {
  enabled: boolean;
  maxBytes: number;
  maxFiles: number;
  extensions: string[];
}

export interface ChatConfig {
  provider: ChatProvider;
  /** True only with OpenRouter and at least one model enabled by the admin. */
  modelPicker: boolean;
  models: ChatModelOption[];
  defaultModel: string | null;
  attachments: ChatAttachmentLimits;
}

/** What the composer shows before the config has loaded (or when it cannot be loaded): no picker, no files. */
export const EMPTY_CHAT_CONFIG: ChatConfig = {
  provider: "bedrock",
  modelPicker: false,
  models: [],
  defaultModel: null,
  attachments: { enabled: false, maxBytes: 0, maxFiles: 0, extensions: [] },
};

/** localStorage key of the user's last pick (a per-viewer convenience; the server keeps the session's pick). */
export const LAST_MODEL_STORAGE_KEY = "qs.chatModel";

// Labels for model ids that write replies but are not in the picker (the scorecard builder's model).
const KNOWN_LABELS: Record<string, string> = {
  "openai/gpt-5-mini": "GPT-5 mini",
  "openai/gpt-5.6-luna": "GPT-5.6 Luna",
  "anthropic/claude-sonnet-5.5": "Claude Sonnet 5.5",
  "anthropic/claude-opus-5.5": "Claude Opus 5.5",
};

interface BeChatConfig {
  provider?: string | null;
  model_picker?: boolean | null;
  models?: { id?: string; label?: string; vendor?: string }[] | null;
  default_model?: string | null;
  attachments?: { enabled?: boolean; max_bytes?: number; max_files?: number; extensions?: string[] } | null;
}

/** Wire -> app shape; tolerant of missing blocks (falls back to "no picker, no files"). */
export function mapChatConfig(raw: unknown): ChatConfig {
  const be = (raw && typeof raw === "object" ? raw : {}) as BeChatConfig;
  const models = (be.models ?? [])
    .filter((m): m is { id: string; label?: string; vendor?: string } => typeof m?.id === "string" && m.id !== "")
    .map((m) => ({ id: m.id, label: m.label || m.id, vendor: m.vendor || "" }));
  const a = be.attachments ?? {};
  return {
    provider: be.provider === "openrouter" ? "openrouter" : "bedrock",
    modelPicker: be.model_picker === true && models.length > 0,
    models,
    defaultModel: typeof be.default_model === "string" && be.default_model ? be.default_model : null,
    attachments: {
      enabled: a.enabled === true,
      maxBytes: typeof a.max_bytes === "number" ? a.max_bytes : 0,
      maxFiles: typeof a.max_files === "number" ? a.max_files : 0,
      extensions: Array.isArray(a.extensions) ? a.extensions.filter((e) => typeof e === "string") : [],
    },
  };
}

/** The model the picker starts on: the session's own pick, else the user's last pick, else the admin's default,
 * else the first offered model — whichever is still offered. null when there is no picker. */
export function initialChatModel(
  config: ChatConfig,
  sessionModel: string | null | undefined,
  lastPick: string | null | undefined,
): string | null {
  if (!config.modelPicker) return null;
  const offered = new Set(config.models.map((m) => m.id));
  for (const candidate of [sessionModel, lastPick, config.defaultModel]) {
    if (candidate && offered.has(candidate)) return candidate;
  }
  return config.models[0]?.id ?? null;
}

/** A short display name for the model that wrote a reply; null when it should not be shown (unknown / Bedrock). */
export function modelLabel(config: ChatConfig, id: string | null | undefined): string | null {
  if (!id) return null;
  const offered = config.models.find((m) => m.id === id);
  if (offered) return offered.label;
  return KNOWN_LABELS[id] ?? null;
}
