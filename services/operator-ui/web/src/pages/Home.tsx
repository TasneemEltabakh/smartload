import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import {
  Activity,
  AlertTriangle,
  ArrowRight,
  CheckCircle2,
  Cpu,
  Gauge,
  Layers,
  ScrollText,
  Settings,
  ShieldCheck,
  Sparkles,
  TrendingUp,
  XCircle,
} from "lucide-react";

import {
  api,
  formatBytes,
  resourcesByService,
  type ActivityItem,
  type HealthSummary,
  type OpsMetrics,
  type RoutingMetrics,
  type ServiceHealth,
  type ServiceResource,
  type ThroughputResponse,
} from "../api";

const POLL_MS = 10_000;

function classFor(svc: ServiceHealth): "ok" | "degraded" | "bad" {
  if (svc.status === "ok") return "ok";
  if (svc.status === "degraded") return "degraded";
  return "bad";
}

function shortTime(iso: string): string {
  const m = iso.match(/T(\d{2}:\d{2}:\d{2})/);
  return m ? m[1] : iso;
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

function Donut({ pct, label, value, color }: { pct: number; label: string; value: string; color: string }) {
  const r = 44;
  const c = 2 * Math.PI * r;
  const stroke = Math.max(0, Math.min(100, pct));
  return (
    <div className="donut">
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
    </div>
  );
}

function Sparkline({ points }: { points: number[] }) {
  if (points.length === 0) return null;
  const w = 100, h = 100;
  const max = Math.max(...points, 1);
  const min = Math.min(...points, 0);
  const span = max - min || 1;
  const step = w / Math.max(1, points.length - 1);
  const pathD = points
    .map((p, i) => `${i === 0 ? "M" : "L"}${(i * step).toFixed(2)},${(h - ((p - min) / span) * h).toFixed(2)}`)
    .join(" ");
  const areaD = `${pathD} L${w},${h} L0,${h} Z`;
  return (
    <div className="sparkline">
      <svg viewBox={`0 0 ${w} ${h}`} preserveAspectRatio="none">
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
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    async function tick() {
      try {
        const [h, m, t, r, a, res] = await Promise.all([
          api.health(),
          api.getOpsMetrics().catch(() => null),
          api.getThroughput(24).catch(() => null),
          api.getRoutingMetrics().catch(() => null),
          api.getActivity(12).catch(() => []),
          api.getResources().catch(() => null),
        ]);
        if (cancelled) return;
        setHealth(h);
        setMetrics(m);
        setThroughput(t);
        setRouting(r);
        setActivity(a);
        if (res) setResources(resourcesByService(res));
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
  }, []);

  const services = health ? Object.entries(health.services) : [];
  const okCount = services.filter(([, s]) => classFor(s) === "ok").length;
  const degradedCount = services.filter(([, s]) => classFor(s) === "degraded").length;
  const badCount = services.filter(([, s]) => classFor(s) === "bad").length;

  const compliancePct = metrics?.policy_compliance_pct ?? null;
  const platformPct = services.length === 0 ? 0 : Math.round((okCount / services.length) * 100);

  const throughputPoints = (throughput?.buckets ?? []).map((b) => b.rpm);
  const safePoints = throughputPoints.length >= 2 ? throughputPoints : [0, 0];

  const alertsFromActivity = activity.filter((a) => a.severity !== "info").slice(0, 6);

  return (
    <>
      <div className="page-header">
        <div>
          <h2>Operations Overview</h2>
          <div className="subtitle">
            Real-time health, traffic and policy posture across the SmartLoad fleet
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
            <span className="kpi-icon"><Layers size={14} strokeWidth={2} /></span> Total services
          </div>
          <div className="kpi-value">{services.length || "—"}</div>
          <div className="kpi-trend">across SmartLoad fleet</div>
        </div>
        <div className="kpi green">
          <div className="kpi-label">
            <span className="kpi-icon"><CheckCircle2 size={14} strokeWidth={2} /></span> Healthy
          </div>
          <div className="kpi-value">{okCount}</div>
          <div className="kpi-trend up">
            {services.length > 0 ? `${Math.round((okCount / services.length) * 100)}% of fleet` : "—"}
          </div>
        </div>
        <div className="kpi amber">
          <div className="kpi-label">
            <span className="kpi-icon"><AlertTriangle size={14} strokeWidth={2} /></span> Active alerts
          </div>
          <div className="kpi-value">
            {metrics?.active_alerts ?? "0"}
          </div>
          <div className="kpi-trend">last 5 min · anomaly stream</div>
        </div>
        <div className="kpi violet">
          <div className="kpi-label">
            <span className="kpi-icon"><Activity size={14} strokeWidth={2} /></span> Requests
          </div>
          <div className="kpi-value">
            {throughput?.total_requests != null
              ? throughput.total_requests.toLocaleString()
              : "—"}
          </div>
          <div className="kpi-trend">last {(throughput?.buckets?.length ?? 24)} min · live</div>
        </div>
        <div className="kpi pink">
          <div className="kpi-label">
            <span className="kpi-icon"><ShieldCheck size={14} strokeWidth={2} /></span> Policy compliance
          </div>
          <div className="kpi-value">
            {compliancePct != null ? `${compliancePct}%` : "—"}
          </div>
          <div className="kpi-trend">services running policy-aligned</div>
        </div>
      </div>

      {/* ── Platform health + Active alerts ───────────────────────── */}
      <div className="grid-2 grid-stretch">
        <div className="card card-fill">
          <div className="card-head">
            <h2>Platform health</h2>
            <span className="meta">{services.length} services · polled every {POLL_MS / 1000}s</span>
          </div>
          <div className="donut" style={{ marginBottom: 16 }}>
            <Donut pct={platformPct} label="Overall" value={`${platformPct}%`} color="#22d3ee" />
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
                <span className="value">{services.length}</span>
              </div>
            </div>
          </div>
          {services.length === 0 ? (
            <div className="meta">Loading services…</div>
          ) : (
            <>
              <div className="svc-row svc-row-head">
                <div className="muted small">Service</div>
                <div className="muted small">Status</div>
                <div className="muted small" title="CPU — % of one core">CPU</div>
                <div className="muted small" title="Memory used">Memory</div>
              </div>
              {services.map(([name, svc]) => {
                const cls = classFor(svc);
                const res = resources[name];
                return (
                  <div className="svc-row" key={name}>
                    <div>
                      <div className="svc-name">{name}</div>
                      <div className="svc-meta">
                        {svc.status_code != null ? `HTTP ${svc.status_code}` : "no response"}
                        {svc.redis != null ? ` · redis:${svc.redis ? "ok" : "off"}` : ""}
                        {svc.timescaledb != null ? ` · tsdb:${svc.timescaledb ? "ok" : "off"}` : ""}
                        {svc.error ? ` · ${svc.error}` : null}
                      </div>
                    </div>
                    <span className={`svc-pill ${cls}`}>{svc.status}</span>
                    <span className="mono small" title="CPU — % of one core">
                      {res?.cpu_percent != null
                        ? `${res.cpu_percent.toFixed(res.cpu_percent < 10 ? 1 : 0)}%`
                        : "—"}
                    </span>
                    <span className="mono small" title="Memory used">
                      {res?.memory_used_bytes != null ? formatBytes(res.memory_used_bytes) : "—"}
                    </span>
                  </div>
                );
              })}
            </>
          )}
          {error ? <div className="meta" style={{ color: "var(--bad)", marginTop: 8 }}>{error}</div> : null}
        </div>

        <div className="card card-fill">
          <div className="card-head">
            <h2>Active alerts</h2>
            <Link className="link" to="/audit">View all <ArrowRight size={12} /></Link>
          </div>
          {alertsFromActivity.length === 0 ? (
            <div className="empty-state">
              <CheckCircle2 size={28} strokeWidth={1.5} />
              <div>No active alerts</div>
              <div className="empty-sub">All anomaly streams within thresholds in the last 5 min.</div>
            </div>
          ) : (
            alertsFromActivity.map((a, i) => (
              <div key={i} className={`alert-row ${a.severity === "bad" ? "bad" : "warn"}`}>
                <div className="icon">
                  {a.severity === "bad" ? <XCircle size={16} /> : <AlertTriangle size={16} />}
                </div>
                <div>
                  <div className="title">{a.summary}</div>
                  <div className="meta">{a.source} · {a.kind}{a.actor ? ` · ${a.actor}` : ""}</div>
                </div>
                <div className="time">{shortTime(a.time)}</div>
              </div>
            ))
          )}
          <div style={{ flex: 1 }} />
        </div>
      </div>

      {/* ── Throughput row ────────────────────────────────────────── */}
      <div className="grid-2 grid-stretch">
        <div className="card card-fill">
          <div className="card-head">
            <h2>Throughput</h2>
            <span className="meta">
              Requests / min · last {throughput?.buckets?.length ?? 24} buckets
              {throughput?.current_rpm != null ? <> · current <strong>{throughput.current_rpm} rpm</strong></> : null}
            </span>
          </div>
          <Sparkline points={safePoints} />
          {throughputPoints.length === 0 ? (
            <div className="meta" style={{ marginTop: 8 }}>
              Waiting for first <code>request_count</code> samples — the telemetry service is querying TimescaleDB now.
            </div>
          ) : null}
        </div>

        <div className="stack card-fill">
          <div className="card">
            <div className="card-head">
              <h2>Policy compliance</h2>
            </div>
            <Donut
              pct={compliancePct ?? 0}
              label="Compliant"
              value={`${compliancePct ?? "—"}${compliancePct != null ? "%" : ""}`}
              color="#a78bfa"
            />
          </div>
          <div className="card stretch">
            <div className="card-head">
              <h2>Routing &amp; scaling</h2>
              {routing?.autoscaler ? (
                <span className={`svc-pill ${routing.autoscaler.status === "ok" ? "ok" : "degraded"}`}>
                  autoscaler {routing.autoscaler.status === "ok" ? "alive" : routing.autoscaler.status ?? "—"}
                </span>
              ) : null}
            </div>
            <div className="svc-row">
              <div className="svc-name">Routing decisions / min</div>
              <span className="mono">{routing?.routing_decisions_per_min ?? "—"}</span>
              <span />
              <span />
            </div>
            <div className="svc-row">
              <div className="svc-name">Scale events (1h)</div>
              <span className="mono">{routing?.scale_events_1h ?? "—"}</span>
              <span />
              <span />
            </div>
            <div className="svc-row">
              <div className="svc-name">Cluster size</div>
              <span className="mono">{routing?.cluster_size_current ?? "—"}</span>
              <span />
              <span />
            </div>
            {routing?.autoscaler ? (
              <>
                <div className="svc-row">
                  <div className="svc-name">Autoscaler decisions</div>
                  <span className="mono">{routing.autoscaler.decisions_total.toLocaleString()}</span>
                  <span className="muted small">
                    {routing.autoscaler.decisions_actuated} actuated
                  </span>
                  <span className="muted small">
                    {routing.autoscaler.decisions_noop.toLocaleString()} noop
                  </span>
                </div>
                {routing.autoscaler.last_actuation?.time ? (
                  <div className="svc-row">
                    <div className="svc-name">Last actuation</div>
                    <span className={`badge-action ${routing.autoscaler.last_actuation.action ?? "scale_out"}`}>
                      {routing.autoscaler.last_actuation.action}
                    </span>
                    <span className="muted small">
                      → {routing.autoscaler.last_actuation.instance_count} backend(s)
                    </span>
                    <span className="muted small">{timeAgo(routing.autoscaler.last_actuation.time)}</span>
                  </div>
                ) : null}
              </>
            ) : null}
          </div>
        </div>
      </div>

      {/* ── Recent activity ───────────────────────────────────────── */}
      <div className="card">
        <div className="card-head">
          <h2>Recent activity</h2>
          <Link className="link" to="/audit">Open audit log <ArrowRight size={12} /></Link>
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
          <h2>Quick actions</h2>
          <span className="meta">Shortcuts to common operator flows</span>
        </div>
        <div className="quick-actions">
          <Link to="/engines" className="qa-tile">
            <span className="qa-icon"><Cpu size={16} /></span>
            <span>View live engines</span>
            <span className="qa-sub">Per-engine status + activity stream</span>
          </Link>
          <Link to="/policy" className="qa-tile violet">
            <span className="qa-icon"><ShieldCheck size={16} /></span>
            <span>Review policy</span>
            <span className="qa-sub">Edit, validate, commit</span>
          </Link>
          <Link to="/actions" className="qa-tile amber">
            <span className="qa-icon"><Settings size={16} /></span>
            <span>Manual actions</span>
            <span className="qa-sub">Scale, isolate, route</span>
          </Link>
          <Link to="/audit" className="qa-tile green">
            <span className="qa-icon"><ScrollText size={16} /></span>
            <span>View audit log</span>
            <span className="qa-sub">Policy &amp; scaling history</span>
          </Link>
        </div>
      </div>
    </>
  );
}

// Silence unused-import warnings for variants we may surface later.
void Sparkles; void TrendingUp; void Gauge;
