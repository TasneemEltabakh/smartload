// ============================================================================
// Verdicts -- the anomaly detector feed, evidence-carrying
// ----------------------------------------------------------------------------
// Every health ruling on this page carries its evidence: the metric, the
// observed value against the threshold it crossed, and the auto-action the
// decision plane took. Three reads compose the page:
//   - a per-backend health board (status + score + triggering evidence), with
//     excluded nodes held visibly distinct;
//   - a chronological verdict feed built from structured alerts merged with
//     anomaly-kind activity (timestamped rulings + severity + action);
//   - a detail Drawer with the full per-metric breakdown for one backend plus
//     that backend's status/score verdict history (a score sparkline and a
//     status-change timeline), sourced from the anomaly-history endpoint behind
//     the BFF (/api/ui/anomaly/history) and loaded on demand when it opens.
// Each panel tries the live API and falls back to inline sample data on error
// or timeout, so the page renders complete with no backend running.
// ============================================================================

import { useEffect, useMemo, useState } from "react";
import {
  Clock,
  ShieldAlert,
  ShieldCheck,
  ShieldX,
} from "lucide-react";

import {
  api,
  type ActivityItem,
  type AlertItem,
  type AnomalyHistoryRow,
  type BackendMetrics,
  type LbState,
  type Policy,
  serviceOfInstance,
} from "../api";
import {
  Badge,
  Card,
  DataTable,
  Drawer,
  EvidenceLine,
  KpiStat,
  Sparkline,
  StatusPill,
  type Column,
  type Status,
} from "../ui";
import { loadWithFallback, type DataSource } from "./loader";
import { useShell } from "./shell-context";
import {
  SAMPLE_VERDICT_ACTIVITY,
  SAMPLE_VERDICT_ALERTS,
  SAMPLE_VERDICT_BACKENDS,
  SAMPLE_VERDICT_ERROR_THRESHOLD_PCT,
  SAMPLE_VERDICT_LATENCY_THRESHOLD_MS,
  SAMPLE_VERDICT_SCAN_AGE_SECONDS,
  sampleAnomalyHistory,
  type VerdictBackend,
  type VerdictMetric,
} from "./_sampleVerdicts";

const REFRESH_MS = 20_000;

// ── status / formatting helpers ──────────────────────────────────────────────

const fmtInt = (n: number) => n.toLocaleString("en-US", { maximumFractionDigits: 0 });

// Map a board health ruling to the kit's Status palette.
function statusOfVerdict(v: VerdictBackend): Status {
  if (v.status === "unhealthy") return "crit";
  if (v.status === "degraded") return "warn";
  return "ok";
}

// Map a structured alert to the kit's Status palette.
function statusOfAlert(a: AlertItem): Status {
  if (a.severity === "critical" || a.status === "unhealthy") return "crit";
  if (a.severity === "warning" || a.status === "degraded") return "warn";
  return "ok";
}

// Map an activity severity to the kit's Status palette.
function statusOfActivity(sev: ActivityItem["severity"]): Status {
  if (sev === "bad") return "crit";
  if (sev === "warn") return "warn";
  return "ok";
}

// Format an "Xs / Xm ago" age from seconds.
function fmtAge(seconds: number): { value: string; unit: string } {
  if (seconds < 90) return { value: String(Math.max(0, Math.round(seconds))), unit: "s ago" };
  const mins = seconds / 60;
  if (mins < 90) return { value: mins.toFixed(0), unit: "m ago" };
  return { value: (mins / 60).toFixed(1), unit: "h ago" };
}

// A single feed entry, normalized from either an alert or an anomaly activity.
interface FeedVerdict {
  key: string;
  backend_id: string | null;
  status: Status;
  label: string; // status word for the severity badge
  summary: string;
  time: string | null;
  action: string | null;
  evidence?: { metric: string; observed: number; threshold: number };
}

