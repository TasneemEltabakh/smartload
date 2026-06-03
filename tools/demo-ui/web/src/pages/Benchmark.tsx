/**
 * tools/demo-ui/web/src/pages/Benchmark.tsx
 * ──────────────────────────────────────────
 * Surfaces the v1.0.7r baseline-vs-smartload benchmark harness outputs.
 *
 * Read-only: this page does NOT trigger new runs (a UI button would need
 * docker-socket access to run the bash script — punted). Operators run
 *
 *     bash experiments/baseline-vs-smartload/scripts/run_experiment.sh
 *
 * from the repo root, then refresh this page. The BFF lists the runs
 * mounted at /benchmark-results and serves their PNGs + SUMMARY.md.
 *
 * Two columns:
 *   - left: list of runs (newest first), select to view detail
 *   - right: SUMMARY.md prose + 6 plot images for the selected run
 */

import { useEffect, useState } from "react";

import {
  api,
  benchmarkPlotUrl,
  type BenchmarkRun,
} from "../api";
import { CLR_BAD, CLR_MUTED, CLR_OK } from "../utils";


const PLOT_KEYS: { key: string; label: string }[] = [
  { key: "rps",            label: "Sustained RPS over time" },
  { key: "p50_p95_p99",    label: "Latency percentiles (p50 / p95 / p99)" },
  { key: "error_rate",     label: "Failure rate during the run" },
  { key: "recovery_curve", label: "Recovery curve near the anomaly window" },
  { key: "per_phase_p95",  label: "Per-phase p95 (Ramp / Hold / Anomaly / Sustain)" },
  { key: "total_requests", label: "Cumulative request count" },
];


function fmtTimestamp(ts: string): string {
  // Inputs look like 20260603T194102Z — render as 2026-06-03 19:41:02 UTC.
  if (ts.length !== 16 || ts[8] !== "T" || !ts.endsWith("Z")) return ts;
  return `${ts.slice(0, 4)}-${ts.slice(4, 6)}-${ts.slice(6, 8)} ${ts.slice(9, 11)}:${ts.slice(11, 13)}:${ts.slice(13, 15)} UTC`;
}

function fmtKnobs(knobs?: BenchmarkRun["manifest"]["knobs"]): string {
  if (!knobs) return "—";
  const isShort = knobs.SHORT === "1";
  const totalSecs = knobs.SUSTAIN_END_SECS ?? 0;
  const tag = isShort ? "SHORT" : "full";
  return `${tag} · ${totalSecs}s/side · ramp ${knobs.RAMP_USERS}u/${knobs.RAMP_SECS}s · anomaly ${knobs.ANOMALY_HOLD_SECS}s at t=${knobs.ANOMALY_AT_SECS}s`;
}


