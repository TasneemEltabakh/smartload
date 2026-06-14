/**
 * tools/demo-ui/web/src/pages/Run.tsx  (cockpit "Drive")
 * ───────────────────────────────────────────────────────
 * One-click load automation + live monitor, rebuilt on the shared kit (dark
 * "Mission Control" theme).
 *
 *   - Pick a load profile (5-phase adaptive shape, spike, anomaly-under-load).
 *   - Start it: the BFF drives the traffic-simulator through the phases over
 *     HTTP and injects the phase anomaly the same way the manual scenarios do.
 *     The live autoscaler reacts within the compose pool (1..5).
 *   - Watch it: live RPS / pool-size / p95 monitor (KpiStat + kit sparklines)
 *     accumulates from a ~2 s livestats poll; a phase bar tracks progress.
 *   - Review: recent-runs list with side-by-side compare (up to 2) and
 *     lost-run ("stale") detection.
 *
 * This is the in-cluster automation path. The canonical publishable artefacts
 * (SUMMARY.md + plots) still come from the host-side harness and are surfaced
 * read-only on the Proof (Benchmarks) page.
 *
 * Degrades gracefully when the BFF is down: a static offline profile preview
 * keeps the cockpit legible, the run button is disabled, and every live panel
 * shows a calm empty state instead of crashing.
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

import { api, type BenchHistoryRun, type BenchProfile, type BenchStatus } from "../api";
import { useDemo } from "../state/DemoStateContext";
import { LIVESTATS_POLL_MS } from "../utils";
import {
  Badge,
  Button,
  Card,
  KpiStat,
  StatusPill,
  type Status,
} from "../ui";
import { SAMPLE_PROFILES } from "./_sampleDrive";

/* ── Dark-theme chart palette (resolved --sl-* dark values) ─────────────────
   recharts paints to <canvas>-ish SVG that can't read CSS custom props on its
   own attributes reliably, so we mirror the dark tokens here as literals. */
const C_RPS = "#4dabf7";   // blue — requests/sec
const C_POOL = "#4ade80";  // mint — pool size (--sl-mint dark)
const C_P95 = "#f5b544";   // amber — p95 latency (--sl-warn dark)
const C_GRID = "#232c36";  // --sl-grid dark
const C_AXIS = "#697585";  // --sl-text-low dark
const COMPARE_COLORS = ["#4dabf7", "#f5b544"];   // run A (blue) / run B (amber)

const TOOLTIP_STYLE: React.CSSProperties = {
  background: "#171d25",          // --sl-surface dark
  border: "1px solid #232c36",    // --sl-hairline dark
  borderRadius: 8,
  color: "#e8edf2",               // --sl-text dark
  fontFamily: "var(--sl-font-mono)",
  fontSize: 11,
};

interface Sample {
  ts: string;
  rps: number | null;
  pool: number;
  p95: number | null;
}

const SERIES_CAP = 150;   // ~5 min at 2 s/sample
const SPARK_CAP = 32;     // recent window for the KPI sparklines