// ── live → board rows ────────────────────────────────────────────────────────
// When live, synthesize the per-backend board from /metrics/backends, the
// excluded set from /lb/state, and the breach thresholds from the policy. p95
// over the anomaly latency threshold rules unhealthy; error rate over its
// threshold rules degraded. The breakdown the Drawer reads is built per metric.
function liveBoard(
  metrics: BackendMetrics,
  lb: LbState,
  policy: Policy,
): VerdictBackend[] {
  const slo = policy.slo_p95_latency_ms > 0 ? policy.slo_p95_latency_ms : 200;
  const mult = policy.anomaly_latency_multiplier > 0 ? policy.anomaly_latency_multiplier : 2;
  const latencyThreshold = Math.round(slo * mult);
  const errorThreshold = SAMPLE_VERDICT_ERROR_THRESHOLD_PCT;
  const capacity = policy.per_instance_capacity_rps > 0 ? policy.per_instance_capacity_rps * 60 : 7200;
  const excluded = new Set(lb.excluded_backends ?? []);

  return metrics.backends.map((b) => {
    const p95 = b.p95_ms ?? 0;
    const err = b.error_rate_pct;
    const isExcluded = excluded.has(b.instance);
    const overLatency = b.p95_ms != null && b.p95_ms > latencyThreshold;
    const overError = err > errorThreshold;

    const status: VerdictBackend["status"] = isExcluded || overLatency
      ? "unhealthy"
      : overError
        ? "degraded"
        : "healthy";

    // Health score: 1 minus normalized latency and error penalties, clamped.
    const score = Math.max(
      0,
      Math.min(1, 1 - p95 / (latencyThreshold * 2) - err / 100),
    );

    const evidence = overLatency
      ? { metric: "p95_latency_ms", observed: p95, threshold: latencyThreshold }
      : overError
        ? { metric: "error_rate_pct", observed: Number(err.toFixed(2)), threshold: errorThreshold }
        : undefined;

    const action = isExcluded
      ? "Excluded from rotation; traffic redistributed automatically"
      : status === "unhealthy"
        ? "Exclusion pending; held under watch"
        : status === "degraded"
          ? "Kept in rotation, weight reduced; under watch"
          : "In rotation, full weight";

    const metricsBreakdown: VerdictMetric[] = [
      {
        metric: "p95_latency_ms",
        observed: p95,
        threshold: latencyThreshold,
        unit: "ms",
        direction: "over",
        breach: overLatency,
      },
      {
        metric: "error_rate_pct",
        observed: Number(err.toFixed(2)),
        threshold: errorThreshold,
        unit: "%",
        direction: "over",
        breach: overError,
      },
      {
        metric: "req_per_min",
        observed: b.rpm,
        threshold: capacity,
        unit: "rpm",
        direction: "over",
        breach: false,
      },
    ];

    return {
      instance: b.instance,
      zone: serviceOfInstance(b.instance),
      status,
      score: Number(score.toFixed(2)),
      excluded: isExcluded,
      action,
      time: null,
      evidence,
      metrics: metricsBreakdown,
    };
  });
}

// ── component ────────────────────────────────────────────────────────────────

