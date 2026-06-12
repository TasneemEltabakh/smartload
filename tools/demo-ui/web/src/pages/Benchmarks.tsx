/**
 * tools/demo-ui/web/src/pages/Benchmarks.tsx
 * ───────────────────────────────────────────
 * Surfaces BOTH benchmark suites the project ships:
 *   - adaptive-bench (RQ4): pool-grows / pool-shrinks + time-to-react
 *   - baseline-vs-smartload (#148): RR vs full plane
 *
 * Read-only: this page does NOT trigger canonical runs (those are host-side
 * harnesses — see the command hints). Use the Run page for the in-cluster
 * one-click load profiles. Here we list each suite's runs, show their plots +
 * SUMMARY.md, and pull a few headline facts straight off the manifest.
 *
 * Layout: a suite tab bar, then run-list (left) + run detail (right).
 */

import { useEffect, useState } from "react";

import {
  api,
  benchmarkPlotUrl,
  type BenchKpi,
  type BenchSuite,
  type BenchmarkRun,
} from "../api";
import { CLR_BAD, CLR_MUTED, CLR_OK, CLR_WARN } from "../utils";


function kpiColor(tone: BenchKpi["tone"]): string {
  if (tone === "ok") return CLR_OK;
  if (tone === "warn") return CLR_WARN;
  if (tone === "bad") return CLR_BAD;
  return "var(--text)";
}


function fmtTimestamp(ts: string): string {
  if (ts.length !== 16 || ts[8] !== "T" || !ts.endsWith("Z")) return ts;
  return `${ts.slice(0, 4)}-${ts.slice(4, 6)}-${ts.slice(6, 8)} ${ts.slice(9, 11)}:${ts.slice(11, 13)}:${ts.slice(13, 15)} UTC`;
}

const HARNESS_HINT: Record<string, string> = {
  adaptive: "COMPOSE_PROJECT_NAME=smartload python experiments/adaptive-bench/run.py",
  baseline: "bash experiments/baseline-vs-smartload/scripts/run_experiment.sh",
};


