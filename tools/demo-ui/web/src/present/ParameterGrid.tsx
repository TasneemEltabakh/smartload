/**
 * tools/demo-ui/web/src/present/ParameterGrid.tsx
 * ─────────────────────────────────────────────────
 * The explicit two-axis benchmark view: systems (rows) × parameter
 * configurations (columns), for one selectable metric. This is what makes the
 * parameter dimension first-class — you see, at a glance, how every system
 * behaves across every load profile / scenario / phase, with the winner of each
 * configuration starred (per the metric's direction-of-better). A metric
 * selector switches which metric the grid shows. Missing cells render PENDING.
 */

import { useState } from "react";

import { fmtMeasure, isPending, measureAt, paramConfigs, winnerId } from "../results/adapter";
import type { Direction, MetricDef, Suite, SystemDef } from "../results/schema";
import { DataTable, type Column } from "../ui";
import { PendingValue } from "./Pending";

const DIR_TEXT: Record<Direction, string> = {
  "lower-better": "lower is better",
  "higher-better": "higher is better",
  target: "closer to target is better",
  neutral: "",
};

export function ParameterGrid({ suite }: { suite: Suite }) {
  const configs = paramConfigs(suite);
  const [metricKey, setMetricKey] = useState<string>(
    suite.primaryMetricKey || suite.metrics[0]?.key || "",
  );
  const metric: MetricDef | undefined = suite.metrics.find((m) => m.key === metricKey) ?? suite.metrics[0];
  if (!metric || configs.length < 2) return null;

  const winners: Record<string, string | null> = {};
  for (const c of configs) winners[c.id] = winnerId(suite, metric, c.id);

  const columns: Column<SystemDef>[] = [
    {
      key: "system",
      header: "system",
      render: (sys) => {
        const isSubject = sys.id === suite.subjectId;
        return (
          <span style={{ display: "inline-flex", alignItems: "center", gap: 8 }}>
            <span aria-hidden style={{ width: 3, height: 16, borderRadius: 2, background: isSubject ? "var(--sl-mint)" : "transparent" }} />
            <span style={{ fontWeight: isSubject ? 700 : 500, color: "var(--sl-text)" }}>{sys.label}</span>
          </span>
        );
      },
    },
    ...configs.map<Column<SystemDef>>((c) => ({
      key: c.id,
      numeric: true,
      header: <span title={c.params ? JSON.stringify(c.params) : undefined}>{c.label}</span>,
      render: (sys) => {
        const cell = measureAt(suite, sys.id, c.id, metric.key);
        if (isPending(cell)) return <PendingValue />;
        const isWinner = winners[c.id] === sys.id;
        return (
          <span style={{ color: isWinner ? "var(--sl-ok)" : "var(--sl-text)", fontWeight: isWinner ? 700 : 500 }}>
            {fmtMeasure(cell, metric)}
            {isWinner ? <span aria-hidden> ★</span> : null}
          </span>
        );
      },
    })),
  ];

  return (
    <div>
      {/* Metric selector */}
      <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap", marginBottom: 12 }}>
        {suite.metrics.map((m) => {
          const on = m.key === metric.key;
          return (
            <button
              key={m.key}
              type="button"
              onClick={() => setMetricKey(m.key)}
              style={{
                cursor: "pointer",
                fontFamily: "var(--sl-font-sans)",
                fontSize: 12.5,
                fontWeight: on ? 700 : 500,
                color: on ? "var(--sl-text)" : "var(--sl-text-mid)",
                background: on ? "var(--sl-mint-tint)" : "var(--sl-surface)",
                border: `1px solid ${on ? "var(--sl-mint-line)" : "var(--sl-hairline)"}`,
                borderRadius: 999,
                padding: "5px 12px",
              }}
            >
              {m.label}
              {m.unit ? <span style={{ color: "var(--sl-text-low)", fontWeight: 400 }}> ({m.unit})</span> : null}
            </button>
          );
        })}
        {DIR_TEXT[metric.direction] ? (
          <span style={{ marginLeft: 4, fontSize: 11.5, color: "var(--sl-text-low)" }}>· {DIR_TEXT[metric.direction]}</span>
        ) : null}
      </div>

      <DataTable<SystemDef>
        columns={columns}
        rows={suite.systems}
        rowKey={(s) => s.id}
        rowMuted={(s) => ["ceiling", "floor", "reference"].includes(s.role)}
      />
    </div>
  );
}
