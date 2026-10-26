// The admin "AI provider" setting: display logic (lib/ai-provider.ts, pure) and the API client
// (lib/settings-client.ts: request building, wire mapping and the 409 "locked" error).
import test from "node:test";
import assert from "node:assert/strict";
import { register } from "node:module";

import {
  chatModelsDraftError,
  sameChatModels,
  LOCK_REASONS,
  PROVIDER_LABELS,
  UNAVAILABLE_MESSAGE,
  describeAvailability,
  describeLastChange,
  describeStatus,
  modelRows,
  radioKeyTarget,
  selectedProvider,
  tabStopProvider,
  webSearchLabel,
  withProvider,
} from "../lib/ai-provider.ts";

register("./fixtures/alias-loader.mjs", import.meta.url);
globalThis.window = { location: { pathname: "/settings", search: "", assign() {} } };
globalThis.document = {
  cookie: "qs_csrf=tok",
  visibilityState: "visible",
  addEventListener() {},
  removeEventListener() {},
};

const calls = [];
const resp = (status, body) => ({
  ok: status < 400,
  status,
  statusText: "",
  headers: { get: () => null },
  text: async () => (body === undefined ? "" : JSON.stringify(body)),
  json: async () => body,
});
let handler = () => resp(200, {});
globalThis.fetch = async (url, init = {}) => {
  calls.push({ url: String(url), init });
  return handler(String(url), init);
};

const client = await import("../lib/settings-client.ts");
const { ApiError } = await import("../lib/api-client.ts");

const MODELS = {
  bedrock: {
    chat: "zai.glm-5",
    judge: "zai.glm-4.7-flash",
    master: "us.meta.llama4-maverick-17b-instruct-v1:0",
    embedding: "amazon.titan-embed-text-v2:0",
    builder: "zai.glm-5",
    vision: ["moonshotai.kimi-k2.5", "qwen.qwen3-vl-235b-a22b"],
    transcription: ["amazon.transcribe", "mistral.voxtral-small-24b-2507", "mistral.voxtral-mini-3b-2507"],
    video: ["us.amazon.nova-2-lite-v1:0", "qwen.qwen3-vl-235b-a22b"],
  },
  openrouter: {
    chat: "openai/gpt-5-mini",
    judge: "openai/gpt-5-mini",
    master: "openai/gpt-5-mini",
    embedding: "openai/text-embedding-3-small",
    builder: "openai/gpt-5.6-luna",
    vision: ["qwen/qwen3.7-flash", "qwen/qwen3-vl-235b-a22b-instruct"],
    transcription: ["mistralai/voxtral-small-24b-2507", "mistralai/voxtral-mini-3b-2507"],
    video: ["qwen/qwen3.7-flash", "google/gemini-3.1-flash-lite"],
  },
};

const CHAT_MODELS = {
  options: [
    { id: "openai/gpt-5-mini", label: "GPT-5 mini", vendor: "OpenAI" },
    { id: "anthropic/claude-opus-5.5", label: "Claude Opus 5.5", vendor: "Anthropic" },
  ],
  enabled: ["openai/gpt-5-mini", "anthropic/claude-opus-5.5"],
  default: "openai/gpt-5-mini",
};

/** App-shape settings (camelCase), as `getAiProvider()` returns them. */
const settings = (over = {}) => ({
  provider: "bedrock",
  effectiveProvider: "bedrock",
  available: { bedrock: true, openrouter: true },
  models: MODELS,
  webSearch: { agentcore: true, effective: "agentcore" },
  chatModels: CHAT_MODELS,
  updatedAt: "2026-10-23T12:00:00Z",
  updatedBy: { id: "u1", name: "Jane" },
  ...over,
});

test("labels and lock reasons", () => {
  assert.deepEqual(PROVIDER_LABELS, { bedrock: "AWS Bedrock", openrouter: "OpenRouter" });
  assert.equal(LOCK_REASONS.bedrock, "No AWS Bedrock credentials are configured on the server.");
  assert.equal(LOCK_REASONS.openrouter, "OPENROUTER_API_KEY is not set on the server.");
});

