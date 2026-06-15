/**
 * tools/demo-ui/web/src/results/adapter.ts
 * ──────────────────────────────────────────
 * THE ONE ADAPTER. Every transformation from "raw results input" to the typed
 * contract (schema.ts) lives here, and every derived presentation concern
 * (formatting a value, deciding which system wins a metric for a given
 * parameter configuration, computing a delta vs a baseline) is a pure helper
 * exported from here. Components import these; they never reformat or re-rank
 * inline. When the VPS format is finalised you adjust THIS file once.
 *
 * `normalizeBundle` is defensive: it accepts a loosely-typed JSON blob (whatever
 * the seam delivers) and returns a `ResultsBundle` with sane defaults and a
 * pending fallback, so a malformed or half-written results file degrades to the
 * PENDING state instead of crashing the presentation.
 */

import {
  SCHEMA_VERSION,
  type AuditSection,
  type ChartDef,
  type ConfigDef,
  type Direction,
  type GrafanaConfig,
  type Kpi,
  type Measure,
  type MetricDef,
  type Provenance,
  type ResultKind,
  type ResultsBundle,
  type Suite,
  type SystemDef,
  type Tone,
} from "./schema";

/* ── provenance ────────────────────────────────────────────────────────────── */

const PENDING_PROVENANCE: Provenance = {
  runId: "pending",
  generatedUtc: null,
  host: "—",
  gitCommit: null,
  kind: "pending",
  note: "Awaiting an updated benchmark run.",
};

function asProvenance(raw: any, fallback: Provenance = PENDING_PROVENANCE): Provenance {
  if (!raw || typeof raw !== "object") return fallback;
  const kind = (["final", "stale", "sample", "pending"] as ResultKind[]).includes(raw.kind)
    ? (raw.kind as ResultKind)
    : fallback.kind;
  return {
    runId: str(raw.runId, fallback.runId),
    generatedUtc: raw.generatedUtc == null ? fallback.generatedUtc : String(raw.generatedUtc),
    host: str(raw.host, fallback.host),
    gitCommit: raw.gitCommit == null ? fallback.gitCommit : String(raw.gitCommit),
    kind,
    note: raw.note == null ? fallback.note : String(raw.note),
  };
}

/* ── primitive coercion ────────────────────────────────────────────────────── */

function str(v: any, fallback = ""): string {
  return v == null ? fallback : String(v);
}
function num(v: any): number | null {
  if (v == null || v === "") return null;
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
}
function asMeasure(raw: any): Measure {
  if (raw == null) return { value: null };
  if (typeof raw === "number") return { value: Number.isFinite(raw) ? raw : null };
  return { value: num(raw.value), ci95: num(raw.ci95), display: raw.display ?? null };
}
function asTone(v: any, fallback: Tone = "muted"): Tone {
  return (["ok", "warn", "bad", "muted"] as Tone[]).includes(v) ? v : fallback;
}
function asDirection(v: any): Direction {
  return (["lower-better", "higher-better", "target", "neutral"] as Direction[]).includes(v)
    ? v
    : "neutral";
}

/* ── section normalizers ───────────────────────────────────────────────────── */

function asKpi(raw: any): Kpi {
  return {
    key: str(raw?.key),
    label: str(raw?.label),
    value: num(raw?.value),
    unit: str(raw?.unit),
    direction: asDirection(raw?.direction),
    tone: raw?.tone ? asTone(raw.tone) : undefined,
    hint: raw?.hint == null ? undefined : String(raw.hint),
    baselineValue: num(raw?.baselineValue),
    baselineLabel: raw?.baselineLabel == null ? undefined : String(raw.baselineLabel),
  };
}

function asMetricDef(raw: any): MetricDef {
  return {
    key: str(raw?.key),
    label: str(raw?.label),
    unit: str(raw?.unit),
    direction: asDirection(raw?.direction),
    target: num(raw?.target) ?? undefined,
    hint: raw?.hint == null ? undefined : String(raw.hint),
    precision: num(raw?.precision) ?? undefined,
  };
}

function asSystemDef(raw: any): SystemDef {
  const role = ["subject", "baseline", "candidate", "ceiling", "floor", "reference"].includes(raw?.role)
    ? raw.role
    : "baseline";
  return { id: str(raw?.id), label: str(raw?.label), role, hint: raw?.hint };
}

