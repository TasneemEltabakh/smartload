/**
 * tools/demo-ui/web/src/pages/Dashboard.tsx  (cockpit "Deck")
 * ───────────────────────────────────────────────────────────
 * Flagship live flight-deck for developers, on the shared kit (dark theme):
 *   - Hero strip: the signature Heartbeat motion + session vitals.
 *   - Stack-health grid (every watched service, polled every 5 s).
 *   - Live session metrics (p95 / mean / SLO viol / total reqs) as kit KpiStat
 *     with rolling sparklines.
 *   - Current decision card (mode / inference age / top / bottom / basis).
 *   - Backend pool weights as kit ShareBars.
 *
 * Read-only. Automation lives on /run (Drive); manual ops live on /controls
 * (Lab). Degrades gracefully when the BFF is down: zero / empty states, no
 * crash.
 */

import { useEffect, useRef, useState } from "react";

import {
  Badge,
  Card,
  Heartbeat,
  KpiStat,
  ShareBars,
  StatusPill,
  type ShareRow,
  type Status,
} from "../ui";
import { useDemo } from "../state/DemoStateContext";
import {
  bottomRanked,
  decisionBasis,
  modeLabel,
  shortName,
  topRanked,
} from "../utils";


const SPARK_MAX = 24;

function svcStatus(healthy: boolean, status: string): Status {
  if (healthy) return "ok";
  if (status === "down") return "crit";
  return "warn";
}