export default function Verdicts() {
  const { setDataSource, setPlane } = useShell();

  const [board, setBoard] = useState<VerdictBackend[]>(SAMPLE_VERDICT_BACKENDS);
  const [alerts, setAlerts] = useState<AlertItem[]>(SAMPLE_VERDICT_ALERTS);
  const [activity, setActivity] = useState<ActivityItem[]>(SAMPLE_VERDICT_ACTIVITY);
  const [scanAgeSeconds, setScanAgeSeconds] = useState<number>(SAMPLE_VERDICT_SCAN_AGE_SECONDS);
  const [selected, setSelected] = useState<VerdictBackend | null>(null);

  // Per-backend verdict history for the detail Drawer (score sparkline +
  // status-change timeline). Loaded on demand when a backend is selected; the
  // sample is keyed to the selected backend so the Drawer renders even offline.
  const [history, setHistory] = useState<AnomalyHistoryRow[]>([]);
  const [historyLive, setHistoryLive] = useState<boolean>(false);

  // ── data load (live, with sample fallback) ─────────────────────────────────
  useEffect(() => {
    let cancelled = false;

    async function tick() {
      const [bkR, lbR, poR, alR, acR] = await Promise.all([
        loadWithFallback(() => api.getBackendMetrics(), null as BackendMetrics | null),
        loadWithFallback(() => api.getLbState(), null as LbState | null),
        loadWithFallback(() => api.getPolicy(), null as Policy | null),
        loadWithFallback(() => api.getAlerts(), SAMPLE_VERDICT_ALERTS),
        loadWithFallback(() => api.getActivity(40), SAMPLE_VERDICT_ACTIVITY),
      ]);

      if (cancelled) return;

      const sources: DataSource[] = [bkR.source, lbR.source, poR.source, alR.source, acR.source];

      // Build the health board from live metrics + lb state + policy when all
      // three are live; otherwise keep the rich inline sample board. The locals
      // narrow the nullable loader values so liveBoard gets non-null inputs.
      const bk = bkR.value;
      const lb = lbR.value;
      const po = poR.value;
      const boardLive = bkR.source === "live" && bk != null && lb != null && po != null;
      if (boardLive) {
        setBoard(liveBoard(bk, lb, po));
      } else {
        setBoard(SAMPLE_VERDICT_BACKENDS);
      }

      setAlerts(alR.value);
      setActivity(acR.value);

      // Last-scan age from the freshest verdict timestamp we can find. Live
      // timestamps are wall-clock strings; sample times are HH:MM:SS, so we keep
      // the sample constant when offline rather than mis-parsing.
      if (boardLive) {
        const freshest = freshestTimestamp(alR.value, acR.value);
        setScanAgeSeconds(freshest != null ? freshest : SAMPLE_VERDICT_SCAN_AGE_SECONDS);
      } else {
        setScanAgeSeconds(SAMPLE_VERDICT_SCAN_AGE_SECONDS);
      }

      const anySample = sources.some((s) => s === "sample");
      const allSample = sources.every((s) => s === "sample");
      setDataSource(anySample ? "sample" : "live");
      setPlane(allSample ? "bad" : anySample ? "warn" : "ok");
    }

    tick();
    const id = window.setInterval(tick, REFRESH_MS);
    return () => {
      cancelled = true;
      window.clearInterval(id);
    };
  }, [setDataSource, setPlane]);

  // ── per-backend verdict history (loaded when the Drawer opens) ──────────────
  // Fetch the selected backend's status/score history via loadWithFallback so
  // the Drawer shows a real trail over time, falling back to a sample shaped to
  // that backend when the endpoint is empty or unreachable.
  useEffect(() => {
    if (selected == null) {
      setHistory([]);
      setHistoryLive(false);
      return;
    }
    let cancelled = false;
    const backendId = selected.instance;
    const sample = sampleAnomalyHistory(backendId);

    async function load() {
      const res = await loadWithFallback(
        () => api.getAnomalyHistory(3600, backendId, 100),
        null as { history: AnomalyHistoryRow[] } | null,
      );
      if (cancelled) return;
      const rows = res.source === "live" ? res.value?.history ?? [] : [];
      if (res.source === "live" && rows.length > 0) {
        setHistory(rows);
        setHistoryLive(true);
      } else {
        setHistory(sample);
        setHistoryLive(false);
      }
    }

    load();
    return () => {
      cancelled = true;
    };
  }, [selected]);

  // ── derived KPIs ───────────────────────────────────────────────────────────

  const openVerdicts = useMemo(
    () => board.filter((b) => b.status !== "healthy").length,
    [board],
  );
  const degradedCount = useMemo(
    () => board.filter((b) => b.status === "degraded").length,
    [board],
  );
  const excludedCount = useMemo(
    () => board.filter((b) => b.excluded || b.status === "unhealthy").length,
    [board],
  );
  const age = fmtAge(scanAgeSeconds);

  // ── verdict feed (alerts + anomaly activity, chronological) ────────────────

  const feed = useMemo<FeedVerdict[]>(() => {
    const fromAlerts: FeedVerdict[] = alerts.map((a, i) => ({
      key: `alert-${a.backend_id}-${a.time ?? i}`,
      backend_id: a.backend_id,
      status: statusOfAlert(a),
      label: (a.status ?? a.severity).toUpperCase(),
      summary: a.summary,
      time: a.time,
      action: actionForAlert(a),
      evidence:
        a.metric && a.observed_value != null && a.threshold != null
          ? { metric: a.metric, observed: a.observed_value, threshold: a.threshold }
          : undefined,
    }));

    // Only anomaly-kind activity belongs on the Verdicts feed.
    const fromActivity: FeedVerdict[] = activity
      .filter((ev) => ev.kind === "anomaly")
      .map((ev, i) => ({
        key: `act-${ev.time}-${i}`,
        backend_id: backendOfSummary(ev.summary),
        status: statusOfActivity(ev.severity),
        label: ev.severity === "bad" ? "UNHEALTHY" : ev.severity === "warn" ? "DEGRADED" : "CLEARED",
        summary: ev.summary,
        time: ev.time,
        action: null,
      }));

    // Merge, de-duplicate by (backend, time), and sort newest first by the
    // HH:MM:SS-ish time string (lexical sort is correct for same-day stamps).
    const seen = new Set<string>();
    const merged: FeedVerdict[] = [];
    for (const v of [...fromAlerts, ...fromActivity]) {
      const dedupe = `${v.backend_id ?? ""}|${v.time ?? ""}`;
      if (seen.has(dedupe)) continue;
      seen.add(dedupe);
      merged.push(v);
    }
    merged.sort((a, b) => (b.time ?? "").localeCompare(a.time ?? ""));
    return merged;
  }, [alerts, activity]);

  // ── render ─────────────────────────────────────────────────────────────────

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 22 }}>
      <SectionHead
        title="Verdicts"
        sub="The anomaly detector feed. Every health ruling carries its evidence (the metric, the observed value against the threshold it crossed) and the auto-action the decision plane took."
      />

      <KpiRail
        openVerdicts={openVerdicts}
        degradedCount={degradedCount}
        excludedCount={excludedCount}
        age={age}
      />

      <SectionHead
        title="Backend health board"
        sub={`${board.length} nodes under watch. ${
          excludedCount > 0
            ? "An unhealthy node is held out of rotation; its row is shown distinct. Click a row for the full evidence."
            : "All nodes are clear. Click a row for the per-metric breakdown."
        }`}
      />

      <HealthBoard board={board} onSelect={setSelected} />

      <SectionHead
        title="Verdict feed"
        sub="Chronological rulings, newest first. Each entry pins the backend, severity, evidence, and the action taken."
      />

      <VerdictFeed feed={feed} />

      <VerdictDrawer
        verdict={selected}
        history={history}
        historyLive={historyLive}
        onClose={() => setSelected(null)}
      />
    </div>
  );
}

