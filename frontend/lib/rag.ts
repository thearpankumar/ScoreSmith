import {
  CircleCheckBig,
  ThumbsUp,
  Circle,
  TriangleAlert,
  CircleAlert,
  CircleX,
  OctagonAlert,
  type LucideIcon,
} from "lucide-react";
import type { RagBandKey } from "./types";

/**
 * RAG (red/amber/green) score-band palette.
 *
 * This is deliberately a SEPARATE palette from the brand lemon yellow
 * (`--lemon`) per the design system rule in the plan: amber sits visually
 * close to lemon and mixing the two palettes would make brand chrome look
 * like a quality signal. Every band also carries a distinct icon shape and
 * text label so colour is never the only differentiator (colour-blindness /
 * accessibility rule from the plan).
 */
export interface RagBand {
  key: RagBandKey;
  label: string;
  color: string;
  min: number;
  max: number;
  icon: LucideIcon;
}

export const RAG_BANDS: RagBand[] = [
  { key: "excellent", label: "Excellent", color: "#1B5E20", min: 9, max: 10, icon: CircleCheckBig },
  { key: "good", label: "Good", color: "#66BB6A", min: 8, max: 8.999, icon: ThumbsUp },
  { key: "acceptable", label: "Acceptable", color: "#9CCC65", min: 7, max: 7.999, icon: Circle },
  { key: "needs-improvement", label: "Needs Improvement", color: "#F9A825", min: 6, max: 6.999, icon: TriangleAlert },
  { key: "weak", label: "Weak", color: "#E65100", min: 5, max: 5.999, icon: CircleAlert },
  { key: "poor", label: "Poor", color: "#D32F2F", min: 4, max: 4.999, icon: CircleX },
  { key: "critical", label: "Critical", color: "#7F0000", min: 0, max: 3.999, icon: OctagonAlert },
];

/**
 * Threshold lookup identical to the backend's `rag_band_for_score`
 * (backend/app/models/enums.py): the first band (highest first) whose `min` the score
 * reaches. Using `>= min` only (not a `<= max` window) avoids gaps such as 8.9995
 * falling between "Good" (max 8.999) and "Excellent" (min 9).
 */
export function getRagBand(score: number): RagBand {
  const clamped = Math.min(10, Math.max(0, score));
  return RAG_BANDS.find((band) => clamped >= band.min) ?? RAG_BANDS[RAG_BANDS.length - 1];
}

export function getRagBandByKey(key: RagBandKey): RagBand {
  return RAG_BANDS.find((band) => band.key === key) ?? RAG_BANDS[RAG_BANDS.length - 1];
}

/** Convert a "#RRGGBB" hex colour + alpha into an rgba() string. */
export function hexToRgba(hex: string, alpha: number): string {
  const normalized = hex.replace("#", "");
  const r = parseInt(normalized.substring(0, 2), 16);
  const g = parseInt(normalized.substring(2, 4), 16);
  const b = parseInt(normalized.substring(4, 6), 16);
  return `rgba(${r}, ${g}, ${b}, ${alpha})`;
}

// ---------------------------------------------------------------------------
// Target-relative bands
// ---------------------------------------------------------------------------

/**
 * Target-relative score bands. The absolute 0-10 bands above stay for the ScorePicker
 * (its pills are fixed guideline rungs); everything that reports a scorecard's *result*
 * is coloured against that scorecard's target score instead, so a scorecard with a low
 * target (e.g. 4) shows 4.0 as "Meets target", not as a red absolute score.
 *
 * Maths (mirrored by backend/app/reporting/palette.py and checked against the shared
 * fixture backend/tests/data/target_band_cases.json), with T the effective target:
 *   exceeds    score >= min(1.10 T, (T + 10) / 2)   (only when that is above "meets")
 *   meets      score >= T
 *   near       score >= 0.90 T
 *   below      score >= 0.75 T
 *   well_below score >= 0.50 T
 *   critical   otherwise
 * Score and cut-offs are rounded half-up to 2 decimals; every cut-off is inclusive.
 */
