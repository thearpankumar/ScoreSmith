// Pure-logic tests for the visual scoring-formula builder: the token model (lib/formula-tokens.ts), the
// KPI list (lib/formula-kpis.ts) and undo history (lib/formula-history.ts). No network, no deps.
import test from "node:test";
import assert from "node:assert/strict";

import {
  backspace,
  insertFunction,
  insertTokens,
  kpiToken,
  kpiUsage,
  matchParens,
  moveToken,
  moveWithJoiner,
  numberToken,
  opToken,
  readable,
  removeAt,
  removeWithJoiner,
  serialize,
  setNumber,
  templateTokens,
  tokenize,
  typeDigit,
} from "../lib/formula-tokens.ts";
import { formulaKpis, highlight, kpiMonogram, kpiTint, visibleKpis } from "../lib/formula-kpis.ts";
import { historyReducer, initHistory } from "../lib/formula-history.ts";

const tokens = (text) => {
  const r = tokenize(text);
  assert.equal(r.ok, true, r.error);
  return r.tokens;
};
const roundTrip = (text) => serialize(tokens(text));
const kinds = (ts) => ts.map((t) => t.kind);
const snap = (text, caret) => {
  const ts = tokens(text);
  return { tokens: ts, caret: caret ?? ts.length };
};

test("formulas the backend accepts round-trip through tokens unchanged", () => {
  for (const text of [
    'min(kpi["Complaint Rate"], kpi["Customer Effort Score"]) * 0.6 + kpi["First Contact Resolution Rate"] * 0.4',
    'avg(kpi["A"], kpi["B"], kpi["C"])',
    'kpi["Speed"] ** 2 - -0.5',
    'sqrt(abs(kpi["A"] - kpi["B"])) / 2',
    'avg([kpi["A"], kpi["B"]])',
    "1e3 + .5",
  ]) {
    assert.equal(roundTrip(text), text);
  }
});

test("whitespace and single quotes are normalised, names keep quotes and backslashes", () => {
  assert.equal(roundTrip("kpi[ 'A' ]*2"), 'kpi["A"] * 2');
  assert.equal(roundTrip("min( kpi['A'] ,kpi['B'] )"), 'min(kpi["A"], kpi["B"])');
  const name = 'Say "hi" \\ there';
  const text = serialize([kpiToken(name)]);
  assert.equal(text, 'kpi["Say \\"hi\\" \\\\ there"]');
  assert.equal(tokens(text)[0].name, name);
});

test("a KPI reference is one token; a function call's name and paren are one token", () => {
  assert.deepEqual(kinds(tokens('min(kpi["A"], 3)')), ["fn", "kpi", "comma", "number", "close"]);
  assert.equal(tokens('min(kpi["A"])')[0].name, "min");
});

test("text the visual editor can't show is reported with its position", () => {
  const r = tokenize('kpi["A"] + "text"');
  assert.equal(r.ok, false);
  assert.equal(r.index, 11);
  assert.equal(tokenize("kpi[0]").ok, false);
  assert.equal(tokenize('kpi["unterminated').ok, false);
});

test("the readable form uses names and × ÷ − ^", () => {
  assert.equal(
    readable(tokens('min(kpi["Complaint Rate"], kpi["Effort"]) * 0.6 - kpi["X"] / 2 + kpi["Y"] ** 2')),
    "min(Complaint Rate, Effort) × 0.6 − X ÷ 2 + Y ^ 2",
  );
});

test("parens: partners, depth and unmatched ones", () => {
  const ts = tokens('min(kpi["A"], (kpi["B"] + 1))');
  const info = matchParens(ts);
  assert.equal(info.partner.get(0), ts.length - 1);
  assert.equal(info.partner.get(3), ts.length - 2);
  assert.equal(info.unmatched.size, 0);
  assert.equal(info.depth[3], 1);
  const bad = matchParens(tokens('(kpi["A"] + 1))'));
  assert.deepEqual([...bad.unmatched], [5]);
  assert.deepEqual([...matchParens(tokens("min(1, 2")).unmatched], [0]);
});

