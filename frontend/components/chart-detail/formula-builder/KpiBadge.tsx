import { kpiMonogram, kpiTint, type FormulaKpi } from "@/lib/formula-kpis";
import { cn } from "@/lib/utils";

/** A KPI's monogram ("LD6", "CR") on its family colour. Decorative: the KPI name is always next to it. */
export function KpiBadge({ kpi, size = "md" }: { kpi: Pick<FormulaKpi, "name" | "path">; size?: "sm" | "md" }) {
  const tint = kpiTint(kpi);
  const mono = kpiMonogram(kpi.name);
  return (
    <span
      aria-hidden
      className={cn(
        "inline-flex shrink-0 items-center justify-center font-semibold leading-none tabular-nums",
        size === "md" ? "size-8 rounded-lg text-[11px]" : "size-5 rounded-md text-[8.5px]",
        mono.length > 3 && (size === "md" ? "text-[9.5px]" : "text-[7px]"),
      )}
      style={{ backgroundColor: tint.bg, color: tint.fg }}
    >
      {mono}
    </span>
  );
}