function asConfigDef(raw: any): ConfigDef {
  return {
    id: str(raw?.id),
    label: str(raw?.label, str(raw?.id)),
    params: raw?.params && typeof raw.params === "object" ? raw.params : undefined,
    isAggregate: !!raw?.isAggregate,
  };
}

function asChart(raw: any): ChartDef {
  const kind = raw?.kind === "lines" ? "lines" : "bars";
  return {
    key: str(raw?.key),
    title: str(raw?.title),
    kind,
    xLabel: raw?.xLabel,
    yLabel: raw?.yLabel,
    yUnit: raw?.yUnit,
    direction: raw?.direction ? asDirection(raw.direction) : undefined,
    bars: Array.isArray(raw?.bars)
      ? raw.bars.map((b: any) => ({
          label: str(b?.label),
          value: num(b?.value),
          emphasis: !!b?.emphasis,
          reference: !!b?.reference,
        }))
      : undefined,
    series: Array.isArray(raw?.series)
      ? raw.series.map((s: any) => ({
          id: str(s?.id),
          label: str(s?.label),
          emphasis: !!s?.emphasis,
          color: s?.color,
          points: Array.isArray(s?.points) ? s.points.map((p: any) => ({ x: p?.x, y: num(p?.y) })) : [],
        }))
      : undefined,
    note: raw?.note,
  };
}

function asSuite(raw: any): Suite {
  const systems: SystemDef[] = Array.isArray(raw?.systems) ? raw.systems.map(asSystemDef) : [];
  const metrics: MetricDef[] = Array.isArray(raw?.metrics) ? raw.metrics.map(asMetricDef) : [];
  let configurations: ConfigDef[] = Array.isArray(raw?.configurations)
    ? raw.configurations.map(asConfigDef)
    : [];
  // A suite with no parameter sweep still needs one configuration to render on.
  if (configurations.length === 0) configurations = [{ id: "overall", label: "Overall", isAggregate: true }];

  // Build a dense 3-D matrix: system → config → metric → Measure.
  const matrix: Record<string, Record<string, Record<string, Measure>>> = {};
  const rawMatrix = raw?.matrix ?? {};
  for (const sys of systems) {
    matrix[sys.id] = {};
    const rawSys = rawMatrix[sys.id] ?? {};
    for (const cfg of configurations) {
      matrix[sys.id][cfg.id] = {};
      const rawCfg = rawSys[cfg.id] ?? {};
      for (const m of metrics) matrix[sys.id][cfg.id][m.key] = asMeasure(rawCfg[m.key]);
    }
  }

  const defaultConfigId =
    str(raw?.defaultConfigId) ||
    configurations.find((c) => c.isAggregate)?.id ||
    configurations[0]?.id ||
    "";

  return {
    id: str(raw?.id),
    label: str(raw?.label),
    group: raw?.group == null ? undefined : String(raw.group),
    question: str(raw?.question),
    summary: str(raw?.summary),
    provenance: asProvenance(raw?.provenance),
    verdict: raw?.verdict ? { tone: asTone(raw.verdict.tone), text: str(raw.verdict.text) } : undefined,
    kpis: Array.isArray(raw?.kpis) ? raw.kpis.map(asKpi) : [],
    systems,
    configurations,
    metrics,
    matrix,
    defaultConfigId,
    primaryMetricKey: str(raw?.primaryMetricKey) || metrics[0]?.key || "",
    subjectId: str(raw?.subjectId, systems.find((s) => s.role === "subject")?.id ?? ""),
    charts: Array.isArray(raw?.charts) ? raw.charts.map(asChart) : [],
  };
}

function asAuditSection(raw: any): AuditSection {
  return {
    key: str(raw?.key),
    title: str(raw?.title),
    summary: raw?.summary,
    provenance: asProvenance(raw?.provenance),
    verdict: raw?.verdict ? { tone: asTone(raw.verdict.tone), text: str(raw.verdict.text) } : undefined,
    kpis: Array.isArray(raw?.kpis) ? raw.kpis.map(asKpi) : [],
    items: Array.isArray(raw?.items)
      ? raw.items.map((i: any) => ({
          id: str(i?.id),
          label: str(i?.label),
          status: ["pass", "fail", "warn", "fixed", "info", "pending"].includes(i?.status) ? i.status : "info",
          metric: i?.metric,
          severity: i?.severity,
          detail: i?.detail,
          ref: i?.ref,
        }))
      : [],
    stages: Array.isArray(raw?.stages)
      ? raw.stages.map((s: any) => ({
          label: str(s?.label),
          value: num(s?.value),
          unit: str(s?.unit),
          tone: s?.tone ? asTone(s.tone) : undefined,
          note: s?.note,
        }))
      : undefined,
  };
}

