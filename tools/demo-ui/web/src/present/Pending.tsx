/**
 * tools/demo-ui/web/src/present/Pending.tsx
 * ───────────────────────────────────────────
 * The single, defined PENDING/EMPTY state used everywhere a value can be
 * absent — KPI cards, comparison cells, charts, audit lists. It always reads as
 * an intentional "awaiting updated benchmark run" placeholder, never a fake
 * number and never a broken layout.
 */

import type { ReactNode } from "react";

/** Inline em-dash placeholder for a single missing value (table cells, KPIs). */
export function PendingValue() {
  return (
    <span
      title="awaiting updated benchmark run"
      style={{ color: "var(--sl-text-low)", fontVariantNumeric: "tabular-nums" }}
    >
      —
    </span>
  );
}

/** A small inline "pending" chip. */
export function PendingChip({ children }: { children?: ReactNode }) {
  return (
    <span
      style={{
        fontFamily: "var(--sl-font-sans)",
        fontSize: 9,
        letterSpacing: "0.6px",
        textTransform: "uppercase",
        color: "var(--sl-text-low)",
        border: "1px dashed var(--sl-hairline)",
        borderRadius: 6,
        padding: "2px 7px",
        background: "var(--sl-surface-sunk)",
      }}
    >
      {children ?? "pending"}
    </span>
  );
}

/** A full-panel empty state for a chart or section with no data yet. */
export function PendingBlock({
  label = "Awaiting updated benchmark run",
  height = 160,
}: {
  label?: string;
  height?: number;
}) {
  return (
    <div
      style={{
        display: "flex",
        flexDirection: "column",
        alignItems: "center",
        justifyContent: "center",
        gap: 10,
        minHeight: height,
        border: "1px dashed var(--sl-hairline)",
        borderRadius: "var(--sl-radius-md)",
        background: "var(--sl-surface-sunk)",
        color: "var(--sl-text-low)",
        fontFamily: "var(--sl-font-sans)",
        fontSize: 12,
        textAlign: "center",
        padding: 18,
      }}
    >
      <PendingChip />
      <span>{label}</span>
    </div>
  );
}
