// Pure-logic tests for Content-Disposition parsing (lib/download.ts).
import test from "node:test";
import assert from "node:assert/strict";

import { filenameFromDisposition } from "../lib/download.ts";

const FB = "fallback.xlsx";

test("plain, quoted and unquoted filenames", () => {
  assert.equal(filenameFromDisposition('attachment; filename="evaluations_multi_20261007-1432.xlsx"', FB), "evaluations_multi_20261007-1432.xlsx");
  assert.equal(filenameFromDisposition("attachment; filename=report.xlsx", FB), "report.xlsx");
});

test("RFC 6266 filename* wins over filename and is percent-decoded", () => {
  const h = "attachment; filename=\"export.xlsx\"; filename*=UTF-8''Caf%C3%A9%20report.xlsx";
  assert.equal(filenameFromDisposition(h, FB), "Café report.xlsx");
});

test("missing / empty / malformed headers fall back", () => {
  assert.equal(filenameFromDisposition(null, FB), FB);
  assert.equal(filenameFromDisposition("", FB), FB);
  assert.equal(filenameFromDisposition("attachment", FB), FB);
  assert.equal(filenameFromDisposition("attachment; filename*=UTF-8''%E0%A4%A", FB), FB);
});

test("path separators in a server-provided name are neutralised", () => {
  assert.equal(filenameFromDisposition('attachment; filename="../../etc/passwd.xlsx"', FB), ".._.._etc_passwd.xlsx");
});