test("smart insert joins operands with + at top level and with a comma inside a call", () => {
  let s = { tokens: [], caret: 0 };
  s = insertTokens(s, [kpiToken("A")], { join: true });
  s = insertTokens(s, [kpiToken("B")], { join: true });
  assert.equal(serialize(s.tokens), 'kpi["A"] + kpi["B"]');
  s = insertFunction(s, "min");
  assert.equal(serialize(s.tokens), 'kpi["A"] + kpi["B"] + min()');
  s = insertTokens(s, [kpiToken("C")], { join: true });
  s = insertTokens(s, [kpiToken("D")], { join: true });
  assert.equal(serialize(s.tokens), 'kpi["A"] + kpi["B"] + min(kpi["C"], kpi["D"])');
  // inserting in front of an operand puts the separator after the new token
  s = insertTokens({ ...s, caret: 0 }, [kpiToken("Z")], { join: true });
  assert.equal(serialize(s.tokens).slice(0, 21), 'kpi["Z"] + kpi["A"] +');
  assert.equal(s.caret, 1);
  // operators are inserted as-is
  const o = insertTokens(snap('kpi["A"]'), [opToken("*")]);
  assert.equal(serialize(o.tokens), 'kpi["A"] *');
});

test("removing one side of a matched pair removes the other, keeping what was inside", () => {
  const s = snap('min(kpi["A"], kpi["B"]) * 2');
  const r = removeAt(s, 0);
  assert.equal(serialize(r.tokens), 'kpi["A"], kpi["B"] * 2');
  assert.equal(r.caret, s.caret - 2);
  assert.equal(serialize(removeAt(snap('(kpi["A"])'), 2).tokens), 'kpi["A"]');
});

test("typing digits builds one number; backspace trims it digit by digit", () => {
  let s = snap('kpi["A"] *');
  for (const ch of "0.65") s = typeDigit(s, ch);
  assert.equal(serialize(s.tokens), 'kpi["A"] * 0.65');
  assert.equal(serialize(typeDigit(s, ".").tokens), 'kpi["A"] * 0.65'); // a second "." is ignored
  s = backspace(backspace(backspace(s)));
  assert.equal(serialize(s.tokens), 'kpi["A"] * 0');
  s = backspace(s);
  assert.equal(serialize(s.tokens), 'kpi["A"] *');
  assert.equal(serialize(typeDigit({ tokens: [], caret: 0 }, ".").tokens), "0.");
});

test("setNumber edits in place and removes the token for an empty or bad value", () => {
  const s = snap('kpi["A"] * 0.6');
  assert.equal(serialize(setNumber(s, 2, " 0.75 ").tokens), 'kpi["A"] * 0.75');
  assert.equal(serialize(setNumber(s, 2, "").tokens), 'kpi["A"] *');
  assert.equal(serialize(setNumber(s, 2, "abc").tokens), 'kpi["A"] *');
});

test("moveToken drops a token into a gap; next to itself is a no-op", () => {
  const s = snap('kpi["A"] + kpi["B"] * 2');
  const moved = moveToken(s, 0, 3); // A into the gap before "*"
  assert.equal(serialize(moved.tokens), '+kpi["B"] kpi["A"] * 2'); // a leading + is a sign
  assert.equal(moved.caret, 3);
  const back = moveToken(s, 4, 0); // 2 to the front
  assert.equal(serialize(back.tokens), '2 kpi["A"] + kpi["B"] *');
  assert.equal(moveToken(s, 1, 1), s);
  assert.equal(moveToken(s, 1, 2), s);
});

test("a KPI chip's remove button and drag-move take its joining operator with it", () => {
  const s = snap('kpi["A"] + kpi["B"] + kpi["C"]');
  assert.equal(serialize(removeWithJoiner(s, 2).tokens), 'kpi["A"] + kpi["C"]');
  assert.equal(serialize(removeWithJoiner(s, 0).tokens), 'kpi["B"] + kpi["C"]');
  assert.equal(serialize(removeWithJoiner(snap('min(kpi["A"], kpi["B"])'), 1).tokens), 'min(kpi["B"])');
  assert.equal(serialize(removeWithJoiner(snap('(kpi["A"])'), 1).tokens), "()");
  assert.equal(serialize(removeWithJoiner(snap('kpi["A"] * 0.6'), 2).tokens), 'kpi["A"] *'); // not a KPI: just the token
  assert.equal(removeWithJoiner(s, 2).caret, 3);
  const ab = snap('kpi["A"] + kpi["B"]');
  assert.equal(serialize(moveWithJoiner(ab, 2, 0).tokens), 'kpi["B"] + kpi["A"]');
  assert.equal(serialize(moveWithJoiner(ab, 0, 3).tokens), 'kpi["B"] + kpi["A"]');
  const moved = moveWithJoiner(snap('kpi["A"] + min(kpi["B"], kpi["C"])'), 0, 6);
  assert.equal(serialize(moved.tokens), 'min(kpi["B"], kpi["C"], kpi["A"])');
  assert.equal(moveWithJoiner(ab, 2, 2), ab);
  assert.equal(serialize(moveWithJoiner(snap('kpi["A"] * 2'), 2, 0).tokens), '2 kpi["A"] *'); // numbers move as they are
});