// ── section header ───────────────────────────────────────────────────────────

function SectionHead({ title, sub }: { title: string; sub: string }) {
  return (
    <div style={{ margin: "8px 2px 0" }}>
      <h2 style={{ fontSize: 17, fontWeight: 700, letterSpacing: "-0.3px", margin: 0, color: "var(--sl-text)" }}>
        {title}
      </h2>
      <div style={{ fontSize: 12.5, color: "var(--sl-text-low)", marginTop: 3, maxWidth: "92ch" }}>{sub}</div>
    </div>
  );
}

// ── KPI rail ─────────────────────────────────────────────────────────────────

function KpiRail({
  openVerdicts,
  degradedCount,
  excludedCount,
  age,
}: {
  openVerdicts: number;
  degradedCount: number;
  excludedCount: number;
  age: { value: string; unit: string };
}) {
  return (
    <div style={{ display: "grid", gridTemplateColumns: "repeat(4, 1fr)", gap: 16 }}>
      <KpiStat
        label={<><ShieldAlert size={12} strokeWidth={2} /> Open verdicts</>}
        value={String(openVerdicts)}
        unit="active"
        deltaDir={openVerdicts > 0 ? "down" : "flat"}
        delta={openVerdicts > 0 ? "needs attention" : "all clear"}
        footnote="non-healthy rulings"
      />
      <KpiStat
        label={<><ShieldAlert size={12} strokeWidth={2} /> Backends degraded</>}
        value={String(degradedCount)}
        unit="nodes"
        deltaDir={degradedCount > 0 ? "down" : "flat"}
        delta={degradedCount > 0 ? "weight reduced" : "none"}
        footnote="kept in rotation"
      />
      <KpiStat
        label={<><ShieldX size={12} strokeWidth={2} /> Excluded / unhealthy</>}
        value={String(excludedCount)}
        unit="nodes"
        deltaDir={excludedCount > 0 ? "down" : "flat"}
        delta={excludedCount > 0 ? "out of rotation" : "none"}
        footnote="held out automatically"
      />
      <KpiStat
        label={<><Clock size={12} strokeWidth={2} /> Last scan</>}
        value={age.value}
        unit={age.unit}
        deltaDir="flat"
        delta="detector live"
        footnote="anomaly sweep"
      />
    </div>
  );
}

// ── health board ─────────────────────────────────────────────────────────────

