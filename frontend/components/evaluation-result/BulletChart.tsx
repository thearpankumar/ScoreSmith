"use client";

import { Bar, BarChart, ReferenceArea, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";

import { RAG_BANDS } from "@/lib/rag";

/**
 * Hero final-score BULLET chart (per the plan, deliberately not a gauge):
 * qualitative RAG-band ranges as background bands, a single bold bar for
 * the achieved score, and a target tick. Bar/tick colour choices avoid
 * relying on colour alone — the achieved score and target are also stated
 * as plain text next to the chart (see EvaluationResultView).
 */
export function BulletChart({ score, target }: { score: number; target: number }) {
  const data = [{ name: "Final score", achieved: score }];

  return (
    <div>
      <ResponsiveContainer width="100%" height={110}>
        <BarChart data={data} layout="vertical" margin={{ top: 24, right: 24, bottom: 4, left: 8 }}>
          <XAxis
            type="number"
            domain={[0, 10]}
            ticks={[0, 2, 4, 6, 8, 10]}
            tick={{ fontSize: 11, fill: "var(--ink-muted)" }}
            axisLine={{ stroke: "var(--hairline)" }}
            tickLine={false}
          />
          <YAxis type="category" dataKey="name" hide />
          {RAG_BANDS.map((band) => (
            <ReferenceArea
              key={band.key}
              x1={band.min}
              x2={Math.min(10, band.max)}
              fill={band.color}
              fillOpacity={0.16}
              ifOverflow="visible"
            />
          ))}
          <ReferenceLine
            x={target}
            stroke="var(--ink)"
            strokeWidth={2}
            label={{ value: `Target ${target.toFixed(1)}`, position: "top", fill: "var(--ink)", fontSize: 11, fontWeight: 600 }}
          />
          <Bar dataKey="achieved" fill="var(--ink)" radius={[4, 4, 4, 4]} barSize={24} />
          <Tooltip
            cursor={false}
            formatter={(value: number) => [`${value.toFixed(1)} / 10`, "Achieved"]}
            contentStyle={{
              background: "var(--solid)",
              border: "1px solid var(--hairline)",
              borderRadius: 8,
              fontSize: 12,
            }}
          />
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}
