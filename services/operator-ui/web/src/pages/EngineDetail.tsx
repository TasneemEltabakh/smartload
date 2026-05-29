import { useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import {
  ArrowLeft,
  AlertTriangle,
  CheckCircle2,
  ExternalLink,
  RefreshCw,
  XCircle,
} from "lucide-react";

import {
  api,
  type EngineStateBody,
  type EngineStreamEvent,
  type EnginesSnapshot,
} from "../api";

// Per-engine deep-dive page (slice #121 session 2, OUI.3 close-out).
// Reached via the /engines/<service> route. Renders the full engine
// state + a feed filtered to that engine's primary publish channel +
// a one-click jump to the matching Grafana dashboard. The right-slide
// drawer on the Live Engines page remains the quick preview; this
// page is the full surface.

const REFRESH_MS = 5_000;
const FEED_MAX = 80;

// Grafana origin: the dashboard server runs on :3000 alongside the BFF
// on :8090. Anonymous viewer + ALLOW_EMBEDDING are set in docker-compose
// so cross-origin iframe loads render without auth (#131 Phase 3, v1.0.7l).
// In production this'll switch to a same-origin /grafana/* reverse proxy.
const GRAFANA_ORIGIN = "http://localhost:3000";

type EmbedPanel = { id: number; title: string };

const ENGINE_LABELS: Record<
  string,
  { title: string; channel: string; dashUid: string; dashSlug: string; panels: EmbedPanel[] }
> = {
  "anomaly-detector": {
    title: "Anomaly Detector",
    channel: "smartload.anomaly",
    dashUid: "smartload-anomaly",
    dashSlug: "smartload-anomaly",
    panels: [
      { id: 1, title: "Backend health timeline" },
      { id: 4, title: "Time-in-state per backend" },
    ],
  },
  "forecasting": {
    title: "Forecasting",
    channel: "smartload.forecast",
    dashUid: "smartload-forecast",
    dashSlug: "smartload-forecast",
    panels: [
      { id: 1, title: "Predicted vs actual RPS" },
      { id: 3, title: "Forecast accuracy at decision moments" },
    ],
  },
  "rl-engine": {
    title: "RL Engine",
    channel: "smartload.routing",
    dashUid: "smartload-rl-routing",
    dashSlug: "smartload-rl-routing",
    panels: [
      { id: 1, title: "Request share per backend" },
      { id: 2, title: "p95 latency per backend" },
    ],
  },
};

function dashUrl(uid: string, slug: string): string {
  return `${GRAFANA_ORIGIN}/d/${uid}/${slug}?orgId=1&from=now-30m&to=now&refresh=10s`;
}

function soloPanelUrl(uid: string, slug: string, panelId: number): string {
  return (
    `${GRAFANA_ORIGIN}/d-solo/${uid}/${slug}` +
    `?orgId=1&panelId=${panelId}&theme=dark&from=now-30m&to=now&refresh=10s`
  );
}

type Status = "ok" | "warn" | "bad";

function statusOf(svc: EngineStateBody | undefined): Status {
  if (!svc || !svc.reachable) return "bad";
  if (!svc.runloop_enabled) return "warn";
  if (svc.engine && !svc.engine.ready) return "warn";
  return "ok";
}

function StatusBadge({ s }: { s: Status }) {
  if (s === "ok")
    return (
      <span className="badge badge-ok">
        <CheckCircle2 size={12} /> Healthy
      </span>
    );
  if (s === "warn")
    return (
      <span className="badge badge-warn">
        <AlertTriangle size={12} /> Degraded
      </span>
    );
  return (
    <span className="badge badge-bad">
      <XCircle size={12} /> Unreachable
    </span>
  );
}

function shortTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  return iso.replace("T", " ").replace(/\.\d+.*/, "").replace(/\+.*$/, "");
}

