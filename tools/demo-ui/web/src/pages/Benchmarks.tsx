/**
 * tools/demo-ui/web/src/pages/Benchmarks.tsx  (cockpit "Proof")
 * ──────────────────────────────────────────────────────────────
 * The EVIDENCE surface. Surfaces BOTH benchmark suites the project ships and
 * presents their headline numbers as confident proof cards:
 *   - adaptive (RQ4): pool-grows / pool-shrinks + time-to-react
 *   - baseline-vs-smartload (#148): round-robin vs. the full plane
 *
 * Read-only: this page does NOT trigger canonical runs (those are host-side
 * harnesses — see the command hints). It lists each suite's runs, shows a
 * per-run KPI strip, the SUMMARY body, a themed comparison chart distilled
 * from the headline numbers, and the canonical result plots the harness
 * rendered (served as images by the BFF).
 *
 * Layout: suite tab bar, then a run-history rail (left) + the proof detail
 * column (right). Rebuilt on the shared kit, dark "Mission Control" theme.
 *
 * Degrades gracefully when the BFF / results are absent: it falls back to a
 * clearly-flagged offline preview so the cockpit still reads as a real
 * evidence surface instead of an empty shell.
 */

import { useEffect, useMemo, useState } from "react";
import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  LabelList,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import {
  api,
  benchmarkPlotUrl,
  type BenchKpi,
  type BenchSuite,
  type BenchmarkRun,
} from "../api";
import {
  Badge,
  Card,
  DataTable,
  KpiStat,
  StatusPill,
  Tabs,
  type Column,
  type Status,
} from "../ui";
import {
  SAMPLE_KPIS,
  SAMPLE_RUNS,
  SAMPLE_SUITES,
  SAMPLE_SUMMARY,
} from "./_sampleProof";

/* ── dark-palette chart colours (kept in sync with tokens.css [data-theme=dark]) */
const C_GRAPHITE = "#5b6472"; // "actual" / baseline series
const C_MINT = "#4ade80"; // the highlighted / proof series
const C_GRID = "#232c36";
const C_TEXT_LOW = "#697585";
const C_TEXT = "#e8edf2";
const C_SURFACE = "#171d25";
const C_HAIR = "#232c36";


/** Map a BFF KPI tone onto the kit StatusPill status vocabulary. */
function kpiStatus(tone: BenchKpi["tone"]): Status {
  if (tone === "ok") return "ok";
  if (tone === "warn") return "warn";
  if (tone === "bad") return "crit";
  return "neutral";
}

/** KPI value color for the proof card readout. */
function kpiColor(tone: BenchKpi["tone"]): string {
  if (tone === "ok") return "var(--sl-ok)";
  if (tone === "warn") return "var(--sl-warn)";
  if (tone === "bad") return "var(--sl-crit)";
  return "var(--sl-text)";
}

/** Render an "20260612T091500Z" stamp as a readable UTC string. */
function fmtTimestamp(ts: string): string {
  if (ts.length !== 16 || ts[8] !== "T" || !ts.endsWith("Z")) return ts;
  return `${ts.slice(0, 4)}-${ts.slice(4, 6)}-${ts.slice(6, 8)} ${ts.slice(9, 11)}:${ts.slice(11, 13)}:${ts.slice(13, 15)} UTC`;
}

/** Pull a leading numeric magnitude out of a KPI value like "38%" / "4.2 s". */
function kpiMagnitude(value: string): number | null {
  const m = value.replace(/,/g, "").match(/-?\d+(\.\d+)?/);
  if (!m) return null;
  const n = Number(m[0]);
  return Number.isFinite(n) ? n : null;
}

const HARNESS_HINT: Record<string, string> = {
  adaptive: "COMPOSE_PROJECT_NAME=smartload python experiments/adaptive-bench/run.py",
  baseline: "bash experiments/baseline-vs-smartload/scripts/run_experiment.sh",
};


