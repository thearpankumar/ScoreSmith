"use client";

import { useRef, type MouseEvent } from "react";
import { ChevronDown, Hash, SquareFunction } from "lucide-react";

import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { FUNCTIONS } from "@/lib/formula-tokens";

export type PaletteKey = "+" | "-" | "*" | "/" | "**" | "(" | ")" | ",";

const KEYS: { key: PaletteKey; label: string; name: string; hint?: string }[] = [
  { key: "+", label: "+", name: "Plus" },
  { key: "-", label: "−", name: "Minus" },
  { key: "*", label: "×", name: "Multiply" },
  { key: "/", label: "÷", name: "Divide" },
  { key: "**", label: "xʸ", name: "Power", hint: "Raise to a power (written **)" },
  { key: "(", label: "(", name: "Open bracket" },
  { key: ")", label: ")", name: "Close bracket" },
  { key: ",", label: ",", name: "Comma", hint: "Separates a function's values" },
];

// Palette buttons don't take focus on click, so the formula keeps its focus and visible caret.
const keepFocus = (e: MouseEvent) => e.preventDefault();

/** Operator keys, the Functions menu and the number key. Everything inserts at the formula's caret. */
export function FormulaPalette({
  onKey,
  onFunction,
  onNumber,
  onMenuClosed,
}: {
  onKey: (key: PaletteKey) => void;
  onFunction: (name: string) => void;
  onNumber: () => void;
  /** Where focus goes after a function is picked (the formula, not back to the menu button). */
  onMenuClosed: () => void;
}) {
  const picked = useRef(false);
  return (
    <div className="flex flex-wrap items-center gap-1.5" role="toolbar" aria-label="Insert into the formula">
      {KEYS.map((k) => (
        <button
          key={k.key}
          type="button"
          onMouseDown={keepFocus}
          onClick={() => onKey(k.key)}
          aria-label={k.name}
          title={k.hint ?? k.name}
          className="flex h-9 min-w-9 items-center justify-center rounded-lg border border-hairline bg-solid px-2 font-mono text-base font-semibold text-ink shadow-elev-1 transition-colors hover:border-lemon-ink hover:bg-lemon-soft focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-[var(--focus)]"
        >
          {k.label}
        </button>
      ))}
      <Button
        type="button"
        variant="outline"
        size="sm"
        onMouseDown={keepFocus}
        onClick={onNumber}
        className="h-9 rounded-lg"
        title="Insert a number"
      >
        <Hash aria-hidden />
        Number
      </Button>
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <Button type="button" variant="outline" size="sm" className="h-9 rounded-lg">
            <SquareFunction aria-hidden />
            Functions
            <ChevronDown aria-hidden />
          </Button>
        </DropdownMenuTrigger>
        <DropdownMenuContent
          align="start"
          className="w-64"
          onCloseAutoFocus={(e) => {
            // Escape / outside click: focus goes back to the menu button as usual.
            if (!picked.current) return;
            picked.current = false;
            e.preventDefault();
            onMenuClosed();
          }}
        >
          <DropdownMenuLabel>Functions</DropdownMenuLabel>
          {FUNCTIONS.map((fn) => (
            <DropdownMenuItem
              key={fn.name}
              onSelect={() => {
                picked.current = true;
                onFunction(fn.name);
              }}
            >
              <code className="mt-px w-14 shrink-0 font-mono text-xs font-semibold text-[#6d28d9]">{fn.name}( )</code>
              <span className="flex flex-col">
                <span className="font-medium">{fn.label}</span>
                <span className="text-xs text-ink-muted">{fn.help}</span>
              </span>
            </DropdownMenuItem>
          ))}
        </DropdownMenuContent>
      </DropdownMenu>
    </div>
  );
}
