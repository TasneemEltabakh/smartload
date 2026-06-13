import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import {
  Activity,
  AlertTriangle,
  ArrowRight,
  BookOpen,
  CheckCircle2,
  Cpu,
  Layers,
  ScrollText,
  ShieldCheck,
  XCircle,
} from "lucide-react";

import {
  api,
  backendsByService,
  resourcesByService,
  type ActivityItem,
  type AlertItem,
  type BackendMetrics,
  type HealthSummary,
  type OpsMetrics,
  type Policy,
  type RelatedMetrics,
  type RoutingMetrics,
  type ServiceBackendStat,
  type ServiceHealth,
  type ServiceResource,
  type ThroughputResponse,
} from "../api";

const POLL_MS = 10_000;

// Throughput range selector → number of 1-minute buckets to request.
const RANGE_OPTIONS = [15, 30, 60] as const;
type RangeMinutes = (typeof RANGE_OPTIONS)[number];

function classFor(svc: ServiceHealth): "ok" | "degraded" | "bad" {
  if (svc.status === "ok") return "ok";
  if (svc.status === "degraded") return "degraded";
  return "bad";
}

function timeAgo(iso: string | null): string {
  if (!iso) return "—";
  const then = Date.parse(iso);
  if (Number.isNaN(then)) return iso;
  const s = Math.max(0, (Date.now() - then) / 1000);
  if (s < 60) return `${Math.floor(s)}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  return `${Math.floor(s / 3600)}h ago`;
}

// Percentage change between the first half and second half of a series. Used
// for the "+X% vs previous window" throughput delta. Returns null when there
// aren't enough buckets, or when the previous window summed to zero.
function windowDelta(points: number[]): number | null {
  if (points.length < 4) return null;
  const mid = Math.floor(points.length / 2);
  const prev = points.slice(0, mid).reduce((a, b) => a + b, 0);
  const curr = points.slice(mid).reduce((a, b) => a + b, 0);
  if (prev === 0) return null;
  return ((curr - prev) / prev) * 100;
}

function Donut({ pct, label, value, color }: { pct: number; label: string; value: string; color: string }) {
  const r = 44;
  const c = 2 * Math.PI * r;
  const stroke = Math.max(0, Math.min(100, pct));
  return (
    <svg viewBox="0 0 120 120">
      <circle cx="60" cy="60" r={r} stroke="rgba(255,255,255,0.06)" strokeWidth="10" fill="none" />
      <circle
        cx="60" cy="60" r={r}
        stroke={color}
        strokeWidth="10"
        fill="none"
        strokeLinecap="round"
        strokeDasharray={`${(stroke / 100) * c} ${c}`}
        transform="rotate(-90 60 60)"
      />
      <text x="60" y="64" textAnchor="middle" fontSize="20" fontWeight="600" fill="#e6edf3">
        {value}
      </text>
      <text x="60" y="82" textAnchor="middle" fontSize="9" fill="#7d8590" letterSpacing="0.8">
        {label.toUpperCase()}
      </text>
    </svg>
  );
}

// Line + area sparkline with Y/X axis labels derived from the buckets.
function Sparkline({ buckets }: { buckets: { time: string; rpm: number }[] }) {
  if (buckets.length === 0) return null;
  const points = buckets.map((b) => b.rpm);
  const w = 100, h = 100;
  const max = Math.max(...points, 1);
  const min = Math.min(...points, 0);
  const span = max - min || 1;
  const step = w / Math.max(1, points.length - 1);
  const pathD = points
    .map((p, i) => `${i === 0 ? "M" : "L"}${(i * step).toFixed(2)},${(h - ((p - min) / span) * h).toFixed(2)}`)
    .join(" ");
  const areaD = `${pathD} L${w},${h} L0,${h} Z`;

  // Compact y-axis labels (max / mid / min) and a few x-axis time ticks.
  const fmtNum = (n: number) => (n >= 1000 ? `${(n / 1000).toFixed(n >= 10000 ? 0 : 1)}K` : `${Math.round(n)}`);
  const fmtTime = (iso: string) => {
    const m = iso.match(/T(\d{2}:\d{2})/);
    return m ? m[1] : iso;
  };
  const yLabels = [max, (max + min) / 2, min];
  const tickIdx = [0, Math.floor((points.length - 1) / 2), points.length - 1].filter(
    (v, i, arr) => arr.indexOf(v) === i,
  );

  return (
    <div className="sparkline" style={{ display: "grid", gridTemplateColumns: "34px 1fr", gap: 4 }}>
      <div
        className="axis"
        style={{ display: "flex", flexDirection: "column", justifyContent: "space-between", textAlign: "right", paddingRight: 4 }}
      >
        {yLabels.map((v, i) => (
          <span key={i} style={{ fill: "var(--muted)", color: "var(--muted)", fontSize: 10 }}>{fmtNum(v)}</span>
        ))}
      </div>
      <div style={{ display: "flex", flexDirection: "column" }}>
        <svg viewBox={`0 0 ${w} ${h}`} preserveAspectRatio="none" style={{ flex: 1 }}>
          <defs>
            <linearGradient id="sparkFill" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor="rgba(34,211,238,0.35)" />
              <stop offset="100%" stopColor="rgba(34,211,238,0)" />
            </linearGradient>
          </defs>
          <g className="grid">
            {[25, 50, 75].map((y) => (
              <line key={y} x1="0" x2={w} y1={y} y2={y} />
            ))}
          </g>
          <path d={areaD} fill="url(#sparkFill)" />
          <path d={pathD} fill="none" stroke="#22d3ee" strokeWidth="1.4" />
        </svg>
        <div className="axis" style={{ display: "flex", justifyContent: "space-between", marginTop: 4 }}>
          {tickIdx.map((idx) => (
            <span key={idx} style={{ color: "var(--muted)", fontSize: 10 }}>{fmtTime(buckets[idx].time)}</span>
          ))}
        </div>
      </div>
    </div>
  );
}

export default function HomePage() {
  const [health, setHealth] = useState<HealthSummary | null>(null);
  const [metrics, setMetrics] = useState<OpsMetrics | null>(null);
  const [throughput, setThroughput] = useState<ThroughputResponse | null>(null);
  const [routing, setRouting] = useState<RoutingMetrics | null>(null);
  const [activity, setActivity] = useState<ActivityItem[]>([]);
  const [resources, setResources] = useState<Record<string, ServiceResource>>({});
  const [backends, setBackends] = useState<Record<string, ServiceBackendStat>>({});
  const [backendAgg, setBackendAgg] = useState<BackendMetrics["aggregate"]>(null);
  const [alerts, setAlerts] = useState<AlertItem[]>([]);
  const [policy, setPolicy] = useState<Policy | null>(null);
  const [related, setRelated] = useState<RelatedMetrics | null>(null);
  const [rangeMinutes, setRangeMinutes] = useState<RangeMinutes>(15);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    async function tick() {
      try {
        const [h, m, t, r, a, res, bm, al, pol, rel] = await Promise.all([
          api.health(),
          api.getOpsMetrics().catch(() => null),
          api.getThroughput(rangeMinutes).catch(() => null),
          api.getRoutingMetrics().catch(() => null),
          api.getActivity(12).catch(() => []),
          api.getResources().catch(() => null),
          api.getBackendMetrics().catch(() => null),
          api.getAlerts().catch(() => []),
          api.getPolicy().catch(() => null),
          api.getRelatedMetrics().catch(() => null),
        ]);
        if (cancelled) return;
        setHealth(h);
        setMetrics(m);
        setThroughput(t);
        setRouting(r);
        setActivity(a);
        setResources(res ? resourcesByService(res) : {});
        setBackends(bm ? backendsByService(bm) : {});
        setBackendAgg(bm?.aggregate ?? null);
        setAlerts(al);
        setPolicy(pol);
        setRelated(rel);
        setError(null);
      } catch (err: any) {
        if (!cancelled) setError(err.message || "load failed");
      }
    }
    tick();
    const id = setInterval(tick, POLL_MS);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, [rangeMinutes]);

  const services = health ? Object.entries(health.services) : [];
  const okCount = services.filter(([, s]) => classFor(s) === "ok").length;
  const degradedCount = services.filter(([, s]) => classFor(s) === "degraded").length;
  const badCount = services.filter(([, s]) => classFor(s) === "bad").length;

  const servicesTotal = metrics?.services_total ?? services.length;
  const servicesHealthy = metrics?.services_healthy ?? okCount;
  const compliancePct = metrics?.policy_compliance_pct ?? null;
  const platformPct = servicesTotal === 0 ? 0 : Math.round((servicesHealthy / servicesTotal) * 100);

  // Active-alerts subtext: count by severity from the structured alerts feed.
  const criticalCount = alerts.filter((a) => a.severity === "critical").length;
  const warningCount = alerts.filter((a) => a.severity === "warning").length;
  const activeAlerts = metrics?.active_alerts ?? alerts.length;

  // Requests/min KPI: total requests over the window + first-half vs second-half delta.
  const throughputBuckets = throughput?.buckets ?? [];
  const throughputPoints = throughputBuckets.map((b) => b.rpm);
  const reqDelta = windowDelta(throughputPoints);
  const requestsTotal = throughput?.total_requests ?? metrics?.requests_total ?? null;

  // Per-backend rows: the load-balancer reads the aggregate (its view across all
  // backends); control-plane services serve no proxied traffic, so p95/req-min
  // stay "—" for them. The test-backend pulls from the per-service rollup.
  // Rows are real services (health + resource-collector, both clean names). We
  // deliberately do NOT seed rows from backend stats: their instance addresses
  // carry the compose project prefix (e.g. "smartload-test-backend"), which
  // would otherwise show up as a duplicate of the clean "test-backend" row.
  const tableServices = Array.from(
    new Set([
      ...services.map(([name]) => name),
      ...Object.keys(resources),
    ]),
  );

  // Attach per-backend request stats to a service row, tolerating the compose
  // project prefix on the instance-derived key (suffix match).
  function backendStatFor(name: string) {
    if (backends[name]) return backends[name];
    const key = Object.keys(backends).find((k) => k === name || k.endsWith(`-${name}`));
    return key ? backends[key] : undefined;
  }

  function pForService(name: string): { p95: string; rpm: string } {
    if (name === "load-balancer") {
      return {
        p95: backendAgg?.p95_ms != null ? `${Math.round(backendAgg.p95_ms)} ms` : (related?.p95_latency_ms != null ? `${Math.round(related.p95_latency_ms)} ms` : "—"),
        rpm: backendAgg?.rpm != null ? Math.round(backendAgg.rpm).toLocaleString() : "—",
      };
    }
    const b = backendStatFor(name);
    if (b) {
      return {
        p95: b.p95_ms != null ? `${Math.round(b.p95_ms)} ms` : "—",
        rpm: b.rpm != null ? Math.round(b.rpm).toLocaleString() : "—",
      };
    }
    return { p95: "—", rpm: "—" };
  }

  // Routing & scaling derived values.
  const clusterSize = routing?.cluster_size_current ?? null;
  const maxBackends = policy?.max_backends ?? null;
  const capacityPct =
    clusterSize != null && maxBackends != null && maxBackends > 0
      ? Math.round((clusterSize / maxBackends) * 100)
      : null;
  const capacityClass = capacityPct == null ? "" : capacityPct > 90 ? "bad" : capacityPct > 75 ? "warn" : "";
  const autoscalingLabel = policy ? (policy.safe_mode ? "Disabled (safe mode)" : "Enabled") : "—";

  return (
    <>
      <div className="page-header">
        <div>
          <h2>Operations Overview</h2>
          <div className="subtitle">
            Real-time summary of system health, performance and policy posture across the SmartLoad fleet
          </div>
        </div>
        <div className="header-actions">
          <span className="refresh-chip">
            <span className="pulse" /> Auto-refresh {POLL_MS / 1000}s
          </span>
        </div>
      </div>

      {/* ── KPI row ────────────────────────────────────────────────── */}
      <div className="kpi-row">
        <div className="kpi cyan">
          <div className="kpi-label">
            <span className="kpi-icon"><Layers size={14} strokeWidth={2} /></span> Total Services
          </div>
          <div className="kpi-value">{servicesTotal || "—"}</div>
          <div className="kpi-trend">All configured services</div>
        </div>
        <div className="kpi green">
          <div className="kpi-label">
            <span className="kpi-icon"><CheckCircle2 size={14} strokeWidth={2} /></span> Healthy Services
          </div>
          <div className="kpi-value">{servicesHealthy}</div>
          <div className="kpi-trend">
            {servicesTotal > 0 ? `${((servicesHealthy / servicesTotal) * 100).toFixed(1)}% of total` : "—"}
          </div>
        </div>
        <div className="kpi amber">
          <div className="kpi-label">
            <span className="kpi-icon"><AlertTriangle size={14} strokeWidth={2} /></span> Active Alerts
          </div>
          <div className="kpi-value">{activeAlerts}</div>
          <div className="kpi-trend">
            {alerts.length > 0 ? `${criticalCount} critical · ${warningCount} warning` : "No active alerts"}
          </div>
        </div>
        <div className="kpi violet">
          <div className="kpi-label">
            <span className="kpi-icon"><Activity size={14} strokeWidth={2} /></span> Requests / Min
          </div>
          <div className="kpi-value">
            {requestsTotal != null ? requestsTotal.toLocaleString() : "—"}
          </div>
          {reqDelta != null ? (
            <div className={`kpi-trend ${reqDelta >= 0 ? "up" : "down"}`}>
              {reqDelta >= 0 ? "+" : ""}{reqDelta.toFixed(1)}% vs {rangeMinutes}m ago
            </div>
          ) : (
            <div className="kpi-trend">last {rangeMinutes}m · live</div>
          )}
        </div>
        <div className="kpi pink">
          <div className="kpi-label">
            <span className="kpi-icon"><ShieldCheck size={14} strokeWidth={2} /></span> Policy Compliance
          </div>
          <div className="kpi-value">
            {compliancePct != null ? `${compliancePct}%` : "—"}
          </div>
          <div className="kpi-trend">{servicesTotal > 0 ? `${servicesHealthy} / ${servicesTotal} services passing` : "—"}</div>
        </div>
      </div>

      {/* ── Platform health + Service health + Active alerts ───────── */}
      <div className="grid-3 grid-stretch">
        <div className="card card-fill">
          <div className="card-head">
            <h2>Platform Health</h2>
            <span className="meta">{servicesTotal} services</span>
          </div>
          <div className="donut">
            <Donut pct={platformPct} label="Healthy" value={`${platformPct}%`} color="#3fb950" />
            <div className="donut-legend">
              <div className="row">
                <span className="label"><span className="sw-wrap"><span className="swatch" style={{ background: "#3fb950" }} /></span> Healthy</span>
                <span className="value">{okCount}</span>
              </div>
              <div className="row">
                <span className="label"><span className="sw-wrap"><span className="swatch" style={{ background: "#d29922" }} /></span> Degraded</span>
                <span className="value">{degradedCount}</span>
              </div>
              <div className="row">
                <span className="label"><span className="sw-wrap"><span className="swatch" style={{ background: "#f85149" }} /></span> Unreachable</span>
                <span className="value">{badCount}</span>
              </div>
              <div className="row">
                <span className="label muted">Total</span>
                <span className="value">{servicesTotal}</span>
              </div>
            </div>
          </div>
          {badCount === 0 && degradedCount === 0 && services.length > 0 ? (
            <div className="alert-row ok" style={{ marginTop: 16 }}>
              <div className="icon"><CheckCircle2 size={16} /></div>
              <div><div className="title">All critical systems are operational.</div></div>
              <div />
            </div>
          ) : null}
          {error ? <div className="meta" style={{ color: "var(--bad)", marginTop: 8 }}>{error}</div> : null}
        </div>

        <div className="card card-fill">
          <div className="card-head">
            <h2>Service Health</h2>
            <Link className="link" to="/engines">View all services <ArrowRight size={12} /></Link>
          </div>
          {tableServices.length === 0 ? (
            <div className="meta">Loading services…</div>
          ) : (
            <>
              <div className="svc-row svc-row-head" style={{ gridTemplateColumns: "1fr 90px 70px 80px 56px" }}>
                <div className="muted small">Service</div>
                <div className="muted small">Status</div>
                <div className="muted small" title="Response time — p95">p95</div>
                <div className="muted small">Req/min</div>
                <div className="muted small" title="CPU — % of one core">CPU</div>
              </div>
              {tableServices.map((name) => {
                const svc = health?.services[name];
                const cls = svc ? classFor(svc) : "ok";
                const res = resources[name];
                const { p95, rpm } = pForService(name);
                return (
                  <div className="svc-row" key={name} style={{ gridTemplateColumns: "1fr 90px 70px 80px 56px" }}>
                    <div className="svc-name">{name}</div>
                    {svc ? (
                      <span className={`svc-pill ${cls}`}>{svc.status}</span>
                    ) : (
                      <span className="mono small muted">—</span>
                    )}
                    <span className="mono small" title="Response time — p95">{p95}</span>
                    <span className="mono small">{rpm}</span>
                    <span className="mono small" title="CPU — % of one core">
                      {res?.cpu_percent != null
                        ? `${res.cpu_percent.toFixed(res.cpu_percent < 10 ? 1 : 0)}%`
                        : "—"}
                    </span>
                  </div>
                );
              })}
            </>
          )}
        </div>

        <div className="card card-fill">
          <div className="card-head">
            <h2>Active Alerts</h2>
            <Link className="link" to="/audit">
              {alerts.length > 0 ? `${alerts.length} active` : "View all"} <ArrowRight size={12} />
            </Link>
          </div>
          {alerts.length === 0 ? (
            <div className="empty-state">
              <CheckCircle2 size={28} strokeWidth={1.5} />
              <div>No active alerts</div>
              <div className="empty-sub">All backends are within their latency and error-rate thresholds.</div>
            </div>
          ) : (
            alerts.slice(0, 6).map((a, i) => (
              <div key={i} className={`alert-row ${a.severity === "critical" ? "bad" : "warn"}`}>
                <div className="icon">
                  {a.severity === "critical" ? <XCircle size={16} /> : <AlertTriangle size={16} />}
                </div>
                <div>
                  <span className={`sev-badge ${a.severity}`}>{a.severity}</span>
                  <div className="title" style={{ marginTop: 4 }}>{a.backend_id}</div>
                  <div className="meta">{a.summary}</div>
                </div>
                <div className="time">{timeAgo(a.time)}</div>
              </div>
            ))
          )}
          <div style={{ flex: 1 }} />
        </div>
      </div>

      {/* ── Throughput + Policy compliance + Routing & scaling ─────── */}
      <div className="grid-3 grid-stretch">
        <div className="card card-fill">
          <div className="card-head">
            <h2>Throughput <span className="meta" style={{ fontWeight: 400 }}>(Requests / Min)</span></h2>
            <select
              value={rangeMinutes}
              onChange={(e) => setRangeMinutes(Number(e.target.value) as RangeMinutes)}
              style={{ padding: "4px 8px", fontSize: 12 }}
            >
              {RANGE_OPTIONS.map((m) => (
                <option key={m} value={m}>{m} minutes</option>
              ))}
            </select>
          </div>
          {throughputBuckets.length === 0 ? (
            <div className="meta" style={{ marginTop: 8 }}>
              Waiting for the first request samples from the telemetry service.
            </div>
          ) : (
            <>
              <Sparkline buckets={throughputBuckets} />
              <div style={{ marginTop: 10, display: "flex", alignItems: "center", gap: 10 }}>
                {reqDelta != null ? (
                  <span className={`kpi-trend ${reqDelta >= 0 ? "up" : "down"}`} style={{ margin: 0 }}>
                    {reqDelta >= 0 ? "+" : ""}{reqDelta.toFixed(1)}% vs previous {rangeMinutes} minutes
                  </span>
                ) : null}
                {throughput?.current_rpm != null ? (
                  <span className="meta" style={{ margin: 0 }}>current <strong>{throughput.current_rpm.toLocaleString()} rpm</strong></span>
                ) : null}
              </div>
            </>
          )}
        </div>

        <div className="card card-fill">
          <div className="card-head">
            <h2>Policy Compliance</h2>
          </div>
          <div className="donut">
            <Donut
              pct={compliancePct ?? 0}
              label="Compliant"
              value={compliancePct != null ? `${compliancePct}%` : "—"}
              color="#3fb950"
            />
            <div className="donut-legend">
              <div className="row">
                <span className="label"><span className="sw-wrap"><span className="swatch" style={{ background: "#3fb950" }} /></span> Passing</span>
                <span className="value">{okCount}</span>
              </div>
              <div className="row">
                <span className="label"><span className="sw-wrap"><span className="swatch" style={{ background: "#d29922" }} /></span> Warning</span>
                <span className="value">{degradedCount}</span>
              </div>
              <div className="row">
                <span className="label"><span className="sw-wrap"><span className="swatch" style={{ background: "#f85149" }} /></span> Failing</span>
                <span className="value">{badCount}</span>
              </div>
            </div>
          </div>
        </div>

        <div className="card card-fill">
          <div className="card-head">
            <h2>Routing &amp; Scaling</h2>
          </div>
          <div className="svc-row" style={{ gridTemplateColumns: "1fr auto" }}>
            <div className="svc-name">Routing Mode</div>
            <span className="mono" style={{ color: "var(--ok)" }}>{policy?.operating_mode ?? "—"}</span>
          </div>
          <div className="svc-row" style={{ gridTemplateColumns: "1fr auto" }}>
            <div className="svc-name">Autoscaling</div>
            <span className="mono" style={{ color: policy && policy.safe_mode ? "var(--warn)" : "var(--ok)" }}>
              {autoscalingLabel}
            </span>
          </div>
          <div className="svc-row" style={{ gridTemplateColumns: "1fr auto" }}>
            <div className="svc-name">Cluster Size</div>
            <span className="mono">{clusterSize ?? "—"}{maxBackends != null ? ` / ${maxBackends}` : ""}</span>
          </div>
          <div className="svc-row" style={{ gridTemplateColumns: "1fr auto" }}>
            <div className="svc-name">Routing decisions / min</div>
            <span className="mono">{routing?.routing_decisions_per_min ?? "—"}</span>
          </div>
          <div className="svc-row" style={{ gridTemplateColumns: "1fr auto", borderBottom: 0 }}>
            <div className="svc-name">Scale events (1h)</div>
            <span className="mono">{routing?.scale_events_1h ?? "—"}</span>
          </div>
          <div style={{ marginTop: 12 }}>
            <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 6, fontSize: 12 }}>
              <span className="muted">Cluster Capacity</span>
              <span className="mono">{capacityPct != null ? `${capacityPct}%` : "—"}</span>
            </div>
            <div className={`capacity-bar ${capacityClass}`}>
              <div className="fill" style={{ width: `${Math.min(100, capacityPct ?? 0)}%` }} />
            </div>
          </div>
        </div>
      </div>

      {/* ── Recent activity ───────────────────────────────────────── */}
      <div className="card">
        <div className="card-head">
          <h2>Recent Activity</h2>
          <Link className="link" to="/audit">View all <ArrowRight size={12} /></Link>
        </div>
        {activity.length === 0 ? (
          <div className="meta">No events recorded yet.</div>
        ) : (
          activity.slice(0, 8).map((a, i) => (
            <div key={i} className={`activity-row ${a.kind}`}>
              <div className="marker" />
              <div className="body">
                <div className="summary">{a.summary}</div>
                <div className="meta">{a.kind} · {a.source}{a.actor ? ` · ${a.actor}` : ""}</div>
              </div>
              <div className="time">{timeAgo(a.time)}</div>
            </div>
          ))
        )}
      </div>

      {/* ── Quick actions ─────────────────────────────────────────── */}
      <div className="card">
        <div className="card-head">
          <h2>Quick Actions</h2>
        </div>
        <div className="quick-actions">
          <Link to="/engines" className="qa-tile">
            <span className="qa-icon"><Cpu size={16} /></span>
            <span>View Live Engines</span>
            <span className="qa-sub">Real-time engine status</span>
          </Link>
          <Link to="/audit" className="qa-tile bad">
            <span className="qa-icon"><AlertTriangle size={16} /></span>
            <span>Review Alerts</span>
            <span className="qa-sub">Open alert center</span>
          </Link>
          <Link to="/policy" className="qa-tile violet">
            <span className="qa-icon"><ShieldCheck size={16} /></span>
            <span>Open Policy</span>
            <span className="qa-sub">Manage policies</span>
          </Link>
          <Link to="/audit" className="qa-tile green">
            <span className="qa-icon"><ScrollText size={16} /></span>
            <span>View Audit Log</span>
            <span className="qa-sub">Recent system activity</span>
          </Link>
          <a href="/api/docs" className="qa-tile">
            <span className="qa-icon"><BookOpen size={16} /></span>
            <span>API Documentation</span>
            <span className="qa-sub">Explore API docs</span>
          </a>
        </div>
      </div>
    </>
  );
}
