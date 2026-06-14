/* ============================================================================
   EvidenceLine -- "metric observed vs threshold"
   ----------------------------------------------------------------------------
   Renders an anomaly verdict's evidence: the metric name, the observed value
   (colored by severity), a verdict word, and the threshold it crossed. Mono
   throughout, because every value here is a real measurement.
   ============================================================================ */
import type { Status } from "./StatusPill";

export interface EvidenceLineProps {
  /** Metric identifier, e.g. "p95_latency_ms". */
  metric: string;
  /** Observed value (already formatted with its unit). */
  observed: string;
  /** Threshold value (already formatted with its unit). */
  threshold: string;
  /** Verdict word between observed and threshold, e.g. "breached". */
  verdict?: string;
  /** Severity coloring the observed value. */
  status?: Status;
  className?: string;
}

const obsColor: Record<Status, string> = {
  ok: "var(--sl-ok)",
  warn: "var(--sl-warn)",
  crit: "var(--sl-crit)",
  neutral: "var(--sl-text)",
};

export function EvidenceLine({
  metric,
  observed,
  threshold,
  verdict = "vs",
  status = "crit",
  className,
}: EvidenceLineProps) {
  return (
    <div
      className={className}
      style={{
        display: "flex",
        alignItems: "center",
        gap: 8,
        flexWrap: "wrap",
        fontFamily: "var(--sl-font-mono)",
        fontSize: 11,
        color: "var(--sl-text-mid)",
      }}
    >
      <span style={{ fontSize: 9, letterSpacing: "1px", color: "var(--sl-text-low)" }}>METRIC</span>
      <span>{metric}</span>
      <span style={{ color: obsColor[status], fontWeight: 600 }}>{observed}</span>
      <span style={{ color: "var(--sl-text-low)" }}>{`→ ${verdict}`}</span>
      <span style={{ color: "var(--sl-text-low)" }}>{`threshold ${threshold}`}</span>
    </div>
  );
}
