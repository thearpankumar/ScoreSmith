"use client";

import { useId } from "react";

import { getRagBand, hexToRgba } from "@/lib/rag";
import { cn } from "@/lib/utils";

const LEVELS = [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10];

/**
 * 0-10 anchored score picker: an NPS-style horizontal strip of 11 pills.
 *
 * Built on native `<input type="radio">` elements (visually hidden, each wrapped by its
 * pill `<label>`) inside a `<fieldset>`/`<legend>`, so keyboard and screen-reader
 * behaviour comes from the platform: Tab enters the group, arrow keys move between
 * levels, and the selection is announced — per the WAI-ARIA APG radio-group pattern.
 * A discrete pill strip (rather than a slider) because every level is a distinct,
 * named guideline anchor the evaluator should be able to see and hit directly.
 *
 * Each pill carries its RAG band colour as a swatch bar (never colour alone — the
 * number is always the label), and the selected pill gets a band-tinted fill and a
 * band-coloured ring.
 */
export function ScorePicker({
  label,
  value,
  onChange,
  disabled,
  describedBy,
}: {
  label: string;
  value: number | null;
  onChange: (score: number) => void;
  disabled?: boolean;
  describedBy?: string;
}) {
  const name = useId();

  return (
    <fieldset className="min-w-0" disabled={disabled} aria-describedby={describedBy}>
      <legend className="sr-only">{label}</legend>
      <div className="grid grid-cols-11 gap-1" data-score-picker>
        {LEVELS.map((level) => {
          const band = getRagBand(level);
          const selected = value === level;
          return (
            <label
              key={level}
              title={`${level} — ${band.label}`}
              className={cn(
                "relative flex h-10 cursor-pointer select-none flex-col items-center justify-center overflow-hidden rounded-lg border text-sm tabular-nums transition-colors",
                "has-[:focus-visible]:outline-2 has-[:focus-visible]:outline-offset-2 has-[:focus-visible]:outline-[var(--focus)]",
                selected ? "font-bold text-ink" : "border-hairline bg-solid text-ink-muted hover:bg-bg hover:text-ink",
                disabled && "cursor-not-allowed opacity-60",
              )}
              style={
                selected
                  ? { backgroundColor: hexToRgba(band.color, 0.2), borderColor: band.color, boxShadow: `0 0 0 1px ${band.color}` }
                  : undefined
              }
            >
              <input
                type="radio"
                name={name}
                value={level}
                checked={selected}
                onChange={() => onChange(level)}
                className="sr-only"
                aria-label={`${level} — ${band.label}`}
              />
              <span>{level}</span>
              <span aria-hidden className="absolute inset-x-0 bottom-0 h-1" style={{ backgroundColor: band.color }} />
            </label>
          );
        })}
      </div>
    </fieldset>
  );
}
