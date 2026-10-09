import { BarChart3, Target, Users } from "lucide-react";

const CARDS = [
  { id: "align", Icon: Users, title: "Align Teams", body: "Turn strategy into measurable action across teams." },
  { id: "set", Icon: Target, title: "Set Meaningful KPIs", body: "Design KPIs that align with your goals." },
  { id: "eval", Icon: BarChart3, title: "Evaluate & Improve", body: "Get AI-powered insights and drive real impact." },
] as const;

/**
 * Three glass cards around the dashboard. They hold real copy (read by assistive tech); the tilt and the idle float
 * are transform-only (the blur itself is never animated) and switch off under prefers-reduced-motion. On phones they
 * become a plain stacked list (see marketing.css).
 */
export function FloatingCards() {
  return (
    <ul className="mkc-list">
      {CARDS.map(({ id, Icon, title, body }) => (
        <li key={id} className={`mkc mkc-${id}`}>
          <div className="mkc-float">
            <span className="mkc-ico">
              <Icon strokeWidth={2.2} aria-hidden />
            </span>
            <div>
              <h3>{title}</h3>
              <p>{body}</p>
            </div>
          </div>
        </li>
      ))}
    </ul>
  );
}
