/**
 * tools/demo-ui/web/src/present/ComparisonMatrix.tsx
 * ────────────────────────────────────────────────────
 * Systems (rows) × metrics (columns) for ONE selected parameter configuration.
 *   - "this system" (subject) is marked and pinned.
 *   - the winner of each metric is highlighted (computed from the metric's
 *     declared direction, for the selected configuration — no per-metric code).
 *   - ceiling/floor/reference rows are de-emphasised (bounds, not contenders).
 *   - each column header states the direction of "better".
 *   - missing values render the PENDING placeholder.
 */

import { fmtMeasure, isPending, measureAt, winnerId } from "../results/adapter";
import type { Direction, Suite, SystemDef } from "../results/schema";
import { DataTable, StatusPill, type Column } from "../ui";
import { PendingValue } from "./Pending";

const DIR_ARROW: Record<Direction, string> = {
  "lower-better": "↓ lower better",
  "higher-better": "↑ higher better",
  target: "◎ target",
  neutral: "",
};

const ROLE_LABEL: Record<SystemDef["role"], string | null> = {
  subject: "this system",
  baseline: "baseline",
  candidate: "candidate",
  ceiling: "ceiling",
  floor: "floor",
  reference: "reference",
};

export function ComparisonMatrix({ suite, configId }: { suite: Suite; configId: string }) {
  const winners: Record<string, string | null> = {};
  for (const m of suite.metrics) winners[m.key] = winnerId(suite, m, configId);

  const columns: Column<SystemDef>[] = [
    {
      key: "system",
      header: "system",
      render: (sys) => {
        const isSubject = sys.id === suite.subjectId;
        const role = ROLE_LABEL[sys.role];
        return (
          <span style={{ display: "inline-flex", alignItems: "center", gap: 8 }}>
            <span
              aria-hidden
              style={{ width: 3, height: 18, borderRadius: 2, background: isSubject ? "var(--sl-mint)" : "transparent", flex: "0 0 auto" }}
            />
            <span style={{ fontWeight: isSubject ? 700 : 500, color: "var(--sl-text)" }}>{sys.label}</span>
            {isSubject ? (
              <StatusPill status="ok" hideDot>
                this system
              </StatusPill>
            ) : role && sys.role !== "baseline" && sys.role !== "candidate" ? (
              <span
                style={{
                  fontFamily: "var(--sl-font-sans)",
                  fontSize: 9.5,
                  fontWeight: 600,
                  letterSpacing: "0.06em",
                  textTransform: "uppercase",
                  color: "var(--sl-text-low)",
                  border: "1px solid var(--sl-hairline)",
                  borderRadius: 5,
                  padding: "1px 6px",
                }}
              >
                {role}
              </span>
            ) : null}
            {sys.hint ? <span style={{ fontSize: 11.5, color: "var(--sl-text-low)" }}>{sys.hint}</span> : null}
          </span>
        );
      },
    },
    ...suite.metrics.map<Column<SystemDef>>((m) => ({
      key: m.key,
      numeric: true,
      header: (
        <span title={m.hint} style={{ display: "inline-flex", flexDirection: "column", alignItems: "flex-end" }}>
          <span>
            {m.label}
            {m.unit ? <span style={{ color: "var(--sl-text-low)" }}> ({m.unit})</span> : null}
          </span>
          {DIR_ARROW[m.direction] ? (
            <span style={{ fontSize: 9, color: "var(--sl-text-low)", fontWeight: 400 }}>{DIR_ARROW[m.direction]}</span>
          ) : null}
        </span>
      ),
      render: (sys) => {
        const cell = measureAt(suite, sys.id, configId, m.key);
        if (isPending(cell)) return <PendingValue />;
        const isWinner = winners[m.key] === sys.id;
        return (
          <span
            style={{ color: isWinner ? "var(--sl-ok)" : "var(--sl-text)", fontWeight: isWinner ? 700 : 500 }}
            title={isWinner ? "best among contenders" : undefined}
          >
            {fmtMeasure(cell, m)}
            {isWinner ? <span aria-hidden> ★</span> : null}
          </span>
        );
      },
    })),
  ];

  return (
    <DataTable<SystemDef>
      columns={columns}
      rows={suite.systems}
      rowKey={(s) => s.id}
      rowMuted={(s) => ["ceiling", "floor", "reference"].includes(s.role)}
    />
  );
}