export default function Benchmarks() {
  const [suites, setSuites] = useState<BenchSuite[] | null>(null);
  const [offline, setOffline] = useState(false);
  const [active, setActive] = useState<string>("adaptive");
  const [runs, setRuns] = useState<BenchmarkRun[] | null>(null);
  const [listNote, setListNote] = useState<string | null>(null);
  const [listError, setListError] = useState<string | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [summary, setSummary] = useState<string | null>(null);
  const [summaryError, setSummaryError] = useState<string | null>(null);
  const [kpis, setKpis] = useState<BenchKpi[]>([]);

  // Suites once. If the BFF is unreachable, fall back to the offline catalogue.
  useEffect(() => {
    api.listBenchSuites()
      .then((r) => {
        if (r.suites.length === 0) {
          setSuites(SAMPLE_SUITES);
          setOffline(true);
          return;
        }
        setSuites(r.suites);
        setOffline(false);
        if (!r.suites.some((s) => s.id === "adaptive")) setActive(r.suites[0].id);
      })
      .catch(() => {
        setSuites(SAMPLE_SUITES);
        setOffline(true);
      });
  }, []);

  // Runs whenever the active suite changes.
  useEffect(() => {
    let cancelled = false;
    setRuns(null); setSelected(null); setSummary(null); setListError(null); setListNote(null);

    if (offline) {
      const sample = SAMPLE_RUNS[active] ?? [];
      setRuns(sample);
      setListNote("offline preview");
      if (sample.length > 0) setSelected(sample[0].timestamp);
      return () => { cancelled = true; };
    }

    api.listBenchmarkRuns(active)
      .then((resp) => {
        if (cancelled) return;
        setRuns(resp.runs);
        setListNote(resp.note ?? null);
        if (resp.runs.length > 0) setSelected(resp.runs[0].timestamp);
      })
      .catch((err) => { if (!cancelled) setListError(err.message || "failed to list runs"); });
    return () => { cancelled = true; };
  }, [active, offline]);

  // Summary + KPIs for the selected run.
  useEffect(() => {
    if (!selected) { setSummary(null); setKpis([]); return; }
    let cancelled = false;
    setSummary(null); setSummaryError(null); setKpis([]);

    if (offline) {
      setSummary(SAMPLE_SUMMARY[active] ?? "");
      setKpis(SAMPLE_KPIS[active] ?? []);
      return () => { cancelled = true; };
    }

    api.getBenchmarkSummary(active, selected)
      .then((text) => { if (!cancelled) setSummary(text); })
      .catch((err) => { if (!cancelled) setSummaryError(err.message || "no SUMMARY.md"); });
    api.getBenchmarkKpis(active, selected)
      .then((r) => { if (!cancelled) setKpis(r.kpis); })
      .catch(() => { if (!cancelled) setKpis([]); });
    return () => { cancelled = true; };
  }, [active, selected, offline]);

  const suite = suites?.find((s) => s.id === active) ?? null;
  const selectedRun = runs?.find((r) => r.timestamp === selected) ?? null;

  const tabItems = (suites ?? []).map((s) => ({ id: s.id, label: s.label }));

  return (
    <>
      {/* ── Header: the evidence surface, suite switch, provenance ─────────── */}
      <Card
        title="Proof"
        eyebrow="// benchmark evidence"
        actions={
          <>
            {offline ? (
              <StatusPill status="warn">offline preview</StatusPill>
            ) : (
              <StatusPill status="ok">results on disk</StatusPill>
            )}
            {tabItems.length > 0 ? (
              <Tabs items={tabItems} value={active} onChange={setActive} />
            ) : null}
          </>
        }
      >
        <div style={{ fontSize: 13, color: "var(--sl-text-mid)", maxWidth: 720, lineHeight: 1.5 }}>
          Published benchmark suites — the headline numbers as proof cards, the run
          SUMMARY, a comparison chart distilled from the results, and the canonical
          plots each run rendered. Read-only; canonical runs are host-side harnesses.
        </div>
        {offline ? (
          <div
            style={{
              marginTop: 12,
              fontFamily: "var(--sl-font-mono)",
              fontSize: 11,
              color: "var(--sl-text-low)",
            }}
          >
            BFF unreachable — showing a static preview. Start the demo BFF for live runs.
          </div>
        ) : null}
      </Card>

      {/* ── History rail + proof detail ──────────────────────────────────── */}
      <div style={{ display: "grid", gridTemplateColumns: "300px 1fr", gap: 18, alignItems: "start" }}>

        {/* ── Run history ───────────────────────────────────────────────── */}
        <Card
          title="Run history"
          eyebrow="// runs"
          actions={
            <Badge tone="neutral">
              {runs == null ? "…" : `${runs.length} run${runs.length === 1 ? "" : "s"}`}
            </Badge>
          }
        >
          {listNote ? (
            <div
              style={{
                fontFamily: "var(--sl-font-mono)",
                fontSize: 10,
                color: "var(--sl-text-low)",
                marginBottom: 8,
              }}
            >
              {listNote}
            </div>
          ) : null}

          {listError ? (
            <div style={{ color: "var(--sl-crit)", fontSize: 12, fontFamily: "var(--sl-font-mono)" }}>
              {listError}
            </div>
          ) : null}

          {runs == null && !listError ? (
            <div style={{ fontFamily: "var(--sl-font-mono)", fontSize: 12, color: "var(--sl-text-low)", fontStyle: "italic" }}>
              loading…
            </div>
          ) : null}

          {runs && runs.length === 0 ? (
            <div style={{ fontSize: 12, color: "var(--sl-text-low)", lineHeight: 1.6 }}>
              No runs yet. Generate one with:
              <pre
                style={{
                  background: "var(--sl-surface-sunk)",
                  border: "1px solid var(--sl-hairline)",
                  color: "var(--sl-text-mid)",
                  padding: 9,
                  marginTop: 7,
                  borderRadius: "var(--sl-radius-sm)",
                  fontFamily: "var(--sl-font-mono)",
                  fontSize: 10.5,
                  whiteSpace: "pre-wrap",
                  wordBreak: "break-word",
                }}
              >
                {HARNESS_HINT[active] ?? suite?.harness}
              </pre>
              then refresh.
            </div>
          ) : null}

          {runs && runs.length > 0 ? (
            <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
              {runs.map((r) => {
                const isSel = r.timestamp === selected;
                const nPlots = suite?.plots.length ?? r.plots.length;
                return (
                  <button
                    key={r.timestamp}
                    type="button"
                    onClick={() => setSelected(r.timestamp)}
                    style={{
                      textAlign: "left",
                      padding: "10px 12px",
                      borderRadius: "var(--sl-radius-md)",
                      cursor: "pointer",
                      background: isSel ? "var(--sl-mint-tint)" : "var(--sl-surface-sunk)",
                      border: `1px solid ${isSel ? "var(--sl-mint-line)" : "var(--sl-hairline)"}`,
                      transition: "background var(--sl-dur-fast), border-color var(--sl-dur-fast)",
                    }}
                  >
                    <div
                      style={{
                        fontFamily: "var(--sl-font-mono)",
                        fontSize: 11.5,
                        fontWeight: 600,
                        color: isSel ? "var(--sl-text)" : "var(--sl-text-mid)",
                      }}
                    >
                      {fmtTimestamp(r.timestamp)}
                    </div>
                    <div
                      style={{
                        display: "flex",
                        alignItems: "center",
                        gap: 6,
                        marginTop: 6,
                        fontFamily: "var(--sl-font-mono)",
                        fontSize: 9,
                        color: "var(--sl-text-low)",
                      }}
                    >
                      <span style={{ color: r.has_summary ? "var(--sl-ok)" : "var(--sl-text-low)" }}>
                        {r.has_summary ? "summary" : "no summary"}
                      </span>
                      <span>·</span>
                      <span>{r.plots.length}/{nPlots} plots</span>
                    </div>
                  </button>
                );
              })}
            </div>
          ) : null}
        </Card>

        {/* ── Proof detail ─────────────────────────────────────────────── */}
        <div style={{ display: "flex", flexDirection: "column", gap: 18, minWidth: 0 }}>
          {!selectedRun ? (
            <Card title="No run selected" eyebrow="// evidence">
              <div style={{ fontSize: 13, color: "var(--sl-text-mid)" }}>
                {runs && runs.length > 0
                  ? "Select a run on the left to view its proof cards, summary, and plots."
                  : "No runs to show for this suite yet."}
              </div>
            </Card>
          ) : (
            <>
              <RunHeader suite={active} run={selectedRun} suiteLabel={suite?.label ?? active} />

              {/* Headline proof cards (per-run KPI strip). */}
              {kpis.length > 0 ? (
                <Card title="Headline results" eyebrow="// proof">
                  <div
                    style={{
                      display: "grid",
                      gridTemplateColumns: "repeat(auto-fill, minmax(170px, 1fr))",
                      gap: 12,
                    }}
                  >
                    {kpis.map((k) => (
                      <KpiStat
                        key={k.label}
                        label={
                          <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
                            {k.label}
                            <StatusPill status={kpiStatus(k.tone)} hideDot>
                              proof
                            </StatusPill>
                          </span>
                        }
                        value={<span style={{ color: kpiColor(k.tone) }}>{k.value}</span>}
                        footnote={k.hint}
                      />
                    ))}
                  </div>

                  <EvidenceChart kpis={kpis} />
                </Card>
              ) : null}

              {/* SUMMARY body. */}
              <Card title="Summary" eyebrow="// SUMMARY.md">
                {summary === null && summaryError === null ? (
                  <div style={{ fontFamily: "var(--sl-font-mono)", fontSize: 12, color: "var(--sl-text-low)" }}>
                    loading…
                  </div>
                ) : null}
                {summaryError ? (
                  <div style={{ fontSize: 12, color: "var(--sl-text-low)", fontStyle: "italic" }}>
                    {summaryError}
                  </div>
                ) : null}
                {summary ? (
                  <pre
                    style={{
                      background: "var(--sl-surface-sunk)",
                      border: "1px solid var(--sl-hairline)",
                      color: "var(--sl-text-mid)",
                      padding: 14,
                      borderRadius: "var(--sl-radius-md)",
                      marginTop: 4,
                      fontFamily: "var(--sl-font-mono)",
                      fontSize: 11.5,
                      whiteSpace: "pre-wrap",
                      lineHeight: 1.6,
                      maxHeight: 360,
                      overflow: "auto",
                    }}
                  >
                    {summary}
                  </pre>
                ) : null}
              </Card>

              {/* Canonical result plots (PNGs rendered by the harness). */}
              {(suite?.plots ?? []).map(({ key, label }) => {
                const present = selectedRun.plots.includes(key);
                return (
                  <Card key={key} title={label} eyebrow="// result plot" flush>
                    <div style={{ padding: "4px 18px 18px" }}>
                      {present && !offline ? (
                        <img
                          src={benchmarkPlotUrl(active, selectedRun.timestamp, key)}
                          alt={label}
                          style={{
                            maxWidth: "100%",
                            border: "1px solid var(--sl-hairline)",
                            borderRadius: "var(--sl-radius-md)",
                            display: "block",
                            background: "var(--sl-surface-sunk)",
                          }}
                        />
                      ) : (
                        <div
                          style={{
                            fontFamily: "var(--sl-font-mono)",
                            fontSize: 12,
                            color: "var(--sl-text-low)",
                            fontStyle: "italic",
                            padding: "24px 0",
                            textAlign: "center",
                            border: "1px dashed var(--sl-hairline)",
                            borderRadius: "var(--sl-radius-md)",
                            background: "var(--sl-surface-sunk)",
                          }}
                        >
                          {offline ? "Plot image unavailable in offline preview." : "Plot not generated for this run."}
                        </div>
                      )}
                    </div>
                  </Card>
                );
              })}
            </>
          )}
        </div>
      </div>
    </>
  );
}