test("kpiUsage counts every reference", () => {
  const usage = kpiUsage(tokens('kpi["A"] + min(kpi["A"], kpi["B"])'));
  assert.equal(usage.get("A"), 2);
  assert.equal(usage.get("B"), 1);
  assert.equal(usage.get("C"), undefined);
});

test("templates build formulas over every KPI", () => {
  const kpis = [
    { name: "A", weight: 25 },
    { name: "B", weight: 75 },
    { name: "C", weight: 0 },
  ];
  assert.deepEqual(templateTokens("default", kpis), []);
  assert.equal(serialize(templateTokens("average", kpis)), 'avg(kpi["A"], kpi["B"], kpi["C"])');
  assert.equal(serialize(templateTokens("weakest", kpis)), 'min(kpi["A"], kpi["B"], kpi["C"])');
  assert.equal(serialize(templateTokens("weights", kpis)), 'kpi["A"] * 0.25 + kpi["B"] * 0.75');
  assert.equal(
    serialize(templateTokens("blend", kpis.slice(0, 2))),
    'avg(kpi["A"], kpi["B"]) * 0.7 + min(kpi["A"], kpi["B"]) * 0.3',
  );
  assert.equal(serialize(templateTokens("weights", [{ name: "A", weight: null }])), 'avg(kpi["A"])');
  assert.equal(serialize(templateTokens("weights", [{ name: "A", weight: 100 / 3 }])), 'kpi["A"] * 0.3333');
});

test("formulaKpis lists leaf KPIs in tree order with their category path and scoring weight", () => {
  const nodes = [
    { id: "c2", name: "Security", parentId: null, weight: null, displayOrder: 1 },
    { id: "c1", name: "Design", parentId: null, weight: null, displayOrder: 0 },
    { id: "k3", name: "SC-1 Threat model", parentId: "c2", weight: 30, displayOrder: 0 },
    { id: "k2", name: "LD-2 Contracts", parentId: "c1", weight: 20, displayOrder: 1 },
    { id: "k1", name: "LD-1 Patterns", parentId: "c1", weight: 50, displayOrder: 0, includedInScoring: false },
    { id: "k4", name: "Docs", parentId: null, weight: 0, displayOrder: 2 },
    { id: "k5", name: "Docs", parentId: "missing", weight: 0, displayOrder: 3 },
  ];
  const list = formulaKpis(nodes);
  assert.deepEqual(
    list.map((k) => k.name),
    ["LD-1 Patterns", "LD-2 Contracts", "SC-1 Threat model", "Docs"],
  );
  assert.deepEqual(list[0].path, ["Design"]);
  assert.equal(list[0].weight, 0); // excluded from scoring
  assert.equal(list[1].weight, 20);
  assert.deepEqual(list[3].path, []);
  assert.deepEqual(
    list.map((k) => k.order),
    [0, 1, 2, 3],
  );
  // draft KPIs have no displayOrder: array order is kept
  assert.deepEqual(
    formulaKpis([
      { id: "b", name: "B", parentId: null, weight: 50 },
      { id: "a", name: "A", parentId: null, weight: 50 },
    ]).map((k) => k.name),
    ["B", "A"],
  );
});

test("monograms use a KPI code when there is one, otherwise initials", () => {
  assert.equal(kpiMonogram("LD-6: Patterns are not decorative"), "LD6");
  assert.equal(kpiMonogram("NF-10 Throughput"), "NF10");
  assert.equal(kpiMonogram("Complaint Rate"), "CR");
  assert.equal(kpiMonogram("Speed"), "SP");
  assert.equal(kpiMonogram("—"), "#");
});

