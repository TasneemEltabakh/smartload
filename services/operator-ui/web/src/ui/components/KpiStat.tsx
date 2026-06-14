/* ============================================================================
   KpiStat -- a vital sign
   ----------------------------------------------------------------------------
   Mono label, big mono value with an optional unit, a delta reading, and an
   optional sparkline. The value is the focal point; everything else supports it.
   ============================================================================ */
import type { ReactNode } from "react";
import { Sparkline } from "../charts/Sparkline";

export type DeltaDir = "up" | "down" | "flat";

export interface KpiStatProps {
  label: ReactNode;
  /** The headline number (string keeps formatting under caller control). */
  value: ReactNode;
  /** Small unit suffix (e.g. "ms", "k rpm"). */
  unit?: string;
  /** Delta direction; colors the reading. */
  deltaDir?: DeltaDir;
  /** Delta text (e.g. "▲ 18.2%"). */
  delta?: ReactNode;
  /** Trailing context (e.g. "vs 1h ago"). */
  footnote?: ReactNode;
  /** Optional sparkline series. */
  spark?: number[];
  sparkTone?: "mint" | "graphite";
  className?: string;
}

const deltaColor: Record<DeltaDir, string> = {
  up: "var(--sl-ok)",
  down: "var(--sl-crit)",
  flat: "var(--sl-text-low)",
};

export function KpiStat({
  label,
  value,
  unit,
  deltaDir = "flat",
  delta,
  footnote,
  spark,
  sparkTone = "mint",
  className,
}: KpiStatProps) {
  return (
    <div
      className={className}
      style={{
        background: "var(--sl-surface)",
        border: "1px solid var(--sl-hairline)",
        borderRadius: "var(--sl-radius-lg)",
        boxShadow: "var(--sl-shadow-1)",
        padding: "15px 17px",
        position: "relative",
        overflow: "hidden",
      }}
    >
      <div
        style={{
          fontFamily: "var(--sl-font-mono)",
          fontSize: 9.5,
          letterSpacing: "1.2px",
          color: "var(--sl-text-low)",
          textTransform: "uppercase",
          display: "flex",
          alignItems: "center",
          gap: 7,
        }}
      >
        {label}
      </div>
      <div
        style={{
          fontFamily: "var(--sl-font-mono)",
          fontSize: 27,
          fontWeight: 700,
          letterSpacing: "-1px",
          marginTop: 9,
          color: "var(--sl-text)",
          lineHeight: 1,
          fontVariantNumeric: "tabular-nums",
        }}
      >
        {value}
        {unit ? (
          <span style={{ fontSize: 13, color: "var(--sl-text-low)", fontWeight: 500, marginLeft: 3 }}>
            {unit}
          </span>
        ) : null}
      </div>
      {(delta != null || footnote != null) && (
        <div
          style={{
            display: "flex",
            alignItems: "center",
            gap: 6,
            marginTop: 9,
            fontFamily: "var(--sl-font-mono)",
            fontSize: 10.5,
          }}
        >
          {delta != null ? <span style={{ color: deltaColor[deltaDir], fontWeight: 600 }}>{delta}</span> : null}
          {footnote != null ? <span style={{ color: "var(--sl-text-low)" }}>{footnote}</span> : null}
        </div>
      )}
      {spark ? (
        <div style={{ marginTop: 11 }}>
          <Sparkline data={spark} tone={sparkTone} width={120} height={26} />
        </div>
      ) : null}
    </div>
  );
}