test("both providers available: nothing is locked and the stored choice is active", () => {
  const s = settings({ provider: "openrouter", effectiveProvider: "openrouter" });
  assert.deepEqual(describeAvailability(s), {
    bedrock: { locked: false, reason: null },
    openrouter: { locked: false, reason: null },
  });
  const status = describeStatus(s);
  assert.equal(status.kind, "active");
  assert.equal(status.effective, "openrouter");
  assert.equal(status.label, "OpenRouter");
  assert.equal(status.isDefault, false);
  assert.equal(selectedProvider(s), "openrouter");
  assert.equal(tabStopProvider(s), "openrouter");
});

test("only Bedrock available: OpenRouter is locked with the missing-key reason", () => {
  const s = settings({
    provider: null,
    available: { bedrock: true, openrouter: false },
    updatedAt: null,
    updatedBy: null,
  });
  const a = describeAvailability(s);
  assert.deepEqual(a.bedrock, { locked: false, reason: null });
  assert.deepEqual(a.openrouter, { locked: true, reason: "OPENROUTER_API_KEY is not set on the server." });
  const status = describeStatus(s);
  assert.equal(status.kind, "active");
  assert.equal(status.label, "AWS Bedrock");
  assert.equal(status.isDefault, true, "never chosen: the server default is in use");
  assert.equal(selectedProvider(s), "bedrock");
});

test("only OpenRouter available: Bedrock is locked with the missing-credentials reason", () => {
  const s = settings({
    provider: null,
    effectiveProvider: "openrouter",
    available: { bedrock: false, openrouter: true },
  });
  const a = describeAvailability(s);
  assert.deepEqual(a.bedrock, { locked: true, reason: "No AWS Bedrock credentials are configured on the server." });
  assert.deepEqual(a.openrouter, { locked: false, reason: null });
  const status = describeStatus(s);
  assert.equal(status.kind, "active");
  assert.equal(status.effective, "openrouter");
  assert.equal(status.label, "OpenRouter");
  assert.equal(selectedProvider(s), "openrouter");
  assert.equal(tabStopProvider(s), "openrouter");
});

test("no provider configured: both locked and AI is unavailable", () => {
  const s = settings({ provider: null, effectiveProvider: null, available: { bedrock: false, openrouter: false } });
  const a = describeAvailability(s);
  assert.equal(a.bedrock.locked, true);
  assert.equal(a.openrouter.locked, true);
  assert.deepEqual(describeStatus(s), { kind: "none", message: "AI is unavailable: no provider keys are configured." });
  assert.equal(UNAVAILABLE_MESSAGE, "AI is unavailable: no provider keys are configured.");
  assert.equal(selectedProvider(s), null);
  assert.equal(tabStopProvider(s), "bedrock", "the group still has one tab stop");
  for (const key of ["ArrowRight", "ArrowLeft", "Home", "End"]) assert.equal(radioKeyTarget(key, "bedrock", s), null);
});

test("stored choice locked: fallback warning names the saved provider, the reason and the one in use", () => {
  const s = settings({
    provider: "openrouter",
    effectiveProvider: "bedrock",
    available: { bedrock: true, openrouter: false },
  });
  const status = describeStatus(s);
  assert.equal(status.kind, "fallback");
  assert.equal(status.stored, "openrouter");
  assert.equal(status.effective, "bedrock");
  assert.equal(status.label, "AWS Bedrock");
  assert.equal(
    status.message,
    "The saved choice, OpenRouter, is unavailable. OPENROUTER_API_KEY is not set on the server. AWS Bedrock is used instead.",
  );
  // The saved (locked) choice stays checked so the admin sees it; the other option can be chosen to clear the warning.
  assert.equal(selectedProvider(s), "openrouter");
  assert.equal(describeAvailability(s).openrouter.locked, true);
  assert.equal(radioKeyTarget("ArrowRight", "openrouter", s), "bedrock");

  const reverse = settings({
    provider: "bedrock",
    effectiveProvider: "openrouter",
    available: { bedrock: false, openrouter: true },
  });
  assert.match(
    describeStatus(reverse).message,
    /AWS Bedrock, is unavailable\. No AWS Bedrock credentials .* OpenRouter is used instead\./,
  );
});

test("optimistic choice: stored and effective both become the new provider, the rest is kept", () => {
  const before = settings();
  const after = withProvider(before, "openrouter");
  assert.equal(after.provider, "openrouter");
  assert.equal(after.effectiveProvider, "openrouter");
  assert.equal(after.models, before.models);
  assert.equal(before.provider, "bedrock", "the previous state is not mutated (it is the rollback value)");
});

