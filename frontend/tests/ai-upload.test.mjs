// Pure-logic tests for lib/ai-upload.ts: part math, bounded concurrency, per-part retry/backoff,
// ETag collection, abort, file validation and Google Drive link classification. No network.
import test from "node:test";
import assert from "node:assert/strict";

const up = await import("../lib/ai-upload.ts");

const MiB = 1024 * 1024;
const mkParts = (n) => Array.from({ length: n }, (_, i) => ({ partNumber: i + 1, url: `https://s3/p${i + 1}` }));
const blobOf = (size) => new Blob([new Uint8Array(size)]);
const noSleep = async () => {};

test("partSlices: byte ranges, short last part, single tiny file", () => {
  const s = up.partSlices(25 * MiB, 10 * MiB, mkParts(3));
  assert.deepEqual(s.map((p) => [p.partNumber, p.start, p.end]), [
    [1, 0, 10 * MiB],
    [2, 10 * MiB, 20 * MiB],
    [3, 20 * MiB, 25 * MiB],
  ]);
  assert.equal(up.partSlices(5, 10 * MiB, mkParts(1))[0].end, 5);
  // Extra presigned parts are ignored; unordered input is sorted.
  const t = up.partSlices(15 * MiB, 10 * MiB, mkParts(4).reverse());
  assert.deepEqual(t.map((p) => p.partNumber), [1, 2]);
  assert.throws(() => up.partSlices(25 * MiB, 10 * MiB, mkParts(2)), /needed/);
  assert.throws(() => up.partSlices(10, 0, mkParts(1)), /part size/);
});

test("uploadFileMultipart collects ETags in part order and respects concurrency 3", async () => {
  let inFlight = 0;
  let maxInFlight = 0;
  const putPart = async (url, body, { onProgress }) => {
    inFlight++;
    maxInFlight = Math.max(maxInFlight, inFlight);
    onProgress(body.size);
    await new Promise((r) => setTimeout(r, 5));
    inFlight--;
    return `"etag-${url.split("/").pop()}"`;
  };
  const seen = [];
  const parts = await up.uploadFileMultipart(blobOf(7 * MiB + 1), {
    partSize: MiB,
    parts: mkParts(8),
    putPart,
    sleep: noSleep,
    onProgress: (l, t) => seen.push([l, t]),
  });
  assert.deepEqual(parts.map((p) => p.partNumber), [1, 2, 3, 4, 5, 6, 7, 8]);
  assert.equal(parts[0].etag, '"etag-p1"');
  assert.equal(parts[7].etag, '"etag-p8"');
  assert.equal(maxInFlight, 3);
  const last = seen.at(-1);
  assert.deepEqual(last, [7 * MiB + 1, 7 * MiB + 1]);
  // Progress never exceeds the file size and never goes backwards without a retry.
  assert.ok(seen.every(([l, t]) => l <= t));
});

test("a failing part is retried with backoff, then succeeds", async () => {
  const attempts = new Map();
  const delays = [];
  const putPart = async (url) => {
    const n = (attempts.get(url) ?? 0) + 1;
    attempts.set(url, n);
    if (url.endsWith("p2") && n < 3) throw new up.PartUploadError("boom", { retryable: true, status: 503 });
    return `"e${url.slice(-1)}"`;
  };
  const parts = await up.uploadFileMultipart(blobOf(3 * MiB), {
    partSize: MiB,
    parts: mkParts(3),
    putPart,
    sleep: async (ms) => void delays.push(ms),
    rand: () => 0.5,
  });
  assert.equal(attempts.get("https://s3/p2"), 3);
  assert.deepEqual(parts.map((p) => p.etag), ['"e1"', '"e2"', '"e3"']);
  assert.deepEqual(delays, [500, 1000]); // rand 0.5 => x1.0 jitter
});

test("retries are bounded; non-retryable errors fail immediately", async () => {
  let calls = 0;
  await assert.rejects(
    up.uploadFileMultipart(blobOf(MiB), {
      partSize: MiB,
      parts: mkParts(1),
      retries: 2,
      sleep: noSleep,
      putPart: async () => {
        calls++;
        throw new up.PartUploadError("503", { retryable: true });
      },
    }),
    /503/,
  );
  assert.equal(calls, 3); // 1 try + 2 retries

  calls = 0;
  await assert.rejects(
    up.uploadFileMultipart(blobOf(MiB), {
      partSize: MiB,
      parts: mkParts(1),
      sleep: noSleep,
      putPart: async () => {
        calls++;
        throw new up.PartUploadError("403 expired", { retryable: false, status: 403 });
      },
    }),
    /403/,
  );
  assert.equal(calls, 1);
});

test("abort signal cancels the upload and is never retried", async () => {
  const ac = new AbortController();
  let calls = 0;
  const putPart = async (_u, _b, { signal }) => {
    calls++;
    ac.abort();
    if (signal.aborted) throw new DOMException("aborted", "AbortError");
    return "x";
  };
  await assert.rejects(
    up.uploadFileMultipart(blobOf(4 * MiB), { partSize: MiB, parts: mkParts(4), putPart, signal: ac.signal, sleep: noSleep }),
    (e) => e.name === "AbortError",
  );
  assert.equal(calls, 1);
});

