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
  { key: "acceptable", label: "Acceptable", color: "#9E9E9E", min: 7, max: 7.999, icon: Circle },
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
