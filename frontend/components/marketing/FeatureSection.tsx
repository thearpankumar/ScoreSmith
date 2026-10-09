import { ArrowUpRight, ClipboardCheck, Sparkles, Target } from "lucide-react";

const POINTS = [
  { Icon: Target, title: "Focused", body: "Every KPI ties back to a goal." },
  { Icon: ClipboardCheck, title: "Aligned", body: "Teams score against the same scorecard." },
  { Icon: Sparkles, title: "Always improving", body: "AI-assisted evaluation shows what to fix next." },
] as const;

/** "Turn Strategy into Measurable Outcomes": a glass dashboard panel on the left, copy on the right. */
export function FeatureSection() {
  return (
    <section id="outcomes" className="mk-feature" aria-labelledby="mk-h2">
      <div className="mk-feature-art" aria-hidden="true">
        <div className="mk-feature-beam" />
        <div className="mk-feature-panel">
          <div className="mkf-bar">
            <i />
            <i />
            <i />
            <span />
          </div>
          <div className="mkf-body">
            <div className="mkf-side">
              {[0, 1, 2, 3, 4].map((i) => (
                <span key={i} className={i === 0 ? "is-on" : ""} />
              ))}
            </div>
            <div className="mkf-grid">
              {[0, 1, 2, 3, 4, 5].map((i) => (
                <span key={i} />
              ))}
            </div>
          </div>
        </div>
        <div className="mk-chip" role="img" aria-label="Productivity up 42 percent versus last quarter">
          <span className="mk-chip-ico">
            <ArrowUpRight strokeWidth={2.6} />
          </span>
          <span>
            <small>Productivity</small>
            <b>+42%</b>
            <small>vs last quarter</small>
          </span>
        </div>
      </div>
      <div className="mk-feature-copy">
        <p className="mk-eyebrow">Built for every team</p>
        <h2 id="mk-h2" className="mk-h2">
          Turn Strategy into
          <br />
          Measurable <span className="mk-gold-text">Outcomes</span>
        </h2>
        <p className="mk-lead">
          From defining the right KPIs to evaluating performance with AI, our platform helps teams stay focused, aligned
          and always moving forward.
        </p>
        <ul id="why-us" className="mk-points">
          {POINTS.map(({ Icon, title, body }) => (
            <li key={title}>
              <span className="mk-points-ico" aria-hidden>
                <Icon strokeWidth={2.2} />
              </span>
              <span>
                <b>{title}</b> {body}
              </span>
            </li>
          ))}
        </ul>
      </div>
    </section>
  );
}
