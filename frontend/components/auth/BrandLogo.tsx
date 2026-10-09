/**
 * The KPI Metrics mark: three fully rounded bars (the middle one tallest) with the third split into a dot and a
 * stem - a bar chart that doubles as an "i". Inline SVG, drawn in the 1672x941 stage's pixel units.
 */
export function BrandMark({ className }: { className?: string }) {
  return (
    <svg
      className={className}
      width="48"
      height="58"
      viewBox="0 0 48 58"
      fill="none"
      aria-hidden="true"
      focusable="false"
    >
      <defs>
        <linearGradient id="bm-a" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" stopColor="#FFB300" />
          <stop offset="0.55" stopColor="#FFC21A" />
          <stop offset="1" stopColor="#FFD54A" />
        </linearGradient>
        <linearGradient id="bm-b" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" stopColor="#F5A300" />
          <stop offset="0.35" stopColor="#FFC21A" />
          <stop offset="0.6" stopColor="#FFCB3D" />
          <stop offset="1" stopColor="#FFD966" />
        </linearGradient>
        <linearGradient id="bm-c" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" stopColor="#FFB300" />
          <stop offset="1" stopColor="#FFD54A" />
        </linearGradient>
      </defs>
      {/* bars are 12px wide: x 0-12.2, 17.5-30, 34.8-46.8 (offset so the mark's left edge is x=0) */}
      <rect x="0" y="3.2" width="12.2" height="53" rx="6.1" fill="url(#bm-a)" />
      <rect x="17.5" y="0" width="12.5" height="56.2" rx="6.25" fill="url(#bm-b)" />
      <rect x="34.8" y="2.4" width="12" height="18.3" rx="6" fill="url(#bm-c)" />
      <rect x="34.8" y="26.2" width="12" height="30" rx="6" fill="url(#bm-c)" />
    </svg>
  );
}

export function BrandLogo() {
  return (
    <div className="auth-brand">
      <BrandMark className="auth-brand-mark" />
      <div className="auth-brand-text">
        <div className="auth-brand-name">KPI Metrics</div>
        <div className="auth-brand-sub">Designer &amp; Evaluator</div>
      </div>
    </div>
  );
}