function HealthBoard({
  board,
  onSelect,
}: {
  board: VerdictBackend[];
  onSelect: (v: VerdictBackend) => void;
}) {
  const columns: Column<VerdictBackend>[] = [
    {
      key: "backend",
      header: "Backend",
      render: (b) => {
        const s = statusOfVerdict(b);
        const led = s === "crit" ? "var(--sl-crit)" : s === "warn" ? "var(--sl-warn)" : "var(--sl-ok)";
        return (
          <div style={{ display: "flex", alignItems: "center", gap: 11 }}>
            <span style={{ width: 9, height: 9, borderRadius: "50%", background: led, flex: "0 0 auto", boxShadow: `0 0 6px ${led}` }} />
            <div>
              <div style={{ fontFamily: "var(--sl-font-mono)", fontSize: 13, fontWeight: 600, color: "var(--sl-text)" }}>{b.instance}</div>
              {b.zone ? <div style={{ fontFamily: "var(--sl-font-mono)", fontSize: 10, color: "var(--sl-text-faint)" }}>{b.zone}</div> : null}
            </div>
          </div>
        );
      },
    },
    {
      key: "score",
      header: "Health score",
      render: (b) => {
        const s = statusOfVerdict(b);
        const color = s === "crit" ? "var(--sl-crit)" : s === "warn" ? "var(--sl-warn)" : "var(--sl-mint)";
        return (
          <div style={{ display: "flex", alignItems: "center", gap: 10, minWidth: 120 }}>
            <div style={{ flex: 1, height: 6, borderRadius: 6, background: "var(--sl-surface-sunk)", overflow: "hidden", minWidth: 64 }}>
              <div style={{ width: `${Math.round(b.score * 100)}%`, height: "100%", borderRadius: 6, background: color }} />
            </div>
            <span style={{ fontFamily: "var(--sl-font-mono)", fontSize: 12, fontWeight: 600, color: "var(--sl-text)" }}>
              {b.score.toFixed(2)}
            </span>
          </div>
        );
      },
    },
    {
      key: "status",
      header: "Verdict",
      render: (b) => {
        const s = statusOfVerdict(b);
        const word = b.excluded ? "EXCLUDED" : b.status.toUpperCase();
        return <StatusPill status={s}>{word}</StatusPill>;
      },
    },
    {
      key: "evidence",
      header: "Triggering evidence",
      render: (b) => {
        const s = statusOfVerdict(b);
        if (!b.evidence) {
          return <span style={{ fontFamily: "var(--sl-font-mono)", fontSize: 11, color: "var(--sl-text-faint)" }}>no breach</span>;
        }
        return (
          <EvidenceLine
            metric={b.evidence.metric}
            observed={String(b.evidence.observed)}
            threshold={String(b.evidence.threshold)}
            verdict={b.evidence.observed > b.evidence.threshold ? "over" : "under"}
            status={s}
          />
        );
      },
    },
    {
      key: "action",
      header: "Auto-action",
      render: (b) => (
        <span style={{ fontSize: 12, color: "var(--sl-text-mid)" }}>{b.action}</span>
      ),
    },
  ];

  return (
    <Card flush>
      <ClickableTable
        columns={columns}
        rows={board}
        rowKey={(b) => b.instance}
        rowMuted={(b) => b.excluded}
        onRowClick={onSelect}
      />
    </Card>
  );
}

// The kit DataTable has no row-click prop, so wrap it. We render the kit table
// unchanged and delegate clicks from a containing element: resolve the clicked
// row from the closest <tr>'s index within <tbody>, which matches the rows array
// order the kit renders. Keeps the design kit untouched while making rows open
// the detail Drawer.
function ClickableTable<Row extends { instance: string }>({
  columns,
  rows,
  rowKey,
  rowMuted,
  onRowClick,
}: {
  columns: Column<Row>[];
  rows: Row[];
  rowKey: (row: Row) => string;
  rowMuted?: (row: Row) => boolean;
  onRowClick: (row: Row) => void;
}) {
  const onClick = (e: React.MouseEvent<HTMLDivElement>) => {
    const tr = (e.target as HTMLElement).closest("tr");
    if (!tr || !tr.parentElement || tr.parentElement.tagName.toLowerCase() !== "tbody") return;
    const idx = Array.prototype.indexOf.call(tr.parentElement.children, tr);
    if (idx < 0 || idx >= rows.length) return;
    onRowClick(rows[idx]);
  };
  return (
    <div onClick={onClick} style={{ cursor: "pointer" }}>
      <DataTable columns={columns} rows={rows} rowKey={rowKey} rowMuted={rowMuted} />
    </div>
  );
}

// ── verdict feed ─────────────────────────────────────────────────────────────

