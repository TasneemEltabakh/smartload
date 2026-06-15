/**
 * tools/demo-ui/web/src/results/schema.ts
 * ─────────────────────────────────────────
 * THE DATA CONTRACT (v2).
 *
 * The single, typed seam between "benchmark/audit results" and the presentation
 * UI. Every component renders from these types and NOTHING else — no component
 * reaches past this file to a harness, an endpoint, or a hard-coded number, and
 * no suite/system/metric name is hard-coded in a component. The *list of
 * benchmark suites is itself part of the injected data*: the UI reads which
 * suites exist — and each suite's systems, parameter configurations, and
 * metrics — from the bundle and renders them dynamically. Adding a suite later
 * = the bundle includes it = the UI shows it, with zero component changes.
 *
 * A benchmark suite is a **three-axis** comparison:
 *     systems  ×  parameter configurations  ×  metrics
 * e.g. {harmonic_residual, moving_average, …} × {steady, diurnal, …} × {MAPE, …}
 * or   {PPO, round-robin, …} × {homogeneous, heterogeneous, …} × {p95, SLA, …}.
 * The third axis (parameters/configurations) is first-class so the UI can show
 * "systems vs parameters" grids and per-parameter drill-downs, not just a single
 * one-vs-one. A suite with no parameter sweep simply has one configuration.
 *
 * Design rules baked into the types:
 *   - Every measurement is a `Measure` whose `value` may be `null` ⇒ the UI
 *     renders its defined PENDING state, never a fake number.
 *   - Every value-bearing object carries provenance/freshness so "final VPS" vs
 *     "stale local" vs "sample" vs "pending" is always visible.
 *   - Every metric declares its `direction` (which way is "better"), so the
 *     comparison surfaces mark the winner with no per-metric code.
 *   - An arbitrary number of suites, each with arbitrary systems / configs /
 *     metrics. The layout holds whether there are 2 suites or 20, populated or
 *     fully pending.
 */

export type Direction = "lower-better" | "higher-better" | "target" | "neutral";

export type ResultKind = "final" | "stale" | "sample" | "pending";

export interface Provenance {
  runId: string;
  generatedUtc: string | null;
  host: string;
  gitCommit: string | null;
  kind: ResultKind;
  note?: string;
}

/** A single measured quantity. `value === null` ⇒ render the PENDING state. */
export interface Measure {
  value: number | null;
  ci95?: number | null;
  display?: string | null;
}

export interface MetricDef {
  key: string;
  label: string;
  unit: string;
  direction: Direction;
  /** For `direction: "target"`, the ideal value (e.g. 0.95 for CI-coverage). */
  target?: number;
  hint?: string;
  precision?: number;
}

export type SystemRole =
  | "subject"
  | "baseline"
  | "candidate"
  | "ceiling"
  | "floor"
  | "reference";

export interface SystemDef {
  id: string;
  label: string;
  role: SystemRole;
  hint?: string;
}

/**
 * One point on the parameter/configuration axis — a load profile, a scenario, a
 * load phase, a knob setting, or the cross-parameter roll-up (`isAggregate`).
 * `params` records the underlying dimension(s) so the UI can label/group them.
 */
export interface ConfigDef {
  id: string;
  label: string;
  params?: Record<string, string | number>;
  /** Marks the roll-up across all parameter points (e.g. "all profiles"). */
  isAggregate?: boolean;
}

export type Tone = "ok" | "warn" | "bad" | "muted";

export interface Kpi {
  key: string;
  label: string;
  value: number | null;
  unit: string;
  direction: Direction;
  tone?: Tone;
  hint?: string;
  baselineValue?: number | null;
  baselineLabel?: string;
}

export interface ChartBar {
  label: string;
  value: number | null;
  emphasis?: boolean;
  reference?: boolean;
}

export interface ChartSeries {
  id: string;
  label: string;
  emphasis?: boolean;
  color?: string;
  points: { x: string | number; y: number | null }[];
}

export interface ChartDef {
  key: string;
  title: string;
  kind: "bars" | "lines";
  xLabel?: string;
  yLabel?: string;
  yUnit?: string;
  direction?: Direction;
  bars?: ChartBar[];
  series?: ChartSeries[];
  note?: string;
}

/**
 * A comparison suite: systems × configurations × metrics, plus KPIs and charts.
 *
 * `matrix[systemId][configId][metricKey] = Measure`. A missing entry ⇒ pending.
 */
export interface Suite {
  id: string;
  label: string;
  /** Optional grouping for the benchmark hierarchy, e.g. "System comparison". */
  group?: string;
  /** The research question / what this comparison demonstrates. */
  question: string;
  summary: string;
  provenance: Provenance;
  verdict?: { tone: Tone; text: string };
  kpis: Kpi[];
  systems: SystemDef[];
  /** The parameter axis. At least one entry (use an aggregate for no-sweep suites). */
  configurations: ConfigDef[];
  metrics: MetricDef[];
  /** matrix[systemId][configId][metricKey] = Measure. */
  matrix: Record<string, Record<string, Record<string, Measure>>>;
  /** Which configuration the system×metric matrix shows first. */
  defaultConfigId?: string;
  /** Which metric the systems×parameters grid shows first. */
  primaryMetricKey?: string;
  /** Which system is "this system" (gets the subject marker). */
  subjectId: string;
  charts: ChartDef[];
}

export interface AuditItem {
  id: string;
  label: string;
  status: "pass" | "fail" | "warn" | "fixed" | "info" | "pending";
  metric?: string;
  severity?: "critical" | "high" | "medium" | "low";
  detail?: string;
  ref?: string;
}

export interface AuditSection {
  key: string;
  title: string;
  summary?: string;
  provenance: Provenance;
  verdict?: { tone: Tone; text: string };
  kpis: Kpi[];
  items: AuditItem[];
  stages?: { label: string; value: number | null; unit: string; tone?: Tone; note?: string }[];
}

export interface GrafanaDashboard {
  uid: string;
  title: string;
  description?: string;
}

export interface GrafanaConfig {
  baseUrl: string | null;
  embedQuery?: string;
  dashboards: GrafanaDashboard[];
  note?: string;
}

export interface ResultsBundle {
  schemaVersion: number;
  provenance: Provenance;
  /** Optional explicit group order for the benchmark hierarchy. */
  groups?: string[];
  suites: Suite[];
  audit: AuditSection[];
  grafana: GrafanaConfig;
}

export const SCHEMA_VERSION = 2;