function timeOfDay(iso: string | null | undefined): string {
  if (!iso) return "—";
  const m = iso.match(/T(\d{2}:\d{2}:\d{2})/);
  return m ? m[1] : iso;
}

export default function EngineDetailPage() {
  const { service = "" } = useParams<{ service: string }>();
  const navigate = useNavigate();
  const meta = ENGINE_LABELS[service];

  const [snapshot, setSnapshot] = useState<EnginesSnapshot | null>(null);
  const [pollErr, setPollErr] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);

  // Guard: if URL points to an unknown service, bounce home with a hint.
  useEffect(() => {
    if (!meta) {
      // small delay so the message above renders briefly
      const id = setTimeout(() => navigate("/engines", { replace: true }), 1200);
      return () => clearTimeout(id);
    }
  }, [meta, navigate]);

  useEffect(() => {
    if (!meta) return;
    let cancelled = false;
    async function tick() {
      try {
        setRefreshing(true);
        const snap = await api.getEnginesSnapshot();
        if (!cancelled) {
          setSnapshot(snap);
          setPollErr(null);
        }
      } catch (e: any) {
        if (!cancelled) setPollErr(e?.message ?? "snapshot fetch failed");
      } finally {
        if (!cancelled) setRefreshing(false);
      }
    }
    tick();
    const id = setInterval(tick, REFRESH_MS);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, [meta]);

  if (!meta) {
    return (
      <div className="engine-detail">
        <Link to="/engines" className="back-link">
          <ArrowLeft size={14} /> Back to Live Engines
        </Link>
        <h2>Unknown engine</h2>
        <p className="dim">
          <code>{service}</code> isn't one of the three configured engines.
          Redirecting to <code>/engines</code>…
        </p>
      </div>
    );
  }

  const body: EngineStateBody | undefined = snapshot?.services?.[service];
  const status = statusOf(body);
  const channelEvents: EngineStreamEvent[] = snapshot?.channels?.[meta.channel] ?? [];
  const lastOutput = body?.last_output;
  const policy = body?.policy_snapshot as Record<string, unknown> | undefined;

  return (
    <div className="engine-detail">
      <div className="engine-detail-head">
        <Link to="/engines" className="back-link">
          <ArrowLeft size={14} /> Live Engines
        </Link>
        <div className="engine-detail-title">
          <h2>{meta.title}</h2>
          <StatusBadge s={status} />
        </div>
        <div className="engine-detail-meta">
          <code>{meta.channel}</code>
          {body?.engine?.loaded ? (
            <>
              {" · "}
              <span className="dim">
                {body.engine.kind === "policy" ? "policy" : "engine"}:
              </span>{" "}
              <code>{body.engine.loaded}</code>
              {body.engine.requested !== body.engine.loaded && (
                <span className="warn-inline">
                  {" "}
                  (fallback from <code>{body.engine.requested}</code>)
                </span>
              )}
            </>
          ) : null}
          {refreshing ? <span className="dim refresh-indicator"><RefreshCw size={11} /></span> : null}
        </div>
        <div className="engine-detail-actions">
          <a
            href={dashUrl(meta.dashUid, meta.dashSlug)}
            target="_blank"
            rel="noreferrer"
            className="btn-link"
          >
            Open Grafana dashboard <ExternalLink size={12} />
          </a>
          <a
            href={`http://localhost:${
              service === "anomaly-detector" ? 8082 :
              service === "forecasting" ? 8083 : 8084
            }/api/v1/engine/state`}
            target="_blank"
            rel="noreferrer"
            className="btn-link dim"
          >
            Raw /api/v1/engine/state <ExternalLink size={12} />
          </a>
        </div>
      </div>

      {pollErr ? <div className="banner warn">Snapshot poll: {pollErr}</div> : null}

      <div className="engine-detail-grid">
        {/* ── stats card ───────────────────────────────────────────── */}
        <section className="card">
          <h3 className="card-h">Run-loop stats</h3>
          {body?.stats ? (
            <dl className="kv">
              <dt>Ticks</dt>
              <dd>{body.stats.ticks_total ?? 0}</dd>
              <dt>Publishes</dt>
              <dd>{body.stats.publishes_total ?? 0}</dd>
              <dt>Last tick</dt>
              <dd>
                <code>{shortTime(body.stats.last_tick_at)}</code>
                {body.stats.last_tick_age_seconds !== null &&
                body.stats.last_tick_age_seconds !== undefined ? (
                  <span className="dim"> ({body.stats.last_tick_age_seconds.toFixed(1)}s ago)</span>
                ) : null}
              </dd>
              <dt>Last publish</dt>
              <dd><code>{shortTime(body.stats.last_publish_at)}</code></dd>
              <dt>Runloop</dt>
              <dd>{body.runloop_enabled ? "enabled" : <span className="warn-inline">disabled</span>}</dd>
              {body.rl_mode_env ? (
                <>
                  <dt>rl_mode env</dt>
                  <dd><code>{body.rl_mode_env}</code></dd>
                </>
              ) : null}
            </dl>
          ) : (
            <p className="dim">Awaiting first cycle…</p>
          )}
        </section>

        {/* ── policy snapshot ──────────────────────────────────────── */}
        <section className="card">
          <h3 className="card-h">Policy snapshot</h3>
          {policy && Object.keys(policy).length > 0 ? (
            <pre className="json">{JSON.stringify(policy, null, 2)}</pre>
          ) : (
            <p className="dim">No policy snapshot yet.</p>
          )}
        </section>

        {/* ── last output (full width) ──────────────────────────── */}
        <section className="card wide">
          <h3 className="card-h">Last cycle output</h3>
          {lastOutput !== null && lastOutput !== undefined ? (
            <pre className="json">{JSON.stringify(lastOutput, null, 2)}</pre>
          ) : (
            <p className="dim">No output yet.</p>
          )}
        </section>

        {/* ── channel feed ───────────────────────────────────────── */}
        <section className="card wide">
          <h3 className="card-h">
            Activity on <code>{meta.channel}</code>{" "}
            <span className="dim">· last {Math.min(channelEvents.length, FEED_MAX)} events</span>
          </h3>
          {channelEvents.length === 0 ? (
            <p className="dim">No events on this channel yet.</p>
          ) : (
            <ul className="feed">
              {channelEvents.slice(0, FEED_MAX).map((ev, i) => (
                <li key={i} className="feed-row">
                  <span className="feed-time">{timeOfDay(ev.envelope?.timestamp)}</span>
                  <span className="feed-source dim">{ev.envelope?.source}</span>
                  <code className="feed-payload">{JSON.stringify(ev.payload)}</code>
                </li>
              ))}
            </ul>
          )}
        </section>

        {/* ── embedded Grafana panels (v1.0.7l, #131 Phase 3) ────── */}
        <section className="card wide">
          <h3 className="card-h">
            Live charts{" "}
            <span className="dim">
              · from <code>{meta.dashUid}</code> · last 30 min · refresh 10 s
            </span>
          </h3>
          <div className="grafana-embeds">
            {meta.panels.map((p) => (
              <figure key={p.id} className="grafana-embed">
                <figcaption>
                  <span>{p.title}</span>
                  <a
                    href={`${dashUrl(meta.dashUid, meta.dashSlug)}&viewPanel=${p.id}`}
                    target="_blank"
                    rel="noreferrer"
                    className="dim"
                    title="Open this panel in Grafana"
                  >
                    <ExternalLink size={11} />
                  </a>
                </figcaption>
                <iframe
                  src={soloPanelUrl(meta.dashUid, meta.dashSlug, p.id)}
                  title={`${meta.title} — ${p.title}`}
                  loading="lazy"
                  frameBorder={0}
                />
              </figure>
            ))}
          </div>
        </section>
      </div>
    </div>
  );
}
