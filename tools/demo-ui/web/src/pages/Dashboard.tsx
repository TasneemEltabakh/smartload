/**
 * tools/demo-ui/web/src/pages/Dashboard.tsx
 * ──────────────────────────────────────────
 * Big-picture landing surface for developers:
 *   - Stack health grid (every watched service, polled every 5 s)
 *   - Live session metrics (p95 / mean / SLO viol / total reqs — TimescaleDB)
 *   - Current decision card (mode / inference age / top / bottom / basis)
 *   - Backend pool weights chart
 *
 * Read-only. Automation lives on /run; manual ops live on /controls.
 */

import {
  Bar,
  BarChart,
  Cell,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import { useDemo } from "../state/DemoStateContext";
import {
  CLR_BAD,
  CLR_MUTED,
  CLR_OK,
  CLR_WARN,
  TOOLTIP_STYLE,
  bottomRanked,
  decisionBasis,
  modeLabel,
  shortName,
  topRanked,
} from "../utils";


function statusColor(healthy: boolean, status: string): string {
  if (healthy) return CLR_OK;
  if (status === "down") return CLR_BAD;
  return CLR_WARN;
}


export default function Dashboard() {
  const { state, metrics, services } = useDemo();
  const rankings = state?.last_rankings ?? null;

  const weightData = state
    ? Object.entries(state.upstream_weights)
        .sort(([a], [b]) => a.localeCompare(b))
        .map(([id, w]) => ({
          name: shortName(id),
          weight: typeof w === "number" ? w : 0,
          excluded: state.excluded_backends.includes(id),
        }))
    : [];

  return (
    <>
      {/* ── Stack health grid ───────────────────────────────────────────── */}
      <div className="card" style={{ marginBottom: 12 }}>
        <h2>Stack Health</h2>
        <div className="meta">
          {services == null
            ? "probing services…"
            : `${services.healthy}/${services.total} services healthy · polled every 5 s`}
        </div>
        <div style={{
          display: "grid",
          gridTemplateColumns: "repeat(auto-fill, minmax(180px, 1fr))",
          gap: 10, marginTop: 12,
        }}>
          {(services?.services ?? []).map((svc) => {
            const clr = statusColor(svc.healthy, svc.status);
            return (
              <div key={svc.name} className="health-pill" style={{ borderLeft: `3px solid ${clr}` }}>
                <div className="name" style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                  <span>{svc.name}</span>
                  <span style={{ fontSize: 9, color: CLR_MUTED, textTransform: "uppercase" }}>{svc.role}</span>
                </div>
                <div className="status" style={{ color: clr }}>● {svc.status}</div>
                {svc.detail && (
                  <div className="muted" style={{ fontSize: 10, marginTop: 2, fontFamily: "monospace" }}>
                    {svc.detail}
                  </div>
                )}
              </div>
            );
          })}
          {services == null && (
            <div className="muted" style={{ fontStyle: "italic", fontSize: 12 }}>loading…</div>
          )}
        </div>
      </div>

      {/* ── Live session metrics ────────────────────────────────────────── */}
      <div className="card" style={{ marginBottom: 12 }}>
        <h2>Live Session Metrics</h2>
        <div className="meta">
          Last 5 minutes · TimescaleDB
          {metrics && metrics.sample_count > 0
            ? ` · ${metrics.sample_count} latency samples`
            : " · waiting for traffic…"}
        </div>
        {metrics && metrics.sample_count > 0 ? (
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(160px, 1fr))", gap: 12, marginTop: 12 }}>
            <Kpi label="P95 LATENCY"
                 value={metrics.p95_latency_ms != null ? `${metrics.p95_latency_ms} ms` : "—"}
                 color={metrics.p95_latency_ms != null && metrics.p95_latency_ms > 200 ? CLR_BAD : CLR_OK} />
            <Kpi label="MEAN LATENCY"
                 value={metrics.mean_latency_ms != null ? `${metrics.mean_latency_ms} ms` : "—"} />
            <Kpi label="SLO VIOLATIONS"
                 value={`${metrics.slo_violation_pct.toFixed(1)}%`}
                 color={metrics.slo_violation_pct > 5 ? CLR_BAD : metrics.slo_violation_pct > 0 ? CLR_WARN : CLR_OK} />
            <Kpi label="TOTAL REQUESTS" value={metrics.total_requests.toLocaleString()} />
          </div>
        ) : (
          <div className="muted" style={{ fontStyle: "italic", padding: "12px 0", fontSize: 12 }}>
            {metrics === null
              ? "TimescaleDB not reachable — start traffic and check TIMESCALEDB_URL"
              : "Start traffic (top bar or the Run page) to see live metrics…"}
          </div>
        )}
      </div>

      {/* ── Current decision + pool weights ─────────────────────────────── */}
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 }}>
        <div className="card" style={{ margin: 0, borderLeft: "3px solid var(--accent)" }}>
          <h2>Current Decision</h2>
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 }}>
            <Field label="OPERATING MODE" value={modeLabel(state)} />
            <Field label="LAST INFERENCE"
                   value={state?.last_inference_age_seconds != null ? `${state.last_inference_age_seconds}s ago` : "—"} />
            <Field label="TOP RANKED" value={topRanked(rankings)} color={CLR_OK} />
            <Field label="LOWEST RANKED" value={bottomRanked(rankings)} color={CLR_WARN} />
          </div>
          <div style={{ marginTop: 12 }}>
            <div className="muted" style={{ fontSize: 11, marginBottom: 2 }}>DECISION BASIS</div>
            <div style={{ fontStyle: "italic" }}>{decisionBasis(state, rankings)}</div>
          </div>
        </div>

        <div className="card" style={{ margin: 0 }}>
          <h2>Backend Pool Weights</h2>
          <div className="meta">
            {state ? `${Object.keys(state.upstream_weights).length} backends` : "loading…"}
            {state?.excluded_backends.length ? ` · ${state.excluded_backends.length} excluded` : ""}
          </div>
          {weightData.length > 0 ? (
            <ResponsiveContainer width="100%" height={180}>
              <BarChart data={weightData} margin={{ top: 8, right: 8, left: -20, bottom: 0 }}>
                <XAxis dataKey="name" tick={{ fill: CLR_MUTED, fontSize: 11 }} />
                <YAxis tick={{ fill: CLR_MUTED, fontSize: 11 }} />
                <Tooltip
                  contentStyle={TOOLTIP_STYLE}
                  formatter={(val: number, _: string, entry: any) =>
                    [val, entry.payload.excluded ? "EXCLUDED" : "weight"]}
                />
                <Bar dataKey="weight" radius={[3, 3, 0, 0]}>
                  {weightData.map((entry, i) => (
                    <Cell key={i} fill={entry.excluded ? CLR_BAD : CLR_OK} />
                  ))}
                </Bar>
              </BarChart>
            </ResponsiveContainer>
          ) : (
            <div className="muted" style={{ padding: "12px 0" }}>loading…</div>
          )}
        </div>
      </div>
    </>
  );
}


function Kpi({ label, value, color }: { label: string; value: string; color?: string }) {
  return (
    <div>
      <div className="muted" style={{ fontSize: 11, marginBottom: 2 }}>{label}</div>
      <div style={{ fontWeight: 700, fontSize: 20, color: color ?? "var(--text)" }}>{value}</div>
    </div>
  );
}

function Field({ label, value, color }: { label: string; value: string; color?: string }) {
  return (
    <div>
      <div className="muted" style={{ fontSize: 11, marginBottom: 2 }}>{label}</div>
      <div style={{ fontWeight: 600, color: color ?? "var(--text)" }}>{value}</div>
    </div>
  );
}