export default function Dashboard() {
  const { state, metrics, services } = useDemo();
  const rankings = state?.last_rankings ?? null;

  // Rolling sparkline history for the live vitals. Pushed when metrics change.
  const [p95Hist, setP95Hist] = useState<number[]>([]);
  const [sloHist, setSloHist] = useState<number[]>([]);
  const lastSeen = useRef<string>("");

  useEffect(() => {
    if (!metrics || metrics.sample_count <= 0) return;
    // Dedup on the (count,total) signature so identical polls don't pile up.
    const sig = `${metrics.sample_count}:${metrics.total_requests}`;
    if (sig === lastSeen.current) return;
    lastSeen.current = sig;
    setP95Hist((h) => [...h, metrics.p95_latency_ms ?? 0].slice(-SPARK_MAX));
    setSloHist((h) => [...h, metrics.slo_violation_pct].slice(-SPARK_MAX));
  }, [metrics]);

  const haveMetrics = !!metrics && metrics.sample_count > 0;

  const weightRows: ShareRow[] = state
    ? Object.entries(state.upstream_weights)
        .sort(([a], [b]) => a.localeCompare(b))
        .map(([id, w]) => ({
          id,
          label: shortName(id),
          value: typeof w === "number" ? w : 0,
          dim: state.excluded_backends.includes(id),
        }))
    : [];
  // Normalise weights to a 0..1 share so ShareBars reads as routing share.
  const weightTotal = weightRows.reduce((s, r) => s + r.value, 0) || 1;
  const shareRows: ShareRow[] = weightRows.map((r) => ({
    ...r,
    value: r.value / weightTotal,
  }));

  return (
    <>
      {/* ── Hero: live pulse + the headline vitals ─────────────────────────── */}
      <Card
        title="Deck"
        eyebrow="// live flight deck"
        actions={
          <StatusPill status={haveMetrics ? "ok" : "neutral"}>
            {haveMetrics ? "traffic live" : "idle"}
          </StatusPill>
        }
      >
        <div
          style={{
            display: "grid",
            gridTemplateColumns: "minmax(280px, 1.3fr) 2fr",
            gap: 18,
            alignItems: "stretch",
          }}
        >
          <div
            style={{
              height: 150,
              borderRadius: "var(--sl-radius-md)",
              background: "var(--sl-surface-sunk)",
              border: "1px solid var(--sl-hairline)",
              overflow: "hidden",
            }}
          >
            <Heartbeat width={460} height={150} />
          </div>
          <div
            style={{
              display: "grid",
              gridTemplateColumns: "repeat(auto-fit, minmax(150px, 1fr))",
              gap: 12,
            }}
          >
            <KpiStat
              label="P95 latency"
              value={haveMetrics && metrics?.p95_latency_ms != null ? metrics.p95_latency_ms : "—"}
              unit={haveMetrics && metrics?.p95_latency_ms != null ? "ms" : undefined}
              spark={p95Hist.length > 1 ? p95Hist : undefined}
              sparkTone="mint"
              footnote="last 5 min"
            />
            <KpiStat
              label="SLO violations"
              value={haveMetrics ? `${metrics!.slo_violation_pct.toFixed(1)}` : "—"}
              unit={haveMetrics ? "%" : undefined}
              spark={sloHist.length > 1 ? sloHist : undefined}
              sparkTone="graphite"
              footnote="window breach rate"
            />
            <KpiStat
              label="Throughput"
              value={haveMetrics ? metrics!.total_requests.toLocaleString() : "—"}
              footnote="requests in window"
            />
          </div>
        </div>
      </Card>

      {/* ── Stack health grid ──────────────────────────────────────────────── */}
      <Card
        title="Stack health"
        eyebrow="// services"
        actions={
          <StatusPill
            status={
              services == null ? "neutral"
                : services.healthy === services.total ? "ok"
                : services.healthy === 0 ? "crit"
                : "warn"
            }
          >
            {services == null ? "probing" : `${services.healthy}/${services.total} healthy`}
          </StatusPill>
        }
      >
        <div
          style={{
            display: "grid",
            gridTemplateColumns: "repeat(auto-fill, minmax(190px, 1fr))",
            gap: 10,
          }}
        >
          {(services?.services ?? []).map((svc) => (
            <div
              key={svc.name}
              style={{
                background: "var(--sl-surface-sunk)",
                border: "1px solid var(--sl-hairline)",
                borderRadius: "var(--sl-radius-md)",
                padding: "11px 13px",
              }}
            >
              <div
                style={{
                  display: "flex",
                  justifyContent: "space-between",
                  alignItems: "center",
                  gap: 8,
                }}
              >
                <span style={{ fontSize: 13, fontWeight: 600, color: "var(--sl-text)" }}>
                  {svc.name}
                </span>
                <span
                  style={{
                    fontFamily: "var(--sl-font-mono)",
                    fontSize: 9,
                    color: "var(--sl-text-low)",
                    textTransform: "uppercase",
                    letterSpacing: "0.6px",
                  }}
                >
                  {svc.role}
                </span>
              </div>
              <div style={{ marginTop: 8 }}>
                <StatusPill status={svcStatus(svc.healthy, svc.status)}>{svc.status}</StatusPill>
              </div>
              {svc.detail ? (
                <div
                  style={{
                    fontFamily: "var(--sl-font-mono)",
                    fontSize: 10,
                    color: "var(--sl-text-low)",
                    marginTop: 7,
                    wordBreak: "break-word",
                  }}
                >
                  {svc.detail}
                </div>
              ) : null}
            </div>
          ))}
          {services == null ? (
            <div
              style={{
                fontFamily: "var(--sl-font-mono)",
                fontSize: 12,
                color: "var(--sl-text-low)",
                fontStyle: "italic",
              }}
            >
              probing services…
            </div>
          ) : null}
        </div>
      </Card>

      {/* ── Live session metrics (full KPI strip) ──────────────────────────── */}
      <Card title="Live session metrics" eyebrow="// timescaledb · 5 min window">
        {haveMetrics ? (
          <div
            style={{
              display: "grid",
              gridTemplateColumns: "repeat(auto-fill, minmax(170px, 1fr))",
              gap: 12,
            }}
          >
            <KpiStat
              label="P95 latency"
              value={metrics!.p95_latency_ms != null ? metrics!.p95_latency_ms : "—"}
              unit={metrics!.p95_latency_ms != null ? "ms" : undefined}
            />
            <KpiStat
              label="Mean latency"
              value={metrics!.mean_latency_ms != null ? metrics!.mean_latency_ms : "—"}
              unit={metrics!.mean_latency_ms != null ? "ms" : undefined}
            />
            <KpiStat
              label="SLO violations"
              value={metrics!.slo_violation_pct.toFixed(1)}
              unit="%"
            />
            <KpiStat label="Total requests" value={metrics!.total_requests.toLocaleString()} />
            <KpiStat label="Latency samples" value={metrics!.sample_count.toLocaleString()} />
          </div>
        ) : (
          <div
            style={{
              fontFamily: "var(--sl-font-mono)",
              fontSize: 12,
              color: "var(--sl-text-low)",
              padding: "8px 0",
            }}
          >
            {metrics == null
              ? "TimescaleDB not reachable — start traffic and check TIMESCALEDB_URL"
              : "Start traffic (top bar or the Drive page) to see live metrics…"}
          </div>
        )}
      </Card>

      {/* ── Current decision + backend pool weights ────────────────────────── */}
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 18 }}>
        <Card title="Current decision" eyebrow="// decision plane">
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 14 }}>
            <Field label="Operating mode" value={modeLabel(state)} />
            <Field
              label="Last inference"
              value={
                state?.last_inference_age_seconds != null
                  ? `${state.last_inference_age_seconds}s ago`
                  : "—"
              }
            />
            <Field label="Top ranked" value={topRanked(rankings)} tone="ok" />
            <Field label="Lowest ranked" value={bottomRanked(rankings)} tone="warn" />
          </div>
          <div
            style={{
              marginTop: 14,
              paddingTop: 12,
              borderTop: "1px solid var(--sl-hairline-soft)",
            }}
          >
            <div
              style={{
                fontFamily: "var(--sl-font-mono)",
                fontSize: 9.5,
                letterSpacing: "1.2px",
                textTransform: "uppercase",
                color: "var(--sl-text-low)",
                marginBottom: 5,
              }}
            >
              Decision basis
            </div>
            <div style={{ fontSize: 13, color: "var(--sl-text-mid)" }}>
              {decisionBasis(state, rankings)}
            </div>
          </div>
        </Card>

        <Card
          title="Backend pool weights"
          eyebrow="// routing share"
          actions={
            <Badge tone="neutral">
              {state ? `${Object.keys(state.upstream_weights).length} backends` : "—"}
              {state?.excluded_backends.length ? ` · ${state.excluded_backends.length} excluded` : ""}
            </Badge>
          }
        >
          {shareRows.length > 0 ? (
            <ShareBars rows={shareRows} max={1} asPercent />
          ) : (
            <div
              style={{
                fontFamily: "var(--sl-font-mono)",
                fontSize: 12,
                color: "var(--sl-text-low)",
                padding: "8px 0",
              }}
            >
              awaiting backend weights…
            </div>
          )}
        </Card>
      </div>
    </>
  );
}


/* A label/value pair in the decision card. */
function Field({
  label,
  value,
  tone,
}: {
  label: string;
  value: string;
  tone?: "ok" | "warn";
}) {
  const color =
    tone === "ok" ? "var(--sl-ok)" : tone === "warn" ? "var(--sl-warn)" : "var(--sl-text)";
  return (
    <div>
      <div
        style={{
          fontFamily: "var(--sl-font-mono)",
          fontSize: 9.5,
          letterSpacing: "1.2px",
          textTransform: "uppercase",
          color: "var(--sl-text-low)",
          marginBottom: 4,
        }}
      >
        {label}
      </div>
      <div style={{ fontSize: 14, fontWeight: 600, color }}>{value}</div>
    </div>
  );
}
