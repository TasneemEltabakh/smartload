/* ============================================================================
   ShareBars -- per-backend routing weight bars
   ----------------------------------------------------------------------------
   A labeled bar per backend showing its routing share. Excluded / shadow rows
   dim to graphite; the value is mono and right-aligned. Bars grow on mount.
   ============================================================================ */
import { useEffect, useState } from "react";

export interface ShareRow {
  id: string;
  label: string;
  /** Share as a fraction 0..1 (or a percentage 0..100 if max=100). */
  value: number;
  /** Dim this row (excluded node / shadow weight). */
  dim?: boolean;
}

export interface ShareBarsProps {
  rows: ShareRow[];
  /** Scale max; 1 for fractions, 100 for percentages. */
  max?: number;
  /** Render the value as a percentage. */
  asPercent?: boolean;
  className?: string;
}

export function ShareBars({ rows, max = 1, asPercent = true, className }: ShareBarsProps) {
  const [grown, setGrown] = useState(false);
  useEffect(() => {
    const raf = requestAnimationFrame(() => setGrown(true));
    return () => cancelAnimationFrame(raf);
  }, []);

  return (
    <div className={className} style={{ display: "flex", flexDirection: "column", gap: 13 }}>
      {rows.map((row) => {
        const pct = Math.max(0, Math.min(100, (row.value / max) * 100));
        const display = asPercent ? `${Math.round(pct)}%` : row.value.toFixed(2);
        return (
          <div
            key={row.id}
            style={{
              display: "grid",
              gridTemplateColumns: "96px 1fr 56px",
              gap: 12,
              alignItems: "center",
            }}
          >
            <span
              style={{
                fontFamily: "var(--sl-font-mono)",
                fontSize: 12,
                fontWeight: 600,
                color: "var(--sl-text-mid)",
              }}
            >
              {row.label}
            </span>
            <span
              style={{
                height: 9,
                borderRadius: 9,
                background: "var(--sl-surface-sunk)",
                overflow: "hidden",
                position: "relative",
              }}
            >
              <span
                style={{
                  position: "absolute",
                  left: 0,
                  top: 0,
                  bottom: 0,
                  borderRadius: 9,
                  width: grown ? `${pct}%` : "0%",
                  background: row.dim
                    ? "var(--sl-graphite-soft)"
                    : "var(--sl-mint)",
                  transition: "width 1s var(--sl-ease)",
                }}
              />
            </span>
            <span
              style={{
                fontFamily: "var(--sl-font-mono)",
                fontSize: 12,
                fontWeight: 700,
                textAlign: "right",
                color: "var(--sl-text)",
              }}
            >
              {display}
            </span>
          </div>
        );
      })}
    </div>
  );
}