/* ── Run provenance card ──────────────────────────────────────────────────── */
function RunHeader({
  suite,
  run,
  suiteLabel,
}: {
  suite: string;
  run: BenchmarkRun;
  suiteLabel: string;
}) {
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

  type Fact = { label: string; value: string };
  const cols: Column<Fact>[] = [
    { key: "label", header: "field", render: (f) => f.label },
    { key: "value", header: "value", render: (f) => f.value, numeric: true },
  ];

  return (
    <Card
      title={fmtTimestamp(run.timestamp)}
      eyebrow={`// ${suiteLabel}`}
      actions={
        <Badge tone="graphite">
          git {m.git_sha?.slice(0, 12) ?? "?"}{m.git_state ? ` · ${m.git_state}` : ""}
        </Badge>
      }
      flush
    >
      <DataTable<Fact> columns={cols} rows={facts} rowKey={(f) => f.label} />
    </Card>
  );
}


/* ── Evidence comparison chart ────────────────────────────────────────────────
   recharts bar chart distilled from the run's headline numbers. Every numeric
   KPI becomes a bar; the favourable ones light up mint (the "this is the proof"
   series), the rest read graphite. Themed entirely with the dark palette so it
   sits on the kit. Renders nothing when no KPI carries a numeric magnitude. */