test("model rows: fixed order, role labels, fallback chains, empty roles left out", () => {
  const rows = modelRows(MODELS.bedrock);
  assert.deepEqual(
    rows.map((r) => r.label),
    [
      "Scorecard builder (KPI research)",
      "Evaluation judge",
      "Evaluation scoring",
      "Embeddings",
      "Image analysis",
      "Audio transcription",
      "Video frames",
    ],
  );
  assert.deepEqual(rows[0], { role: "builder", label: "Scorecard builder (KPI research)", models: ["zai.glm-5"] });
  assert.deepEqual(rows[4].models, ["moonshotai.kimi-k2.5", "qwen.qwen3-vl-235b-a22b"]);
  assert.deepEqual(modelRows(MODELS.openrouter)[0].models, ["openai/gpt-5.6-luna"]);
  assert.deepEqual(modelRows(MODELS.openrouter)[5].models, [
    "mistralai/voxtral-small-24b-2507",
    "mistralai/voxtral-mini-3b-2507",
  ]);

  const sparse = modelRows({
    chat: "openai/gpt-5-mini",
    builder: "b",
    judge: " ",
    master: "",
    embedding: "e",
    vision: [],
    transcription: [" ", "t"],
    video: [],
  });
  assert.deepEqual(
    sparse.map((r) => [r.role, r.models]),
    [
      ["builder", ["b"]],
      ["embedding", ["e"]],
      ["transcription", ["t"]],
    ],
  );
});

test("chat models draft: needs an enabled default; comparison ignores order", () => {
  assert.equal(chatModelsDraftError(CHAT_MODELS), null);
  assert.match(chatModelsDraftError({ ...CHAT_MODELS, enabled: [] }), /at least one/);
  assert.match(chatModelsDraftError({ ...CHAT_MODELS, default: "x/y" }), /default/);
  assert.equal(sameChatModels(CHAT_MODELS, { ...CHAT_MODELS, enabled: [...CHAT_MODELS.enabled].reverse() }), true);
  assert.equal(sameChatModels(CHAT_MODELS, { ...CHAT_MODELS, default: "anthropic/claude-opus-5.5" }), false);
  assert.equal(sameChatModels(CHAT_MODELS, { ...CHAT_MODELS, enabled: ["openai/gpt-5-mini"] }), false);
});

test("radio keyboard navigation wraps, skips locked options and honours Home / End", () => {
  const both = settings();
  assert.equal(radioKeyTarget("ArrowRight", "bedrock", both), "openrouter");
  assert.equal(radioKeyTarget("ArrowDown", "openrouter", both), "bedrock", "wraps");
  assert.equal(radioKeyTarget("ArrowLeft", "bedrock", both), "openrouter", "wraps backwards");
  assert.equal(radioKeyTarget("ArrowUp", "openrouter", both), "bedrock");
  assert.equal(radioKeyTarget("Home", "openrouter", both), "bedrock");
  assert.equal(radioKeyTarget("End", "bedrock", both), "openrouter");
  assert.equal(radioKeyTarget("Enter", "bedrock", both), null);
  assert.equal(radioKeyTarget("a", "bedrock", both), null);

  const onlyBedrock = settings({ available: { bedrock: true, openrouter: false } });
  assert.equal(radioKeyTarget("ArrowRight", "bedrock", onlyBedrock), "bedrock", "the locked option is skipped");
  assert.equal(radioKeyTarget("End", "bedrock", onlyBedrock), "bedrock");
});

test("web search line", () => {
  assert.equal(webSearchLabel(settings()), "AWS AgentCore");
  assert.equal(
    webSearchLabel(settings({ webSearch: { agentcore: false, effective: "openrouter" } })),
    "OpenRouter web search",
  );
  assert.equal(webSearchLabel(settings({ webSearch: { agentcore: false, effective: "none" } })), "Not configured");
});

test("last change line", () => {
  const fmt = (iso) => `at ${iso.slice(0, 10)}`;
  assert.equal(describeLastChange(settings(), fmt), "Last changed by Jane, at 2026-10-23");
  assert.equal(describeLastChange(settings({ updatedBy: null }), fmt), "Last changed at 2026-10-23", "deleted user");
  assert.equal(describeLastChange(settings({ updatedAt: null, updatedBy: null }), fmt), null, "never changed");
  assert.equal(
    describeLastChange(settings(), () => ""),
    "Last changed by Jane",
    "unparseable time",
  );
});