test("KPIs in the same family share a colour", () => {
  assert.deepEqual(kpiTint({ name: "LD-1 a", path: [] }), kpiTint({ name: "LD-9 b", path: [] }));
  assert.deepEqual(kpiTint({ name: "X", path: ["Security"] }), kpiTint({ name: "Y", path: ["Security"] }));
});

test("visibleKpis searches names and categories, filters by use and sorts", () => {
  const kpis = formulaKpis([
    { id: "c", name: "Support", parentId: null, weight: null, displayOrder: 0 },
    { id: "1", name: "Complaint Rate", parentId: "c", weight: 10, displayOrder: 0 },
    { id: "2", name: "Average Handle Time", parentId: "c", weight: 10, displayOrder: 1 },
    { id: "3", name: "KPI 10", parentId: null, weight: 10, displayOrder: 1 },
    { id: "4", name: "KPI 9", parentId: null, weight: 10, displayOrder: 2 },
  ]);
  const usage = new Map([
    ["Average Handle Time", 2],
    ["KPI 9", 1],
  ]);
  const names = (view) =>
    visibleKpis(kpis, { query: "", filter: "all", sort: "order", ...view }, usage).map((k) => k.name);
  assert.deepEqual(names({}), ["Complaint Rate", "Average Handle Time", "KPI 10", "KPI 9"]);
  assert.deepEqual(names({ query: "support rate" }), ["Complaint Rate"]);
  assert.deepEqual(names({ query: "TIME" }), ["Average Handle Time"]);
  assert.deepEqual(names({ filter: "used" }), ["Average Handle Time", "KPI 9"]);
  assert.deepEqual(names({ filter: "unused" }), ["Complaint Rate", "KPI 10"]);
  assert.deepEqual(names({ sort: "az" }), ["Average Handle Time", "Complaint Rate", "KPI 9", "KPI 10"]);
  assert.deepEqual(names({ sort: "za" }), ["KPI 10", "KPI 9", "Complaint Rate", "Average Handle Time"]);
  assert.deepEqual(names({ sort: "used" }), ["Average Handle Time", "KPI 9", "Complaint Rate", "KPI 10"]);
  assert.deepEqual(names({ sort: "category" }), ["KPI 10", "KPI 9", "Complaint Rate", "Average Handle Time"]);
});

test("highlight marks every search term, merging overlaps", () => {
  assert.deepEqual(highlight("Complaint Rate", "rate comp"), [
    { text: "Comp", hit: true },
    { text: "laint ", hit: false },
    { text: "Rate", hit: true },
  ]);
  assert.deepEqual(highlight("aaa", "aa a"), [{ text: "aaa", hit: true }]);
  assert.deepEqual(highlight("Speed", ""), [{ text: "Speed", hit: false }]);
});

test("history: edits undo and redo, grouped edits merge, caret moves are not edits", () => {
  const a = { tokens: [], caret: 0 };
  const b = { tokens: [numberToken("1")], caret: 1 };
  const c = { tokens: [numberToken("12")], caret: 1 };
  const d = { tokens: [numberToken("12"), opToken("+")], caret: 2 };
  let h = initHistory(a);
  h = historyReducer(h, { type: "edit", next: b, group: "digits" });
  h = historyReducer(h, { type: "edit", next: c, group: "digits" });
  assert.equal(h.past.length, 1); // "1" then "12" is one step
  h = historyReducer(h, { type: "caret", caret: 0 });
  assert.equal(h.present.caret, 0);
  assert.equal(h.past.length, 1);
  h = historyReducer(h, { type: "edit", next: d });
  h = historyReducer(h, { type: "undo" });
  assert.equal(h.present.tokens, c.tokens);
  h = historyReducer(h, { type: "undo" });
  assert.equal(h.present, a);
  assert.equal(historyReducer(h, { type: "undo" }), h);
  h = historyReducer(h, { type: "redo" });
  assert.equal(h.present.tokens, c.tokens);
  h = historyReducer(h, { type: "edit", next: b });
  assert.equal(h.future.length, 0); // a new edit drops the redo stack
  assert.equal(historyReducer(h, { type: "caret", caret: 99 }).present.caret, 1);
});