export default function Benchmark() {
  const [runs, setRuns] = useState<BenchmarkRun[] | null>(null);
  const [listError, setListError] = useState<string | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [summary, setSummary] = useState<string | null>(null);
  const [summaryError, setSummaryError] = useState<string | null>(null);
  const [listNote, setListNote] = useState<string | null>(null);

  // List runs on mount.
  useEffect(() => {
    let cancelled = false;
    async function fetchRuns() {
      try {
        const resp = await api.listBenchmarkRuns();
        if (cancelled) return;
        setRuns(resp.runs);
        setListNote(resp.note ?? null);
        // Auto-select newest if none chosen yet.
        if (!selected && resp.runs.length > 0) {
          setSelected(resp.runs[0].timestamp);
        }
      } catch (err: any) {
        if (!cancelled) setListError(err.message || "failed to list runs");
      }
    }
    fetchRuns();
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Load summary for the selected run.
  useEffect(() => {
    if (!selected) { setSummary(null); return; }
    let cancelled = false;
    async function fetchSummary() {
      setSummary(null);
      setSummaryError(null);
      try {
        const text = await api.getBenchmarkSummary(selected!);
        if (!cancelled) setSummary(text);
      } catch (err: any) {
        if (!cancelled) setSummaryError(err.message || "no SUMMARY.md for this run");
      }
    }
    fetchSummary();
    return () => { cancelled = true; };
  }, [selected]);

  const selectedRun = runs?.find((r) => r.timestamp === selected) ?? null;

  return (
    <div style={{ display: "grid", gridTemplateColumns: "320px 1fr", gap: 12, height: "100%" }}>

      {/* ── Run list ──────────────────────────────────────────────────────── */}
      <div className="card" style={{ margin: 0, alignSelf: "start" }}>
        <h2>Runs</h2>
        <div className="meta">
          {runs == null
            ? "loading…"
            : `${runs.length} run${runs.length === 1 ? "" : "s"} on disk`}
          {listNote ? ` · ${listNote}` : ""}
        </div>

        {listError && (
          <div style={{ color: "var(--bad)", fontSize: 12, marginTop: 6 }}>⚠ {listError}</div>
        )}

        {runs && runs.length === 0 && (
          <div className="muted" style={{ fontSize: 12, fontStyle: "italic", marginTop: 12, lineHeight: 1.5 }}>
            No runs yet. Generate one with:
            <pre style={{
              background: "#0d1117", color: "#e6edf3",
              padding: 8, marginTop: 6, borderRadius: 4,
              fontSize: 11, overflow: "auto",
            }}>
              {`bash experiments/baseline-vs-smartload/scripts/run_experiment.sh\n\n# Or for a quick harness check (~5 min):\nSHORT=1 bash experiments/baseline-vs-smartload/scripts/run_experiment.sh`}
            </pre>
            Then refresh this page.
          </div>
        )}

        {runs && runs.length > 0 && (
          <div style={{ display: "flex", flexDirection: "column", gap: 4, marginTop: 8 }}>
            {runs.map((r) => {
              const isSelected = r.timestamp === selected;
              const isFull = r.manifest.knobs?.SHORT !== "1";
              const isComplete = r.sides_present.length >= 2;
              return (
                <button
                  key={r.timestamp}
                  className="secondary"
                  onClick={() => setSelected(r.timestamp)}
                  style={{
                    textAlign: "left",
                    padding: "8px 10px",
                    background: isSelected ? "var(--ok)" : undefined,
                    color: isSelected ? "#0d1117" : undefined,
                    fontWeight: isSelected ? 600 : 400,
                    fontSize: 12,
                    lineHeight: 1.4,
                  }}
                >
                  <div>{fmtTimestamp(r.timestamp)}</div>
                  <div style={{
                    fontSize: 10,
                    color: isSelected ? "#0d1117" : CLR_MUTED,
                    marginTop: 2,
                  }}>
                    {isFull ? "full" : "SHORT"} ·{" "}
                    <span style={{ color: isSelected ? "#0d1117" : (isComplete ? CLR_OK : CLR_BAD) }}>
                      {r.sides_present.length}/2 sides
                    </span>
                    {" · "}{r.plots.length}/6 plots
                  </div>
                </button>
              );
            })}
          </div>
        )}
      </div>

      {/* ── Run detail ────────────────────────────────────────────────────── */}
      <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>

        {!selected && (
          <div className="card" style={{ margin: 0 }}>
            <div className="muted" style={{ fontSize: 12, fontStyle: "italic" }}>
              Select a run on the left to view its plots + summary.
            </div>
          </div>
        )}

        {selectedRun && (
          <div className="card" style={{ margin: 0 }}>
            <h2>{fmtTimestamp(selectedRun.timestamp)}</h2>
            <div className="meta" style={{ fontFamily: "monospace" }}>
              {fmtKnobs(selectedRun.manifest.knobs)}
            </div>
            <div className="meta" style={{ fontFamily: "monospace", fontSize: 11 }}>
              git={selectedRun.manifest.git_sha?.slice(0, 12) ?? "?"}
              {selectedRun.manifest.git_state ? ` (${selectedRun.manifest.git_state})` : ""}
              {" · sides="}{selectedRun.sides_present.join(", ") || "—"}
            </div>
          </div>
        )}

        {selectedRun && (
          <div className="card" style={{ margin: 0 }}>
            <h2>Summary</h2>
            {summary === null && summaryError === null && (
              <div className="muted" style={{ fontSize: 12 }}>loading…</div>
            )}
            {summaryError && (
              <div className="muted" style={{ fontSize: 12, fontStyle: "italic" }}>
                ⚠ {summaryError}
                <br />
                <span style={{ fontSize: 11 }}>
                  Generate with: <code>python experiments/baseline-vs-smartload/scripts/plot_results.py
                  experiments/baseline-vs-smartload/results/{selectedRun.timestamp}</code>
                </span>
              </div>
            )}
            {summary && (
              <pre style={{
                background: "#0d1117", color: "#e6edf3",
                padding: 12, borderRadius: 4, marginTop: 8,
                fontSize: 12, whiteSpace: "pre-wrap", lineHeight: 1.6,
                fontFamily: "system-ui, sans-serif",
              }}>
                {summary}
              </pre>
            )}
          </div>
        )}

        {selectedRun && PLOT_KEYS.map(({ key, label }) => {
          const present = selectedRun.plots.includes(key);
          return (
            <div className="card" key={key} style={{ margin: 0 }}>
              <h2 style={{ fontSize: 14 }}>{label}</h2>
              {present ? (
                <img
                  src={benchmarkPlotUrl(selectedRun.timestamp, key)}
                  alt={label}
                  style={{
                    maxWidth: "100%",
                    border: "1px solid var(--border)",
                    borderRadius: 4,
                    display: "block",
                  }}
                />
              ) : (
                <div className="muted" style={{ fontSize: 12, fontStyle: "italic" }}>
                  Plot not generated for this run. Run{" "}
                  <code>plot_results.py</code> after the experiment to populate.
                </div>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}