test("backoffMs is capped exponential with +-20% jitter", () => {
  assert.equal(up.backoffMs(0, () => 0.5), 500);
  assert.equal(up.backoffMs(2, () => 0.5), 2000);
  assert.equal(up.backoffMs(10, () => 0.5), 8000);
  assert.equal(up.backoffMs(0, () => 0), 400);
  assert.equal(up.backoffMs(0, () => 1), 600);
});

test("validateUploadFile: extensions, size and count limits", () => {
  const ok = (name, size = 10, p = "submission", n = 0) => up.validateUploadFile({ name, size }, p, n);
  for (const e of ["pdf", "docx", "mp4", "mov", "mkv", "webm", "m4v", "MP4"]) assert.equal(ok(`a.${e}`), null, e);
  assert.match(ok("a.exe"), /Unsupported/);
  assert.match(ok("noext"), /Unsupported/);
  assert.match(ok("a.xlsx"), /Unsupported/); // sheets are not submissions
  assert.equal(ok("a.xlsx", 10, "batch_sheet"), null);
  assert.equal(ok("a.csv", 10, "batch_sheet"), null);
  assert.match(ok("a.pdf", 0), /empty/);
  assert.equal(ok("a.mp4", 2 * 1024 ** 3), null);
  assert.match(ok("a.mp4", 2 * 1024 ** 3 + 1), /2 GiB/);
  assert.equal(ok("a.pdf", 10, "submission", 9), null);
  assert.match(ok("a.pdf", 10, "submission", 10), /At most 10/);
});

test("classifyDriveLink: folder / file / doc / invalid", () => {
  const id = "1AbCdEfGhIjKlMnOpQrStUvWxYz";
  const k = (u) => up.classifyDriveLink(u).kind;
  assert.equal(k(`https://drive.google.com/drive/folders/${id}?usp=sharing`), "folder");
  assert.equal(k(`https://drive.google.com/drive/u/0/folders/${id}`), "folder");
  assert.equal(k(`https://drive.google.com/file/d/${id}/view`), "file");
  assert.equal(k(`https://drive.google.com/open?id=${id}`), "file");
  assert.equal(k(`https://drive.google.com/uc?export=download&id=${id}`), "file");
  assert.equal(k(`https://drive.usercontent.google.com/download?id=${id}`), "file");
  assert.equal(k(`https://docs.google.com/document/d/${id}/edit`), "doc");
  assert.equal(k(`https://docs.google.com/spreadsheets/d/${id}/edit#gid=0`), "doc");
  assert.equal(k(`https://docs.google.com/presentation/d/${id}/edit`), "doc");
  // invalid
  assert.equal(k("not a url"), "invalid");
  assert.equal(k(`http://drive.google.com/file/d/${id}/view`), "invalid"); // https only
  assert.equal(k(`https://evil.com/file/d/${id}/view`), "invalid");
  assert.equal(k(`https://drive.google.com.evil.com/file/d/${id}/view`), "invalid");
  assert.equal(k("https://drive.google.com/"), "invalid");
  assert.equal(k("https://drive.google.com/drive/folders/"), "invalid");
  assert.equal(k("https://docs.google.com/forms/d/abc/edit"), "invalid");
  assert.match(up.classifyDriveLink("https://evil.com/x").reason, /Google Drive/);
});

test("parseDriveLinks: one per line, flags invalid, ignores blanks, marks duplicates", () => {
  const id1 = "1AbCdEfGhIjKlMnOpQrStUvWxYz";
  const id2 = "2AbCdEfGhIjKlMnOpQrStUvWxYz";
  const text = [
    `https://drive.google.com/drive/folders/${id1}`,
    "",
    `  https://drive.google.com/file/d/${id2}/view  `,
    `https://drive.google.com/drive/folders/${id1}?usp=sharing`, // same folder, different query
    "https://example.com/nope",
  ].join("\n");
  const e = up.parseDriveLinks(text);
  assert.equal(e.length, 4);
  assert.deepEqual(e.map((x) => x.kind), ["folder", "file", "folder", "invalid"]);
  assert.deepEqual(e.map((x) => x.valid), [true, true, false, false]);
  assert.equal(e[2].duplicate, true);
  assert.equal(e.filter((x) => x.valid).length, 2); // N valid links => N evaluations
  assert.deepEqual(up.parseDriveLinks("  \n \n"), []);
});

test("formatBytes", () => {
  assert.equal(up.formatBytes(512), "512 B");
  assert.equal(up.formatBytes(1536), "1.5 KB");
  assert.equal(up.formatBytes(2 * 1024 ** 3), "2.0 GB");
});

test("validateUploadFile: Markdown and plain-text solution documents are accepted, archives are not", () => {
  const v = (name) => up.validateUploadFile({ name, size: 100 }, "submission");
  assert.equal(v("SOLUTION_DOCUMENT.md"), null);
  assert.equal(v("notes.txt"), null);
  assert.equal(v("README.markdown"), null);
  assert.match(v("code.zip"), /Unsupported file type/);
  assert.equal(up.contentTypeFor("SOLUTION.md"), "text/markdown");
  assert.equal(up.contentTypeFor("notes.txt"), "text/plain");
});