function VerdictFeed({ feed }: { feed: FeedVerdict[] }) {
  return (
    <Card title="Anomaly rulings" eyebrow="// evidence-carrying" flush>
      {feed.length === 0 ? (
        <div style={{ padding: 18, fontSize: 13, color: "var(--sl-text-low)" }}>
          No verdicts on record. The fleet is clear.
        </div>
      ) : (
        <div style={{ display: "flex", flexDirection: "column" }}>
          {feed.map((v, i) => (
            <div
              key={v.key}
              style={{
                display: "grid",
                gridTemplateColumns: "auto 1fr auto",
                gap: 13,
                padding: "14px 18px",
                borderBottom: i < feed.length - 1 ? "1px solid var(--sl-hairline-soft)" : undefined,
                alignItems: "flex-start",
              }}
            >
              <span
                style={{
                  width: 30,
                  height: 30,
                  borderRadius: 9,
                  display: "grid",
                  placeItems: "center",
                  flex: "0 0 auto",
                  background:
                    v.status === "crit" ? "var(--sl-crit-tint)" : v.status === "warn" ? "var(--sl-warn-tint)" : "var(--sl-mint-tint)",
                  color:
                    v.status === "crit" ? "var(--sl-crit)" : v.status === "warn" ? "var(--sl-warn)" : "var(--sl-mint-deep)",
                }}
              >
                {v.status === "crit" ? <ShieldX size={15} strokeWidth={2} /> : v.status === "warn" ? <ShieldAlert size={15} strokeWidth={2} /> : <ShieldCheck size={15} strokeWidth={2} />}
              </span>

              <div>
                <div style={{ display: "flex", alignItems: "center", gap: 9, flexWrap: "wrap" }}>
                  {v.backend_id ? (
                    <span style={{ fontFamily: "var(--sl-font-mono)", fontSize: 13, fontWeight: 600, color: "var(--sl-text)" }}>{v.backend_id}</span>
                  ) : null}
                  <StatusPill status={v.status} hideDot>{v.label}</StatusPill>
                </div>
                <div style={{ fontSize: 11.5, color: "var(--sl-text-mid)", marginTop: 5, lineHeight: 1.45 }}>{v.summary}</div>
                {v.evidence ? (
                  <div style={{ marginTop: 7 }}>
                    <EvidenceLine
                      metric={v.evidence.metric}
                      observed={String(v.evidence.observed)}
                      threshold={String(v.evidence.threshold)}
                      verdict={v.evidence.observed > v.evidence.threshold ? "over" : "under"}
                      status={v.status}
                    />
                  </div>
                ) : null}
                {v.action ? (
                  <div style={{ display: "flex", alignItems: "center", gap: 7, marginTop: 8 }}>
                    <span style={{ fontFamily: "var(--sl-font-mono)", fontSize: 9, letterSpacing: "1px", color: "var(--sl-text-low)" }}>ACTION</span>
                    <Badge tone={v.status === "ok" ? "mint" : "neutral"}>{v.action}</Badge>
                  </div>
                ) : null}
              </div>

              {v.time ? (
                <span style={{ fontFamily: "var(--sl-font-mono)", fontSize: 10.5, color: "var(--sl-text-faint)", whiteSpace: "nowrap" }}>{v.time}</span>
              ) : null}
            </div>
          ))}
        </div>
      )}
    </Card>
  );
}

// ── detail drawer ────────────────────────────────────────────────────────────

function VerdictDrawer({
  verdict,
  history,
  historyLive,
  onClose,
}: {
  verdict: VerdictBackend | null;
  history: AnomalyHistoryRow[];
  historyLive: boolean;
  onClose: () => void;
}) {
  const s = verdict ? statusOfVerdict(verdict) : "neutral";
  return (
    <Drawer
      open={verdict != null}
      onClose={onClose}
      width={460}
      title={
        verdict ? (
          <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
            <span style={{ fontFamily: "var(--sl-font-mono)" }}>{verdict.instance}</span>
            <StatusPill status={s}>{verdict.excluded ? "EXCLUDED" : verdict.status.toUpperCase()}</StatusPill>
          </div>
        ) : null
      }
    >
      {verdict ? (
        <div style={{ display: "flex", flexDirection: "column", gap: 18 }}>
          {/* attribution summary */}
          <div style={{ display: "flex", gap: 18, flexWrap: "wrap" }}>
            <DrawerStat label="Health score" value={verdict.score.toFixed(2)} />
            <DrawerStat label="Zone" value={verdict.zone || "—"} />
            <DrawerStat
              label="Rotation"
              value={verdict.excluded ? "excluded" : "in rotation"}
              tone={verdict.excluded ? "crit" : "ok"}
            />
          </div>

          {/* the ruling + auto-action */}
          <div
            style={{
              borderRadius: "var(--sl-radius-md)",
              border: "1px solid var(--sl-hairline)",
              background: "var(--sl-surface-sunk)",
              padding: "12px 14px",
            }}
          >
            <div style={{ fontSize: 9, letterSpacing: "1px", color: "var(--sl-text-low)", fontFamily: "var(--sl-font-mono)" }}>RULING</div>
            <div style={{ fontSize: 13, fontWeight: 600, color: "var(--sl-text)", marginTop: 6 }}>
              {verdict.status === "healthy" ? "Cleared healthy" : `Ruled ${verdict.status}`}
              {verdict.time ? <span style={{ fontFamily: "var(--sl-font-mono)", fontSize: 11, color: "var(--sl-text-faint)", fontWeight: 400, marginLeft: 8 }}>{verdict.time}</span> : null}
            </div>
            <div style={{ fontSize: 12, color: "var(--sl-text-mid)", marginTop: 8 }}>
              <span style={{ fontFamily: "var(--sl-font-mono)", fontSize: 9, letterSpacing: "1px", color: "var(--sl-text-low)", marginRight: 8 }}>ACTION</span>
              {verdict.action}
            </div>
            {verdict.evidence ? (
              <div style={{ marginTop: 10 }}>
                <EvidenceLine
                  metric={verdict.evidence.metric}
                  observed={String(verdict.evidence.observed)}
                  threshold={String(verdict.evidence.threshold)}
                  verdict={verdict.evidence.observed > verdict.evidence.threshold ? "over" : "under"}
                  status={s}
                />
              </div>
            ) : null}
          </div>

          {/* per-metric breakdown */}
          <div>
            <div style={{ fontSize: 12, fontWeight: 700, color: "var(--sl-text)", marginBottom: 10 }}>Per-metric breakdown</div>
            <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
              {verdict.metrics.map((m) => (
                <MetricRow key={m.metric} metric={m} />
              ))}
            </div>
          </div>

          {/* verdict history: score sparkline + status-change timeline */}
          <VerdictHistory history={history} live={historyLive} />
        </div>
      ) : null}
    </Drawer>
  );
}

