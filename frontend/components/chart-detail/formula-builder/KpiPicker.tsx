"use client";

import {
  memo,
  useDeferredValue,
  useEffect,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent,
  type RefObject,
} from "react";
import { useDraggable } from "@dnd-kit/core";
import { ArrowDownUp, ChevronDown, ListPlus, Plus, Search, X } from "lucide-react";

import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuLabel,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { highlight, KPI_SORTS, visibleKpis, type FormulaKpi, type KpiFilter, type KpiSort } from "@/lib/formula-kpis";
import { cn } from "@/lib/utils";
import { KpiBadge } from "./KpiBadge";

const FILTERS: { id: KpiFilter; label: string }[] = [
  { id: "all", label: "All" },
  { id: "used", label: "Used" },
  { id: "unused", label: "Unused" },
];

const LIST_ID = "formula-kpi-list";

/**
 * The left pane: every KPI the formula can reference, searchable, filterable by whether the formula
 * uses it, sortable. A row's + adds the KPI at the formula's caret; a row can also be dragged into the
 * formula. Keyboard: one Tab stop for the whole list, arrow keys / Home / End move between rows, Enter
 * in the search box adds the first match, ArrowDown moves from the search box into the list.
 */
export function KpiPicker({
  kpis,
  usage,
  query,
  onQuery,
  filter,
  onFilter,
  sort,
  onSort,
  onInsert,
  onInsertMany,
  searchRef,
  dragEnabled,
}: {
  kpis: FormulaKpi[];
  usage: Map<string, number>;
  query: string;
  onQuery: (q: string) => void;
  filter: KpiFilter;
  onFilter: (f: KpiFilter) => void;
  sort: KpiSort;
  onSort: (s: KpiSort) => void;
  onInsert: (name: string) => void;
  onInsertMany: (names: string[]) => void;
  searchRef: RefObject<HTMLInputElement | null>;
  dragEnabled: boolean;
}) {
  const deferredQuery = useDeferredValue(query);
  const visible = useMemo(
    () => visibleKpis(kpis, { query: deferredQuery, filter, sort }, usage),
    [kpis, deferredQuery, filter, sort, usage],
  );
  const usedCount = useMemo(() => kpis.filter((k) => (usage.get(k.name) ?? 0) > 0).length, [kpis, usage]);
  const counts: Record<KpiFilter, number> = { all: kpis.length, used: usedCount, unused: kpis.length - usedCount };
  const [active, setActive] = useState(0);
  const activeIndex = Math.min(active, Math.max(0, visible.length - 1));
  const listRef = useRef<HTMLUListElement>(null);

  // A new search, filter or sort starts the list from the top.
  useEffect(() => {
    listRef.current?.scrollTo({ top: 0 });
    setActive(0);
  }, [deferredQuery, filter, sort]);

  function focusRow(i: number) {
    const buttons = listRef.current?.querySelectorAll<HTMLButtonElement>("[data-kpi-insert]");
    if (!buttons?.length) return;
    const next = Math.max(0, Math.min(i, buttons.length - 1));
    setActive(next);
    buttons[next].focus();
  }

  function onListKey(e: KeyboardEvent<HTMLUListElement>) {
    const moves: Record<string, number> = {
      ArrowDown: activeIndex + 1,
      ArrowUp: activeIndex - 1,
      PageDown: activeIndex + 8,
      PageUp: activeIndex - 8,
      Home: 0,
      End: visible.length - 1,
    };
    if (!(e.key in moves)) return;
    e.preventDefault();
    if (e.key === "ArrowUp" && activeIndex === 0) searchRef.current?.focus();
    else focusRow(moves[e.key]);
  }

  function onSearchKey(e: KeyboardEvent<HTMLInputElement>) {
    if (e.key === "Enter") {
      e.preventDefault();
      if (visible[0]) onInsert(visible[0].name);
    } else if (e.key === "ArrowDown") {
      e.preventDefault();
      focusRow(0);
    }
  }

  const sortLabel = KPI_SORTS.find((s) => s.id === sort)?.label ?? "Sort";
  let lastGroup: string | null = null;

  return (
    <section aria-label="KPIs" className="@container flex min-h-0 flex-1 flex-col gap-2.5">
      <div className="relative">
        <Search
          className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-ink-muted"
          aria-hidden
        />
        <input
          ref={searchRef}
          type="search"
          value={query}
          onChange={(e) => onQuery(e.target.value)}
          onKeyDown={onSearchKey}
          placeholder="Search KPIs or categories…"
          aria-label="Search KPIs"
          aria-controls={LIST_ID}
          autoComplete="off"
          spellCheck={false}
          className="h-10 w-full rounded-xl border border-hairline bg-solid pl-9 pr-9 text-sm text-ink placeholder:text-ink-muted focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-[var(--focus)] [&::-webkit-search-cancel-button]:hidden"
        />
        {query && (
          <button
            type="button"
            onClick={() => {
              onQuery("");
              searchRef.current?.focus();
            }}
            aria-label="Clear search"
            className="absolute right-2 top-1/2 -translate-y-1/2 rounded-full p-1 text-ink-muted hover:bg-black/5 hover:text-ink"
          >
            <X className="size-4" aria-hidden />
          </button>
        )}
      </div>

      <div role="group" aria-label="Show KPIs" className="grid grid-cols-3 gap-1 rounded-xl bg-black/[0.04] p-1">
        {FILTERS.map((f) => (
          <button
            key={f.id}
            type="button"
            aria-pressed={filter === f.id}
            onClick={() => onFilter(f.id)}
            className={cn(
              "flex min-w-0 items-center justify-center gap-1.5 rounded-lg px-1.5 py-1.5 text-xs font-medium transition-colors",
              filter === f.id ? "bg-solid text-ink shadow-elev-1" : "text-ink-muted hover:text-ink",
            )}
          >
            {f.label}
            <span
              className={cn(
                "rounded-full px-1.5 py-px text-[11px] tabular-nums",
                filter === f.id ? "bg-lemon-soft text-lemon-ink" : "bg-black/[0.06]",
              )}
            >
              {counts[f.id]}
            </span>
          </button>
        ))}
      </div>

      <div className="flex items-center justify-between gap-2">
        <p id="formula-kpi-heading" className="min-w-0 truncate text-sm font-semibold text-ink">
          KPIs{" "}
          <span className="font-normal text-ink-muted">
            ({visible.length}
            {visible.length !== kpis.length ? ` of ${kpis.length}` : ""})
          </span>
        </p>
        <div className="flex shrink-0 items-center gap-1.5">
          <Button
            type="button"
            variant="outline"
            size="sm"
            onClick={() => onInsertMany(visible.map((k) => k.name))}
            disabled={visible.length === 0}
            aria-label={`Add all ${visible.length} KPIs shown to the formula`}
            title="Adds every KPI in the list at the caret, joined by + (or by commas inside a function)"
          >
            <ListPlus aria-hidden />
            <span className="@max-sm:hidden">Add all {visible.length}</span>
          </Button>
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <Button type="button" variant="outline" size="sm" aria-label={`Sort KPIs: ${sortLabel}`}>
                <ArrowDownUp aria-hidden />
                <span className="@max-xs:hidden">{sortLabel}</span>
                <ChevronDown aria-hidden />
              </Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end">
              <DropdownMenuLabel>Sort KPIs</DropdownMenuLabel>
              <DropdownMenuRadioGroup value={sort} onValueChange={(v) => onSort(v as KpiSort)}>
                {KPI_SORTS.map((s) => (
                  <DropdownMenuRadioItem key={s.id} value={s.id}>
                    {s.label}
                  </DropdownMenuRadioItem>
                ))}
              </DropdownMenuRadioGroup>
            </DropdownMenuContent>
          </DropdownMenu>
        </div>
      </div>

      <ul
        id={LIST_ID}
        ref={listRef}
        aria-labelledby="formula-kpi-heading"
        onKeyDown={onListKey}
        className="thin-scrollbar min-h-0 flex-1 overflow-y-auto overscroll-contain rounded-xl border border-hairline bg-solid"
      >
        {visible.length === 0 ? (
          <li className="px-4 py-8 text-center text-sm text-ink-muted">
            {query ? (
              <>
                No KPI matches “{query}”.{" "}
                <button
                  type="button"
                  onClick={() => onQuery("")}
                  className="font-medium text-ink underline underline-offset-2"
                >
                  Clear the search
                </button>
              </>
            ) : filter === "used" ? (
              "No KPI is in the formula yet."
            ) : filter === "unused" ? (
              "Every KPI is in the formula."
            ) : (
              "This scorecard has no scored KPIs yet."
            )}
          </li>
        ) : (
          visible.flatMap((kpi, i) => {
            const group = sort === "category" ? kpi.path.join(" › ") || "No category" : null;
            const header =
              group !== null && group !== lastGroup ? (
                <li
                  key={`group:${group}`}
                  role="presentation"
                  className="sticky top-0 z-10 border-b border-hairline bg-bg/95 px-3 py-1 text-[11px] font-semibold uppercase tracking-wide text-ink-muted backdrop-blur-sm"
                >
                  {group}
                </li>
              ) : null;
            lastGroup = group;
            const row = (
              <KpiRow
                key={kpi.name}
                kpi={kpi}
                index={i}
                uses={usage.get(kpi.name) ?? 0}
                query={deferredQuery}
                tabbable={i === activeIndex}
                onInsert={onInsert}
                onFocusRow={setActive}
                dragEnabled={dragEnabled}
                showPath={sort !== "category"}
              />
            );
            return header ? [header, row] : [row];
          })
        )}
      </ul>
    </section>
  );
}

