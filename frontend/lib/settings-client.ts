import { ApiError, authApiFetch } from "./api-client";

/**
 * Client for the administrator settings endpoints (`/api/v1/admin/settings/*`). Same plumbing as the rest of the app
 * (`apiFetch` through `authApiFetch`: cookies, CSRF header on unsafe methods, single-flight refresh on 401),
 * snake_case on the wire, camelCase here. The backend enforces `require_admin` on every call.
 */

export type ProviderName = "bedrock" | "openrouter";
export type WebSearchBackend = "agentcore" | "openrouter" | "none";

/** Model ids one provider uses. `vision` and `transcription` are fallback chains, first choice first. */
export interface ProviderModels {
  chat: string;
  judge: string;
  master: string;
  embedding: string;
  vision: string[];
  transcription: string[];
}

export interface AiProviderSettings {
  /** The administrator's stored choice; null when it has never been set. */
  provider: ProviderName | null;
  /** What new chat turns and evaluations use; null when no provider is configured on the server at all. */
  effectiveProvider: ProviderName | null;
  /** false = locked: the server has no key / credentials for that provider. */
  available: Record<ProviderName, boolean>;
  models: Record<ProviderName, ProviderModels>;
  webSearch: { agentcore: boolean; effective: WebSearchBackend };
  updatedAt: string | null;
  updatedBy: { id: string; name: string } | null;
}

interface BeProviderModels {
  chat?: string | null;
  judge?: string | null;
  master?: string | null;
  embedding?: string | null;
  vision?: string[] | string | null;
  transcription?: string[] | string | null;
}

interface BeAiProviderSettings {
  provider: ProviderName | null;
  effective_provider: ProviderName | null;
  available?: Partial<Record<ProviderName, boolean>> | null;
  models?: Partial<Record<ProviderName, BeProviderModels | null>> | null;
  web_search?: { agentcore?: boolean; effective?: WebSearchBackend | null } | null;
  updated_at?: string | null;
  updated_by?: { id: string; name: string } | null;
}

const PROVIDER_NAMES: readonly ProviderName[] = ["bedrock", "openrouter"];

const asProvider = (v: unknown): ProviderName | null =>
  typeof v === "string" && (PROVIDER_NAMES as readonly string[]).includes(v) ? (v as ProviderName) : null;

const asList = (v: unknown): string[] =>
  Array.isArray(v)
    ? v.filter((x): x is string => typeof x === "string" && x.trim() !== "")
    : typeof v === "string" && v.trim()
      ? [v]
      : [];

function mapModels(m: BeProviderModels | null | undefined): ProviderModels {
  return {
    chat: m?.chat ?? "",
    judge: m?.judge ?? "",
    master: m?.master ?? "",
    embedding: m?.embedding ?? "",
    vision: asList(m?.vision),
    transcription: asList(m?.transcription),
  };
}

/** Wire -> app shape. Tolerant of missing optional blocks so a partial answer still renders. */
export function mapAiProviderSettings(be: BeAiProviderSettings): AiProviderSettings {
  const effective = be.web_search?.effective;
  return {
    provider: asProvider(be.provider),
    effectiveProvider: asProvider(be.effective_provider),
    available: { bedrock: be.available?.bedrock === true, openrouter: be.available?.openrouter === true },
    models: { bedrock: mapModels(be.models?.bedrock), openrouter: mapModels(be.models?.openrouter) },
    webSearch: {
      agentcore: be.web_search?.agentcore === true,
      effective: effective === "agentcore" || effective === "openrouter" ? effective : "none",
    },
    updatedAt: be.updated_at ?? null,
    updatedBy: be.updated_by ?? null,
  };
}

/** The server refused the choice because that provider is locked (HTTP 409). `message` is the server's detail. */
export class ProviderLockedError extends ApiError {
  constructor(message: string) {
    super(message, 409);
    this.name = "ProviderLockedError";
  }
}

const PATH = "/api/v1/admin/settings/ai-provider";

export async function getAiProvider(signal?: AbortSignal): Promise<AiProviderSettings> {
  return mapAiProviderSettings(await authApiFetch<BeAiProviderSettings>(PATH, { signal }));
}

/** Saves the product-wide provider. Throws `ProviderLockedError` (with the server's reason) when it is locked. */
export async function setAiProvider(provider: ProviderName): Promise<AiProviderSettings> {
  try {
    return mapAiProviderSettings(await authApiFetch<BeAiProviderSettings>(PATH, { method: "PUT", body: { provider } }));
  } catch (err) {
    if (err instanceof ApiError && err.status === 409) {
      throw new ProviderLockedError(err.message || "That provider is not available on the server.");
    }
    throw err;
  }
}
