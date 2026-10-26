// The chat composer's model picker and attachments: config mapping and the starting model
// (lib/chat-models.ts) and file validation / chip state / outline summaries (lib/chat-attachments.ts).
import test from "node:test";
import assert from "node:assert/strict";

import { EMPTY_CHAT_CONFIG, initialChatModel, mapChatConfig, modelLabel } from "../lib/chat-models.ts";
import {
  attachmentsPending,
  outlineSummary,
  readyAttachmentIds,
  validateChatFile,
} from "../lib/chat-attachments.ts";

const WIRE = {
  provider: "openrouter",
  model_picker: true,
  models: [
    { id: "openai/gpt-5-mini", label: "GPT-5 mini", vendor: "OpenAI" },
    { id: "anthropic/claude-opus-5.5", label: "Claude Opus 5.5", vendor: "Anthropic" },
  ],
  default_model: "openai/gpt-5-mini",
  attachments: { enabled: true, max_bytes: 20 * 1024 * 1024, max_files: 5, extensions: ["pdf", "xlsx", "png"] },
};

test("config mapping: snake_case to the app shape, tolerant of junk", () => {
  const c = mapChatConfig(WIRE);
  assert.equal(c.provider, "openrouter");
  assert.equal(c.modelPicker, true);
  assert.deepEqual(c.models.map((m) => m.id), ["openai/gpt-5-mini", "anthropic/claude-opus-5.5"]);
  assert.equal(c.attachments.maxFiles, 5);
  assert.deepEqual(mapChatConfig(null), EMPTY_CHAT_CONFIG);
  // A picker with no models is no picker.
  assert.equal(mapChatConfig({ ...WIRE, models: [] }).modelPicker, false);
  assert.equal(mapChatConfig({ provider: "gemini" }).provider, "bedrock");
});

test("starting model: the session's pick, else the last pick, else the default, if still offered", () => {
  const c = mapChatConfig(WIRE);
  assert.equal(initialChatModel(c, "anthropic/claude-opus-5.5", "openai/gpt-5-mini"), "anthropic/claude-opus-5.5");
  assert.equal(initialChatModel(c, null, "anthropic/claude-opus-5.5"), "anthropic/claude-opus-5.5");
  assert.equal(initialChatModel(c, "anthropic/claude-sonnet-5.5", "x/disabled"), "openai/gpt-5-mini");
  assert.equal(initialChatModel({ ...c, defaultModel: null }, null, null), "openai/gpt-5-mini");
  assert.equal(initialChatModel(EMPTY_CHAT_CONFIG, "openai/gpt-5-mini", null), null);
});

test("model labels: offered models, the builder's model, nothing for unknown ids", () => {
  const c = mapChatConfig(WIRE);
  assert.equal(modelLabel(c, "anthropic/claude-opus-5.5"), "Claude Opus 5.5");
  assert.equal(modelLabel(c, "openai/gpt-5.6-luna"), "GPT-5.6 Luna");
  assert.equal(modelLabel(c, "zai.glm-5"), null);
  assert.equal(modelLabel(c, null), null);
});

test("file validation against the server's limits", () => {
  const limits = mapChatConfig(WIRE).attachments;
  assert.equal(validateChatFile({ name: "sop.PDF", size: 10 }, limits, 0), null);
  assert.match(validateChatFile({ name: "x.exe", size: 10 }, limits, 0), /unsupported type/);
  assert.match(validateChatFile({ name: "a.pdf", size: 0 }, limits, 0), /empty/);
  assert.match(validateChatFile({ name: "a.pdf", size: 21 * 1024 * 1024 }, limits, 0), /larger than 20 MB/);
  assert.match(validateChatFile({ name: "a.pdf", size: 10 }, limits, 5), /At most 5/);
  assert.match(validateChatFile({ name: "a.pdf", size: 10 }, EMPTY_CHAT_CONFIG.attachments, 0), /not available/);
});

test("chip state: only ready files are sent; anything in flight blocks sending", () => {
  const item = (status, serverId = "s") => ({
    localId: status, name: "f", size: 1, status, progress: 0, error: null, serverId, outline: null,
  });
  const items = [item("ready", "a"), item("failed", "b"), item("ready", null)];
  assert.deepEqual(readyAttachmentIds(items), ["a"]);
  assert.equal(attachmentsPending(items), false);
  assert.equal(attachmentsPending([...items, item("reading")]), true);
  assert.equal(attachmentsPending([item("uploading")]), true);
});

test("outline summaries", () => {
  assert.equal(outlineSummary({ pages: 12, truncated: false }), "12 pages");
  assert.equal(outlineSummary({ pages: 1, scanned_pages: [1] }), "1 page, scanned");
  assert.equal(
    outlineSummary({ sheets: [{ rows: 300 }, { rows: 40 }], truncated: true }),
    "2 sheets, 340 rows, long: the assistant reads it in parts",
  );
  assert.equal(outlineSummary({ described: true }), null);
  assert.equal(outlineSummary(null), null);
});