// ---------------------------------------------------------------------------------------------------------------
// lib/settings-client.ts
// ---------------------------------------------------------------------------------------------------------------

const WIRE = {
  provider: "openrouter",
  effective_provider: "openrouter",
  available: { bedrock: true, openrouter: true },
  models: MODELS,
  web_search: { agentcore: true, effective: "agentcore" },
  chat_models: CHAT_MODELS,
  updated_at: "2026-10-23T12:00:00Z",
  updated_by: { id: "u1", name: "Jane" },
};

test("getAiProvider: GET the admin endpoint and map snake_case to the app shape", async () => {
  calls.length = 0;
  handler = () => resp(200, WIRE);
  const s = await client.getAiProvider();
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, "/api/v1/admin/settings/ai-provider");
  assert.equal(calls[0].init.method, "GET");
  assert.deepEqual(s, settings({ provider: "openrouter", effectiveProvider: "openrouter" }));
});

test("mapping tolerates missing blocks and unknown values", () => {
  const s = client.mapAiProviderSettings({ provider: "gemini", effective_provider: null });
  assert.equal(s.provider, null);
  assert.equal(s.effectiveProvider, null);
  assert.deepEqual(s.available, { bedrock: false, openrouter: false });
  assert.deepEqual(s.models.openrouter, {
    chat: "",
    judge: "",
    master: "",
    embedding: "",
    builder: "",
    vision: [],
    transcription: [],
    video: [],
  });
  assert.deepEqual(s.chatModels, { options: [], enabled: [], default: null });
  assert.deepEqual(s.webSearch, { agentcore: false, effective: "none" });
  assert.equal(s.updatedAt, null);
  assert.equal(s.updatedBy, null);
  assert.deepEqual(
    client.mapAiProviderSettings({ ...WIRE, models: { bedrock: { vision: "one" } } }).models.bedrock.vision,
    ["one"],
  );
});

test("setAiProvider: PUT with the CSRF header and a JSON body, answer mapped", async () => {
  calls.length = 0;
  handler = () => resp(200, { ...WIRE, provider: "bedrock", effective_provider: "bedrock" });
  const s = await client.setAiProvider("bedrock");
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, "/api/v1/admin/settings/ai-provider");
  assert.equal(calls[0].init.method, "PUT");
  assert.equal(calls[0].init.headers["X-CSRF-Token"], "tok");
  assert.equal(calls[0].init.headers["Content-Type"], "application/json");
  assert.deepEqual(JSON.parse(calls[0].init.body), { provider: "bedrock" });
  assert.equal(s.provider, "bedrock");
  assert.equal(s.effectiveProvider, "bedrock");
});

test("setAiProvider: 409 becomes ProviderLockedError carrying the server's detail", async () => {
  handler = () => resp(409, { detail: "OpenRouter is locked: OPENROUTER_API_KEY is not set." });
  await assert.rejects(client.setAiProvider("openrouter"), (err) => {
    assert.ok(err instanceof client.ProviderLockedError);
    assert.ok(err instanceof ApiError);
    assert.equal(err.status, 409);
    assert.equal(err.message, "OpenRouter is locked: OPENROUTER_API_KEY is not set.");
    return true;
  });
  handler = () => resp(409, { detail: "" });
  await assert.rejects(client.setAiProvider("openrouter"), {
    name: "ProviderLockedError",
    message: "That provider is not available on the server.",
  });
});

test("setAiProvider: other errors pass through unchanged", async () => {
  handler = () => resp(403, { detail: "Administrators only." });
  await assert.rejects(client.setAiProvider("bedrock"), (err) => {
    assert.ok(err instanceof ApiError);
    assert.ok(!(err instanceof client.ProviderLockedError));
    assert.equal(err.status, 403);
    assert.equal(err.message, "Administrators only.");
    return true;
  });
});

test("setChatModels: PUT the enabled list and default, answer mapped", async () => {
  calls.length = 0;
  handler = () => resp(200, WIRE);
  const s = await client.setChatModels(["openai/gpt-5-mini"], "openai/gpt-5-mini");
  assert.equal(calls[0].url, "/api/v1/admin/settings/chat-models");
  assert.equal(calls[0].init.method, "PUT");
  assert.deepEqual(JSON.parse(calls[0].init.body), { enabled: ["openai/gpt-5-mini"], default: "openai/gpt-5-mini" });
  assert.deepEqual(s.chatModels, CHAT_MODELS);
});
