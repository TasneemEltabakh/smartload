/**
 * tools/demo-ui/web/src/pages/Overview.tsx
 * ─────────────────────────────────────────
 * Landing page — what someone watching the demo sees at-a-glance:
 *   - "Current Active Decision" card (mode / inference age / top / bottom / basis)
 *   - Backend Pool Weights chart (recharts bar)
 *   - RL Rankings chart (recharts bar)
 *   - Live Session Metrics (p95 / mean / SLO viol / total reqs from TimescaleDB)
 *   - Training Evaluation Results table (offline holdout numbers from #27)
 *
 * Read-only — no controls live here. Controls live on /controls.
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
  barColor,
  bottomRanked,
  decisionBasis,
  modeLabel,
  shortName,
  topRanked,
} from "../utils";


export default function Overview() {
  const { state, metrics } = useDemo();
  const displayRankings = state?.last_rankings ?? null;

  const weightData = state
    ? Object.entries(state.upstream_weights)
        .sort(([a], [b]) => a.localeCompare(b))
        .map(([id, w]) => ({
          name: shortName(id),
          weight: typeof w === "number" ? w : 0,
          excluded: state.excluded_backends.includes(id),
        }))
    : [];

  const rankData = displayRankings
    ? [...displayRankings]
        .sort((a, b) => b.score - a.score)
        .map((r) => ({ name: shortName(r.backend_id), score: r.score }))
    : [];

  return (
    <>
      {/* Current Active Decision */}
      <div className="card" style={{ borderLeft: "3px solid var(--accent)", marginBottom: 12 }}>
        <h2 style={{ marginBottom: 8 }}>Current Active Decision</h2>
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(200px, 1fr))", gap: 12 }}>
          <div>
            <div className="muted" style={{ fontSize: 11, marginBottom: 2 }}>OPERATING MODE</div>
            <div style={{ fontWeight: 600 }}>{modeLabel(state)}</div>
          </div>
          <div>
            <div className="muted" style={{ fontSize: 11, marginBottom: 2 }}>LAST INFERENCE</div>
            <div style={{ fontWeight: 600 }}>
              {state?.last_inference_age_seconds != null
                ? `${state.last_inference_age_seconds}s ago`
                : "—"}
            </div>
          </div>
          <div>
            <div className="muted" style={{ fontSize: 11, marginBottom: 2 }}>TOP RANKED</div>
            <div style={{ fontWeight: 600, color: "var(--ok)" }}>{topRanked(displayRankings)}</div>
          </div>
          <div>
            <div className="muted" style={{ fontSize: 11, marginBottom: 2 }}>LOWEST RANKED</div>
            <div style={{ fontWeight: 600, color: "var(--warn)" }}>{bottomRanked(displayRankings)}</div>
          </div>
          <div style={{ gridColumn: "1 / -1" }}>
            <div className="muted" style={{ fontSize: 11, marginBottom: 2 }}>DECISION BASIS</div>
            <div style={{ fontStyle: "italic" }}>{decisionBasis(state, displayRankings)}</div>
          </div>
        </div>
      </div>

      {/* Charts row */}
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12, marginBottom: 12 }}>
        <div className="card" style={{ margin: 0 }}>
          <h2>Backend Pool Weights</h2>
          <div className="meta">
            {state ? `${Object.keys(state.upstream_weights).length} backends` : "loading…"}
            {state?.excluded_backends.length
              ? ` · ${state.excluded_backends.length} excluded`
              : ""}
          </div>
          {weightData.length > 0 ? (
            <ResponsiveContainer width="100%" height={180}>
              <BarChart data={weightData} margin={{ top: 8, right: 8, left: -20, bottom: 0 }}>
                <XAxis dataKey="name" tick={{ fill: CLR_MUTED, fontSize: 11 }} />
                <YAxis tick={{ fill: CLR_MUTED, fontSize: 11 }} />
                <Tooltip
                  contentStyle={TOOLTIP_STYLE}
                  formatter={(val: number, _: string, entry: any) =>
                    [val, entry.payload.excluded ? "EXCLUDED" : "weight"]
                  }
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

        <div className="card" style={{ margin: 0 }}>
          <h2>RL Rankings</h2>
          <div className="meta">
            {displayRankings
              ? `${displayRankings.length} backends ranked`
              : "awaiting first inference"}
            {state?.last_inference_age_seconds != null
              ? ` · ${state.last_inference_age_seconds}s ago`
              : ""}
          </div>
          {rankData.length > 0 ? (
            <ResponsiveContainer width="100%" height={180}>
              <BarChart data={rankData} margin={{ top: 8, right: 8, left: -20, bottom: 0 }}>
                <XAxis dataKey="name" tick={{ fill: CLR_MUTED, fontSize: 11 }} />
                <YAxis domain={[0, 1]} tick={{ fill: CLR_MUTED, fontSize: 11 }} />
                <Tooltip
                  contentStyle={TOOLTIP_STYLE}
                  formatter={(val: number) => [val.toFixed(3), "score"]}
                />
                <Bar dataKey="score" radius={[3, 3, 0, 0]}>
                  {rankData.map((entry, i) => (
                    <Cell key={i} fill={barColor(entry.score)} />
                  ))}
                </Bar>
              </BarChart>
            </ResponsiveContainer>
          ) : (
            <div className="muted" style={{ fontStyle: "italic", padding: "12px 0" }}>
              Awaiting first inference…
            </div>
          )}
        </div>
      </div>

      {/* Live Session Metrics */}
      <div className="card" style={{ marginBottom: 12 }}>
        <h2>Live Session Metrics</h2>
        <div className="meta">
          Last 5 minutes · TimescaleDB · updates every 5 s
          {metrics && metrics.sample_count > 0
            ? ` · ${metrics.sample_count} latency samples`
            : " · waiting for traffic…"}
        </div>
        {metrics && metrics.sample_count > 0 ? (
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(160px, 1fr))", gap: 12, marginTop: 12 }}>
            <div>
              <div className="muted" style={{ fontSize: 11, marginBottom: 2 }}>P95 LATENCY</div>
              <div style={{
                fontWeight: 700, fontSize: 20,
                color: metrics.p95_latency_ms != null && metrics.p95_latency_ms > 200 ? CLR_BAD : CLR_OK,
              }}>
                {metrics.p95_latency_ms != null ? `${metrics.p95_latency_ms} ms` : "—"}
              </div>
            </div>
            <div>
              <div className="muted" style={{ fontSize: 11, marginBottom: 2 }}>MEAN LATENCY</div>
              <div style={{ fontWeight: 700, fontSize: 20 }}>
                {metrics.mean_latency_ms != null ? `${metrics.mean_latency_ms} ms` : "—"}
              </div>
            </div>
            <div>
              <div className="muted" style={{ fontSize: 11, marginBottom: 2 }}>SLO VIOLATIONS</div>
              <div style={{
                fontWeight: 700, fontSize: 20,
                color: metrics.slo_violation_pct > 5 ? CLR_BAD
                  : metrics.slo_violation_pct > 0 ? CLR_WARN : CLR_OK,
              }}>
                {metrics.slo_violation_pct.toFixed(1)}%
              </div>
            </div>
            <div>
              <div className="muted" style={{ fontSize: 11, marginBottom: 2 }}>TOTAL REQUESTS</div>
              <div style={{ fontWeight: 700, fontSize: 20 }}>
                {metrics.total_requests.toLocaleString()}
              </div>
            </div>
          </div>
        ) : (
          <div className="muted" style={{ fontStyle: "italic", padding: "12px 0", fontSize: 12 }}>
            {metrics === null
              ? "TimescaleDB not reachable — start traffic and check TIMESCALEDB_URL"
              : "Start traffic to see live metrics…"}
          </div>
        )}
      </div>

      {/* Training Evaluation Results */}
      <div className="card">
        <h2>Training Evaluation Results</h2>
        <div className="meta">
          Offline · 20-episode holdout · Alibaba dataset · 2 M-step MaskablePPO
        </div>
        <table style={{ marginTop: 8 }}>
          <thead>
            <tr>
              <th>Policy</th>
              <th>Reward</th>
              <th>p95 Latency</th>
              <th>SLO Viol.</th>
              <th style={{ width: "28%" }}>Efficiency</th>
            </tr>
          </thead>
          <tbody>
            {(
              [
                { name: "PPO (RL Engine)",  reward: -0.0056, p95: 15.86, slo: 0.0, best: true  },
                { name: "Round-Robin",       reward: -0.0056, p95: 15.86, slo: 0.0, best: true  },
                { name: "Random (Shadow)",   reward: -0.0211, p95: 15.86, slo: 0.0, best: false },
                { name: "Least Connections", reward: -0.0536, p95: 15.86, slo: 0.0, best: false },
              ] as { name: string; reward: number; p95: number; slo: number; best: boolean }[]
            ).map((r) => {
              const barPct = Math.max(4, Math.round((1 - Math.abs(r.reward) / 0.06) * 100));
              const barClr = r.best ? CLR_OK : Math.abs(r.reward) < 0.025 ? CLR_WARN : CLR_BAD;
              return (
                <tr key={r.name} style={{ background: r.best ? "rgba(0,200,100,0.07)" : undefined }}>
                  <td style={{ fontWeight: r.best ? 700 : 400 }}>
                    {r.name}
                    {r.best && <span style={{ fontSize: 10, color: CLR_OK, marginLeft: 6 }}>★ BEST</span>}
                  </td>
                  <td style={{ color: r.best ? CLR_OK : "var(--text)" }}>{r.reward.toFixed(4)}</td>
                  <td>{r.p95} ms</td>
                  <td>{(r.slo * 100).toFixed(0)}%</td>
                  <td>
                    <div style={{ height: 8, background: "var(--border)", borderRadius: 4, overflow: "hidden" }}>
                      <div style={{ width: `${barPct}%`, height: "100%", background: barClr, borderRadius: 4 }} />
                    </div>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
        <div className="muted" style={{ fontSize: 11, marginTop: 8, fontStyle: "italic" }}>
          PPO matches Round-Robin on homogeneous backends (industry-standard result) and
          outperforms Least-Connections by 9.6×
        </div>
      </div>
    </>
  );
}
