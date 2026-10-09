import Link from "next/link";
import { ArrowRight, Check } from "lucide-react";
import type { ReactNode } from "react";

/** Yellow call-to-action link. */
export function CtaLink({ href, children, large }: { href: string; children: ReactNode; large?: boolean }) {
  return (
    <Link href={href} className={`mk-btn mk-btn-gold${large ? " mk-btn-lg" : ""}`}>
      {children}
      <ArrowRight className="mk-btn-icon" aria-hidden />
    </Link>
  );
}

export function Perks() {
  return (
    <ul className="mk-perks">
      {["Easy KPI creation", "AI-powered insights", "Built for modern teams"].map((p) => (
        <li key={p}>
          <span className="mk-check" aria-hidden>
            <Check strokeWidth={3} />
          </span>
          {p}
        </li>
      ))}
    </ul>
  );
}

/** Decorative background: soft glow plus a big translucent golden ribbon. Pure SVG, no raster, no blur filters. */
export function Swoosh() {
  return (
    <div className="mk-bg" aria-hidden="true">
      <svg className="mk-swoosh mk-swoosh-r" viewBox="0 0 600 900" preserveAspectRatio="xMaxYMid slice" focusable="false">
        <defs>
          <linearGradient id="mk-sw1" x1="0" y1="0" x2="1" y2="1">
            <stop offset="0" stopColor="#FFE680" stopOpacity="0.85" />
            <stop offset="0.5" stopColor="#FFC82E" stopOpacity="0.55" />
            <stop offset="1" stopColor="#FFB300" stopOpacity="0.35" />
          </linearGradient>
          <linearGradient id="mk-sw2" x1="1" y1="0" x2="0" y2="1">
            <stop offset="0" stopColor="#FFF3B0" stopOpacity="0.9" />
            <stop offset="1" stopColor="#FFC82E" stopOpacity="0.2" />
          </linearGradient>
        </defs>
        <path d="M600 40 C 470 90, 420 220, 470 330 C 520 440, 600 470, 600 470 L600 640 C 440 600, 300 470, 330 300 C 355 160, 470 60, 600 40Z" fill="url(#mk-sw1)" />
        <path d="M600 120 C 520 150, 490 230, 520 300 C 545 360, 600 380, 600 380 L600 430 C 500 410, 430 340, 450 270 C 470 190, 540 130, 600 120Z" fill="url(#mk-sw2)" />
      </svg>
      <svg className="mk-swoosh mk-swoosh-l" viewBox="0 0 300 500" preserveAspectRatio="xMinYMid slice" focusable="false">
        <path d="M0 120 C 90 160, 150 230, 120 330 C 100 395, 40 430, 0 440Z" fill="url(#mk-sw1)" />
      </svg>
    </div>
  );
}