export default function Run() {
  const { action, busy } = useDemo();
  const [profiles, setProfiles] = useState<BenchProfile[] | null>(null);
  const [bffUp, setBffUp] = useState<boolean>(true);
  const [selected, setSelected] = useState<string>("");
  const [status, setStatus] = useState<BenchStatus>({ status: "idle" });
  const [series, setSeries] = useState<Sample[]>([]);
  const seriesRef = useRef<Sample[]>([]);
  const [history, setHistory] = useState<BenchHistoryRun[]>([]);
  const [compareIds, setCompareIds] = useState<string[]>([]);

  // Load profiles once. On failure, fall back to the static catalogue so the
  // picker stays legible offline (clearly flagged, run disabled).
  useEffect(() => {
    api.listBenchProfiles()
      .then((r) => {
        setBffUp(true);
        setProfiles(r.profiles);
        if (r.profiles.length > 0) setSelected(r.profiles[0].id);
      })
      .catch(() => {
        setBffUp(false);
        setProfiles(SAMPLE_PROFILES);
        if (SAMPLE_PROFILES.length > 0) setSelected(SAMPLE_PROFILES[0].id);
      });
  }, []);

  // Poll bench status (~1.5 s).
  useEffect(() => {
    let cancelled = false;
    async function tick() {
      try {
        const s = await api.getBenchStatus();
        if (!cancelled) { setStatus(s); setBffUp(true); }
      } catch {
        if (!cancelled) setBffUp(false);
      }
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

  // Refresh history on mount and whenever a run reaches a terminal state.
  useEffect(() => {
    if (status.status === "running") return;
    let cancelled = false;
    api.getBenchHistory()
      .then((r) => { if (!cancelled) setHistory(r.runs); })
      .catch(() => { /* leave last */ });
    return () => { cancelled = true; };
  }, [status.status]);

  const running = status.status === "running";
  const selectedProfile = profiles?.find((p) => p.id === selected) ?? null;
  const compareRuns = history.filter((r) => compareIds.includes(r.run_id));

  function toggleCompare(runId: string) {
    setCompareIds((prev) =>
      prev.includes(runId) ? prev.filter((x) => x !== runId) : [...prev, runId].slice(-2),
    );
  }

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

  // Live readings + recent sparkline windows.
  const latest = series.length ? series[series.length - 1] : null;
  const rpsSpark = series.map((s) => s.rps ?? 0).slice(-SPARK_CAP);
  const poolSpark = series.map((s) => s.pool).slice(-SPARK_CAP);
  const p95Spark = series.map((s) => s.p95 ?? 0).slice(-SPARK_CAP);
  const haveLive = series.length > 1;

  const showPhaseBar =
    (running || status.status === "done" || status.status === "stopped") && !!status.phase_names;

  return (
    <>
      {/* ── Profile picker + run control ───────────────────────────────────── */}
      <Card
        title="Drive"
        eyebrow="// load profiles"
        actions={<HeadStatus status={status} bffUp={bffUp} />}
      >
        <div style={{ fontSize: 13, color: "var(--sl-text-mid)", maxWidth: 640, lineHeight: 1.6 }}>
          One click drives the traffic-simulator through a timed shape; the live
          autoscaler reacts within the compose pool (1..5). Pick a profile, run
          it, and watch RPS / pool-size / p95 react below.
        </div>

        {!bffUp && (
          <div
            style={{
              marginTop: 14,
              padding: "10px 13px",
              borderRadius: "var(--sl-radius-md)",
              background: "var(--sl-warn-tint)",
              border: "1px solid var(--sl-warn)",
              fontFamily: "var(--sl-font-mono)",
              fontSize: 11.5,
              color: "var(--sl-warn)",
            }}
          >
            BFF unreachable — showing an offline profile preview. Start the demo
            stack to run a profile and stream live stats.
          </div>
        )}

        <div
          style={{
            display: "grid",
            gridTemplateColumns: "repeat(auto-fill, minmax(245px, 1fr))",
            gap: 12,
            marginTop: 16,
          }}
        >
          {(profiles ?? []).map((p) => {
            const isSel = p.id === selected;
            return (
              <button
                key={p.id}
                type="button"
                onClick={() => setSelected(p.id)}
                disabled={running}
                style={{
                  textAlign: "left",
                  padding: "13px 14px",
                  lineHeight: 1.4,
                  cursor: running ? "default" : "pointer",
                  borderRadius: "var(--sl-radius-md)",
                  background: isSel ? "var(--sl-mint-tint)" : "var(--sl-surface-sunk)",
                  border: `1px solid ${isSel ? "var(--sl-mint-line)" : "var(--sl-hairline)"}`,
                  boxShadow: isSel ? "var(--sl-shadow-1)" : "none",
                  opacity: running && !isSel ? 0.5 : 1,
                  transition: "background var(--sl-dur-fast), border-color var(--sl-dur-fast)",
                }}
              >
                <div
                  style={{
                    display: "flex",
                    alignItems: "center",
                    justifyContent: "space-between",
                    gap: 8,
                  }}
                >
                  <span style={{ fontWeight: 700, fontSize: 13.5, color: "var(--sl-text)" }}>
                    {p.label}
                  </span>
                  {isSel ? <Badge tone="mint">selected</Badge> : null}
                </div>
                <div style={{ fontSize: 11.5, color: "var(--sl-text-low)", marginTop: 5 }}>
                  {p.description}
                </div>
                <div style={{ marginTop: 9, display: "flex", gap: 4, flexWrap: "wrap" }}>
                  {p.phases.map((ph, i) => (
                    <span
                      key={i}
                      title={`${ph.name} · ${ph.secs}s · ${ph.users}u${ph.anomaly ? " · anomaly" : ""}`}
                      style={{
                        fontFamily: "var(--sl-font-mono)",
                        fontSize: 9,
                        padding: "2px 6px",
                        borderRadius: 5,
                        background: ph.anomaly ? "var(--sl-warn-tint)" : "var(--sl-surface)",
                        border: `1px solid ${ph.anomaly ? "var(--sl-warn)" : "var(--sl-hairline)"}`,
                        color: ph.anomaly ? "var(--sl-warn)" : "var(--sl-text-low)",
                      }}
                    >
                      {ph.users}u/{ph.secs}s{ph.anomaly ? " ⚡" : ""}
                    </span>
                  ))}
                </div>
              </button>
            );
          })}
          {profiles == null && (
            <div
              style={{
                fontFamily: "var(--sl-font-mono)",
                fontSize: 12,
                color: "var(--sl-text-low)",
                fontStyle: "italic",
              }}
            >
              loading profiles…
            </div>
          )}
        </div>

        <div style={{ display: "flex", gap: 12, alignItems: "center", marginTop: 18, flexWrap: "wrap" }}>
          {!running ? (
            <Button
              variant="primary"
              onClick={start}
              disabled={busy || !selected || !bffUp}
              style={{ fontWeight: 700, letterSpacing: "0.3px" }}
            >
              ▶ Run profile
            </Button>
          ) : (
            <Button
              variant="danger"
              onClick={stop}
              disabled={busy}
              style={{ fontWeight: 700, letterSpacing: "0.3px" }}
            >
              ■ Stop run
            </Button>
          )}
          <RunStatusLine status={status} bffUp={bffUp} />
        </div>

        {/* Phase progress indicator */}
        {showPhaseBar && status.phase_names && (
          <div style={{ marginTop: 18 }}>
            <div style={{ display: "flex", gap: 5 }}>
              {status.phase_names.map((name, i) => {
                const active = i === (status.phase_index ?? -1) && running;
                const done = i < (status.phase_index ?? 0) || status.status === "done";
                return (
                  <div key={`${name}-${i}`} style={{ flex: 1, minWidth: 0 }}>
                    <div
                      style={{
                        height: 6,
                        borderRadius: 3,
                        background: active
                          ? C_RPS
                          : done
                            ? "var(--sl-mint)"
                            : "var(--sl-hairline)",
                        boxShadow: active ? `0 0 10px ${C_RPS}` : "none",
                        transition: "background var(--sl-dur-mid)",
                      }}
                    />
                    <div
                      style={{
                        fontFamily: "var(--sl-font-mono)",
                        fontSize: 9,
                        marginTop: 5,
                        textAlign: "center",
                        color: active ? C_RPS : "var(--sl-text-low)",
                        whiteSpace: "nowrap",
                        overflow: "hidden",
                        textOverflow: "ellipsis",
                      }}
                    >
                      {name}
                    </div>
                  </div>
                );
              })}
            </div>
            <div
              style={{
                marginTop: 9,
                height: 5,
                background: "var(--sl-surface-sunk)",
                border: "1px solid var(--sl-hairline)",
                borderRadius: 3,
                overflow: "hidden",
              }}
            >
              <div
                style={{
                  width: `${progressPct}%`,
                  height: "100%",
                  background: "var(--sl-mint)",
                  transition: "width 0.5s var(--sl-ease)",
                }}
              />
            </div>
          </div>
        )}
      </Card>

      {/* ── Live monitor: KPI strip + line charts ──────────────────────────── */}
      <Card
        title="Live monitor"
        eyebrow="// rps · pool · p95"
        actions={
          <StatusPill status={running ? "ok" : "neutral"}>
            {running ? "streaming" : haveLive ? "idle traffic" : "no traffic"}
          </StatusPill>
        }
      >
        <div
          style={{
            display: "grid",
            gridTemplateColumns: "repeat(auto-fit, minmax(170px, 1fr))",
            gap: 12,
          }}
        >
          <KpiStat
            label="Requests / sec"
            value={latest?.rps != null ? latest.rps : "—"}
            spark={rpsSpark.length > 1 ? rpsSpark : undefined}
            sparkTone="graphite"
            footnote="live offered load"
          />
          <KpiStat
            label="Backend pool"
            value={latest?.pool != null ? latest.pool : "—"}
            unit={latest?.pool != null ? "/ 5" : undefined}
            spark={poolSpark.length > 1 ? poolSpark : undefined}
            sparkTone="mint"
            footnote="autoscaler reaction"
          />
          <KpiStat
            label="P95 latency"
            value={latest?.p95 != null ? latest.p95 : "—"}
            unit={latest?.p95 != null ? "ms" : undefined}
            spark={p95Spark.length > 1 ? p95Spark : undefined}
            sparkTone="graphite"
            footnote="tail latency"
          />
        </div>

        <div
          style={{
            display: "grid",
            gridTemplateColumns: "1fr 1fr",
            gap: 14,
            marginTop: 16,
          }}
        >
          <LiveChart title="Requests / sec" data={series} dataKey="rps" color={C_RPS} />
          <LiveChart
            title="Backend pool size"
            data={series}
            dataKey="pool"
            color={C_POOL}
            yDomain={[0, 6]}
            stepAfter
          />
          <LiveChart title="p95 latency (ms)" data={series} dataKey="p95" color={C_P95} />
          <AboutRun />
        </div>
      </Card>

      {/* ── Run history + compare ──────────────────────────────────────────── */}
      <Card
        title="Recent runs"
        eyebrow="// history · compare"
        actions={
          <Badge tone="neutral">
            click up to 2 to compare · last {history.length}
          </Badge>
        }
      >
        {history.length === 0 ? (
          <div
            style={{
              fontFamily: "var(--sl-font-mono)",
              fontSize: 12,
              color: "var(--sl-text-low)",
              fontStyle: "italic",
              padding: "6px 0",
            }}
          >
            No completed runs yet — run a profile above and it'll be captured here.
          </div>
        ) : (
          <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
            {history.map((r) => {
              const sel = compareIds.indexOf(r.run_id);
              const isSel = sel >= 0;
              return (
                <button
                  key={r.run_id}
                  type="button"
                  onClick={() => toggleCompare(r.run_id)}
                  style={{
                    textAlign: "left",
                    padding: "9px 12px",
                    fontSize: 12,
                    cursor: "pointer",
                    display: "flex",
                    gap: 12,
                    alignItems: "center",
                    borderRadius: "var(--sl-radius-md)",
                    background: "var(--sl-surface-sunk)",
                    border: `1px solid ${isSel ? COMPARE_COLORS[sel] : "var(--sl-hairline)"}`,
                    transition: "border-color var(--sl-dur-fast)",
                  }}
                >
                  <span
                    style={{
                      width: 9,
                      height: 9,
                      borderRadius: 3,
                      flexShrink: 0,
                      background: isSel ? COMPARE_COLORS[sel] : "var(--sl-hairline)",
                      boxShadow: isSel ? `0 0 8px ${COMPARE_COLORS[sel]}` : "none",
                    }}
                  />
                  <span style={{ fontWeight: 600, minWidth: 160, color: "var(--sl-text)" }}>
                    {r.profile_label}
                  </span>
                  <span
                    style={{
                      fontFamily: "var(--sl-font-mono)",
                      fontSize: 10.5,
                      color: "var(--sl-text-low)",
                    }}
                  >
                    {r.started_utc.replace("T", " ").replace("Z", "")}
                  </span>
                  <span
                    style={{
                      marginLeft: "auto",
                      display: "flex",
                      gap: 16,
                      fontFamily: "var(--sl-font-mono)",
                      fontSize: 11,
                      alignItems: "center",
                    }}
                  >
                    <span style={{ color: "var(--sl-text-mid)" }}>
                      pool <strong style={{ color: "var(--sl-mint)" }}>{r.peak_pool ?? "—"}</strong>
                    </span>
                    <span style={{ color: "var(--sl-text-mid)" }}>
                      rps <strong style={{ color: "var(--sl-text)" }}>{r.peak_rps ?? "—"}</strong>
                    </span>
                    <span style={{ color: "var(--sl-text-mid)" }}>
                      p95 <strong style={{ color: "var(--sl-warn)" }}>{r.peak_p95 ?? "—"}</strong>
                    </span>
                    <StatusPill status={r.status === "done" ? "ok" : "warn"} hideDot>
                      {r.status}
                    </StatusPill>
                  </span>
                </button>
              );
            })}
          </div>
        )}

        {compareRuns.length > 0 && (
          <div style={{ marginTop: 18 }}>
            <div
              style={{
                fontFamily: "var(--sl-font-mono)",
                fontSize: 10.5,
                color: "var(--sl-text-low)",
                marginBottom: 10,
                letterSpacing: "0.8px",
                textTransform: "uppercase",
                display: "flex",
                alignItems: "center",
                flexWrap: "wrap",
                gap: 4,
              }}
            >
              Compare — x-axis is seconds into each run
              {compareRuns.map((r, i) => (
                <span key={r.run_id} style={{ color: COMPARE_COLORS[i], marginLeft: 12 }}>
                  ● {r.profile_label}
                </span>
              ))}
            </div>
            <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr 1fr", gap: 14 }}>
              <CompareChart runs={compareRuns} metric="pool" title="Pool size" yDomain={[0, 6]} stepAfter />
              <CompareChart runs={compareRuns} metric="rps" title="Requests / sec" />
              <CompareChart runs={compareRuns} metric="p95" title="p95 latency (ms)" />
            </div>
          </div>
        )}
      </Card>
    </>
  );
}


/* ── Header status pill mapping bench status -> kit Status ──────────────────── */
function HeadStatus({ status, bffUp }: { status: BenchStatus; bffUp: boolean }) {
  if (!bffUp) return <StatusPill status="crit">offline</StatusPill>;
  const map: Record<BenchStatus["status"], { s: Status; t: string }> = {
    running: { s: "ok", t: "running" },
    done: { s: "ok", t: "last run done" },
    stopped: { s: "warn", t: "stopped early" },
    stale: { s: "crit", t: "run lost" },
    idle: { s: "neutral", t: "idle" },
  };
  const { s, t } = map[status.status] ?? { s: "neutral" as Status, t: "idle" };
  return <StatusPill status={s}>{t}</StatusPill>;
}


/* ── In-flight run summary line next to the run button ──────────────────────── */
function RunStatusLine({ status, bffUp }: { status: BenchStatus; bffUp: boolean }) {
  if (!bffUp) {
    return (
      <span style={{ fontSize: 12, color: "var(--sl-crit)" }}>
        ● BFF offline — start the demo stack to run a profile
      </span>
    );
  }
  if (status.status === "running") {
    return (
      <span style={{ fontSize: 12, color: "var(--sl-text-mid)" }}>
        <span style={{ color: C_RPS, fontWeight: 600 }}>● running</span>
        {" — "}
        {status.profile_label}
        {" · phase "}
        <strong style={{ color: "var(--sl-text)" }}>{status.phase}</strong>
        {" · "}
        {status.elapsed_secs}s / {status.total_secs}s
        {status.anomaly_active && (
          <span style={{ color: "var(--sl-warn)", marginLeft: 8, fontWeight: 600 }}>
            ⚡ anomaly active
          </span>
        )}
      </span>
    );
  }
  if (status.status === "done") {
    return (
      <span style={{ fontSize: 12, color: "var(--sl-ok)" }}>
        ● last run complete ({status.profile_label})
      </span>
    );
  }
  if (status.status === "stopped") {
    return <span style={{ fontSize: 12, color: "var(--sl-warn)" }}>● last run stopped early</span>;
  }
  if (status.status === "stale") {
    return (
      <span style={{ fontSize: 12, color: "var(--sl-crit)" }}>
        ● previous run lost (worker died) — safe to start a new one
      </span>
    );
  }
  return (
    <span style={{ fontSize: 12, color: "var(--sl-text-low)" }}>
      idle — pick a profile and run it
    </span>
  );
}


/* ── A single live line chart (recharts, dark-themed) ───────────────────────── */
function LiveChart({
  title,
  data,
  dataKey,
  color,
  yDomain,
  stepAfter,
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
    <div
      style={{
        background: "var(--sl-surface-sunk)",
        border: "1px solid var(--sl-hairline)",
        borderRadius: "var(--sl-radius-md)",
        padding: "12px 14px 8px",
      }}
    >
      <div
        style={{
          display: "flex",
          justifyContent: "space-between",
          alignItems: "baseline",
          marginBottom: 4,
        }}
      >
        <span
          style={{
            fontFamily: "var(--sl-font-mono)",
            fontSize: 10,
            letterSpacing: "1px",
            textTransform: "uppercase",
            color: "var(--sl-text-low)",
          }}
        >
          {title}
        </span>
        <span
          style={{
            fontFamily: "var(--sl-font-mono)",
            fontSize: 18,
            fontWeight: 700,
            color,
            fontVariantNumeric: "tabular-nums",
          }}
        >
          {latest == null ? "—" : latest}
        </span>
      </div>
      {data.length > 1 ? (
        <ResponsiveContainer width="100%" height={160}>
          <LineChart data={data} margin={{ top: 6, right: 8, left: -22, bottom: 0 }}>
            <CartesianGrid stroke={C_GRID} vertical={false} />
            <XAxis dataKey="ts" tick={{ fill: C_AXIS, fontSize: 9 }} minTickGap={48} stroke={C_GRID} />
            <YAxis
              domain={yDomain ?? [0, "auto"]}
              allowDecimals={false}
              tick={{ fill: C_AXIS, fontSize: 10 }}
              stroke={C_GRID}
            />
            <Tooltip contentStyle={TOOLTIP_STYLE} cursor={{ stroke: C_AXIS, strokeWidth: 1 }} />
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
        <div
          style={{
            fontFamily: "var(--sl-font-mono)",
            fontSize: 12,
            color: "var(--sl-text-low)",
            fontStyle: "italic",
            padding: "44px 0",
            textAlign: "center",
          }}
        >
          collecting samples…
        </div>
      )}
    </div>
  );
}


/* ── "About this run" explainer panel ───────────────────────────────────────── */
function AboutRun() {
  return (
    <div
      style={{
        background: "var(--sl-surface-sunk)",
        border: "1px solid var(--sl-hairline)",
        borderRadius: "var(--sl-radius-md)",
        padding: "14px 16px",
      }}
    >
      <div
        style={{
          fontFamily: "var(--sl-font-mono)",
          fontSize: 10,
          letterSpacing: "1px",
          textTransform: "uppercase",
          color: "var(--sl-text-low)",
          marginBottom: 8,
        }}
      >
        About this run
      </div>
      <div style={{ fontSize: 12, color: "var(--sl-text-mid)", lineHeight: 1.65 }}>
        The pool chart is the headline: as offered load rises, the
        forecast-driven autoscaler grows the pool, then shrinks it when load
        drops. The anomaly phase (⚡) slows one backend and publishes an isolate
        event — watch p95 spike then recover.
        <br />
        <br />
        For the canonical, publishable run (SUMMARY.md + plots), use the
        host-side harness:
        <pre
          style={{
            background: "var(--sl-bg)",
            color: "var(--sl-text)",
            border: "1px solid var(--sl-hairline)",
            padding: 9,
            marginTop: 7,
            borderRadius: "var(--sl-radius-sm)",
            fontFamily: "var(--sl-font-mono)",
            fontSize: 11,
            overflow: "auto",
          }}
        >
          {`COMPOSE_PROJECT_NAME=smartload \\\n  python experiments/adaptive-bench/run.py`}
        </pre>
        …then view it under <strong style={{ color: "var(--sl-text)" }}>Proof → Adaptive-bench</strong>.
      </div>
    </div>
  );
}


/* ── Compare chart helpers ──────────────────────────────────────────────────── */
function mergeByT(
  runs: BenchHistoryRun[],
  metric: "pool" | "rps" | "p95",
): Record<string, number | null>[] {
  const rows = new Map<number, Record<string, number | null>>();
  runs.forEach((r) =>
    r.series.forEach((p) => {
      const row = rows.get(p.t) ?? { t: p.t };
      row[r.run_id] = p[metric];
      rows.set(p.t, row);
    }),
  );
  return [...rows.values()].sort((a, b) => (a.t as number) - (b.t as number));
}

function CompareChart({
  runs,
  metric,
  title,
  yDomain,
  stepAfter,
}: {
  runs: BenchHistoryRun[];
  metric: "pool" | "rps" | "p95";
  title: string;
  yDomain?: [number, number];
  stepAfter?: boolean;
}) {
  const data = mergeByT(runs, metric);
  return (
    <div
      style={{
        background: "var(--sl-surface-sunk)",
        border: "1px solid var(--sl-hairline)",
        borderRadius: "var(--sl-radius-md)",
        padding: "12px 14px 8px",
      }}
    >
      <div
        style={{
          fontFamily: "var(--sl-font-mono)",
          fontSize: 10,
          letterSpacing: "1px",
          textTransform: "uppercase",
          color: "var(--sl-text-low)",
          marginBottom: 6,
        }}
      >
        {title}
      </div>
      <ResponsiveContainer width="100%" height={150}>
        <LineChart data={data} margin={{ top: 6, right: 8, left: -24, bottom: 0 }}>
          <CartesianGrid stroke={C_GRID} vertical={false} />
          <XAxis
            dataKey="t"
            tick={{ fill: C_AXIS, fontSize: 9 }}
            minTickGap={30}
            stroke={C_GRID}
            tickFormatter={(t: number) => `${t}s`}
          />
          <YAxis
            domain={yDomain ?? [0, "auto"]}
            allowDecimals={false}
            tick={{ fill: C_AXIS, fontSize: 10 }}
            stroke={C_GRID}
          />
          <Tooltip
            contentStyle={TOOLTIP_STYLE}
            cursor={{ stroke: C_AXIS, strokeWidth: 1 }}
            labelFormatter={(t) => `t=${t}s`}
          />
          {runs.map((r, i) => (
            <Line
              key={r.run_id}
              type={stepAfter ? "stepAfter" : "monotone"}
              dataKey={r.run_id}
              name={r.profile_label}
              stroke={COMPARE_COLORS[i]}
              strokeWidth={2}
              dot={false}
              isAnimationActive={false}
              connectNulls
            />
          ))}
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}