// ── verdict history (score trail + status-change timeline) ────────────────────
// Renders the selected backend's status/score rulings over time: a score
// sparkline (oldest → newest) and a compact timeline of status changes. Fed by
// the anomaly-history endpoint (or sample fallback) via the Drawer.

function statusOfRuling(status: AnomalyHistoryRow["status"]): Status {
  if (status === "unhealthy") return "crit";
  if (status === "degraded") return "warn";
  return "ok";
}

function VerdictHistory({
  history,
  live,
}: {
  history: AnomalyHistoryRow[];
  live: boolean;
}) {
  // Order oldest → newest for the sparkline and timeline reading direction.
  const ordered = useMemo(
    () =>
      [...history].sort(
        (a, b) => (Date.parse(a.time) || 0) - (Date.parse(b.time) || 0),
      ),
    [history],
  );

  // Status-change points: keep the first row and any row whose status differs
  // from the previous one, so the timeline reads as transitions, not every tick.
  const changes = useMemo(() => {
    const out: AnomalyHistoryRow[] = [];
    let prev: string | null = null;
    for (const r of ordered) {
      if (r.status !== prev) {
        out.push(r);
        prev = r.status;
      }
    }
    return out.reverse(); // newest change first
  }, [ordered]);

  const scoreTrail = ordered.map((r) => r.score);
  // Worst (lowest) score and current score for the readout.
  const current = ordered.length ? ordered[ordered.length - 1].score : null;
  const worst = ordered.length ? Math.min(...scoreTrail) : null;

  const fmtTime = (t: string) => {
    const ms = Date.parse(t);
    if (Number.isNaN(ms)) return t;
    return new Date(ms).toLocaleTimeString("en-US", {
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hour12: false,
    });
  };

  return (
    <div style={{ borderTop: "1px solid var(--sl-hairline-soft)", paddingTop: 14 }}>
      <div
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          marginBottom: 10,
        }}
      >
        <div style={{ fontSize: 12, fontWeight: 700, color: "var(--sl-text)" }}>
          Verdict history
        </div>
        <Badge tone={live ? "mint" : "neutral"}>{live ? "LIVE" : "SAMPLE"}</Badge>
      </div>

      {ordered.length === 0 ? (
        <div style={{ fontSize: 11.5, color: "var(--sl-text-low)" }}>
          No verdict history on record for this backend in the window.
        </div>
      ) : (
        <>
          {/* score trail */}
          <div style={{ marginBottom: 6 }}>
            <div style={{ display: "flex", gap: 18, marginBottom: 6 }}>
              <DrawerStat
                label="Score now"
                value={current != null ? current.toFixed(2) : "—"}
              />
              <DrawerStat
                label="Worst in window"
                value={worst != null ? worst.toFixed(2) : "—"}
                tone={worst != null && worst < 0.5 ? "crit" : undefined}
              />
              <DrawerStat label="Rulings" value={String(ordered.length)} />
            </div>
            <Sparkline
              data={scoreTrail}
              tone={worst != null && worst < 0.5 ? "graphite" : "mint"}
              width={412}
              height={40}
            />
            <div
              style={{
                display: "flex",
                justifyContent: "space-between",
                fontFamily: "var(--sl-font-mono)",
                fontSize: 9.5,
                color: "var(--sl-text-faint)",
                marginTop: 2,
              }}
            >
              <span>{fmtTime(ordered[0].time)}</span>
              <span>health score · {fmtTime(ordered[ordered.length - 1].time)}</span>
            </div>
          </div>

          {/* status-change timeline */}
          <div style={{ marginTop: 12 }}>
            <div
              style={{
                fontFamily: "var(--sl-font-mono)",
                fontSize: 9,
                letterSpacing: "1px",
                color: "var(--sl-text-low)",
                marginBottom: 8,
              }}
            >
              STATUS CHANGES
            </div>
            <div style={{ display: "flex", flexDirection: "column" }}>
              {changes.map((r, i) => {
                const st = statusOfRuling(r.status);
                return (
                  <div
                    key={`${r.time}-${i}`}
                    style={{
                      display: "grid",
                      gridTemplateColumns: "auto 1fr auto",
                      gap: 11,
                      alignItems: "center",
                      padding: "8px 0",
                      borderBottom:
                        i < changes.length - 1
                          ? "1px solid var(--sl-hairline-soft)"
                          : undefined,
                    }}
                  >
                    <StatusPill status={st} hideDot>
                      {r.status.toUpperCase()}
                    </StatusPill>
                    <span
                      style={{
                        fontFamily: "var(--sl-font-mono)",
                        fontSize: 11.5,
                        color: "var(--sl-text-mid)",
                      }}
                    >
                      score {r.score.toFixed(2)}
                    </span>
                    <span
                      style={{
                        fontFamily: "var(--sl-font-mono)",
                        fontSize: 10.5,
                        color: "var(--sl-text-faint)",
                        whiteSpace: "nowrap",
                      }}
                    >
                      {fmtTime(r.time)}
                    </span>
                  </div>
                );
              })}
            </div>
          </div>
        </>
      )}
    </div>
  );
}

