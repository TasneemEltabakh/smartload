/**
 * tools/demo-ui/web/src/pages/Run.tsx
 * ────────────────────────────────────
 * One-click load automation + live monitor.
 *
 *   - Pick a load profile (5-phase adaptive shape, spike, anomaly-under-load).
 *   - Start it: the BFF drives the traffic-simulator through the phases over
 *     HTTP and injects the phase-D anomaly the same way the manual scenarios
 *     do. The live autoscaler reacts within the compose pool (1..5).
 *   - Watch it: RPS / pool-size / p95 line charts accumulate from a ~2 s
 *     livestats poll; a phase bar tracks progress.
 *
 * This is the in-cluster automation path. The canonical publishable artefacts
 * (SUMMARY.md + plots) still come from the host-side harness and are surfaced
 * read-only on the Benchmarks page.
 */

import { useEffect, useMemo, useRef, useState } from "react";
import {
  CartesianGrid,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import { api, type BenchProfile, type BenchStatus } from "../api";
import { useDemo } from "../state/DemoStateContext";
import {
  CLR_BLUE,
  CLR_MUTED,
  CLR_OK,
  CLR_WARN,
  LIVESTATS_POLL_MS,
  TOOLTIP_STYLE,
} from "../utils";


interface Sample {
  ts: string;
  rps: number | null;
  pool: number;
  p95: number | null;
}

const SERIES_CAP = 150;   // ~5 min at 2 s/sample


export default function Run() {
  const { action, busy } = useDemo();
  const [profiles, setProfiles] = useState<BenchProfile[] | null>(null);
  const [selected, setSelected] = useState<string>("");
  const [status, setStatus] = useState<BenchStatus>({ status: "idle" });
  const [series, setSeries] = useState<Sample[]>([]);
  const seriesRef = useRef<Sample[]>([]);

  // Load profiles once.
  useEffect(() => {
    api.listBenchProfiles()
      .then((r) => {
        setProfiles(r.profiles);
        if (r.profiles.length > 0) setSelected(r.profiles[0].id);
      })
      .catch(() => setProfiles([]));
  }, []);

  // Poll bench status (~1.5 s).
  useEffect(() => {
    let cancelled = false;
    async function tick() {
      try {
        const s = await api.getBenchStatus();
        if (!cancelled) setStatus(s);
      } catch { /* leave last */ }
    }
    tick();
    const id = setInterval(tick, 1_500);
    return () => { cancelled = true; clearInterval(id); };
  }, []);

  // Poll livestats (~2 s) into a rolling series.
  useEffect(() => {
    let cancelled = false;
    async function tick() {
      try {
        const s = await api.getLiveStats(10);
        if (cancelled) return;
        const sample: Sample = {
          ts: new Date().toLocaleTimeString(),
          rps: s.rps ?? null,
          pool: s.pool_size ?? 0,
          p95: s.p95_latency_ms ?? null,
        };
        const next = [...seriesRef.current, sample].slice(-SERIES_CAP);
        seriesRef.current = next;
        setSeries(next);
      } catch { /* leave last */ }
    }
    tick();
    const id = setInterval(tick, LIVESTATS_POLL_MS);
    return () => { cancelled = true; clearInterval(id); };
  }, []);

  const running = status.status === "running";
  const selectedProfile = profiles?.find((p) => p.id === selected) ?? null;

  const progressPct = useMemo(() => {
    if (!running || !status.total_secs) return 0;
    return Math.min(100, Math.round(((status.elapsed_secs ?? 0) / status.total_secs) * 100));
  }, [running, status.elapsed_secs, status.total_secs]);

  function start() {
    if (!selected) return;
    action(`Start ${selectedProfile?.label ?? selected}`, () => api.startBench(selected));
  }
  function stop() {
    action("Stop run", () => api.stopBench());
  }

  return (
    <>
      {/* ── Profile picker + run control ────────────────────────────────── */}
      <div className="card" style={{ marginBottom: 12 }}>
        <h2>Automated Load Profiles</h2>
        <div className="meta">
          One click drives the traffic-simulator through a timed shape; the live
          autoscaler reacts within the compose pool (1..5). Watch it below.
        </div>

        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(230px, 1fr))", gap: 10, marginTop: 12 }}>
          {(profiles ?? []).map((p) => {
            const isSel = p.id === selected;
            return (
              <button
                key={p.id}
                className="secondary"
                onClick={() => setSelected(p.id)}
                disabled={running}
                style={{
                  textAlign: "left", padding: "10px 12px", lineHeight: 1.4,
                  background: isSel ? "rgba(77,171,247,0.12)" : undefined,
                  borderColor: isSel ? "var(--accent)" : undefined,
                }}
              >
                <div style={{ fontWeight: 600, fontSize: 13 }}>{p.label}</div>
                <div className="muted" style={{ fontSize: 11, marginTop: 3 }}>{p.description}</div>
                <div style={{ marginTop: 6, display: "flex", gap: 3, flexWrap: "wrap" }}>
                  {p.phases.map((ph, i) => (
                    <span key={i} title={`${ph.name} · ${ph.secs}s · ${ph.users}u`} style={{
                      fontSize: 9, padding: "1px 5px", borderRadius: 3,
                      background: ph.anomaly ? "rgba(248,81,73,0.18)" : "var(--border)",
                      color: ph.anomaly ? CLR_WARN : CLR_MUTED,
                    }}>
                      {ph.users}u/{ph.secs}s{ph.anomaly ? " ⚡" : ""}
                    </span>
                  ))}
                </div>
              </button>
            );
          })}
          {profiles == null && <div className="muted" style={{ fontSize: 12 }}>loading profiles…</div>}
        </div>

        <div style={{ display: "flex", gap: 10, alignItems: "center", marginTop: 14 }}>
          {!running ? (
            <button onClick={start} disabled={busy || !selected}
                    style={{ padding: "9px 22px", background: "var(--ok)", color: "#0d1117", fontWeight: 700 }}>
              ▶ RUN PROFILE
            </button>
          ) : (
            <button onClick={stop} disabled={busy}
                    style={{ padding: "9px 22px", background: "var(--bad)", color: "#fff", fontWeight: 700 }}>
              ■ STOP RUN
            </button>
          )}
          <RunStatusLine status={status} />
        </div>

        {/* Phase progress bar */}
        {(running || status.status === "done" || status.status === "stopped") && status.phase_names && (
          <div style={{ marginTop: 14 }}>
            <div style={{ display: "flex", gap: 4 }}>
              {status.phase_names.map((name, i) => {
                const active = i === (status.phase_index ?? -1) && running;
                const done = i < (status.phase_index ?? 0) || status.status === "done";
                return (
                  <div key={name} style={{ flex: 1 }}>
                    <div style={{
                      height: 6, borderRadius: 3,
                      background: active ? CLR_BLUE : done ? CLR_OK : "var(--border)",
                    }} />
                    <div style={{
                      fontSize: 9, marginTop: 3, textAlign: "center",
                      color: active ? CLR_BLUE : CLR_MUTED,
                      whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis",
                    }}>
                      {name}
                    </div>
                  </div>
                );
              })}
            </div>
            <div style={{ marginTop: 8, height: 4, background: "var(--border)", borderRadius: 2, overflow: "hidden" }}>
              <div style={{ width: `${progressPct}%`, height: "100%", background: CLR_OK, transition: "width 0.5s" }} />
            </div>
          </div>
        )}
      </div>

      {/* ── Live monitor ────────────────────────────────────────────────── */}
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 }}>
        <LiveChart title="Requests / sec" data={series} dataKey="rps" color={CLR_BLUE} />
        <LiveChart title="Backend pool size" data={series} dataKey="pool" color={CLR_OK}
                   yDomain={[0, 6]} stepAfter />
        <LiveChart title="p95 latency (ms)" data={series} dataKey="p95" color={CLR_WARN} />
        <div className="card" style={{ margin: 0 }}>
          <h2>About this run</h2>
          <div className="muted" style={{ fontSize: 12, lineHeight: 1.6 }}>
            The pool chart is the headline of RQ4: as offered load rises, the
            forecast-driven autoscaler grows the pool, then shrinks it when load
            drops. The anomaly phase (⚡) slows one backend and publishes an
            isolate event — watch p95 spike then recover.
            <br /><br />
            For the canonical, publishable run (SUMMARY.md + plots), use the
            host-side harness:
            <pre style={{ background: "#0d1117", color: "#e6edf3", padding: 8, marginTop: 6, borderRadius: 4, fontSize: 11, overflow: "auto" }}>
              {`COMPOSE_PROJECT_NAME=smartload \\\n  python experiments/adaptive-bench/run.py`}
            </pre>
            …then view it under <strong>Benchmarks → Adaptive-bench</strong>.
          </div>
        </div>
      </div>
    </>
  );
}


