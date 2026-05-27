import { useCallback, useEffect, useRef, useState } from "react";
import {
  Bar,
  BarChart,
  Cell,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import {
  api,
  type BackendRanking,
  type DemoAlgorithm,
  type DemoMetrics,
  type DemoScenario,
  type DemoState,
} from "../api";

const POLL_MS  = 2_000;
const FEED_MAX = 20;

// Colors matching CSS variables
const CLR_OK    = "#3fb950";
const CLR_WARN  = "#d29922";
const CLR_BAD   = "#f85149";
const CLR_BLUE  = "#4dabf7";
const CLR_MUTED = "#7d8590";

// ── Helpers ───────────────────────────────────────────────────────────────────

function shortName(id: string): string {
  return id.replace("smartload-test-backend-", "b").replace(":8080", "");
}

function modeLabel(state: DemoState | null): string {
  if (!state) return "—";
  if (state.safe_mode) return "Safe Mode (Equal Weights)";
  if (state.rl_mode === "active") return "AI Active";
  return "AI Observing (Shadow)";
}

function modeBadgeClass(state: DemoState | null): string {
  if (!state) return "health-pill";
  if (state.safe_mode) return "health-pill degraded";
  if (state.rl_mode === "active") return "health-pill ok";
  return "health-pill";
}

function decisionBasis(
  state: DemoState | null,
  rankings: BackendRanking[] | null,
): string {
  if (!state) return "Loading…";
  if (!rankings || rankings.length === 0) return "Awaiting first inference…";
  if (state.safe_mode) return "Safe mode active (equal weights)";
  if (state.excluded_backends.length > 0) {
    return `${shortName(state.excluded_backends[0])} excluded (unhealthy)`;
  }
  const sorted = [...rankings].sort((a, b) => b.score - a.score);
  const top    = sorted[0];
  const bottom = sorted[sorted.length - 1];
  if (top && bottom && top.score > 0.8 && bottom.score < 0.35) {
    return `${shortName(bottom.backend_id)} deprioritized (routing active)`;
  }
  if (top) return `RL routing active (${shortName(top.backend_id)} preferred)`;
  return "RL routing active";
}

function topRanked(rankings: BackendRanking[] | null): string {
  if (!rankings || rankings.length === 0) return "—";
  const top = [...rankings].sort((a, b) => b.score - a.score)[0];
  return `${shortName(top.backend_id)} (score ${top.score.toFixed(2)})`;
}

function bottomRanked(rankings: BackendRanking[] | null): string {
  if (!rankings || rankings.length === 0) return "—";
  const bottom = [...rankings].sort((a, b) => a.score - b.score)[0];
  return `${shortName(bottom.backend_id)} (score ${bottom.score.toFixed(2)})`;
}

function barColor(score: number): string {
  return score > 0.7 ? CLR_OK : score > 0.4 ? CLR_WARN : CLR_BAD;
}

// ── SSE feed types ────────────────────────────────────────────────────────────

interface FeedItem {
  id:      string;
  channel: string;
  ts:      string;
  summary: string;
}

function feedSummary(channel: string, envelope: any): string {
  const p = envelope?.payload ?? {};
  if (channel === "smartload.routing") {
    // Routing envelope uses "server_rankings" key
    const rankings = (p.server_rankings ?? p.rankings ?? []) as any[];
    const top = [...rankings].sort((a: any, b: any) => b.score - a.score)[0];
    const topStr = top ? `${shortName(top.backend_id)}=${top.score.toFixed(2)}` : "—";
    return `RL(${p.mode ?? "?"}): ${topStr}`;
  }
  if (channel === "smartload.anomaly") {
    return `Anomaly: ${shortName(p.backend_id ?? "?")} → ${p.status ?? "?"}`;
  }
  if (channel === "smartload.policy") {
    const fields = ((p.changed_fields ?? []) as string[]).join(", ") || "—";
    return `Policy v${p.policy_version ?? "?"}: ${fields}`;
  }
  return channel;
}

function channelColor(channel: string): string {
  if (channel === "smartload.routing") return CLR_BLUE;
  if (channel === "smartload.anomaly") return CLR_WARN;
  if (channel === "smartload.policy")  return "#a78bfa";
  return CLR_MUTED;
}

// ── Toast ─────────────────────────────────────────────────────────────────────

interface ToastState { msg: string; ok: boolean; }

function useToast() {
  const [toast, setToast] = useState<ToastState | null>(null);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const show = useCallback((msg: string, ok: boolean) => {
    if (timerRef.current) clearTimeout(timerRef.current);
    setToast({ msg, ok });
    timerRef.current = setTimeout(() => setToast(null), 3500);
  }, []);
  return { toast, show };
}

// ── Recharts tooltip style ────────────────────────────────────────────────────

const TOOLTIP_STYLE = {
  background: "#161b22",
  border: "1px solid #2d333b",
  color: "#e6edf3",
  fontSize: 12,
};

// ── Main component ────────────────────────────────────────────────────────────

export default function DemoPage() {
  const [state, setState]       = useState<DemoState | null>(null);
  const [error, setError]       = useState<string | null>(null);
  const [busy, setBusy]         = useState(false);
  const [liveMetrics, setLiveMetrics] = useState<DemoMetrics | null>(null);
  const { toast, show }         = useToast();

  // SSE state
  const [sseConnected, setSseConnected] = useState(false);
  const [eventFeed, setEventFeed]       = useState<FeedItem[]>([]);

  // Manual control selectors
  const [degradeTarget, setDegradeTarget] = useState("");
  const [degradeLevel, setDegradeLevel]   = useState<"degraded" | "unhealthy">("unhealthy");
  const [recoverTarget, setRecoverTarget] = useState("");
  const [chaosTarget, setChaosTarget]     = useState("");
  const [chaosDelayMs, setChaosDelayMs]   = useState(800);

  // ── Polling ───────────────────────────────────────────────────────────────

  useEffect(() => {
    let cancelled = false;
    async function tick() {
      try {
        const r = await api.getDemoState();
        if (!cancelled) { setState(r); setError(null); }
      } catch (err: any) {
        if (!cancelled) setError(err.message || "demo/state fetch failed");
      }
    }
    tick();
    const id = setInterval(tick, POLL_MS);
    return () => { cancelled = true; clearInterval(id); };
  }, []);

  // ── SSE ───────────────────────────────────────────────────────────────────

  useEffect(() => {
    const es = new EventSource("/api/ui/events");
    es.onopen  = () => setSseConnected(true);
    es.onerror = () => setSseConnected(false);
    es.onmessage = (evt) => {
      try {
        const msg = JSON.parse(evt.data) as { channel: string; envelope: any };
        const { channel, envelope } = msg;
        // Append to event feed (newest first, capped at FEED_MAX)
        const item: FeedItem = {
          id:      envelope?.event_id ?? String(Date.now()),
          channel,
          ts:      new Date().toLocaleTimeString(),
          summary: feedSummary(channel, envelope),
        };
        setEventFeed((prev) => [item, ...prev].slice(0, FEED_MAX));
      } catch {
        // ignore parse errors
      }
    };
    return () => { es.close(); setSseConnected(false); };
  }, []);

  // Live metrics polling (every 5s)
  useEffect(() => {
    let cancelled = false;
    async function fetchMetrics() {
      try {
        const m = await api.getDemoMetrics();
        if (!cancelled) setLiveMetrics(m);
      } catch { /* no timescaledb configured — silent */ }
    }
    fetchMetrics();
    const id = setInterval(fetchMetrics, 5_000);
    return () => { cancelled = true; clearInterval(id); };
  }, []);

  // Rankings from polling (BFF already translates IP→hostname)
  const displayRankings = state?.last_rankings ?? null;

  // ── Action helper ─────────────────────────────────────────────────────────

  async function run(label: string, fn: () => Promise<unknown>) {
    setBusy(true);
    try {
      await fn();
      show(`${label} — OK`, true);
    } catch (err: any) {
      show(`${label} — ${err.message || "error"}`, false);
    } finally {
      setBusy(false);
    }
  }

  const backends: string[]  = state?.backend_names ?? [];
  const firstBackend         = backends[0] ?? "";

  // Recharts data
  const weightData = state
    ? Object.entries(state.upstream_weights)
        .sort(([a], [b]) => a.localeCompare(b))
        .map(([id, w]) => ({
          name:     shortName(id),
          weight:   typeof w === "number" ? w : 0,
          excluded: state.excluded_backends.includes(id),
        }))
    : [];

  const rankData = displayRankings
    ? [...displayRankings]
        .sort((a, b) => b.score - a.score)
        .map((r) => ({ name: shortName(r.backend_id), score: r.score }))
    : [];

  // ── Render ────────────────────────────────────────────────────────────────

  return (
    <>
      {/* ── Header ─────────────────────────────────────────────────────────── */}
      <div className="card" style={{ marginBottom: 12 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 16, flexWrap: "wrap" }}>
          <div className={modeBadgeClass(state)} style={{ margin: 0, padding: "6px 14px" }}>
            <div className="name" style={{ fontSize: 13 }}>{modeLabel(state)}</div>
          </div>
          <span className="muted" style={{ fontSize: 12 }}>
            Policy: {state?.policy_type ?? "—"}
            {state?.policy_ready === false ? " (not ready)" : ""}
          </span>
          <span className="muted" style={{ fontSize: 12 }}>
            Routing: {state?.algorithm ?? "round_robin"}
          </span>
          <span style={{ fontSize: 11, color: sseConnected ? CLR_OK : CLR_MUTED }}>
            {sseConnected ? "● Live" : "○ Polling"}
          </span>
          <button
            style={{ padding: "8px 20px", fontSize: 13, background: "var(--ok)", color: "#0d1117", fontWeight: 700 }}
            disabled={busy}
            onClick={() => run("Start Traffic", () => api.demoTraffic(20, 5))}
          >
            ▶ START DEMO
          </button>
          <button
            className="secondary"
            style={{ padding: "8px 16px", fontSize: 13 }}
            disabled={busy}
            onClick={() => run("Stop Traffic", () => api.demoTraffic(0, 1))}
          >
            ■ STOP
          </button>
          {error && <span style={{ color: "var(--bad)", fontSize: 12 }}>⚠ {error}</span>}
          <span className="muted" style={{ fontSize: 12, marginLeft: "auto" }}>
            Last inference:{" "}
            {state?.last_inference_age_seconds != null
              ? `${state.last_inference_age_seconds}s ago`
              : "—"}
          </span>
        </div>
      </div>

      {/* ── Current Active Decision ─────────────────────────────────────────── */}
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

      {/* ── Charts row ─────────────────────────────────────────────────────── */}
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12, marginBottom: 12 }}>

        {/* Backend Pool Weights */}
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

        {/* RL Rankings */}
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

      {/* ── Bottom row: event feed + scenario controls ──────────────────────── */}
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12, marginBottom: 12 }}>

        {/* Live Event Feed */}
        <div className="card" style={{ margin: 0 }}>
          <h2>Live Event Feed</h2>
          <div className="meta">
            <span style={{ color: sseConnected ? CLR_OK : CLR_MUTED }}>
              {sseConnected ? "● connected" : "○ connecting…"}
            </span>
            {eventFeed.length > 0 && ` · ${eventFeed.length} events`}
          </div>
          {eventFeed.length === 0 ? (
            <div className="muted" style={{ fontStyle: "italic", padding: "12px 0", fontSize: 12 }}>
              {sseConnected ? "Waiting for events…" : "Connecting to event stream…"}
            </div>
          ) : (
            <div style={{
              maxHeight: 220, overflowY: "auto",
              display: "flex", flexDirection: "column", gap: 3, marginTop: 8,
            }}>
              {eventFeed.map((item) => (
                <div key={item.id} style={{
                  display: "flex", gap: 8, alignItems: "flex-start",
                  fontSize: 11,
                  borderLeft: `3px solid ${channelColor(item.channel)}`,
                  paddingLeft: 6, paddingTop: 2, paddingBottom: 2,
                }}>
                  <span style={{ color: CLR_MUTED, minWidth: 54, flexShrink: 0 }}>{item.ts}</span>
                  <span style={{ color: channelColor(item.channel), minWidth: 72, flexShrink: 0, fontWeight: 600 }}>
                    {item.channel.replace("smartload.", "")}
                  </span>
                  <span style={{ color: "var(--text)", wordBreak: "break-all" }}>{item.summary}</span>
                </div>
              ))}
            </div>
          )}
        </div>

        {/* Scenario Controls */}
        <div className="card" style={{ margin: 0 }}>
          <h2>Scenario Controls</h2>

          {/* Routing algorithm — uses NGINX native directives instead of Python policy */}
          <div style={{ marginBottom: 16 }}>
            <div className="muted" style={{ fontSize: 11, marginBottom: 8 }}>ROUTING ALGORITHM</div>
            <div style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
              {(
                [
                  ["round_robin", "Round-Robin"],
                  ["least_conn",  "Least Conn"],
                  ["random",      "Random"],
                  ["ppo",         "AI / PPO"],
                ] as [DemoAlgorithm, string][]
              ).map(([algo, label]) => (
                <button
                  key={algo}
                  className="secondary"
                  disabled={busy}
                  onClick={() => run(`Algorithm: ${label}`, () => api.demoAlgorithm(algo))}
                  style={{
                    fontSize: 12, padding: "6px 12px",
                    fontWeight: algo === "ppo" ? 700 : 400,
                  }}
                >
                  {label}
                </button>
              ))}
            </div>
            <div className="muted" style={{ fontSize: 10, marginTop: 4 }}>
              Round-Robin / Least Conn / Random are handled natively by NGINX.
              AI / PPO lets the RL engine control weights.
            </div>
          </div>

          <hr style={{ border: "none", borderTop: "1px solid var(--border)", margin: "12px 0" }} />

          <div style={{ marginBottom: 16 }}>
            <div className="muted" style={{ fontSize: 11, marginBottom: 8 }}>DEMO SCENARIOS</div>
            <div style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
              {(
                [
                  ["backend_failure", "Backend Failure"],
                  ["latency_spike",   "Latency Spike"],
                  ["recovery",        "Recovery"],
                  ["high_traffic",    "High Traffic"],
                  ["ai_disabled",     "AI Disabled"],
                ] as [DemoScenario, string][]
              ).map(([id, label]) => (
                <button
                  key={id}
                  className="secondary"
                  disabled={busy}
                  onClick={() => run(label, () => api.demoScenario(id))}
                  style={{ fontSize: 12, padding: "6px 12px" }}
                >
                  {label}
                </button>
              ))}
            </div>
          </div>

          <hr style={{ border: "none", borderTop: "1px solid var(--border)", margin: "12px 0" }} />

          <div className="muted" style={{ fontSize: 11, marginBottom: 8 }}>MANUAL CONTROLS</div>
          <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>

            {/* Degrade */}
            <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
              <span style={{ minWidth: 64, fontSize: 12, color: "var(--muted)" }}>Degrade</span>
              <select
                value={degradeTarget || firstBackend}
                onChange={(e) => setDegradeTarget(e.target.value)}
                style={{ flex: 1 }}
              >
                {backends.map((b) => (
                  <option key={b} value={b}>{shortName(b)}</option>
                ))}
              </select>
              <select
                value={degradeLevel}
                onChange={(e) => setDegradeLevel(e.target.value as "degraded" | "unhealthy")}
                style={{ width: 100 }}
              >
                <option value="unhealthy">unhealthy</option>
                <option value="degraded">degraded</option>
              </select>
              <button
                disabled={busy}
                onClick={() => {
                  const target = (degradeTarget || firstBackend) + ":8080";
                  run(`Degrade ${shortName(target)}`, () => api.demoDegrade(target, degradeLevel));
                }}
                style={{ padding: "6px 12px", fontSize: 12 }}
              >
                Go
              </button>
            </div>

            {/* Recover */}
            <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
              <span style={{ minWidth: 64, fontSize: 12, color: "var(--muted)" }}>Recover</span>
              <select
                value={recoverTarget || firstBackend}
                onChange={(e) => setRecoverTarget(e.target.value)}
                style={{ flex: 1 }}
              >
                {backends.map((b) => (
                  <option key={b} value={b}>{shortName(b)}</option>
                ))}
              </select>
              <button
                disabled={busy}
                onClick={() => {
                  const target = (recoverTarget || firstBackend) + ":8080";
                  run(`Recover ${shortName(target)}`, () => api.demoRecover(target));
                }}
                style={{ padding: "6px 12px", fontSize: 12 }}
              >
                Go
              </button>
            </div>

            {/* Mode toggle */}
            <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
              <span style={{ minWidth: 64, fontSize: 12, color: "var(--muted)" }}>Mode</span>
              <button
                disabled={busy}
                onClick={() => run(
                  state?.safe_mode ? "Enable AI Active" : "Enable Safe Mode",
                  () => api.demoMode(!state?.safe_mode),
                )}
                style={{
                  padding: "6px 12px", fontSize: 12,
                  background: state?.safe_mode ? "var(--ok)" : "var(--warn)",
                  color: "#0d1117",
                }}
              >
                {state?.safe_mode ? "Activate AI" : "Enable Safe Mode"}
              </button>
            </div>

            {/* Traffic presets */}
            <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
              <span style={{ minWidth: 64, fontSize: 12, color: "var(--muted)" }}>Traffic</span>
              {(
                [["Off", 0, 1], ["Low", 5, 1], ["Med", 50, 10], ["High", 200, 50]] as
                  [string, number, number][]
              ).map(([label, users, rate]) => (
                <button
                  key={label}
                  className="secondary"
                  disabled={busy}
                  onClick={() => run(`Traffic ${label}`, () => api.demoTraffic(users, rate))}
                  style={{ padding: "6px 10px", fontSize: 12 }}
                >
                  {label}
                </button>
              ))}
            </div>

            {/* Chaos injection */}
            <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
              <span style={{ minWidth: 64, fontSize: 12, color: "var(--muted)" }}>Chaos</span>
              <select
                value={chaosTarget || firstBackend}
                onChange={(e) => setChaosTarget(e.target.value)}
                style={{ flex: 1 }}
              >
                {backends.map((b) => (
                  <option key={b} value={b}>{shortName(b)}</option>
                ))}
              </select>
              <select
                value={chaosDelayMs}
                onChange={(e) => setChaosDelayMs(Number(e.target.value))}
                style={{ width: 80 }}
              >
                <option value={0}>0ms</option>
                <option value={200}>200ms</option>
                <option value={500}>500ms</option>
                <option value={800}>800ms</option>
                <option value={1500}>1500ms</option>
              </select>
              <button
                disabled={busy}
                onClick={() => {
                  const target = chaosTarget || firstBackend;
                  run(`Chaos ${shortName(target)} ${chaosDelayMs}ms`, () =>
                    api.demoChaos(target, chaosDelayMs),
                  );
                }}
                style={{ padding: "6px 12px", fontSize: 12 }}
              >
                Go
              </button>
            </div>

            {/* Reset All */}
            <div style={{ marginTop: 4 }}>
              <button
                disabled={busy}
                onClick={() => run("Reset All", () => api.demoReset())}
                style={{
                  width: "100%", padding: "8px", fontSize: 12,
                  background: "var(--bad)", color: "#fff",
                }}
              >
                Reset All
              </button>
            </div>
          </div>
        </div>
      </div>

      {/* ── Live Session Metrics ─────────────────────────────────────────────── */}
      <div className="card" style={{ marginBottom: 12 }}>
        <h2>Live Session Metrics</h2>
        <div className="meta">
          Last 5 minutes · TimescaleDB · updates every 5 s
          {liveMetrics && liveMetrics.sample_count > 0
            ? ` · ${liveMetrics.sample_count} latency samples`
            : " · waiting for traffic…"}
        </div>
        {liveMetrics && liveMetrics.sample_count > 0 ? (
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(160px, 1fr))", gap: 12, marginTop: 12 }}>
            <div>
              <div className="muted" style={{ fontSize: 11, marginBottom: 2 }}>P95 LATENCY</div>
              <div style={{
                fontWeight: 700, fontSize: 20,
                color: liveMetrics.p95_latency_ms != null && liveMetrics.p95_latency_ms > 200
                  ? CLR_BAD : CLR_OK,
              }}>
                {liveMetrics.p95_latency_ms != null ? `${liveMetrics.p95_latency_ms} ms` : "—"}
              </div>
            </div>
            <div>
              <div className="muted" style={{ fontSize: 11, marginBottom: 2 }}>MEAN LATENCY</div>
              <div style={{ fontWeight: 700, fontSize: 20 }}>
                {liveMetrics.mean_latency_ms != null ? `${liveMetrics.mean_latency_ms} ms` : "—"}
              </div>
            </div>
            <div>
              <div className="muted" style={{ fontSize: 11, marginBottom: 2 }}>SLO VIOLATIONS</div>
              <div style={{
                fontWeight: 700, fontSize: 20,
                color: liveMetrics.slo_violation_pct > 5 ? CLR_BAD
                  : liveMetrics.slo_violation_pct > 0 ? CLR_WARN : CLR_OK,
              }}>
                {liveMetrics.slo_violation_pct.toFixed(1)}%
              </div>
            </div>
            <div>
              <div className="muted" style={{ fontSize: 11, marginBottom: 2 }}>TOTAL REQUESTS</div>
              <div style={{ fontWeight: 700, fontSize: 20 }}>
                {liveMetrics.total_requests.toLocaleString()}
              </div>
            </div>
          </div>
        ) : (
          <div className="muted" style={{ fontStyle: "italic", padding: "12px 0", fontSize: 12 }}>
            {liveMetrics === null
              ? "TimescaleDB not reachable — start traffic and check TIMESCALEDB_URL"
              : "Start traffic to see live metrics…"}
          </div>
        )}
      </div>

      {/* ── Training Evaluation Results ──────────────────────────────────────── */}
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
              const barClr = r.best
                ? CLR_OK
                : Math.abs(r.reward) < 0.025
                  ? CLR_WARN
                  : CLR_BAD;
              return (
                <tr key={r.name} style={{ background: r.best ? "rgba(0,200,100,0.07)" : undefined }}>
                  <td style={{ fontWeight: r.best ? 700 : 400 }}>
                    {r.name}
                    {r.best && (
                      <span style={{ fontSize: 10, color: CLR_OK, marginLeft: 6 }}>★ BEST</span>
                    )}
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

      {/* Toast */}
      {toast && (
        <div className={`toast ${toast.ok ? "ok" : "bad"}`}>{toast.msg}</div>
      )}
    </>
  );
}
