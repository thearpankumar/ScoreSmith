/**
 * Token model behind the visual scoring-formula builder (ScoringFormulaBuilderDialog).
 *
 * The saved formula is still plain text in the backend grammar (backend/app/ai/scoring_formula.py):
 * `kpi["Name"]` references, numbers, `+ - * / **`, parentheses, commas and the functions
 * min/max/avg/mean/sqrt/abs. The builder edits a flat token list instead so a formula can be shown as
 * chips, clicked and dragged; `tokenize` and `serialize` convert between the two. Whether a formula is
 * VALID is still decided only by the backend validator (the builder calls it on every change) — the
 * paren matching here is just instant visual feedback.
 *
 * Pure and dependency-free so `node --test` can import it directly.
 */

export type FormulaToken =
  | { id: string; kind: "kpi"; name: string }
  | { id: string; kind: "number"; value: string }
  /** + - * / ** // % and comparisons. */
  | { id: string; kind: "op"; value: string }
  /** A function call's name together with its own opening paren, shown as one chip: `min(`. */
  | { id: string; kind: "fn"; name: string }
  | { id: string; kind: "open" }
  | { id: string; kind: "close" }
  | { id: string; kind: "comma" }
  /** Anything else the grammar allows that the palette doesn't offer: and/or/not/if/else, `[`, `]`. */
  | { id: string; kind: "raw"; value: string };

/** A formula being edited: its tokens plus the insertion point (0..tokens.length). */
export interface Snapshot {
  tokens: FormulaToken[];
  caret: number;
}

export type TokenizeResult = { ok: true; tokens: FormulaToken[] } | { ok: false; error: string; index: number };

export const FUNCTIONS = [
  { name: "min", label: "Minimum", help: "Lowest of the values" },
  { name: "max", label: "Maximum", help: "Highest of the values" },
  { name: "avg", label: "Average", help: "Mean of the values" },
  { name: "sqrt", label: "Square root", help: "Square root of one value" },
  { name: "abs", label: "Absolute", help: "Value without its sign" },
] as const;

let seq = 0;
const newId = () => `t${(++seq).toString(36)}`;

export const kpiToken = (name: string): FormulaToken => ({ id: newId(), kind: "kpi", name });
export const numberToken = (value: string): FormulaToken => ({ id: newId(), kind: "number", value });
export const opToken = (value: string): FormulaToken => ({ id: newId(), kind: "op", value });
export const fnToken = (name: string): FormulaToken => ({ id: newId(), kind: "fn", name });
export const openToken = (): FormulaToken => ({ id: newId(), kind: "open" });
export const closeToken = (): FormulaToken => ({ id: newId(), kind: "close" });
export const commaToken = (): FormulaToken => ({ id: newId(), kind: "comma" });

