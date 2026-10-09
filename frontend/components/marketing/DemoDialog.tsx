"use client";

import { useRef, useState } from "react";
import { ClipboardCheck, PenLine, Play, Sparkles, X } from "lucide-react";

const STEPS = [
  {
    Icon: PenLine,
    title: "1. Design the scorecard",
    body: "Describe what matters in plain words. The assistant drafts a KPI tree with weights and scoring guidelines you can edit.",
    rows: [
      ["Customer satisfaction", 40],
      ["Delivery quality", 35],
      ["Team engagement", 25],
    ],
  },
  {
    Icon: ClipboardCheck,
    title: "2. Evaluate against it",
    body: "Upload documents, recordings or notes. Each KPI is scored with the evidence that justifies the score.",
    rows: [
      ["Customer satisfaction", 86],
      ["Delivery quality", 72],
      ["Team engagement", 64],
    ],
  },
  {
    Icon: Sparkles,
    title: "3. See what to improve",
    body: "Compare against your target, spot the weakest KPIs and export the results to Excel for your team.",
    rows: [
      ["On track", 12],
      ["At risk", 4],
      ["Delayed", 2],
    ],
  },
] as const;

/** "Watch Demo": opens a short, three-step walkthrough built from the same mock UI, in a native modal <dialog>. */
export function DemoButton() {
  const ref = useRef<HTMLDialogElement>(null);
  const [step, setStep] = useState(0);
  const s = STEPS[step];
  return (
    <>
      <button
        type="button"
        className="mk-btn mk-btn-ghost mk-btn-lg"
        onClick={() => {
          setStep(0);
          ref.current?.showModal();
        }}
      >
        <span className="mk-play" aria-hidden>
          <Play fill="currentColor" strokeWidth={0} />
        </span>
        Watch Demo
      </button>
      <dialog
        ref={ref}
        className="mk-demo"
        aria-labelledby="mk-demo-title"
        onClick={(e) => {
          if (e.target === ref.current) ref.current?.close(); // click on the backdrop
        }}
      >
        <div className="mk-demo-head">
          <h2 id="mk-demo-title">See how it works</h2>
          <button type="button" className="mk-demo-x" aria-label="Close demo" onClick={() => ref.current?.close()}>
            <X aria-hidden />
          </button>
        </div>
        <div className="mk-demo-body">
          <div className="mk-demo-shot" aria-hidden>
            {s.rows.map(([label, v]) => (
              <div key={label} className="mk-demo-row">
                <span>{label}</span>
                <span className="mk-demo-track">
                  <i style={{ width: `${Math.min(100, step === 2 ? Number(v) * 5 : Number(v))}%` }} />
                </span>
                <b>{v}</b>
              </div>
            ))}
          </div>
          <div aria-live="polite">
            <h3>
              <s.Icon aria-hidden /> {s.title}
            </h3>
            <p>{s.body}</p>
          </div>
        </div>
        <div className="mk-demo-foot">
          <div className="mk-demo-dots" aria-hidden>
            {STEPS.map((_, i) => (
              <i key={i} className={i === step ? "is-on" : ""} />
            ))}
          </div>
          <button type="button" className="mk-btn mk-btn-ghost" disabled={step === 0} onClick={() => setStep(step - 1)}>
            Back
          </button>
          {step < STEPS.length - 1 ? (
            <button type="button" className="mk-btn mk-btn-gold" onClick={() => setStep(step + 1)}>
              Next
            </button>
          ) : (
            <button type="button" className="mk-btn mk-btn-gold" onClick={() => ref.current?.close()}>
              Done
            </button>
          )}
        </div>
      </dialog>
    </>
  );
}