export type TargetBandKey = "exceeds" | "meets" | "near" | "below" | "well_below" | "critical";

export interface TargetBand {
  key: TargetBandKey;
  label: string;
  /** Solid colour: swatch dots, tick marks, tab-style accents. */
  color: string;
  /** Pale tint: cell / chart-area fills where dark text must stay readable. */
  tint: string;
  icon: LucideIcon;
}

export const TARGET_BANDS: TargetBand[] = [
  { key: "exceeds", label: "Exceeds target", color: "#1B5E20", tint: "#A5D6A7", icon: CircleCheckBig },
  { key: "meets", label: "Meets target", color: "#66BB6A", tint: "#C8E6C9", icon: ThumbsUp },
  { key: "near", label: "Near target", color: "#F9A825", tint: "#FFE9A8", icon: TriangleAlert },
  { key: "below", label: "Below target", color: "#E65100", tint: "#FFCC9C", icon: CircleAlert },
  { key: "well_below", label: "Well below target", color: "#D32F2F", tint: "#F6B5B0", icon: CircleX },
  { key: "critical", label: "Critical", color: "#7F0000", tint: "#D99A9A", icon: OctagonAlert },
];

/** Used when a scorecard has no (or a non-positive) target — the app-wide default. */
export const DEFAULT_TARGET_SCORE = 7;

/** The target actually used for colouring: the stored one if it is above 0, else 7. */
export function effectiveTarget(target: number | null | undefined): number {
  return typeof target === "number" && Number.isFinite(target) && target > 0 ? target : DEFAULT_TARGET_SCORE;
}

/** Round half-up to 2 decimals on the decimal representation (matches Python's
 * `Decimal.quantize(ROUND_HALF_UP)`), so 6.995 -> 7.00 and 4.125 -> 4.13 despite binary floats. */
function round2(x: number): number {
  const s = Math.abs(x).toFixed(12);
  const [intPart, frac] = s.split(".");
  const cents = Number(intPart) * 100 + Number(frac.slice(0, 2));
  const up = Number(frac[2]) >= 5 ? 1 : 0;
  return ((cents + up) / 100) * (x < 0 ? -1 : 1);
}

export interface TargetBandThreshold {
  band: TargetBand;
  /** Inclusive lower bound for the band (0 for "critical"). */
  min: number;
}

/**
 * The band cut-offs for a target, highest band first. "Exceeds" is omitted when it
 * would not sit above "meets" (a target of 10 leaves no head-room).
 */
export function targetBandThresholds(target: number | null | undefined): TargetBandThreshold[] {
  const t = effectiveTarget(target);
  const meets = round2(t);
  const exceeds = round2(Math.min(t * 1.1, (t + 10) / 2));
  const mins: Record<TargetBandKey, number | null> = {
    exceeds: exceeds > meets ? exceeds : null,
    meets,
    near: round2(t * 0.9),
    below: round2(t * 0.75),
    well_below: round2(t * 0.5),
    critical: 0,
  };
  return TARGET_BANDS.filter((b) => mins[b.key] !== null).map((band) => ({ band, min: mins[band.key] as number }));
}

/** The target-relative band a score falls in. Same inclusive, half-up rules as the backend. */
export function getTargetBand(score: number, target: number | null | undefined): TargetBand {
  const s = round2(Math.min(10, Math.max(0, score)));
  const hit = targetBandThresholds(target).find((th) => s >= th.min);
  return (hit ?? { band: TARGET_BANDS[TARGET_BANDS.length - 1] }).band;
}

/** One-line description of the cut-offs, e.g. "Meets ≥ 7.0 · Near ≥ 6.3 · Below ≥ 5.3 · Well below ≥ 3.5". */
export function describeTargetThresholds(target: number | null | undefined): string {
  return targetBandThresholds(target)
    .filter((th) => th.band.key !== "critical" && th.band.key !== "exceeds")
    .map((th) => `${th.band.label.replace(" target", "")} ≥ ${th.min.toFixed(1)}`)
    .join(" · ");
}