export default function Benchmarks() {
  const [suites, setSuites] = useState<BenchSuite[] | null>(null);
  const [active, setActive] = useState<string>("adaptive");
  const [runs, setRuns] = useState<BenchmarkRun[] | null>(null);
  const [listNote, setListNote] = useState<string | null>(null);
  const [listError, setListError] = useState<string | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [summary, setSummary] = useState<string | null>(null);
  const [summaryError, setSummaryError] = useState<string | null>(null);
  const [kpis, setKpis] = useState<BenchKpi[]>([]);

  // Suites once.
  useEffect(() => {
    api.listBenchSuites()
      .then((r) => {
        setSuites(r.suites);
        if (r.suites.length && !r.suites.some((s) => s.id === "adaptive")) {
          setActive(r.suites[0].id);
        }
      })
      .catch(() => setSuites([]));
  }, []);

  // Runs whenever the active suite changes.
  useEffect(() => {
    let cancelled = false;
    setRuns(null); setSelected(null); setSummary(null); setListError(null); setListNote(null);
    api.listBenchmarkRuns(active)
      .then((resp) => {
        if (cancelled) return;
        setRuns(resp.runs);
        setListNote(resp.note ?? null);
        if (resp.runs.length > 0) setSelected(resp.runs[0].timestamp);
      })
      .catch((err) => { if (!cancelled) setListError(err.message || "failed to list runs"); });
    return () => { cancelled = true; };
  }, [active]);

  // Summary + KPIs for the selected run.
  useEffect(() => {
    if (!selected) { setSummary(null); setKpis([]); return; }
    let cancelled = false;
    setSummary(null); setSummaryError(null); setKpis([]);
    api.getBenchmarkSummary(active, selected)
      .then((text) => { if (!cancelled) setSummary(text); })
      .catch((err) => { if (!cancelled) setSummaryError(err.message || "no SUMMARY.md"); });
    api.getBenchmarkKpis(active, selected)
      .then((r) => { if (!cancelled) setKpis(r.kpis); })
      .catch(() => { if (!cancelled) setKpis([]); });
    return () => { cancelled = true; };
  }, [active, selected]);

  const suite = suites?.find((s) => s.id === active) ?? null;
  const selectedRun = runs?.find((r) => r.timestamp === selected) ?? null;

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12, height: "100%" }}>

      {/* ── Suite tabs ────────────────────────────────────────────────────── */}
      <div style={{ display: "flex", gap: 8 }}>
        {(suites ?? []).map((s) => {
          const on = s.id === active;
          return (
            <button
              key={s.id}
              className="secondary"
              onClick={() => setActive(s.id)}
              style={{
                padding: "8px 16px", fontSize: 13, fontWeight: on ? 700 : 400,
                background: on ? "var(--accent)" : undefined,
                color: on ? "#0d1117" : undefined,
              }}
            >
              {s.label}
            </button>
          );
        })}
      </div>

      <div style={{ display: "grid", gridTemplateColumns: "300px 1fr", gap: 12, flex: 1, minHeight: 0 }}>

        {/* ── Run list ────────────────────────────────────────────────────── */}
        <div className="card" style={{ margin: 0, alignSelf: "start" }}>
          <h2>Runs</h2>
          <div className="meta">
            {runs == null ? "loading…" : `${runs.length} run${runs.length === 1 ? "" : "s"} on disk`}
            {listNote ? ` · ${listNote}` : ""}
          </div>

          {listError && <div style={{ color: "var(--bad)", fontSize: 12, marginTop: 6 }}>⚠ {listError}</div>}

          {runs && runs.length === 0 && (
            <div className="muted" style={{ fontSize: 12, fontStyle: "italic", marginTop: 12, lineHeight: 1.5 }}>
              No runs yet. Generate one with:
              <pre style={{ background: "#0d1117", color: "#e6edf3", padding: 8, marginTop: 6, borderRadius: 4, fontSize: 11, overflow: "auto" }}>
                {HARNESS_HINT[active] ?? suite?.harness}
              </pre>
              then refresh.
            </div>
          )}

          {runs && runs.length > 0 && (
            <div style={{ display: "flex", flexDirection: "column", gap: 4, marginTop: 8 }}>
              {runs.map((r) => {
                const isSel = r.timestamp === selected;
                const nPlots = suite?.plots.length ?? r.plots.length;
                return (
                  <button
                    key={r.timestamp}
                    className="secondary"
                    onClick={() => setSelected(r.timestamp)}
                    style={{
                      textAlign: "left", padding: "8px 10px", fontSize: 12, lineHeight: 1.4,
                      background: isSel ? "var(--ok)" : undefined,
                      color: isSel ? "#0d1117" : undefined,
                      fontWeight: isSel ? 600 : 400,
                    }}
                  >
                    <div>{fmtTimestamp(r.timestamp)}</div>
                    <div style={{ fontSize: 10, color: isSel ? "#0d1117" : CLR_MUTED, marginTop: 2 }}>
                      <span style={{ color: isSel ? "#0d1117" : (r.has_summary ? CLR_OK : CLR_BAD) }}>
                        {r.has_summary ? "summary" : "no summary"}
                      </span>
                      {" · "}{r.plots.length}/{nPlots} plots
                    </div>
                  </button>
                );
              })}
            </div>
          )}
        </div>

        {/* ── Run detail ──────────────────────────────────────────────────── */}
        <div style={{ display: "flex", flexDirection: "column", gap: 12, overflow: "auto", minHeight: 0 }}>
          {!selectedRun && (
            <div className="card" style={{ margin: 0 }}>
              <div className="muted" style={{ fontSize: 12, fontStyle: "italic" }}>
                {runs && runs.length > 0
                  ? "Select a run on the left to view its plots + summary."
                  : "No runs to show for this suite yet."}
              </div>
            </div>
          )}

          {selectedRun && <RunHeader suite={active} run={selectedRun} />}

          {selectedRun && kpis.length > 0 && (
            <div className="card" style={{ margin: 0 }}>
              <h2>Headline results</h2>
              <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(150px, 1fr))", gap: 10, marginTop: 4 }}>
                {kpis.map((k) => (
                  <div key={k.label} className="health-pill" style={{ borderLeft: `3px solid ${kpiColor(k.tone)}` }}>
                    <div className="muted" style={{ fontSize: 10, textTransform: "uppercase", letterSpacing: 0.3 }}>{k.label}</div>
                    <div style={{ fontWeight: 700, fontSize: 22, color: kpiColor(k.tone), margin: "2px 0" }}>{k.value}</div>
                    <div className="muted" style={{ fontSize: 10, lineHeight: 1.3 }}>{k.hint}</div>
                  </div>
                ))}
              </div>
            </div>
          )}

          {selectedRun && (
            <div className="card" style={{ margin: 0 }}>
              <h2>Summary</h2>
              {summary === null && summaryError === null && <div className="muted" style={{ fontSize: 12 }}>loading…</div>}
              {summaryError && (
                <div className="muted" style={{ fontSize: 12, fontStyle: "italic" }}>⚠ {summaryError}</div>
              )}
              {summary && (
                <pre style={{
                  background: "#0d1117", color: "#e6edf3", padding: 12, borderRadius: 4, marginTop: 8,
                  fontSize: 12, whiteSpace: "pre-wrap", lineHeight: 1.6, fontFamily: "system-ui, sans-serif",
                }}>
                  {summary}
                </pre>
              )}
            </div>
          )}

          {selectedRun && (suite?.plots ?? []).map(({ key, label }) => {
            const present = selectedRun.plots.includes(key);
            return (
              <div className="card" key={key} style={{ margin: 0 }}>
                <h2 style={{ fontSize: 14 }}>{label}</h2>
                {present ? (
                  <img
                    src={benchmarkPlotUrl(active, selectedRun.timestamp, key)}
                    alt={label}
                    style={{ maxWidth: "100%", border: "1px solid var(--border)", borderRadius: 4, display: "block" }}
                  />
                ) : (
                  <div className="muted" style={{ fontSize: 12, fontStyle: "italic" }}>
                    Plot not generated for this run.
                  </div>
                )}
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}


function RunHeader({ suite, run }: { suite: string; run: BenchmarkRun }) {
  const m = run.manifest;
  const facts: { label: string; value: string }[] = [];

  if (suite === "adaptive") {
    if (m.bench_version) facts.push({ label: "BENCH", value: m.bench_version });
    facts.push({ label: "MODE", value: m.short ? "short" : "full" });
    if (m.phases?.PHASE_E_END_SECS) facts.push({ label: "DURATION", value: `${m.phases.PHASE_E_END_SECS}s` });
    if (m.phases?.RAMP_USERS) facts.push({ label: "PEAK USERS", value: String(m.phases.RAMP_USERS) });
    const inj = m.injections?.[0] as Record<string, unknown> | undefined;
    if (inj?.target) facts.push({ label: "ANOMALY TARGET", value: String(inj.target) });
  } else {
    if (m.knobs) {
      facts.push({ label: "MODE", value: m.knobs.SHORT === "1" ? "short" : "full" });
      if (m.knobs.SUSTAIN_END_SECS) facts.push({ label: "PER SIDE", value: `${m.knobs.SUSTAIN_END_SECS}s` });
      if (m.knobs.RAMP_USERS) facts.push({ label: "RAMP USERS", value: String(m.knobs.RAMP_USERS) });
    }
    facts.push({ label: "SIDES", value: run.sides_present.join(", ") || "—" });
  }

  return (
    <div className="card" style={{ margin: 0 }}>
      <h2>{fmtTimestamp(run.timestamp)}</h2>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(130px, 1fr))", gap: 10, marginTop: 4 }}>
        {facts.map((f) => (
          <div key={f.label}>
            <div className="muted" style={{ fontSize: 10, marginBottom: 2 }}>{f.label}</div>
            <div style={{ fontWeight: 600, fontSize: 13, fontFamily: "monospace" }}>{f.value}</div>
          </div>
        ))}
      </div>
      <div className="meta" style={{ fontFamily: "monospace", fontSize: 11, marginTop: 8 }}>
        git={m.git_sha?.slice(0, 12) ?? "?"}{m.git_state ? ` (${m.git_state})` : ""}
      </div>
    </div>
  );
}