function asGrafana(raw: any): GrafanaConfig {
  return {
    baseUrl: raw?.baseUrl == null || raw.baseUrl === "" ? null : String(raw.baseUrl),
    embedQuery: raw?.embedQuery,
    dashboards: Array.isArray(raw?.dashboards)
      ? raw.dashboards.map((d: any) => ({ uid: str(d?.uid), title: str(d?.title), description: d?.description }))
      : [],
    note: raw?.note,
  };
}

/* ── public: normalize the whole bundle ────────────────────────────────────── */

export function emptyBundle(provenance: Provenance = PENDING_PROVENANCE): ResultsBundle {
  return { schemaVersion: SCHEMA_VERSION, provenance, suites: [], audit: [], grafana: { baseUrl: null, dashboards: [] } };
}

export function normalizeBundle(raw: any): ResultsBundle {
  if (!raw || typeof raw !== "object") return emptyBundle();
  return {
    schemaVersion: num(raw.schemaVersion) ?? SCHEMA_VERSION,
    provenance: asProvenance(raw.provenance),
    groups: Array.isArray(raw.groups) ? raw.groups.map((g: any) => String(g)) : undefined,
    suites: Array.isArray(raw.suites) ? raw.suites.map(asSuite) : [],
    audit: Array.isArray(raw.audit) ? raw.audit.map(asAuditSection) : [],
    grafana: asGrafana(raw.grafana),
  };
}

/* ── matrix accessors ──────────────────────────────────────────────────────── */

export function measureAt(
  suite: Suite,
  systemId: string,
  configId: string,
  metricKey: string,
): Measure | undefined {
  return suite.matrix[systemId]?.[configId]?.[metricKey];
}

/** Configurations that represent real parameter points (excludes the roll-up). */
export function paramConfigs(suite: Suite): ConfigDef[] {
  return suite.configurations.filter((c) => !c.isAggregate);
}

/** Does this suite have a parameter axis worth showing as a grid? */
export function hasParamAxis(suite: Suite): boolean {
  return paramConfigs(suite).length >= 2;
}

/* ── derived presentation helpers (the only place these live) ───────────────── */

export function isPending(m: Measure | null | undefined): boolean {
  return !m || m.value == null;
}

function pickPrecision(value: number, metric?: { precision?: number }): number {
  if (metric?.precision != null) return metric.precision;
  if (Number.isInteger(value)) return 0;
  const a = Math.abs(value);
  if (a >= 100) return 0;
  if (a >= 10) return 1;
  return 2;
}

export function fmtNumber(value: number | null | undefined, unit = "", precision?: number): string {
  if (value == null || !Number.isFinite(value)) return "—";
  const p = precision != null ? precision : pickPrecision(value);
  const body = value.toLocaleString(undefined, { minimumFractionDigits: p, maximumFractionDigits: p });
  return unit ? `${body}${unitSep(unit)}${unit}` : body;
}

function unitSep(unit: string): string {
  return unit === "%" ? "" : " ";
}

export function fmtMeasure(m: Measure | null | undefined, metric?: MetricDef): string {
  if (isPending(m)) return "—";
  const value = m!.value as number;
  if (m!.display) return m!.display;
  const p = pickPrecision(value, metric);
  const unit = metric?.unit ?? "";
  let out = fmtNumber(value, unit, p);
  if (m!.ci95 != null && Number.isFinite(m!.ci95)) {
    out += ` ±${m!.ci95.toLocaleString(undefined, { minimumFractionDigits: p, maximumFractionDigits: p })}`;
  }
  return out;
}

export function isBetter(a: number | null, b: number | null, direction: Direction): boolean {
  if (a == null || b == null) return false;
  if (direction === "higher-better") return a > b;
  if (direction === "lower-better") return a < b;
  return false;
}

export function targetDistance(value: number | null, target: number | undefined): number {
  if (value == null || target == null) return Number.POSITIVE_INFINITY;
  return Math.abs(value - target);
}