function DrawerStat({ label, value, tone }: { label: string; value: string; tone?: "ok" | "crit" }) {
  const color = tone === "crit" ? "var(--sl-crit)" : tone === "ok" ? "var(--sl-mint-deep)" : "var(--sl-text)";
  return (
    <div>
      <div style={{ fontSize: 10, color: "var(--sl-text-low)", letterSpacing: "0.4px" }}>{label}</div>
      <div style={{ fontFamily: "var(--sl-font-mono)", fontSize: 18, fontWeight: 700, letterSpacing: "-0.4px", marginTop: 3, color }}>
        {value}
      </div>
    </div>
  );
}

function MetricRow({ metric }: { metric: VerdictMetric }) {
  const s: Status = metric.breach ? (metric.metric === "p95_latency_ms" ? "crit" : "warn") : "ok";
  const obs = `${metric.observed}${metric.unit === "%" ? "%" : metric.unit === "ms" ? " ms" : ` ${metric.unit}`}`;
  const thr = `${metric.threshold}${metric.unit === "%" ? "%" : metric.unit === "ms" ? " ms" : ` ${metric.unit}`}`;
  return (
    <div
      style={{
        display: "flex",
        alignItems: "center",
        justifyContent: "space-between",
        gap: 12,
        padding: "10px 12px",
        borderRadius: "var(--sl-radius-sm)",
        border: `1px solid ${metric.breach ? "var(--sl-hairline)" : "var(--sl-hairline-soft)"}`,
        background: metric.breach ? (s === "crit" ? "var(--sl-crit-tint)" : "var(--sl-warn-tint)") : "var(--sl-surface)",
      }}
    >
      <div style={{ minWidth: 0 }}>
        <div style={{ fontFamily: "var(--sl-font-mono)", fontSize: 12, fontWeight: 600, color: "var(--sl-text)" }}>{metric.metric}</div>
        <div style={{ fontFamily: "var(--sl-font-mono)", fontSize: 10.5, color: "var(--sl-text-low)", marginTop: 2 }}>
          {fmtInt(metric.observed)} {metric.unit} <span style={{ color: "var(--sl-text-faint)" }}>vs threshold {fmtInt(metric.threshold)} {metric.unit}</span>
        </div>
      </div>
      <StatusPill status={s} hideDot>{metric.breach ? "BREACH" : "OK"}</StatusPill>
    </div>
  );
}

// ── feed helpers ─────────────────────────────────────────────────────────────

// Derive the auto-action sentence for a structured alert from its ruling.
function actionForAlert(a: AlertItem): string {
  if (a.status === "unhealthy" || a.severity === "critical") {
    return "Excluded from rotation; traffic redistributed";
  }
  if (a.status === "degraded" || a.severity === "warning") {
    return "Weight reduced; kept under watch";
  }
  return "Cleared; weight restored";
}

// Best-effort backend id from an activity summary like "api-04 ruled ...".
function backendOfSummary(summary: string): string | null {
  const m = summary.match(/\b([a-z][a-z0-9]*-\d+)\b/i);
  return m ? m[1] : null;
}

// Parse the freshest verdict timestamp into an age in seconds. Live alert/
// activity times may be ISO strings; if neither parses, return null so the
// caller keeps a sane fallback.
function freshestTimestamp(alerts: AlertItem[], activity: ActivityItem[]): number | null {
  let newest = -Infinity;
  const consider = (t: string | null | undefined) => {
    if (!t) return;
    const ms = Date.parse(t);
    if (!Number.isNaN(ms)) newest = Math.max(newest, ms);
  };
  alerts.forEach((a) => consider(a.time));
  activity.forEach((ev) => consider(ev.time));
  if (newest === -Infinity) return null;
  return Math.max(0, (Date.now() - newest) / 1000);
}