const KpiRow = memo(function KpiRow({
  kpi,
  index,
  uses,
  query,
  tabbable,
  onInsert,
  onFocusRow,
  dragEnabled,
  showPath,
}: {
  kpi: FormulaKpi;
  index: number;
  uses: number;
  query: string;
  tabbable: boolean;
  onInsert: (name: string) => void;
  onFocusRow: (index: number) => void;
  dragEnabled: boolean;
  showPath: boolean;
}) {
  const { setNodeRef, listeners, isDragging } = useDraggable({
    id: `kpi:${kpi.name}`,
    data: { type: "kpi", name: kpi.name },
    disabled: !dragEnabled,
  });
  return (
    <li
      ref={setNodeRef}
      {...listeners}
      className={cn(
        "formula-kpi-row flex select-none items-center gap-2.5 border-b border-hairline px-2.5 py-2 transition-colors [-webkit-touch-callout:none] last:border-b-0 hover:bg-lemon-soft/40",
        dragEnabled && "cursor-grab active:cursor-grabbing",
        isDragging && "opacity-40",
      )}
    >
      <KpiBadge kpi={kpi} />
      <div className="min-w-0 flex-1">
        <p className="line-clamp-2 break-words text-sm leading-snug text-ink" title={kpi.name}>
          {highlight(kpi.name, query).map((part, i) =>
            part.hit ? (
              <mark key={i} className="rounded-sm bg-lemon-soft px-px text-lemon-ink">
                {part.text}
              </mark>
            ) : (
              <span key={i}>{part.text}</span>
            ),
          )}
        </p>
        {showPath && kpi.path.length > 0 && (
          <p className="truncate text-[11px] text-ink-muted @max-xs:hidden">{kpi.path.join(" › ")}</p>
        )}
      </div>
      <span
        className={cn(
          "shrink-0 whitespace-nowrap rounded-full px-2 py-0.5 text-[11px] font-medium tabular-nums @max-3xs:hidden",
          uses > 0 ? "bg-[#dbeafe] text-[#1e40af]" : "bg-black/[0.05] text-ink-muted",
        )}
      >
        {uses} used
      </span>
      <button
        type="button"
        data-kpi-insert
        tabIndex={tabbable ? 0 : -1}
        onFocus={() => onFocusRow(index)}
        onClick={() => onInsert(kpi.name)}
        aria-label={`Add “${kpi.name}” to the formula${uses > 0 ? `, used ${uses} ${uses === 1 ? "time" : "times"}` : ""}`}
        title="Add to the formula at the caret"
        className="flex size-8 shrink-0 items-center justify-center rounded-full border border-hairline bg-solid text-ink transition-colors hover:border-lemon-ink hover:bg-lemon-soft focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-[var(--focus)]"
      >
        <Plus className="size-4" aria-hidden />
      </button>
    </li>
  );
});