/**
 * Id of the winning system for a metric at a given configuration, considering
 * only competitive roles (subject / baseline / candidate). Returns null if
 * nothing comparable.
 */
export function winnerId(
  suite: Suite,
  metric: MetricDef,
  configId: string,
): string | null {
  const contenders = suite.systems.filter((s) => ["subject", "baseline", "candidate"].includes(s.role));
  let best: { id: string; score: number } | null = null;
  for (const s of contenders) {
    const v = measureAt(suite, s.id, configId, metric.key)?.value ?? null;
    if (v == null) continue;
    let score: number;
    if (metric.direction === "higher-better") score = v;
    else if (metric.direction === "lower-better") score = -v;
    else if (metric.direction === "target") score = -targetDistance(v, metric.target);
    else continue;
    if (best == null || score > best.score) best = { id: s.id, score };
  }
  return best?.id ?? null;
}

export interface Delta {
  abs: number;
  signedBetter: number;
  pct: number | null;
  better: boolean;
  text: string;
}

export function deltaVs(
  value: number | null,
  baseline: number | null,
  direction: Direction,
  unit = "",
): Delta | null {
  if (value == null || baseline == null) return null;
  const abs = value - baseline;
  const better = isBetter(value, baseline, direction);
  const signedBetter = direction === "lower-better" ? -abs : abs;
  const pct = baseline !== 0 ? Math.abs(abs / baseline) * 100 : null;
  const arrow = abs > 0 ? "▲" : abs < 0 ? "▼" : "■";
  const mag = Math.abs(abs);
  const suffix = unit === "%" ? " pp" : unit ? ` ${unit}` : "";
  const text = `${arrow} ${fmtNumber(mag, "", pickPrecision(mag))}${suffix}`;
  return { abs, signedBetter, pct, better, text };
}

export function toneForKpi(k: Kpi): Tone {
  if (k.tone) return k.tone;
  if (k.value == null) return "muted";
  if (k.baselineValue == null) return "muted";
  const d = deltaVs(k.value, k.baselineValue, k.direction, k.unit);
  if (!d) return "muted";
  if (d.abs === 0) return "muted";
  return d.better ? "ok" : "bad";
}

export function freshnessText(p: Provenance): string {
  const kindLabel: Record<ResultKind, string> = {
    final: "Final results",
    stale: "Stale results (pre-VPS)",
    sample: "Sample data",
    pending: "Awaiting results",
  };
  const when = p.generatedUtc ? fmtUtc(p.generatedUtc) : "no run yet";
  return `${kindLabel[p.kind]} · ${p.host} · ${when}`;
}

export function fmtUtc(iso: string | null): string {
  if (!iso) return "—";
  const compact = /^(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})Z$/.exec(iso);
  if (compact) {
    const [, y, mo, d, h, mi] = compact;
    return `${y}-${mo}-${d} ${h}:${mi} UTC`;
  }
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return iso;
  const dt = new Date(t);
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${dt.getUTCFullYear()}-${pad(dt.getUTCMonth() + 1)}-${pad(dt.getUTCDate())} ${pad(
    dt.getUTCHours(),
  )}:${pad(dt.getUTCMinutes())} UTC`;
}

export function kindTone(kind: ResultKind): Tone {
  if (kind === "final") return "ok";
  if (kind === "stale") return "warn";
  if (kind === "sample") return "warn";
  return "muted";
}

export function grafanaEmbedUrl(cfg: GrafanaConfig, uid: string): string | null {
  if (!cfg.baseUrl) return null;
  const base = cfg.baseUrl.replace(/\/$/, "");
  const q = cfg.embedQuery ?? "kiosk&theme=light";
  return `${base}/d/${encodeURIComponent(uid)}/?${q}`;
}

/** Distinct group names, in bundle order (or explicit `groups` order). */
export function suiteGroups(bundle: ResultsBundle): string[] {
  const order = bundle.groups ?? [];
  const seen = new Set<string>(order);
  const out = [...order];
  for (const s of bundle.suites) {
    const g = s.group ?? "Benchmarks";
    if (!seen.has(g)) {
      seen.add(g);
      out.push(g);
    }
  }
  // Only keep groups that actually have suites.
  const used = new Set(bundle.suites.map((s) => s.group ?? "Benchmarks"));
  return out.filter((g) => used.has(g));
}
