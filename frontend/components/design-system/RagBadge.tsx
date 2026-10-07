import { getRagBand, getTargetBand, hexToRgba } from "@/lib/rag";
import { cn } from "@/lib/utils";

export interface RagBadgeProps {
  score: number;
  size?: "sm" | "md";
  showScore?: boolean;
  className?: string;
  /**
   * The scorecard's target score. When given (including `null` = "no target set, use the
   * default"), the badge is coloured and labelled RELATIVE to that target ("Meets target",
   * "Below target"…). Omit it only where the absolute 0-10 band is meant (guideline rungs).
   */
  target?: number | null;
}

/**
 * RAG (red/amber/green) score badge.
 *
 * Colour is never the only signal: every badge pairs the band colour with
 * a distinct icon shape AND a text label (plus the numeric score), per the
 * plan's accessibility rule. The band colour itself is used only as a
 * small swatch dot and a low-alpha background tint — never as the text or
 * icon colour directly — so the badge stays legible (and WCAG-friendlier)
 * even for the lighter bands (e.g. "Good" #66BB6A) which would fail
 * contrast as solid text/icon fill on a light background.
 */
export function RagBadge({ score, size = "md", showScore = true, className, target }: RagBadgeProps) {
  const band = target === undefined ? getRagBand(score) : getTargetBand(score, target);
  const Icon = band.icon;
  const isSm = size === "sm";

  return (
    <span
      className={cn(
        "inline-flex items-center rounded-full font-medium text-ink",
        isSm ? "gap-1 px-2 py-0.5 text-xs" : "gap-1.5 px-2.5 py-1 text-sm",
        className,
      )}
      style={{ backgroundColor: hexToRgba(band.color, 0.14) }}
      title={`${band.label}${showScore ? ` — ${score.toFixed(1)}/10` : ""}`}
    >
      <span
        aria-hidden
        className={cn("shrink-0 rounded-full", isSm ? "size-1.5" : "size-2")}
        style={{ backgroundColor: band.color }}
      />
      <Icon className={isSm ? "size-3" : "size-3.5"} aria-hidden />
      <span>{band.label}</span>
      {showScore && <span className="font-semibold">{score.toFixed(1)}</span>}
    </span>
  );
}
