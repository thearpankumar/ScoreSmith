import { SOLIDS, type Pt, type Solid } from "./iso-bars-data";

/**
 * The translucent golden "glass" bars and cube of the sign-in art, as inline SVG in the 1672x941 stage's pixel
 * units.
 *
 * Each solid has up to three faces. Faces share one gradient per kind (top = lightest, left = mid, right =
 * deepest gold) drawn semi-transparent over the cream backdrop, with a white sheen, a bright 1px rim along the
 * visible edges and a blurred gold halo behind the bar chart. `layer="back"` (the cube and the tall pale pillar
 * behind the card) is drawn under the card, `"front"` (the bar chart) over it. Decorative: hidden from
 * assistive technology.
 */

const BACK = new Set(["pillar", "A"]);
// Bars run off the bottom edge of the stage; extend them so they still reach the viewport edge when the stage is
// letterboxed on a taller screen.
const EXTEND = new Set(["pillar", "B", "C", "D", "E", "F"]);
const EXTEND_BY = 420;
// The tall bar behind the chart only needs to reach the glow at the stage's bottom-right corner.
const EXTEND_OVERRIDE: Record<string, number> = { F: 110 };
const extendBy = (id: string) => EXTEND_OVERRIDE[id] ?? EXTEND_BY;
// Solids that cast a halo (the bottom bar chart); the tall bar behind the card and the cube do not.
const HALO: Record<string, number> = { B: 1, C: 1, D: 0.4, E: 0.3 };
// The pillar is a faint, pale piece of glass; every other solid is full gold.
const PALE = new Set(["pillar"]);

function pts(points: readonly Pt[]): string {
  return points.map(([x, y]) => `${x},${y}`).join(" ");
}

function extended(solid: Solid, face: readonly Pt[]): readonly Pt[] {
  if (!EXTEND.has(solid.id)) return face;
  // left/right faces are [top-a, top-b, bottom-b, bottom-a]: push the two bottom points down.
  return face.map(([x, y], i) => (i >= 2 ? ([x, y + extendBy(solid.id)] as Pt) : ([x, y] as Pt)));
}

const FACE_FILL = { top: "ib-top", left: "ib-left", right: "ib-right" } as const;
const FACE_OPACITY = { top: 0.92, left: 0.74, right: 0.78 } as const;

function SolidShape({ solid }: { solid: Solid }) {
  const { top, left, right } = solid.faces;
  const [L, B, R, F] = top.pts;
  const pale = PALE.has(solid.id);
  const leftPts = left ? extended(solid, left.pts) : undefined;
  const rightPts = right ? extended(solid, right.pts) : undefined;
  const rim = pale ? "#ffffff" : "#fff8d6";
  const rimOpacity = pale ? 0.8 : 0.95;
  const faces: [keyof typeof FACE_FILL, readonly Pt[]][] = [];
  if (leftPts) faces.push(["left", leftPts]);
  if (rightPts) faces.push(["right", rightPts]);
  faces.push(["top", top.pts]);
  const lines = (
    <>
      <polyline points={pts([L, B, R])} fill="none" stroke={rim} strokeOpacity={rimOpacity * 0.7} strokeWidth="1.3" strokeLinejoin="round" strokeLinecap="round" />
      <polyline points={pts([L, F, R])} fill="none" stroke={rim} strokeOpacity={rimOpacity} strokeWidth="1.8" strokeLinejoin="round" strokeLinecap="round" />
      {leftPts && <line x1={leftPts[0][0]} y1={leftPts[0][1]} x2={leftPts[3][0]} y2={leftPts[3][1]} stroke={rim} strokeOpacity={rimOpacity * 0.7} strokeWidth="1.3" strokeLinecap="round" />}
      {rightPts && <line x1={rightPts[0][0]} y1={rightPts[0][1]} x2={rightPts[3][0]} y2={rightPts[3][1]} stroke={rim} strokeOpacity={rimOpacity} strokeWidth="1.8" strokeLinecap="round" />}
      {rightPts && <line x1={rightPts[1][0]} y1={rightPts[1][1]} x2={rightPts[2][0]} y2={rightPts[2][1]} stroke={rim} strokeOpacity={rimOpacity * 0.6} strokeWidth="1.3" strokeLinecap="round" />}
    </>
  );
  return (
    <g opacity={pale ? 0.5 : 1}>
      {faces.map(([name, points]) => (
        <g key={name}>
          <polygon points={pts(points)} fill={`url(#${FACE_FILL[name]})`} fillOpacity={FACE_OPACITY[name]} />
          {name !== "top" && <polygon points={pts(points)} fill="url(#ib-sheen)" />}
        </g>
      ))}
      <g filter="url(#ib-rim-glow)" opacity="0.6">
        {lines}
      </g>
      {lines}
    </g>
  );
}

export function IsoBars({ layer }: { layer: "back" | "front" }) {
  const solids = SOLIDS.filter((s) => (layer === "back" ? BACK.has(s.id) : !BACK.has(s.id)));
  return (
    <svg
      className={`auth-iso auth-iso-${layer}`}
      width="1672"
      height="941"
      viewBox="0 0 1672 941"
      aria-hidden="true"
      focusable="false"
      overflow="visible"
    >
      <defs>
        <linearGradient id="ib-top" x1="0" y1="0" x2="1" y2="1">
          <stop offset="0" stopColor="#fffbe0" />
          <stop offset="1" stopColor="#ffe27a" />
        </linearGradient>
        <linearGradient id="ib-left" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" stopColor="#ffe08a" />
          <stop offset="1" stopColor="#ffc21a" />
        </linearGradient>
        <linearGradient id="ib-right" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" stopColor="#f7b500" />
          <stop offset="1" stopColor="#d98e00" />
        </linearGradient>
        <linearGradient id="ib-sheen" x1="0" y1="0" x2="1" y2="0">
          <stop offset="0" stopColor="#ffffff" stopOpacity="0.5" />
          <stop offset="0.45" stopColor="#ffffff" stopOpacity="0.08" />
          <stop offset="1" stopColor="#ffffff" stopOpacity="0" />
        </linearGradient>
        <filter id="ib-rim-glow" x="-10%" y="-10%" width="120%" height="120%">
          <feGaussianBlur stdDeviation="2.6" />
        </filter>
        <filter id={`ib-blur-${layer}`} x="-40%" y="-40%" width="180%" height="180%">
          <feGaussianBlur stdDeviation="30" />
        </filter>
        <filter id={`ib-blur-wide-${layer}`} x="-60%" y="-60%" width="220%" height="220%">
          <feGaussianBlur stdDeviation="75" />
        </filter>
      </defs>
      {layer === "front" &&
        (
          [
            ["wide", 0.8, "#ffc21a"],
            ["tight", 0.55, "#ffd04a"],
          ] as const
        ).map(([kind, opacity, fill]) => (
          <g key={kind} filter={`url(#${kind === "wide" ? `ib-blur-wide-${layer}` : `ib-blur-${layer}`})`} opacity={opacity} fill={fill}>
            {solids
              .filter((s) => s.id in HALO)
              .map((s) => (
                <g key={s.id} opacity={HALO[s.id]}>
                  <polygon points={pts(s.faces.top.pts)} />
                  {s.faces.left && <polygon points={pts(extended(s, s.faces.left.pts))} />}
                  {s.faces.right && <polygon points={pts(extended(s, s.faces.right.pts))} />}
                </g>
              ))}
          </g>
        ))}
      {solids.map((s) => (
        <SolidShape key={s.id} solid={s} />
      ))}
    </svg>
  );
}
