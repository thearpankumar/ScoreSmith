const STATS = [
  { value: "500+", label: "Teams" },
  { value: "1M+", label: "KPIs Tracked" },
  { value: "98%", label: "User Satisfaction" },
] as const;

/** The marketing column of the sign-in pages: headline, tagline and the three headline numbers. */
export function HeroCopy() {
  return (
    <>
      <h1 className="auth-headline">
        <span>From</span>
        <span>Strategy</span>
        <span>to Measurable</span>
        <span className="auth-headline-accent">Impact</span>
      </h1>
      <p className="auth-tagline">
        <span>Design</span>
        <i aria-hidden="true">·</i>
        <span>Track</span>
        <i aria-hidden="true">·</i>
        <span>Evaluate</span>
        <i aria-hidden="true">·</i>
        <span>Improve</span>
      </p>
      <ul className="auth-stats">
        {STATS.map((s) => (
          <li key={s.label} className="auth-stat">
            <span className="auth-stat-value">{s.value}</span>
            <span className="auth-stat-label">{s.label}</span>
          </li>
        ))}
      </ul>
    </>
  );
}
