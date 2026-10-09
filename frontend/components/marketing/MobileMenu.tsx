"use client";

import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";
import { Menu, X } from "lucide-react";

/**
 * Hamburger + dropdown for the narrow-screen nav. Accessible: aria-expanded/controls, Esc and outside click close it
 * (focus returns to the button), Tab is trapped inside while open, and choosing a link closes it. It is a dropdown
 * under the pill rather than a full-screen sheet, so the page keeps scrolling and no body scroll lock is needed.
 */
export function MobileMenu({ links, startHref }: { links: readonly { href: string; label: string }[]; startHref: string }) {
  const [open, setOpen] = useState(false);
  const btn = useRef<HTMLButtonElement>(null);
  const menu = useRef<HTMLDivElement>(null);

  const close = useCallback((refocus: boolean) => {
    setOpen(false);
    if (refocus) btn.current?.focus();
  }, []);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") return close(true);
      if (e.key !== "Tab") return;
      const items = [btn.current, ...Array.from(menu.current?.querySelectorAll<HTMLElement>("a") ?? [])].filter(
        (x): x is HTMLElement => !!x,
      );
      const first = items[0];
      const last = items[items.length - 1];
      const active = document.activeElement;
      if (e.shiftKey && active === first) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && active === last) {
        e.preventDefault();
        first.focus();
      }
    };
    const onDown = (e: PointerEvent) => {
      const t = e.target as Node;
      if (!menu.current?.contains(t) && !btn.current?.contains(t)) close(false);
    };
    const onResize = () => {
      if (window.innerWidth >= 900) close(false);
    };
    document.addEventListener("keydown", onKey);
    document.addEventListener("pointerdown", onDown);
    window.addEventListener("resize", onResize);
    return () => {
      document.removeEventListener("keydown", onKey);
      document.removeEventListener("pointerdown", onDown);
      window.removeEventListener("resize", onResize);
    };
  }, [open, close]);

  return (
    <>
      <button
        ref={btn}
        type="button"
        className="mk-burger"
        aria-expanded={open}
        aria-controls="mk-menu"
        aria-label={open ? "Close menu" : "Open menu"}
        onClick={() => setOpen((v) => !v)}
      >
        {open ? <X aria-hidden /> : <Menu aria-hidden />}
      </button>
      <div ref={menu} id="mk-menu" className="mk-menu" hidden={!open}>
        <nav aria-label="Mobile">
          {links.map((l) => (
            <a key={l.href} href={l.href} onClick={() => close(false)}>
              {l.label}
            </a>
          ))}
          <Link href="/login" onClick={() => close(false)}>
            Sign in
          </Link>
          <Link href={startHref} onClick={() => close(false)}>
            Get Started
          </Link>
        </nav>
      </div>
    </>
  );
}
