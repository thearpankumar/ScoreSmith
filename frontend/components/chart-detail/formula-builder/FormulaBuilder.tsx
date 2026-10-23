"use client";

import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useReducer,
  useRef,
  useState,
  type KeyboardEvent,
  type RefObject,
} from "react";
import { createPortal } from "react-dom";
import {
  DndContext,
  DragOverlay,
  MeasuringStrategy,
  MouseSensor,
  TouchSensor,
  useSensor,
  useSensors,
  type Active,
  type Collision,
  type CollisionDetection,
  type DragEndEvent,
  type DragStartEvent,
} from "@dnd-kit/core";
import { ChevronDown, Eraser, LayoutTemplate, Maximize2, Minimize2, Redo2, Undo2, type LucideIcon } from "lucide-react";
import { useDefaultLayout, type LayoutStorage } from "react-resizable-panels";

import { Button } from "@/components/ui/button";
import { DialogDescription, DialogTitle } from "@/components/ui/dialog";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { ResizableHandle, ResizablePanel, ResizablePanelGroup } from "@/components/ui/resizable";
import type { FormulaValidation } from "@/lib/api-client";
import { historyReducer, initHistory } from "@/lib/formula-history";
import type { FormulaKpi, KpiFilter, KpiSort } from "@/lib/formula-kpis";
import {
  backspace,
  closeToken,
  commaToken,
  fnToken,
  insertTokens,
  kpiToken,
  kpiUsage,
  moveToken,
  moveWithJoiner,
  numberToken,
  openToken,
  opToken,
  readable,
  removeAt,
  removeWithJoiner,
  serialize,
  setNumber,
  TEMPLATES,
  templateTokens,
  tokenize,
  tokenLabel,
  typeDigit,
  type FormulaToken,
  type Snapshot,
  type TemplateId,
} from "@/lib/formula-tokens";
import { useMediaQuery } from "@/lib/useMediaQuery";
import { usePersistedState } from "@/lib/usePersistedState";
import { cn } from "@/lib/utils";
import { CANVAS_DROP_ID, DragChip, FormulaCanvas } from "./FormulaCanvas";
import { FormulaPalette, type PaletteKey } from "./FormulaPalette";
import { FormulaPreview, type CheckState } from "./FormulaPreview";
import { KpiPicker } from "./KpiPicker";

const VALIDATE_DEBOUNCE_MS = 350;
const WIDE_QUERY = "(min-width: 768px)";
const HELP_ID = "formula-canvas-help";
const READ_ID = "formula-canvas-read";
/** Keys typed on the canvas that insert an operator (`^` means power, which the backend writes `**`). */
const KEY_OPS: Record<string, string> = { "+": "+", "-": "-", "*": "*", "/": "/", "^": "**", "%": "%" };

/** localStorage for the remembered pane split, tolerating private mode / blocked storage. */
const layoutStorage: LayoutStorage = {
  getItem: (key) => {
    try {
      return window.localStorage.getItem(key);
    } catch {
      return null;
    }
  },
  setItem: (key, value) => {
    try {
      window.localStorage.setItem(key, value);
    } catch {
      // quota / private mode: the split just isn't remembered
    }
  },
};

/**
 * Drop position = the gap nearest the pointer, but only while the pointer is over the formula (letting
 * go anywhere else cancels). Vertical distance counts more, so the pointer picks a gap on its own line
 * of a wrapped formula rather than a closer-looking one on the line above.
 */
const nearestGap: CollisionDetection = ({ droppableContainers, droppableRects, pointerCoordinates }) => {
  const area = droppableRects.get(CANVAS_DROP_ID);
  if (!pointerCoordinates || !area) return [];
  const { x, y } = pointerCoordinates;
  if (x < area.left || x > area.right || y < area.top || y > area.bottom) return [];
  let best: Collision | null = null;
  let bestDistance = Infinity;
  for (const container of droppableContainers) {
    if (container.data.current?.type !== "gap") continue;
    const r = droppableRects.get(container.id);
    if (!r) continue;
    const distance = Math.hypot(x - (r.left + Math.min(r.width / 2, 3)), (y - (r.top + r.height / 2)) * 2.5);
    if (distance < bestDistance) {
      bestDistance = distance;
      best = { id: container.id, data: { droppableContainer: container, value: distance } };
    }
  }
  return best ? [best] : [];
};