function EvidenceChart({ kpis }: { kpis: BenchKpi[] }) {
  const data = useMemo(
    () =>
      kpis
        .map((k) => {
          const mag = kpiMagnitude(k.value);
          return mag == null ? null : { name: k.label, value: mag, display: k.value, tone: k.tone };
        })
        .filter((d): d is { name: string; value: number; display: string; tone: BenchKpi["tone"] } => d != null),
    [kpis],
  );

  if (data.length === 0) return null;

  return (
    <div style={{ marginTop: 16 }}>
      <div
        style={{
          fontFamily: "var(--sl-font-mono)",
          fontSize: 9.5,
          letterSpacing: "1.2px",
          textTransform: "uppercase",
          color: "var(--sl-text-low)",
          marginBottom: 8,
        }}
      >
        Headline numbers
      </div>
      <div style={{ width: "100%", height: Math.max(160, data.length * 46) }}>
        <ResponsiveContainer width="100%" height="100%">
          <BarChart
            data={data}
            layout="vertical"
            margin={{ top: 4, right: 56, bottom: 4, left: 8 }}
            barCategoryGap={14}
          >
            <CartesianGrid horizontal={false} stroke={C_GRID} strokeDasharray="2 5" />
            <XAxis
              type="number"
              stroke={C_GRID}
              tick={{ fill: C_TEXT_LOW, fontSize: 10, fontFamily: "var(--sl-font-mono)" }}
              tickLine={false}
              axisLine={{ stroke: C_GRID }}
            />
            <YAxis
              type="category"
              dataKey="name"
              width={130}
              stroke={C_GRID}
              tick={{ fill: C_TEXT_LOW, fontSize: 10.5, fontFamily: "var(--sl-font-mono)" }}
              tickLine={false}
              axisLine={false}
            />
            <Tooltip
              cursor={{ fill: "rgba(74, 222, 128, 0.06)" }}
              contentStyle={{
                background: C_SURFACE,
                border: `1px solid ${C_HAIR}`,
                borderRadius: 8,
                color: C_TEXT,
                fontFamily: "var(--sl-font-mono)",
                fontSize: 11,
              }}
              labelStyle={{ color: C_TEXT_LOW }}
              itemStyle={{ color: C_TEXT }}
              formatter={(_v: number, _n, item) => [
                (item?.payload as { display?: string })?.display ?? String(_v),
                "value",
              ]}
            />
            <Bar dataKey="value" radius={[0, 4, 4, 0]} maxBarSize={22} isAnimationActive={false}>
              {data.map((d) => (
                <Cell key={d.name} fill={d.tone === "muted" ? C_GRAPHITE : C_MINT} />
              ))}
              <LabelList
                dataKey="display"
                position="right"
                style={{ fill: C_TEXT, fontFamily: "var(--sl-font-mono)", fontSize: 10.5 }}
              />
            </Bar>
          </BarChart>
        </ResponsiveContainer>
      </div>
    </div>
  );
}
