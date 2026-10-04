"use client";

import { useId, useRef, useState, type DragEvent, type KeyboardEvent, type ReactNode } from "react";
import { UploadCloud } from "lucide-react";

import { cn } from "@/lib/utils";

/** Keyboard-accessible drag-and-drop target that also opens the native picker on click / Enter / Space. */
export function Dropzone({
  accept,
  multiple,
  onFiles,
  disabled,
  title,
  hint,
  icon,
  compact,
}: {
  accept: string;
  multiple?: boolean;
  onFiles: (files: File[]) => void;
  disabled?: boolean;
  title: string;
  hint: string;
  icon?: ReactNode;
  compact?: boolean;
}) {
  const inputRef = useRef<HTMLInputElement>(null);
  const hintId = useId();
  const [over, setOver] = useState(false);

  const open = () => {
    if (!disabled) inputRef.current?.click();
  };
  const onKey = (e: KeyboardEvent) => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      open();
    }
  };
  const onDrop = (e: DragEvent) => {
    e.preventDefault();
    setOver(false);
    if (disabled) return;
    const files = Array.from(e.dataTransfer.files);
    if (files.length) onFiles(multiple ? files : files.slice(0, 1));
  };

  return (
    <div
      role="button"
      tabIndex={disabled ? -1 : 0}
      aria-disabled={disabled}
      aria-label={title}
      aria-describedby={hintId}
      onClick={open}
      onKeyDown={onKey}
      onDragOver={(e) => {
        e.preventDefault();
        if (!disabled) setOver(true);
      }}
      onDragLeave={() => setOver(false)}
      onDrop={onDrop}
      className={cn(
        "flex cursor-pointer flex-col items-center justify-center gap-2 rounded-2xl border-2 border-dashed px-4 text-center transition-colors",
        compact ? "py-6" : "py-10 sm:py-14",
        over ? "border-ink bg-lemon-soft" : "border-hairline bg-solid/60 hover:border-ink-muted hover:bg-solid",
        disabled && "cursor-not-allowed opacity-60",
        "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--focus)]",
      )}
    >
      <span className="flex size-12 items-center justify-center rounded-full bg-lemon text-lemon-ink">
        {icon ?? <UploadCloud className="size-6" aria-hidden />}
      </span>
      <p className="text-sm font-semibold text-ink">{title}</p>
      <p id={hintId} className="max-w-md text-xs text-ink-muted">
        {hint}
      </p>
      <input
        ref={inputRef}
        type="file"
        className="sr-only"
        tabIndex={-1}
        aria-hidden
        accept={accept}
        multiple={multiple}
        disabled={disabled}
        onClick={(e) => e.stopPropagation()}
        onChange={(e) => {
          const files = Array.from(e.target.files ?? []);
          e.target.value = "";
          if (files.length) onFiles(files);
        }}
      />
    </div>
  );
}
