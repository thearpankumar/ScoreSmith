/**
 * Hairline arcs and isometric floor lines of the sign-in backdrop. One full-bleed SVG in stage units; strokes use
 * `vector-effect: non-scaling-stroke` so they stay 1px crisp at any stage scale. Decorative.
 */

const LINES: { d: string; o: number }[] = [
  // large circle arcs (traced by eye)
  { d: "M842 0 C 765 105, 712 215, 704 385", o: 0.34 },
  { d: "M1316 0 L 1135 148 L 1040 238 L 900 372", o: 0.3 },
  { d: "M1672 14 C 1598 40, 1516 92, 1460 160", o: 0.28 },
  { d: "M0 62 C 120 52, 230 78, 330 140", o: 0.16 },
  // isometric floor lines (found with a Hough transform over the mock's high-pass image)
  { d: "M767 941 L 1672 519", o: 0.2 },
  { d: "M672 941 L 1672 475", o: 0.16 },
  { d: "M0 478 L 1207 941", o: 0.1 },
  { d: "M0 579 L 1113 941", o: 0.1 },
  { d: "M0 459 L 1192 941", o: 0.08 },
  { d: "M0 740 L 1672 30", o: 0.1 },
  // faint verticals
  { d: "M10 0 L 10 941", o: 0.1 },
  { d: "M366 96 L 366 240", o: 0.1 },
  { d: "M576 0 L 576 760", o: 0.07 },
  { d: "M395 0 L 395 250", o: 0.1 },
];

export function BackgroundLines() {
  return (
    <svg className="auth-lines" width="1672" height="941" viewBox="0 0 1672 941" fill="none" aria-hidden="true" focusable="false">
      {LINES.map((l, i) => (
        <path key={i} d={l.d} stroke="#D99A00" strokeOpacity={l.o} strokeWidth="1" vectorEffect="non-scaling-stroke" />
      ))}
    </svg>
  );
}
