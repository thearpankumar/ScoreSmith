import { BarChart3, ClipboardCheck, FileText, LayoutGrid, PenLine, Users } from "lucide-react";

import { BrandMark } from "@/components/auth/BrandLogo";

/**
 * The hero's isometric glass dashboard. Plain HTML/SVG sized in `em` (the stage sets the font size from its container
 * width), tilted with a single CSS 3D transform. No backdrop-filter lives inside the transformed subtree (it would
 * silently stop blurring), so the "glass" here is layered translucent gradients; the real blurred panels are the
 * floating cards outside it. Decorative: hidden from assistive tech.
 */

const NAV = [
  { label: "Overview", Icon: LayoutGrid, active: true },
  { label: "Designer", Icon: PenLine },
  { label: "Evaluator", Icon: ClipboardCheck },
  { label: "Reports", Icon: FileText },
  { label: "Teams", Icon: Users },
] as const;

const BARS = [0.35, 0.55, 0.45, 0.8, 1];

function MiniBars({ tone = "gold" }: { tone?: "gold" | "grey" }) {
  return (
    <span className={`mkd-minibars mkd-minibars-${tone}`}>
      {BARS.map((h, i) => (
        <i key={i} style={{ height: `${h * 100}%` }} />
      ))}
    </span>
  );
}

export function DashboardMock() {
  return (
    <div className="mkd-scene" aria-hidden="true">
      <div className="mkd-slab" />
      <div className="mkd-device">
        <aside className="mkd-side">
          <div className="mkd-side-brand">
            <BrandMark className="mkd-side-mark" />
            <b>KPI Metrics</b>
          </div>
          {NAV.map(({ label, Icon, ...rest }) => (
            <div key={label} className={`mkd-nav${"active" in rest ? " is-active" : ""}`}>
              <Icon strokeWidth={2} />
              {label}
            </div>
          ))}
        </aside>
        <div className="mkd-main">
          <div className="mkd-row">
            <section className="mkd-card mkd-perf">
              <header>
                <div>
                  <h3>
                    Team Performance
                    <br />
                    on Track
                  </h3>
                  <p>Design, evaluate and improve KPIs with clarity.</p>
                </div>
                <span className="mkd-select">Last 3 Months &#9662;</span>
              </header>
              <div className="mkd-chart">
                <svg viewBox="0 0 300 110" preserveAspectRatio="none" focusable="false">
                  <defs>
                    <linearGradient id="mkd-area" x1="0" y1="0" x2="0" y2="1">
                      <stop offset="0" stopColor="#FFC21A" stopOpacity="0.45" />
                      <stop offset="1" stopColor="#FFC21A" stopOpacity="0" />
                    </linearGradient>
                  </defs>
                  {[22, 52, 82].map((y) => (
                    <line key={y} x1="0" x2="300" y1={y} y2={y} stroke="#e8dfc4" strokeWidth="0.6" strokeDasharray="2 3" />
                  ))}
                  <path
                    d="M0 94 C 14 88, 20 72, 34 74 S 56 94, 72 88 S 94 62, 110 66 S 130 88, 148 78 S 168 48, 190 46 S 214 58, 234 38 S 262 14, 300 8 L300 110 L0 110Z"
                    fill="url(#mkd-area)"
                  />
                  <path
                    d="M0 94 C 14 88, 20 72, 34 74 S 56 94, 72 88 S 94 62, 110 66 S 130 88, 148 78 S 168 48, 190 46 S 214 58, 234 38 S 262 14, 300 8"
                    fill="none"
                    stroke="#F2A900"
                    strokeWidth="2.2"
                    strokeLinecap="round"
                    vectorEffect="non-scaling-stroke"
                  />
                </svg>
                <span className="mkd-dot" />
                <span className="mkd-bubble">
                  <b>+32%</b>
                  Improvement
                </span>
              </div>
            </section>
            <section className="mkd-card mkd-goal">
              <h4>Goal Achievement</h4>
              <div className="mkd-ring">
                <svg viewBox="0 0 100 100" focusable="false">
                  <circle cx="50" cy="50" r="40" fill="none" stroke="#f1eadb" strokeWidth="9" />
                  <circle
                    cx="50"
                    cy="50"
                    r="40"
                    fill="none"
                    stroke="#F5B400"
                    strokeWidth="9"
                    strokeLinecap="round"
                    strokeDasharray="196 252"
                    transform="rotate(-90 50 50)"
                  />
                </svg>
                <b>78%</b>
              </div>
              {[
                ["On Track", "12", "#22b34c"],
                ["At Risk", "4", "#FFB800"],
                ["Delayed", "2", "#ee4b4b"],
              ].map(([label, n, c]) => (
                <div key={label} className="mkd-leg">
                  <i style={{ background: c }} />
                  <span>{label}</span>
                  <b>{n}</b>
                </div>
              ))}
            </section>
          </div>
          <div className="mkd-tiles">
            <div className="mkd-card mkd-tile">
              <span className="mkd-tile-ico mkd-ico-gold">
                <BarChart3 strokeWidth={2.4} />
              </span>
              <small>Total KPIs</small>
              <b>124</b>
              <em>&#8593; 12%</em>
              <MiniBars />
            </div>
            <div className="mkd-card mkd-tile">
              <span className="mkd-tile-ico mkd-ico-blue">
                <Users strokeWidth={2.4} />
              </span>
              <small>Active Teams</small>
              <b>18</b>
              <em>&#8593; 3%</em>
              <MiniBars tone="grey" />
            </div>
            <div className="mkd-card mkd-tile">
              <span className="mkd-tile-ico mkd-ico-ring" />
              <small>Avg. Performance</small>
              <b>78%</b>
              <em>&#8593; 5%</em>
              <MiniBars />
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