const TWO_CHAR_OPS = ["**", "//", "<=", ">=", "==", "!="];
const ONE_CHAR_OPS = "+-*/%<>^";
const NUMBER_RE = /^(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?/;
const IDENT_RE = /^[A-Za-z_][A-Za-z0-9_]*/;
const ESCAPES: Record<string, string> = { n: "\n", t: "\t", r: "\r", "\\": "\\", '"': '"', "'": "'", "0": "\0" };

/** Reads a Python string literal starting at `s[i]` (a quote). Returns the value and the index after it. */
function readString(s: string, i: number): { value: string; end: number } | null {
  const quote = s[i];
  let out = "";
  let j = i + 1;
  while (j < s.length) {
    const ch = s[j];
    if (ch === quote) return { value: out, end: j + 1 };
    if (ch === "\n") return null;
    if (ch === "\\") {
      const next = s[j + 1];
      if (next === undefined) return null;
      if (next === "u" || next === "x") {
        const len = next === "u" ? 4 : 2;
        const hex = s.slice(j + 2, j + 2 + len);
        if (!new RegExp(`^[0-9a-fA-F]{${len}}$`).test(hex)) return null;
        out += String.fromCharCode(parseInt(hex, 16));
        j += 2 + len;
        continue;
      }
      out += ESCAPES[next] ?? `\\${next}`;
      j += 2;
      continue;
    }
    out += ch;
    j += 1;
  }
  return null;
}

const skipSpace = (s: string, i: number) => {
  while (i < s.length && /\s/.test(s[i])) i += 1;
  return i;
};

/** Splits formula text into tokens. Fails (with a position) on anything the visual editor can't show,
 * e.g. a string outside `kpi[...]`; the builder then keeps that formula in its text mode. */
export function tokenize(text: string): TokenizeResult {
  const s = text ?? "";
  const tokens: FormulaToken[] = [];
  let i = skipSpace(s, 0);
  while (i < s.length) {
    const rest = s.slice(i);
    const ident = IDENT_RE.exec(rest);
    if (ident) {
      const word = ident[0];
      const after = skipSpace(s, i + word.length);
      if (word === "kpi" && s[after] === "[") {
        const q = skipSpace(s, after + 1);
        const str = s[q] === '"' || s[q] === "'" ? readString(s, q) : null;
        const close = str ? skipSpace(s, str.end) : -1;
        if (!str || s[close] !== "]") {
          return { ok: false, error: `Expected kpi["KPI name"] at position ${i + 1}.`, index: i };
        }
        tokens.push(kpiToken(str.value));
        i = skipSpace(s, close + 1);
        continue;
      }
      if (s[after] === "(") {
        tokens.push(fnToken(word));
        i = skipSpace(s, after + 1);
        continue;
      }
      tokens.push({ id: newId(), kind: "raw", value: word });
      i = skipSpace(s, i + word.length);
      continue;
    }
    const num = NUMBER_RE.exec(rest);
    if (num) {
      tokens.push(numberToken(num[0]));
      i = skipSpace(s, i + num[0].length);
      continue;
    }
    const two = TWO_CHAR_OPS.find((op) => rest.startsWith(op));
    if (two) {
      tokens.push(opToken(two));
      i = skipSpace(s, i + 2);
      continue;
    }
    const ch = s[i];
    if (ONE_CHAR_OPS.includes(ch)) tokens.push(opToken(ch));
    else if (ch === "(") tokens.push(openToken());
    else if (ch === ")") tokens.push(closeToken());
    else if (ch === ",") tokens.push(commaToken());
    else if (ch === "[" || ch === "]") tokens.push({ id: newId(), kind: "raw", value: ch });
    else return { ok: false, error: `The visual editor can't show "${ch}" (position ${i + 1}).`, index: i };
    i = skipSpace(s, i + 1);
  }
  return { ok: true, tokens };
}

/** The backend text for one token. KPI names go through JSON string escaping, which Python string
 * literals accept as-is, so a name containing a quote or backslash still round-trips. */
export function tokenText(t: FormulaToken): string {
  switch (t.kind) {
    case "kpi":
      return `kpi[${JSON.stringify(t.name)}]`;
    case "number":
    case "op":
    case "raw":
      return t.value;
    case "fn":
      return `${t.name}(`;
    case "open":
      return "(";
    case "close":
      return ")";
    case "comma":
      return ",";
  }
}

const OP_LABELS: Record<string, string> = { "*": "×", "/": "÷", "-": "−", "**": "^", "<=": "≤", ">=": "≥", "!=": "≠" };

/** How a token reads in the chips and the preview: KPI names in full, × ÷ − instead of * / -. */
export function tokenLabel(t: FormulaToken): string {
  if (t.kind === "kpi") return t.name;
  if (t.kind === "op") return OP_LABELS[t.value] ?? t.value;
  return tokenText(t);
}

const opensGroup = (t: FormulaToken | undefined) =>
  !!t && (t.kind === "fn" || t.kind === "open" || (t.kind === "raw" && t.value === "["));
const closesGroup = (t: FormulaToken | undefined) =>
  !!t && (t.kind === "close" || (t.kind === "raw" && t.value === "]"));

/** A `+`/`-` with nothing to its left to add to (start, after an operator, an opening paren or a comma)
 * is a sign, written against its operand: `-0.5`, `min(-1, x)`. */
function isSign(tokens: FormulaToken[], i: number): boolean {
  const t = tokens[i];
  if (t.kind !== "op" || (t.value !== "-" && t.value !== "+")) return false;
  const prev = tokens[i - 1];
  return (
    !prev ||
    prev.kind === "op" ||
    prev.kind === "comma" ||
    opensGroup(prev) ||
    (prev.kind === "raw" && /\w/.test(prev.value))
  );
}

/** Whether a space goes before token `i` when the tokens are written out (the preview renders tokens
 * one by one and needs the same spacing as `serialize`). */
export function spaceBefore(tokens: FormulaToken[], i: number): boolean {
  const prev = tokens[i - 1];
  const t = tokens[i];
  return !(!prev || opensGroup(prev) || closesGroup(t) || t.kind === "comma" || isSign(tokens, i - 1));
}

const joined = (tokens: FormulaToken[], text: (t: FormulaToken) => string) =>
  tokens.map((t, i) => (spaceBefore(tokens, i) ? " " : "") + text(t)).join("");

/** Tokens back to the text the backend stores and validates. */
export const serialize = (tokens: FormulaToken[]) => joined(tokens, tokenText);

/** The human-readable form shown in the preview: `min(Complaint Rate, Effort) × 0.6`. */
export const readable = (tokens: FormulaToken[]) => joined(tokens, tokenLabel);

export interface ParenInfo {
  /** index of an opening/closing token -> index of its partner. */
  partner: Map<number, number>;
  /** opening/closing tokens with no partner. */
  unmatched: Set<number>;
  /** nesting depth of each token (an opener and its closer share a depth), for bracket colouring. */
  depth: number[];
}

export function matchParens(tokens: FormulaToken[]): ParenInfo {
  const partner = new Map<number, number>();
  const unmatched = new Set<number>();
  const depth: number[] = [];
  const stack: number[] = [];
  tokens.forEach((t, i) => {
    if (opensGroup(t)) {
      depth[i] = stack.length;
      stack.push(i);
      return;
    }
    if (closesGroup(t)) {
      const top = stack[stack.length - 1];
      const square = t.kind === "raw";
      const fits = top !== undefined && (tokens[top].kind === "raw") === square;
      if (fits) {
        stack.pop();
        partner.set(top, i).set(i, top);
        depth[i] = stack.length;
      } else {
        unmatched.add(i);
        depth[i] = stack.length;
      }
      return;
    }
    depth[i] = stack.length;
  });
  stack.forEach((i) => unmatched.add(i));
  return { partner, unmatched, depth };
}

const endsOperand = (t: FormulaToken | undefined) => !!t && (t.kind === "kpi" || t.kind === "number" || closesGroup(t));
const startsOperand = (t: FormulaToken | undefined) =>
  !!t && (t.kind === "kpi" || t.kind === "number" || t.kind === "fn" || t.kind === "open");

/** The separator that goes between two operands at `caret`: a comma inside a function's argument list,
 * otherwise `+` (so clicking KPI after KPI builds `a + b + c`, or `min(a, b, c)` inside min). */
function joinerAt(tokens: FormulaToken[], caret: number): FormulaToken {
  const stack: FormulaToken[] = [];
  tokens.slice(0, caret).forEach((t) => {
    if (opensGroup(t)) stack.push(t);
    else if (closesGroup(t)) stack.pop();
  });
  const top = stack[stack.length - 1];
  return top && (top.kind === "fn" || top.kind === "raw") ? commaToken() : opToken("+");
}

/**
 * Inserts `items` at the caret. With `join`, adds a separator where the new tokens would otherwise
 * sit directly next to another operand. The caret ends up after the inserted tokens, or `caretAt`
 * tokens into them (e.g. 1 = inside a just-inserted `min(` `)` pair).
 */
export function insertTokens(
  snap: Snapshot,
  items: FormulaToken[],
  opts: { join?: boolean; caretAt?: number } = {},
): Snapshot {
  if (items.length === 0) return snap;
  const { tokens, caret } = snap;
  const lead = opts.join && endsOperand(tokens[caret - 1]) && startsOperand(items[0]) ? [joinerAt(tokens, caret)] : [];
  const trail =
    opts.join && startsOperand(tokens[caret]) && endsOperand(items[items.length - 1]) ? [joinerAt(tokens, caret)] : [];
  const next = [...tokens.slice(0, caret), ...lead, ...items, ...trail, ...tokens.slice(caret)];
  return { tokens: next, caret: caret + lead.length + (opts.caretAt ?? items.length) };
}

/** `fn(` + `)` with the caret between them. */
export const insertFunction = (snap: Snapshot, name: string) =>
  insertTokens(snap, [fnToken(name), closeToken()], { join: true, caretAt: 1 });

/** Removes the token at `index`. Removing one side of a matched pair removes its partner too, so
 * deleting `min(` doesn't leave a stray `)`; whatever was inside stays. */
export function removeAt(snap: Snapshot, index: number): Snapshot {
  if (index < 0 || index >= snap.tokens.length) return snap;
  const partner = matchParens(snap.tokens).partner.get(index);
  const drop = new Set([index, ...(partner === undefined ? [] : [partner])]);
  const tokens = snap.tokens.filter((_, i) => !drop.has(i));
  const removedBeforeCaret = [...drop].filter((i) => i < snap.caret).length;
  return { tokens, caret: Math.max(0, snap.caret - removedBeforeCaret) };
}

const isJoiner = (t: FormulaToken | undefined) =>
  !!t && (t.kind === "comma" || (t.kind === "op" && !/[<>=!]/.test(t.value)));

/**
 * Removes a KPI chip together with the operator or comma that attached it (the one before it, else the
 * one after), so taking `b` out of `a + b + c` leaves `a + c` rather than `a + + c`. Used by the chip's
 * remove button and by dragging; Backspace / Delete stay one-token-at-a-time. Other tokens fall back to
 * `removeAt`. The caret is kept on the same spot of the remaining formula.
 */
export function removeWithJoiner(snap: Snapshot, index: number): Snapshot {
  const t = snap.tokens[index];
  if (t?.kind !== "kpi") return removeAt(snap, index);
  const joiner = isJoiner(snap.tokens[index - 1]) ? index - 1 : isJoiner(snap.tokens[index + 1]) ? index + 1 : -1;
  const drop = new Set(joiner < 0 ? [index] : [index, joiner]);
  const removedBeforeCaret = [...drop].filter((i) => i < snap.caret).length;
  return { tokens: snap.tokens.filter((_, i) => !drop.has(i)), caret: Math.max(0, snap.caret - removedBeforeCaret) };
}

/** Drag-moving a KPI chip: lifted out with its joiner, dropped into gap `gap` with smart joining, so
 * dragging `b` to the front of `a + b` gives `b + a`. Other tokens move as they are (`moveToken`). */
export function moveWithJoiner(snap: Snapshot, from: number, gap: number): Snapshot {
  const t = snap.tokens[from];
  if (t?.kind !== "kpi") return moveToken(snap, from, gap);
  if (gap === from || gap === from + 1) return snap;
  const lifted = removeWithJoiner({ tokens: snap.tokens, caret: gap }, from); // caret = the gap, shifted
  return insertTokens(lifted, [t], { join: true });
}

const NUMBER_DRAFT_RE = /^\d*\.?\d*$/;

/** Typing a digit or "." at the caret: extends the number just before the caret, or starts a new one. */
export function typeDigit(snap: Snapshot, ch: string): Snapshot {
  const prev = snap.tokens[snap.caret - 1];
  if (prev?.kind === "number") {
    const value = prev.value + ch;
    if (!NUMBER_DRAFT_RE.test(value)) return snap;
    const tokens = snap.tokens.slice();
    tokens[snap.caret - 1] = { ...prev, value };
    return { tokens, caret: snap.caret };
  }
  return insertTokens(snap, [numberToken(ch === "." ? "0." : ch)]);
}

/** Backspace at the caret: trims a number one digit at a time, otherwise removes the token before it. */
export function backspace(snap: Snapshot): Snapshot {
  const prev = snap.tokens[snap.caret - 1];
  if (!prev) return snap;
  if (prev.kind === "number" && prev.value.length > 1) {
    const tokens = snap.tokens.slice();
    tokens[snap.caret - 1] = { ...prev, value: prev.value.slice(0, -1) };
    return { tokens, caret: snap.caret };
  }
  return removeAt(snap, snap.caret - 1);
}

/** Sets a number token's value (inline edit). An empty or unusable value removes the token. */
export function setNumber(snap: Snapshot, index: number, raw: string): Snapshot {
  const value = raw.trim();
  const t = snap.tokens[index];
  if (!t || t.kind !== "number") return snap;
  if (!/^(?:\d+\.?\d*|\.\d+)$/.test(value)) return removeAt(snap, index);
  const tokens = snap.tokens.slice();
  tokens[index] = { ...t, value };
  return { tokens, caret: snap.caret };
}

/** Moves the token at `from` into gap `gap` (0..length; gap i is just before token i). The caret
 * lands just after the moved token. Dropping a token next to itself changes nothing. */
export function moveToken(snap: Snapshot, from: number, gap: number): Snapshot {
  const { tokens } = snap;
  if (from < 0 || from >= tokens.length || gap === from || gap === from + 1) return snap;
  const moved = tokens[from];
  const rest = tokens.filter((_, i) => i !== from);
  const at = gap > from ? gap - 1 : gap;
  return { tokens: [...rest.slice(0, at), moved, ...rest.slice(at)], caret: at + 1 };
}

/** How many times each KPI name is referenced. */
export function kpiUsage(tokens: FormulaToken[]): Map<string, number> {
  const usage = new Map<string, number>();
  tokens.forEach((t) => {
    if (t.kind === "kpi") usage.set(t.name, (usage.get(t.name) ?? 0) + 1);
  });
  return usage;
}

/** A number for a formula: up to 4 decimals, no trailing zeros. */
export const formatNumber = (n: number) => String(Number(n.toFixed(4)));

function call(name: string, names: string[]): FormulaToken[] {
  const args = names.flatMap((n, i) => (i === 0 ? [kpiToken(n)] : [commaToken(), kpiToken(n)]));
  return [fnToken(name), ...args, closeToken()];
}

export type TemplateId = "default" | "weights" | "average" | "weakest" | "blend";

export const TEMPLATES: { id: TemplateId; label: string; help: string }[] = [
  { id: "default", label: "Default weighted average", help: "No custom formula: Σ(weight × score) / 100" },
  { id: "weights", label: "Explicit weights", help: "Each KPI × its weight, ready to adjust" },
  { id: "average", label: "Simple average", help: "Every KPI counts the same" },
  { id: "weakest", label: "Weakest link", help: "The lowest KPI score decides" },
  { id: "blend", label: "Average + weakest blend", help: "70% average, 30% the lowest KPI" },
];

/** Builds a starting formula over `kpis` (the scorecard's leaf KPIs, in order). `weight` is the KPI's
 * share of the default weighted average in percent (0 or null when it isn't part of it). */
export function templateTokens(id: TemplateId, kpis: { name: string; weight: number | null }[]): FormulaToken[] {
  const names = kpis.map((k) => k.name);
  if (id === "default" || names.length === 0) return [];
  if (id === "average") return call("avg", names);
  if (id === "weakest") return call("min", names);
  if (id === "blend") {
    return [
      ...call("avg", names),
      opToken("*"),
      numberToken("0.7"),
      opToken("+"),
      ...call("min", names),
      opToken("*"),
      numberToken("0.3"),
    ];
  }
  const weighted = kpis.filter((k) => (k.weight ?? 0) > 0);
  if (weighted.length === 0) return call("avg", names);
  return weighted.flatMap((k, i) => [
    ...(i === 0 ? [] : [opToken("+")]),
    kpiToken(k.name),
    opToken("*"),
    numberToken(formatNumber((k.weight ?? 0) / 100)),
  ]);
}