export interface FormulaBuilderProps {
  initialText: string;
  kpis: FormulaKpi[];
  onValidate: (formula: string) => Promise<FormulaValidation>;
  onApply: (text: string) => void;
  onCancel: () => void;
  maximized: boolean;
  onToggleMaximized: () => void;
  /** Set by the builder: returns true when an Escape press belongs to something inside it (a drag, a
   * number being edited, a search to clear), so the dialog stays open. */
  escapeGuard: RefObject<() => boolean>;
}

/**
 * The scoring-formula builder: a KPI list and the formula side by side in resizable panes (stacked on
 * phones), the formula edited as chips with a caret (click, keyboard, or drag-and-drop) or as plain text.
 * Every change is checked by the same backend validator as the plain editor, and the result is the same
 * backend formula text as before.
 */
export function FormulaBuilder({
  initialText,
  kpis,
  onValidate,
  onApply,
  onCancel,
  maximized,
  onToggleMaximized,
  escapeGuard,
}: FormulaBuilderProps) {
  const [initialParse] = useState(() => tokenize(initialText));
  const [hist, dispatch] = useReducer(historyReducer, initialParse, (parsed) => {
    const tokens = parsed.ok ? parsed.tokens : [];
    return initHistory({ tokens, caret: tokens.length });
  });
  const snap = hist.present;
  const [mode, setMode] = useState<"visual" | "text">(initialParse.ok ? "visual" : "text");
  const [text, setText] = useState(initialText);
  const [notice, setNotice] = useState<string | null>(
    initialParse.ok ? null : `The visual editor can't show this formula (${initialParse.error}). Edit it as text.`,
  );
  const [query, setQuery] = useState("");
  const [filter, setFilter] = useState<KpiFilter>("all");
  const [sort, setSort] = usePersistedState<KpiSort>("qs.formulaBuilder.sort", "order");
  const [editingId, setEditingId] = useState<string | null>(null);
  const [dragToken, setDragToken] = useState<FormulaToken | null>(null);
  const [message, setMessage] = useState("");
  const [check, setCheck] = useState<CheckState>({ kind: "blank" });

  const snapRef = useRef(snap);
  const editingRef = useRef<string | null>(null);
  const newNumberRef = useRef<string | null>(null);
  const draggingRef = useRef(false);
  const requestIdRef = useRef(0);
  const searchRef = useRef<HTMLInputElement>(null);
  const canvasRef = useRef<HTMLDivElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  // Event handlers read the latest snapshot from here; a layout effect keeps it current before any
  // further input can arrive.
  useLayoutEffect(() => {
    snapRef.current = snap;
  });

  const isWide = useMediaQuery(WIDE_QUERY, true);
  const orientation = isWide ? "horizontal" : "vertical";
  const { defaultLayout, onLayoutChanged } = useDefaultLayout({
    id: `qs.formulaBuilder.split.${orientation}`,
    storage: layoutStorage,
  });

  const visualText = useMemo(() => serialize(snap.tokens), [snap.tokens]);
  const formula = mode === "visual" ? visualText : text;
  const textParse = useMemo(() => (mode === "text" ? tokenize(text) : null), [mode, text]);
  const shownTokens = mode === "visual" ? snap.tokens : textParse?.ok ? textParse.tokens : null;
  const usage = useMemo(() => kpiUsage(shownTokens ?? []), [shownTokens]);
  const kpiByName = useMemo(() => new Map(kpis.map((k) => [k.name, k])), [kpis]);
  const usedCount = kpis.filter((k) => usage.has(k.name)).length;
  const blank = formula.trim() === "";
  const baseline = useMemo(
    () => (initialParse.ok ? serialize(initialParse.tokens) : initialText.trim()),
    [initialParse, initialText],
  );

  // Live check against the real backend validator (debounced; a newer edit supersedes an older reply).
  useEffect(() => {
    const id = ++requestIdRef.current;
    if (!formula.trim()) {
      setCheck({ kind: "blank" });
      return;
    }
    setCheck({ kind: "checking" });
    const timer = setTimeout(async () => {
      try {
        const result = await onValidate(formula);
        if (requestIdRef.current !== id) return;
        setCheck(
          result.valid ? { kind: "valid" } : { kind: "invalid", message: result.error ?? "This formula is not valid." },
        );
      } catch (err) {
        if (requestIdRef.current !== id) return;
        setCheck({
          kind: "unavailable",
          message: err instanceof Error ? err.message : "Could not check the formula right now.",
        });
      }
    }, VALIDATE_DEBOUNCE_MS);
    return () => clearTimeout(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps -- onValidate is bound once at the call site
  }, [formula]);

  // Desktop starts in the search box; phones start on the formula so the on-screen keyboard stays shut.
  useEffect(() => {
    const target = window.matchMedia(WIDE_QUERY).matches
      ? searchRef.current
      : (canvasRef.current ?? textareaRef.current);
    target?.focus({ preventScroll: true });
  }, []);

  useEffect(() => {
    escapeGuard.current = () => {
      if (draggingRef.current || editingRef.current) return true;
      const search = searchRef.current;
      if (search && document.activeElement === search && search.value) {
        setQuery("");
        return true;
      }
      return false;
    };
  });

  const announce = useCallback((msg: string) => setMessage((m) => (m === msg ? `${msg} ` : msg)), []);
  const update = useCallback(
    (next: Snapshot | ((s: Snapshot) => Snapshot), group?: string) => dispatch({ type: "edit", next, group }),
    [],
  );
  const setCaret = useCallback((caret: number) => dispatch({ type: "caret", caret }), []);
  const focusFormula = useCallback(() => {
    requestAnimationFrame(() => (canvasRef.current ?? textareaRef.current)?.focus({ preventScroll: true }));
  }, []);

  /** Text mode: puts `snippet` at the textarea's cursor, like the palette always did. */
  const insertText = useCallback((snippet: string, back = 0) => {
    const el = textareaRef.current;
    if (!el) {
      setText((t) => t + snippet);
      return;
    }
    const start = el.selectionStart ?? el.value.length;
    const end = el.selectionEnd ?? el.value.length;
    setText(el.value.slice(0, start) + snippet + el.value.slice(end));
    const pos = start + snippet.length - back;
    requestAnimationFrame(() => {
      el.focus();
      el.setSelectionRange(pos, pos);
    });
  }, []);

  const insertKpi = useCallback(
    (name: string) => {
      if (mode === "text") {
        insertText(`kpi[${JSON.stringify(name)}]`);
        return;
      }
      const token = kpiToken(name);
      update((s) => insertTokens(s, [token], { join: true }));
      announce(`Added ${name}.`);
    },
    [mode, insertText, update, announce],
  );

  const insertMany = useCallback(
    (names: string[]) => {
      if (names.length === 0) return;
      if (mode === "text") {
        insertText(names.map((n) => `kpi[${JSON.stringify(n)}]`).join(" + "));
        return;
      }
      const tokens = names.map(kpiToken);
      update((s) => tokens.reduce((acc, t) => insertTokens(acc, [t], { join: true }), s));
      announce(`Added ${names.length} KPIs.`);
    },
    [mode, insertText, update, announce],
  );

  const removeToken = useCallback(
    (index: number) => {
      const token = snapRef.current.tokens[index];
      if (!token) return;
      update((s) => removeWithJoiner(s, index));
      announce(`Removed ${tokenLabel(token)}.`);
    },
    [update, announce],
  );

  const startEdit = useCallback((id: string) => {
    editingRef.current = id;
    setEditingId(id);
  }, []);

  const endEdit = useCallback((refocus: boolean) => {
    editingRef.current = null;
    newNumberRef.current = null;
    setEditingId(null);
    // Synchronously, while the number field is still mounted: once it unmounts focus would fall to the
    // page for a frame and a quick Ctrl+Z would go nowhere. (Its blur is ignored: the edit already ended.)
    if (refocus) canvasRef.current?.focus({ preventScroll: true });
  }, []);

  const commitNumber = useCallback(
    (id: string, value: string, refocus: boolean) => {
      // Enter refocuses the formula; a blur means focus went somewhere else on purpose. Enter already
      // committed when the blur that follows it arrives.
      if (editingRef.current !== id) return;
      update((s) => {
        const i = s.tokens.findIndex((t) => t.id === id);
        return i < 0 ? s : setNumber(s, i, value);
      }, `number:${id}`);
      endEdit(refocus);
    },
    [update, endEdit],
  );

  const cancelEdit = useCallback(
    (id: string) => {
      if (editingRef.current !== id) return;
      if (newNumberRef.current === id) {
        update((s) => {
          const i = s.tokens.findIndex((t) => t.id === id);
          return i < 0 ? s : removeAt(s, i);
        }, `number:${id}`);
      }
      endEdit(true);
    },
    [update, endEdit],
  );

  function addNumber() {
    if (mode === "text") {
      textareaRef.current?.focus();
      return;
    }
    const token = numberToken("1");
    update((s) => insertTokens(s, [token]), `number:${token.id}`);
    newNumberRef.current = token.id;
    startEdit(token.id);
  }

  function paletteKey(key: PaletteKey) {
    if (mode === "text") {
      insertText(key === "(" || key === ")" ? key : key === "," ? ", " : ` ${key} `);
      return;
    }
    const token = key === "(" ? openToken() : key === ")" ? closeToken() : key === "," ? commaToken() : opToken(key);
    update((s) => insertTokens(s, [token]));
  }

  function addFunction(name: string) {
    if (mode === "text") {
      insertText(`${name}()`, 1);
      return;
    }
    const tokens = [fnToken(name), closeToken()];
    update((s) => insertTokens(s, tokens, { join: true, caretAt: 1 }));
    announce(`Added ${name}( ). The caret is inside the brackets.`);
  }

  function applyTemplate(id: TemplateId) {
    const tokens = templateTokens(id, kpis);
    update({ tokens, caret: tokens.length });
    setMode("visual");
    setNotice(null);
    const label = TEMPLATES.find((t) => t.id === id)?.label ?? "Template";
    announce(`${label} applied. Undo brings back the previous formula.`);
  }

  function clearFormula() {
    if (mode === "text") setText("");
    else update({ tokens: [], caret: 0 });
    announce("Formula cleared.");
  }

  function toText() {
    if (mode === "text") return;
    setText(visualText);
    setMode("text");
    setNotice(null);
  }

  function toVisual() {
    if (mode === "visual") return;
    const parsed = tokenize(text);
    if (!parsed.ok) {
      setNotice(`The visual editor can't show this formula (${parsed.error}). Fix it here, or clear it to start over.`);
      return;
    }
    update({ tokens: parsed.tokens, caret: parsed.tokens.length });
    setMode("visual");
    setNotice(null);
  }

  function apply() {
    const out = formula.trim();
    onApply(out === baseline || out === initialText.trim() ? initialText : out);
  }

  /** Undo / redo anywhere in the builder except inside a text field (which has its own undo). */
  function onRootKey(e: KeyboardEvent<HTMLDivElement>) {
    if (!(e.ctrlKey || e.metaKey) || mode !== "visual") return;
    if ((e.target as HTMLElement).closest("input, textarea")) return;
    const key = e.key.toLowerCase();
    if (key === "z") dispatch({ type: e.shiftKey ? "redo" : "undo" });
    else if (key === "y") dispatch({ type: "redo" });
    else return;
    e.preventDefault();
  }

  function onCanvasKey(e: KeyboardEvent<HTMLDivElement>) {
    if (e.target !== e.currentTarget || e.ctrlKey || e.metaKey || e.altKey) return;
    const s = snapRef.current;
    const k = e.key;
    if (k === "ArrowLeft" || k === "ArrowRight") {
      const step = k === "ArrowLeft" ? -1 : 1;
      if (e.shiftKey) {
        // Shift+arrow moves the chip just before the caret one place.
        const from = s.caret - 1;
        const gap = step < 0 ? from - 1 : from + 2;
        if (from >= 0 && gap >= 0 && gap <= s.tokens.length) {
          update((x) => moveToken(x, from, gap));
          announce(`Moved ${tokenLabel(s.tokens[from])} ${step < 0 ? "left" : "right"}.`);
        }
      } else setCaret(s.caret + step);
    } else if (k === "Home") setCaret(0);
    else if (k === "End") setCaret(s.tokens.length);
    else if (k === "Backspace") {
      const prev = s.tokens[s.caret - 1];
      if (!prev) return;
      update(backspace, "backspace");
      if (!(prev.kind === "number" && prev.value.length > 1)) announce(`Removed ${tokenLabel(prev)}.`);
    } else if (k === "Delete") {
      const next = s.tokens[s.caret];
      if (!next) return;
      update((x) => removeAt(x, x.caret));
      announce(`Removed ${tokenLabel(next)}.`);
    } else if (/^[0-9.]$/.test(k)) update((x) => typeDigit(x, k), "digits");
    else if (k in KEY_OPS) {
      const token = opToken(KEY_OPS[k]);
      update((x) => insertTokens(x, [token]));
    } else if (k === "(" || k === ")" || k === ",") {
      const token = k === "(" ? openToken() : k === ")" ? closeToken() : commaToken();
      update((x) => insertTokens(x, [token]));
    } else if (k === "Enter") {
      const prev = s.tokens[s.caret - 1];
      if (prev?.kind === "number") startEdit(prev.id);
    } else if (k === " ") {
      // nothing to insert; just don't scroll the pane
    } else if (k.length === 1 && /\p{L}/u.test(k)) {
      // Letters search the KPI list.
      setQuery(k);
      searchRef.current?.focus();
      requestAnimationFrame(() => searchRef.current?.setSelectionRange(1, 1));
    } else return;
    e.preventDefault();
  }

  const sensors = useSensors(
    useSensor(MouseSensor, { activationConstraint: { distance: 5 } }),
    // Touch: a short press-and-hold starts a drag, so a swipe still scrolls the KPI list.
    useSensor(TouchSensor, { activationConstraint: { delay: 220, tolerance: 6 } }),
  );

  const dragName = (active: Active) => {
    const data = active.data.current;
    if (data?.type === "kpi") return String(data.name);
    const token = snapRef.current.tokens.find((t) => t.id === data?.id);
    return token ? tokenLabel(token) : "item";
  };

  function onDragStart({ active }: DragStartEvent) {
    draggingRef.current = true;
    const data = active.data.current;
    if (data?.type === "kpi") setDragToken(kpiToken(String(data.name)));
    else setDragToken(snapRef.current.tokens.find((t) => t.id === data?.id) ?? null);
  }

  function onDragEnd({ active, over }: DragEndEvent) {
    draggingRef.current = false;
    setDragToken(null);
    if (over?.data.current?.type !== "gap") return;
    const gap = over.data.current.index as number;
    const data = active.data.current;
    if (data?.type === "kpi") {
      const token = kpiToken(String(data.name));
      update((s) => insertTokens({ ...s, caret: gap }, [token], { join: true }));
      announce(`Added ${String(data.name)}.`);
    } else if (data?.type === "token") {
      update((s) => {
        const from = s.tokens.findIndex((t) => t.id === data.id);
        return from < 0 ? s : moveWithJoiner(s, from, gap);
      });
    }
    focusFormula();
  }

  function onDragCancel() {
    draggingRef.current = false;
    setDragToken(null);
  }

  const templateLabel = blank ? "Default weighted average" : "Custom formula";

  return (
    <DndContext
      sensors={sensors}
      collisionDetection={nearestGap}
      measuring={{ droppable: { strategy: MeasuringStrategy.Always } }}
      onDragStart={onDragStart}
      onDragEnd={onDragEnd}
      onDragCancel={onDragCancel}
      accessibility={{
        announcements: {
          onDragStart: ({ active }) => `Picked up ${dragName(active)}.`,
          onDragOver: ({ over }) => (over ? "Over the formula." : "Outside the formula."),
          onDragEnd: ({ active, over }) =>
            over
              ? `Dropped ${dragName(active)} into the formula.`
              : `${dragName(active)} was not dropped on the formula.`,
          onDragCancel: ({ active }) => `Stopped dragging ${dragName(active)}.`,
        },
        screenReaderInstructions: {
          draggable: "Use the add buttons and the formula's keyboard keys instead of dragging.",
        },
      }}
    >
      <div className="flex min-h-0 flex-1 flex-col" onKeyDown={onRootKey}>
        <header className="flex flex-wrap items-start justify-between gap-x-4 gap-y-2 border-b border-hairline py-3 pl-4 pr-14 sm:pl-5">
          <div className="min-w-0">
            <DialogTitle className="text-lg sm:text-xl">Build a scoring formula</DialogTitle>
            <DialogDescription className="text-xs max-sm:sr-only sm:text-sm">
              Click or drag KPIs into the formula, then combine them with operators and functions.
            </DialogDescription>
          </div>
          <div className="flex items-center gap-1.5">
            <TemplateMenu
              label={templateLabel}
              hasKpis={kpis.length > 0}
              onPick={applyTemplate}
              onPicked={focusFormula}
            />
            <Button
              type="button"
              variant="ghost"
              size="icon"
              onClick={onToggleMaximized}
              aria-label={maximized ? "Restore the builder size" : "Maximize the builder"}
              title={maximized ? "Restore size" : "Maximize"}
              className="size-9 max-md:hidden"
            >
              {maximized ? <Minimize2 aria-hidden /> : <Maximize2 aria-hidden />}
            </Button>
          </div>
        </header>

        <div className="flex min-h-0 flex-1 px-3 py-3 sm:px-4">
          <ResizablePanelGroup
            key={orientation}
            orientation={orientation}
            defaultLayout={defaultLayout}
            onLayoutChanged={onLayoutChanged}
          >
            <ResizablePanel
              id="kpis"
              defaultSize={isWide ? "40%" : "50%"}
              minSize={isWide ? 250 : 180}
              className={cn("flex min-h-0 min-w-0 flex-col", isWide ? "pr-2" : "pb-2")}
            >
              <KpiPicker
                kpis={kpis}
                usage={usage}
                query={query}
                onQuery={setQuery}
                filter={filter}
                onFilter={setFilter}
                sort={sort}
                onSort={setSort}
                onInsert={insertKpi}
                onInsertMany={insertMany}
                searchRef={searchRef}
                dragEnabled={mode === "visual"}
              />
            </ResizablePanel>
            <ResizableHandle withHandle aria-label="Resize the KPI list" />
            <ResizablePanel
              id="formula"
              defaultSize={isWide ? "60%" : "50%"}
              minSize={isWide ? 340 : 220}
              className={cn("flex min-h-0 min-w-0 flex-col", isWide ? "pl-2" : "pt-2")}
            >
              <section aria-label="Formula" className="@container flex min-h-0 flex-1 flex-col gap-2.5">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <div className="flex items-center gap-2">
                    <h3 className="text-sm font-semibold text-ink">Formula</h3>
                    <div role="group" aria-label="Editor" className="flex rounded-lg bg-black/[0.05] p-0.5">
                      {(["visual", "text"] as const).map((m) => (
                        <button
                          key={m}
                          type="button"
                          aria-pressed={mode === m}
                          onClick={m === "visual" ? toVisual : toText}
                          className={cn(
                            "rounded-md px-2.5 py-1 text-xs font-medium capitalize transition-colors",
                            mode === m ? "bg-solid text-ink shadow-elev-1" : "text-ink-muted hover:text-ink",
                          )}
                        >
                          {m}
                        </button>
                      ))}
                    </div>
                  </div>
                  <div className="flex items-center gap-0.5">
                    <ToolButton
                      icon={Undo2}
                      label="Undo (Ctrl+Z)"
                      onClick={() => dispatch({ type: "undo" })}
                      disabled={mode !== "visual" || hist.past.length === 0}
                    />
                    <ToolButton
                      icon={Redo2}
                      label="Redo (Ctrl+Shift+Z)"
                      onClick={() => dispatch({ type: "redo" })}
                      disabled={mode !== "visual" || hist.future.length === 0}
                    />
                    <ToolButton icon={Eraser} label="Clear the formula" onClick={clearFormula} disabled={blank} />
                  </div>
                </div>

                {notice && (
                  <p
                    role="status"
                    className="rounded-lg border border-[#f59e0b]/50 bg-[#fffbeb] px-3 py-2 text-xs text-ink"
                  >
                    {notice}
                  </p>
                )}

                {mode === "visual" ? (
                  <FormulaCanvas
                    tokens={snap.tokens}
                    caret={snap.caret}
                    kpiByName={kpiByName}
                    editingId={editingId}
                    emptyHint="Click + on a KPI or drag it here. Leave the formula empty to use the default weighted average."
                    describedBy={`${READ_ID} ${HELP_ID}`}
                    canvasRef={canvasRef}
                    onKeyDown={onCanvasKey}
                    onCaret={setCaret}
                    onRemove={removeToken}
                    onStartEdit={startEdit}
                    onCommitNumber={commitNumber}
                    onCancelEdit={cancelEdit}
                  />
                ) : (
                  <textarea
                    ref={textareaRef}
                    value={text}
                    onChange={(e) => setText(e.target.value)}
                    spellCheck={false}
                    aria-label="Formula text"
                    placeholder='e.g. min(kpi["Speed"], kpi["Accuracy"]) * 0.7 + kpi["Accuracy"] * 0.3'
                    className="thin-scrollbar min-h-28 flex-1 resize-none rounded-xl border border-hairline bg-solid px-3 py-2 font-mono text-xs leading-relaxed text-ink placeholder:font-sans placeholder:italic placeholder:text-ink-muted focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-[var(--focus)]"
                  />
                )}

                <p id={HELP_ID} className="text-[11px] leading-relaxed text-ink-muted @max-sm:sr-only">
                  {mode === "visual"
                    ? "Type numbers and + − × ÷ ^ ( ) , straight into the formula. ← → move the caret, Backspace / Delete remove, Shift + ← → moves a chip, double-click a number to change it, letters search the KPIs."
                    : 'Reference a KPI as kpi["KPI name"]; use ** for powers.'}
                </p>
                <p id={READ_ID} className="sr-only">
                  {shownTokens && shownTokens.length > 0
                    ? `Formula: ${readable(shownTokens)}.`
                    : "The formula is empty, so the default weighted average is used."}
                </p>

                <FormulaPalette
                  onKey={paletteKey}
                  onFunction={addFunction}
                  onNumber={addNumber}
                  onMenuClosed={focusFormula}
                />
                <FormulaPreview tokens={shownTokens} text={formula} check={check} />
              </section>
            </ResizablePanel>
          </ResizablePanelGroup>
        </div>

        <footer className="flex flex-wrap items-center justify-between gap-x-4 gap-y-2 border-t border-hairline px-4 py-3 sm:px-5">
          <p className="text-sm text-ink-muted">
            Used KPIs: <strong className="font-semibold tabular-nums text-ink">{usedCount}</strong> of {kpis.length}
            {!blank && usedCount < kpis.length && (
              <>
                {" · "}
                <button
                  type="button"
                  onClick={() => {
                    setFilter("unused");
                    setQuery("");
                  }}
                  className="font-medium text-ink underline underline-offset-2 hover:text-lemon-ink"
                >
                  {kpis.length - usedCount} not used
                </button>
              </>
            )}
          </p>
          <div className="flex gap-2">
            <Button type="button" variant="ghost" onClick={onCancel}>
              Cancel
            </Button>
            <Button type="button" onClick={apply} disabled={check.kind === "checking" || check.kind === "invalid"}>
              Use this formula
            </Button>
          </div>
        </footer>

        <p aria-live="polite" className="sr-only">
          {message}
        </p>
      </div>

      {/* Portaled: the dialog's transform and backdrop-filter would otherwise offset the fixed overlay. */}
      {typeof document !== "undefined" &&
        createPortal(
          <DragOverlay dropAnimation={null}>
            {dragToken ? (
              <DragChip token={dragToken} kpi={dragToken.kind === "kpi" ? kpiByName.get(dragToken.name) : undefined} />
            ) : null}
          </DragOverlay>,
          document.body,
        )}
    </DndContext>
  );
}

function ToolButton({
  icon: Icon,
  label,
  onClick,
  disabled,
}: {
  icon: LucideIcon;
  label: string;
  onClick: () => void;
  disabled?: boolean;
}) {
  return (
    <Button
      type="button"
      variant="ghost"
      size="icon"
      onMouseDown={(e) => e.preventDefault()}
      onClick={onClick}
      disabled={disabled}
      aria-label={label}
      title={label}
      className="size-8"
    >
      <Icon aria-hidden />
    </Button>
  );
}

function TemplateMenu({
  label,
  hasKpis,
  onPick,
  onPicked,
}: {
  label: string;
  hasKpis: boolean;
  onPick: (id: TemplateId) => void;
  onPicked: () => void;
}) {
  const picked = useRef(false);
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button
          type="button"
          variant="outline"
          size="sm"
          className="h-9 max-w-[15rem]"
          aria-label={`Formula template: ${label}`}
        >
          <LayoutTemplate aria-hidden />
          <span className="truncate max-sm:sr-only">{label}</span>
          <ChevronDown aria-hidden />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent
        align="end"
        className="w-72"
        onCloseAutoFocus={(e) => {
          if (!picked.current) return;
          picked.current = false;
          e.preventDefault();
          onPicked();
        }}
      >
        <DropdownMenuLabel>Start from</DropdownMenuLabel>
        {TEMPLATES.map((t) => (
          <DropdownMenuItem
            key={t.id}
            disabled={t.id !== "default" && !hasKpis}
            onSelect={() => {
              picked.current = true;
              onPick(t.id);
            }}
          >
            <span className="flex flex-col">
              <span className="font-medium">{t.label}</span>
              <span className="text-xs text-ink-muted">{t.help}</span>
            </span>
          </DropdownMenuItem>
        ))}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
