/**
 * tools/demo-ui/web/src/present/ComparisonChart.tsx
 * ───────────────────────────────────────────────────
 * Renders a ChartDef from the data contract — either a categorical bar chart
 * (kind: "bars") or a multi-series line chart (kind: "lines"). Axes are labelled
 * with the metric and unit; the "better" direction is stated in the caption.
 * Emphasised bars/series light mint; reference bars read amber; everything else
 * graphite. Renders the PENDING block when the chart carries no data.
 */

import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  LabelList,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import { fmtNumber } from "../results/adapter";
import type { ChartDef, Direction } from "../results/schema";
import { Donut, ForecastChart, ShareBars } from "../ui";
import type { DonutSegment, ShareRow } from "../ui";
import { C, SERIES_COLORS, TOOLTIP_STYLE } from "./palette";
import { PendingBlock } from "./Pending";

const DIR_TEXT: Record<Direction, string> = {
  "lower-better": "lower is better",
  "higher-better": "higher is better",
  target: "closer to target is better",
  neutral: "",
};

function Caption({ chart }: { chart: ChartDef }) {
  const dir = chart.direction ? DIR_TEXT[chart.direction] : "";
  if (!dir) return null;
  return (
    <div style={{ marginTop: 8, fontSize: 11.5, fontWeight: 600, color: "var(--sl-text-low)" }}>{dir}</div>
  );
}