function RunStatusLine({ status }: { status: BenchStatus }) {
  if (status.status === "running") {
    return (
      <span style={{ fontSize: 12 }}>
        <span style={{ color: CLR_BLUE, fontWeight: 600 }}>● running</span>
        {" — "}{status.profile_label}{" · phase "}
        <strong>{status.phase}</strong>
        {" · "}{status.elapsed_secs}s / {status.total_secs}s
        {status.anomaly_active && <span style={{ color: CLR_WARN, marginLeft: 8 }}>⚡ anomaly active</span>}
      </span>
    );
  }
  if (status.status === "done") {
    return <span style={{ fontSize: 12, color: CLR_OK }}>● last run complete ({status.profile_label})</span>;
  }
  if (status.status === "stopped") {
    return <span style={{ fontSize: 12, color: CLR_WARN }}>● last run stopped early</span>;
  }
  return <span className="muted" style={{ fontSize: 12 }}>idle — pick a profile and run it</span>;
}


function LiveChart({
  title, data, dataKey, color, yDomain, stepAfter,
}: {
  title: string;
  data: Sample[];
  dataKey: "rps" | "pool" | "p95";
  color: string;
  yDomain?: [number, number];
  stepAfter?: boolean;
}) {
  const latest = data.length ? data[data.length - 1][dataKey] : null;
  return (
    <div className="card" style={{ margin: 0 }}>
      <h2 style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline" }}>
        <span style={{ fontSize: 14 }}>{title}</span>
        <span style={{ fontSize: 18, fontWeight: 700, color }}>
          {latest == null ? "—" : latest}
        </span>
      </h2>
      {data.length > 1 ? (
        <ResponsiveContainer width="100%" height={170}>
          <LineChart data={data} margin={{ top: 6, right: 8, left: -22, bottom: 0 }}>
            <CartesianGrid stroke="#21262d" vertical={false} />
            <XAxis dataKey="ts" tick={{ fill: CLR_MUTED, fontSize: 9 }} minTickGap={48} />
            <YAxis domain={yDomain ?? [0, "auto"]} allowDecimals={false} tick={{ fill: CLR_MUTED, fontSize: 10 }} />
            <Tooltip contentStyle={TOOLTIP_STYLE} />
            <Line
              type={stepAfter ? "stepAfter" : "monotone"}
              dataKey={dataKey}
              stroke={color}
              strokeWidth={2}
              dot={false}
              isAnimationActive={false}
              connectNulls
            />
          </LineChart>
        </ResponsiveContainer>
      ) : (
        <div className="muted" style={{ fontSize: 12, fontStyle: "italic", padding: "24px 0" }}>
          collecting samples…
        </div>
      )}
    </div>
  );
}
