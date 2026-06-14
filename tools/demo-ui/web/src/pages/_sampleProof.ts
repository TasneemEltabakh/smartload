/**
 * tools/demo-ui/web/src/pages/_sampleProof.ts
 * ────────────────────────────────────────────
 * Static fall-back catalogue for the Proof page. When the BFF is unreachable
 * the suite list + run list would otherwise be empty, so the page would read
 * as broken rather than "offline". These shapes mirror the live
 * /benchmark/suites and /benchmark/<suite>/runs surfaces so the evidence
 * cockpit still renders a real-looking proof layout (clearly flagged
 * "offline preview"). They are never written back; plot images cannot be
 * fetched offline, so plot panels show their graceful "not generated" state.
 */

import type { BenchKpi, BenchmarkRun, BenchSuite } from "../api";

/** Suites the harness ships, used when the BFF cannot answer. */
export const SAMPLE_SUITES: BenchSuite[] = [
  {
    id: "adaptive",
    label: "Adaptive bench · RQ4",
    harness: "COMPOSE_PROJECT_NAME=smartload python experiments/adaptive-bench/run.py",
    plots: [
      { key: "pool_vs_load", label: "Pool size vs. offered load" },
      { key: "latency_timeline", label: "p95 latency timeline" },
      { key: "react_time", label: "Time-to-react after anomaly" },
    ],
  },
  {
    id: "baseline",
    label: "Baseline vs. SmartLoad",
    harness: "bash experiments/baseline-vs-smartload/scripts/run_experiment.sh",
    plots: [
      { key: "p95_compare", label: "p95 latency: round-robin vs. full plane" },
      { key: "slo_compare", label: "SLO violation rate by side" },
      { key: "throughput_compare", label: "Sustained throughput by side" },
    ],
  },
];

/** A representative adaptive-bench run for the offline preview. */
export const SAMPLE_RUNS: Record<string, BenchmarkRun[]> = {
  adaptive: [
    {
      timestamp: "20260612T091500Z",
      manifest: {
        timestamp_utc: "20260612T091500Z",
        git_sha: "0eeb388abc12",
        git_state: "clean",
        bench_version: "v1.0.7",
        short: false,
        phases: { PHASE_E_END_SECS: 300, RAMP_USERS: 90 },
        injections: [{ target: "smartload-test-backend-2:8080", at_secs: 180 }],
      },
      plots: ["pool_vs_load", "latency_timeline", "react_time"],
      has_summary: true,
      sides_present: [],
    },
  ],
  baseline: [
    {
      timestamp: "20260611T143000Z",
      manifest: {
        timestamp_utc: "20260611T143000Z",
        git_sha: "a0fbdc7def34",
        git_state: "clean",
        sides: "baseline,smartload",
        knobs: { SHORT: "0", SUSTAIN_END_SECS: 240, RAMP_USERS: 80 },
      },
      plots: ["p95_compare", "slo_compare", "throughput_compare"],
      has_summary: true,
      sides_present: ["baseline", "smartload"],
    },
  ],
};

/** Headline proof cards shown alongside the offline preview run. */
export const SAMPLE_KPIS: Record<string, BenchKpi[]> = {
  adaptive: [
    { label: "Scaling actions", value: "7", hint: "pool grow + shrink decisions", tone: "ok" },
    { label: "Time to react", value: "4.2 s", hint: "anomaly → isolation", tone: "ok" },
    { label: "p95 under load", value: "186 ms", hint: "held below 250 ms SLO", tone: "ok" },
    { label: "Peak pool", value: "6", hint: "instances at ramp peak", tone: "muted" },
  ],
  baseline: [
    { label: "p95 reduction", value: "38%", hint: "vs. round-robin baseline", tone: "ok" },
    { label: "SLO violations", value: "1.9%", hint: "down from 11.4%", tone: "ok" },
    { label: "Throughput", value: "+22%", hint: "sustained req/s gain", tone: "ok" },
    { label: "Agreement", value: "0.94", hint: "decision vs. oracle", tone: "muted" },
  ],
};

/** Illustrative offline SUMMARY body (mirrors the harness SUMMARY.md tone). */
export const SAMPLE_SUMMARY: Record<string, string> = {
  adaptive:
    "Adaptive bench (RQ4) — offline preview\n" +
    "=======================================\n\n" +
    "Pool grew ahead of offered load through the ramp and held p95 below the\n" +
    "250 ms SLO. After the anomaly injection at 180 s, the slow backend was\n" +
    "isolated in 4.2 s and the pool shrank cleanly during drain.\n\n" +
    "(BFF unreachable — showing static preview. Start the demo BFF for live runs.)",
  baseline:
    "Baseline vs. SmartLoad — offline preview\n" +
    "========================================\n\n" +
    "Against the round-robin baseline, the full plane cut p95 by ~38% and SLO\n" +
    "violations from 11.4% to 1.9% at matched offered load, while sustaining\n" +
    "~22% higher throughput.\n\n" +
    "(BFF unreachable — showing static preview. Start the demo BFF for live runs.)",
};