function Bars({ chart }: { chart: ChartDef }) {
  const data = (chart.bars ?? []).filter((b) => b.value != null);
  if (data.length === 0) return <PendingBlock label="No data for this comparison yet" />;
  const fmt = (v: number) => fmtNumber(v, chart.yUnit ?? "", undefined);
  return (
    <div style={{ width: "100%", height: Math.max(150, data.length * 46) }}>
      <ResponsiveContainer width="100%" height="100%">
        <BarChart data={data} layout="vertical" margin={{ top: 4, right: 64, bottom: 4, left: 8 }} barCategoryGap={14}>
          <CartesianGrid horizontal={false} stroke={C.grid} strokeDasharray="2 5" />
          <XAxis
            type="number"
            stroke={C.grid}
            tick={{ fill: C.textLow, fontSize: 10, fontFamily: "var(--sl-font-sans)" }}
            tickLine={false}
            axisLine={{ stroke: C.grid }}
          />
          <YAxis
            type="category"
            dataKey="label"
            width={150}
            stroke={C.grid}
            tick={{ fill: C.textLow, fontSize: 10.5, fontFamily: "var(--sl-font-sans)" }}
            tickLine={false}
            axisLine={false}
          />
          <Tooltip
            cursor={{ fill: C.accentSoft }}
            contentStyle={TOOLTIP_STYLE}
            labelStyle={{ color: C.textLow }}
            itemStyle={{ color: C.text }}
            formatter={(v: number) => [fmt(v), chart.yLabel ?? "value"]}
          />
          <Bar dataKey="value" radius={[0, 4, 4, 0]} maxBarSize={22} isAnimationActive={false}>
            {data.map((b) => (
              <Cell key={b.label} fill={b.emphasis ? C.accent : b.reference ? C.amber : C.graphite} />
            ))}
            <LabelList
              dataKey="value"
              position="right"
              formatter={(v: number) => fmt(v)}
              style={{ fill: C.text, fontFamily: "var(--sl-font-sans)", fontSize: 10.5 }}
            />
          </Bar>
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}

function Lines({ chart }: { chart: ChartDef }) {
  const series = (chart.series ?? []).filter((s) => s.points.some((p) => p.y != null));
  if (series.length === 0) return <PendingBlock label="No data for this comparison yet" />;

  // Merge series onto a shared category axis keyed by x.
  const xs: (string | number)[] = [];
  for (const s of series) for (const p of s.points) if (!xs.includes(p.x)) xs.push(p.x);
  const data = xs.map((x) => {
    const row: Record<string, string | number | null> = { x: String(x) };
    for (const s of series) row[s.id] = s.points.find((p) => p.x === x)?.y ?? null;
    return row;
  });

  return (
    <>
      <div style={{ display: "flex", gap: 16, flexWrap: "wrap", marginBottom: 6 }}>
        {series.map((s, i) => (
          <span key={s.id} style={{ fontSize: 10.5, color: "var(--sl-text-low)", display: "inline-flex", alignItems: "center", gap: 6 }}>
            <span
              aria-hidden
              style={{
                width: 12,
                height: 3,
                borderRadius: 2,
                background: s.color ?? SERIES_COLORS[i % SERIES_COLORS.length],
                boxShadow: s.emphasis ? `0 0 6px ${s.color ?? SERIES_COLORS[i % SERIES_COLORS.length]}` : "none",
              }}
            />
            <span style={{ color: s.emphasis ? "var(--sl-text)" : "var(--sl-text-low)" }}>{s.label}</span>
          </span>
        ))}
      </div>
      <div style={{ width: "100%", height: 220 }}>
        <ResponsiveContainer width="100%" height="100%">
          <LineChart data={data} margin={{ top: 6, right: 16, left: -10, bottom: 0 }}>
            <CartesianGrid stroke={C.grid} vertical={false} />
            <XAxis dataKey="x" tick={{ fill: C.axis, fontSize: 10 }} stroke={C.grid} />
            <YAxis tick={{ fill: C.axis, fontSize: 10 }} stroke={C.grid} />
            <Tooltip contentStyle={TOOLTIP_STYLE} cursor={{ stroke: C.axis, strokeWidth: 1 }} />
            {series.map((s, i) => (
              <Line
                key={s.id}
                type="monotone"
                dataKey={s.id}
                name={s.label}
                stroke={s.color ?? SERIES_COLORS[i % SERIES_COLORS.length]}
                strokeWidth={s.emphasis ? 2.6 : 1.6}
                dot={{ r: 2 }}
                isAnimationActive={false}
                connectNulls
              />
            ))}
          </LineChart>
        </ResponsiveContainer>
      </div>
    </>
  );
}

/** kind "donut" — a proportion ring with a labelled share-bar legend. */
function DonutShare({ chart }: { chart: ChartDef }) {
  const rows = (chart.shares ?? []).filter((s) => s.value != null);
  if (rows.length === 0) return <PendingBlock label="No distribution for this comparison yet" />;
  const max = chart.shareMax ?? (rows.some((r) => (r.value ?? 0) > 1) ? 100 : 1);
  const segments: DonutSegment[] = rows.map((r, i) => ({
    id: r.id,
    value: r.value ?? 0,
    label: r.label,
    color: r.dim ? C.graphiteSoft : i === 0 ? C.accent : SERIES_COLORS[i % SERIES_COLORS.length],
  }));
  const shareRows: ShareRow[] = rows.map((r) => ({ id: r.id, label: r.label, value: r.value ?? 0, dim: r.dim }));
  return (
    <div style={{ display: "flex", gap: 22, alignItems: "center", flexWrap: "wrap" }}>
      <Donut
        segments={segments}
        size={140}
        thickness={16}
        centerValue={chart.centerValue}
        centerLabel={chart.centerLabel}
      />
      <div style={{ flex: 1, minWidth: 200 }}>
        <ShareBars rows={shareRows} max={max} asPercent />
      </div>
    </div>
  );
}

/** kind "share" — labelled share bars only (no ring). */
function Share({ chart }: { chart: ChartDef }) {
  const rows = (chart.shares ?? []).filter((s) => s.value != null);
  if (rows.length === 0) return <PendingBlock label="No distribution for this comparison yet" />;
  const max = chart.shareMax ?? (rows.some((r) => (r.value ?? 0) > 1) ? 100 : 1);
  const shareRows: ShareRow[] = rows.map((r) => ({ id: r.id, label: r.label, value: r.value ?? 0, dim: r.dim }));
  return <ShareBars rows={shareRows} max={max} asPercent />;
}

/** kind "forecast" — the predicted-vs-actual hero line. */
function Forecast({ chart }: { chart: ChartDef }) {
  const f = chart.forecast;
  if (!f || f.actual.length === 0 || f.forecast.length === 0) {
    return <PendingBlock label="No forecast series yet" />;
  }
  return (
    <div>
      <div style={{ display: "flex", gap: 16, flexWrap: "wrap", marginBottom: 6 }}>
        <span style={{ fontSize: 10.5, color: "var(--sl-text-low)", display: "inline-flex", alignItems: "center", gap: 6 }}>
          <span aria-hidden style={{ width: 12, height: 3, borderRadius: 2, background: C.graphite }} />
          <span>{f.actualLabel ?? "actual"}</span>
        </span>
        <span style={{ fontSize: 10.5, color: "var(--sl-text)", display: "inline-flex", alignItems: "center", gap: 6 }}>
          <span aria-hidden style={{ width: 12, height: 3, borderRadius: 2, background: C.accent, boxShadow: `0 0 6px ${C.accent}` }} />
          <span>{f.forecastLabel ?? "forecast (lead)"}</span>
        </span>
      </div>
      <ForecastChart
        actual={f.actual}
        forecast={f.forecast}
        confLow={f.confLow}
        confHigh={f.confHigh}
        xLabels={f.xLabels}
        scaleIndex={f.scaleIndex}
        scaleLabel={f.scaleLabel}
        unit={chart.yUnit ?? ""}
        height={260}
      />
    </div>
  );
}

export function ComparisonChart({ chart }: { chart: ChartDef }) {
  let body;
  if (chart.kind === "lines") body = <Lines chart={chart} />;
  else if (chart.kind === "donut") body = <DonutShare chart={chart} />;
  else if (chart.kind === "share") body = <Share chart={chart} />;
  else if (chart.kind === "forecast") body = <Forecast chart={chart} />;
  else body = <Bars chart={chart} />;
  return (
    <div>
      {body}
      <Caption chart={chart} />
    </div>
  );
}
