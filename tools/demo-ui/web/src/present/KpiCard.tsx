/**
 * tools/demo-ui/web/src/present/KpiCard.tsx
 * ───────────────────────────────────────────
 * A headline KPI card — self-contained, academic styling. The number is the
 * focal point: large serif display figure (tabular), tone-coloured; a sans
 * label above; a delta vs baseline oriented so "better" is always positive; a
 * quiet footnote naming the baseline and direction-of-better. Pending values
 * render a calm em-dash, never a fake number.
 */

import { deltaVs, fmtNumber, toneForKpi } from "../results/adapter";
import type { Direction, Kpi } from "../results/schema";
import { toneColor } from "./palette";

const DIR_HINT: Record<Direction, string> = {
  "lower-better": "lower is better",
  "higher-better": "higher is better",
  target: "closer to target is better",
  neutral: "",
};

export function KpiCard({ kpi }: { kpi: Kpi }) {
  const pending = kpi.value == null;
  const tone = toneForKpi(kpi);
  const delta = deltaVs(kpi.value, kpi.baselineValue ?? null, kpi.direction, kpi.unit);
  const deltaColor = delta == null ? "var(--sl-text-low)" : delta.better ? "var(--sl-ok)" : delta.abs === 0 ? "var(--sl-text-low)" : "var(--sl-crit)";

  const footnote: string[] = [];
  if (kpi.hint) footnote.push(kpi.hint);
  if (kpi.baselineLabel && delta != null) footnote.push(`vs ${kpi.baselineLabel}`);
  else if (DIR_HINT[kpi.direction]) footnote.push(DIR_HINT[kpi.direction]);

  return (
    <div
      style={{
        background: "var(--sl-surface)",
        border: "1px solid var(--sl-hairline)",
        borderRadius: "var(--sl-radius-lg)",
        boxShadow: "var(--sl-shadow-1)",
        padding: "18px 20px",
        display: "flex",
        flexDirection: "column",
        gap: 10,
        minHeight: 132,
      }}
    >
      <div
        style={{
          fontFamily: "var(--sl-font-sans)",
          fontSize: 12,
          fontWeight: 600,
          letterSpacing: "0.06em",
          textTransform: "uppercase",
          color: "var(--sl-text-low)",
        }}
      >
        {kpi.label}
      </div>

      <div style={{ display: "flex", alignItems: "baseline", gap: 6, flex: 1 }}>
        <span
          style={{
            fontFamily: "var(--sl-font-display)",
            fontSize: "var(--sl-text-3xl)",
            fontWeight: 700,
            lineHeight: 1,
            letterSpacing: "-0.02em",
            fontVariantNumeric: "tabular-nums",
            color: pending ? "var(--sl-text-faint)" : toneColor(tone),
          }}
        >
          {pending ? "—" : fmtNumber(kpi.value, "")}
        </span>
        {!pending && kpi.unit ? (
          <span style={{ fontSize: "var(--sl-text-md)", color: "var(--sl-text-low)", fontWeight: 500 }}>{kpi.unit}</span>
        ) : null}
      </div>

      {!pending && delta != null ? (
        <div style={{ fontSize: 13, fontWeight: 600, color: deltaColor }}>
          {delta.text}
          {delta.pct != null ? <span style={{ color: "var(--sl-text-low)", fontWeight: 400 }}> ({fmtNumber(delta.pct, "", 0)}%)</span> : null}
        </div>
      ) : null}

      <div style={{ fontSize: 12, color: "var(--sl-text-low)", lineHeight: 1.45 }}>
        {pending ? "awaiting updated benchmark run" : footnote.join(" · ")}
      </div>
    </div>
  );
}

export function KpiGrid({ kpis }: { kpis: Kpi[] }) {
  if (!kpis.length) return null;
  return (
    <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(220px, 1fr))", gap: 14 }}>
      {kpis.map((k) => (
        <KpiCard key={k.key} kpi={k} />
      ))}
    </div>
  );
}
