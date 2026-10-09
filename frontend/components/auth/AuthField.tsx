"use client";

import { useState, type ComponentType, type InputHTMLAttributes, type Ref } from "react";
import { Eye, EyeOff } from "lucide-react";

type IconType = ComponentType<{ className?: string; strokeWidth?: number; "aria-hidden"?: boolean }>;

/**
 * One glass input of the sign-in forms: leading icon, text input, optional show/hide toggle for passwords.
 * The visible placeholder doubles as the design's label; a visually hidden <label> gives screen readers the
 * accessible name.
 */
export function AuthField({
  id,
  label,
  icon: Icon,
  invalid,
  inputRef,
  reveal = false,
  ...input
}: {
  id: string;
  label: string;
  icon: IconType;
  invalid?: boolean;
  inputRef?: Ref<HTMLInputElement>;
  /** Adds the eye toggle (password fields). */
  reveal?: boolean;
} & Omit<InputHTMLAttributes<HTMLInputElement>, "id" | "type"> & { type?: string }) {
  const [shown, setShown] = useState(false);
  const type = reveal ? (shown ? "text" : "password") : (input.type ?? "text");
  return (
    <div className="auth-field" data-invalid={invalid ? "true" : undefined}>
      <label htmlFor={id} className="sr-only">
        {label}
      </label>
      <Icon className="auth-field-icon" strokeWidth={1.7} aria-hidden />
      <input
        {...input}
        id={id}
        ref={inputRef}
        type={type}
        placeholder={label}
        aria-invalid={invalid ? true : undefined}
        className="auth-input"
      />
      {reveal && (
        <button
          type="button"
          className="auth-reveal"
          onClick={() => setShown((v) => !v)}
          aria-label={shown ? "Hide password" : "Show password"}
          aria-pressed={shown}
        >
          {shown ? <Eye strokeWidth={1.7} aria-hidden /> : <EyeOff strokeWidth={1.7} aria-hidden />}
        </button>
      )}
    </div>
  );
}
