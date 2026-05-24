import { useEffect, useMemo, useRef, useState } from "react";

import {
  ENGINES_STREAM_URL,
  api,
  type EngineStateBody,
  type EngineStreamEvent,
  type EnginesSnapshot,
} from "../api";

const SNAPSHOT_REFRESH_MS = 5_000;
const FEED_MAX = 200;
const ENGINE_ORDER = ["anomaly-detector", "forecasting", "rl-engine"] as const;
const CHANNEL_FILTERS = [
  { id: "all",                 label: "All" },
  { id: "smartload.anomaly",   label: "anomaly" },
  { id: "smartload.forecast",  label: "forecast" },
  { id: "smartload.routing",   label: "routing" },
  { id: "smartload.scale",     label: "scale" },
] as const;
type ChannelFilter = (typeof CHANNEL_FILTERS)[number]["id"];

// ── formatting helpers ─────────────────────────────────────────────────────

function shortTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  return iso.replace("T", " ").replace(/\.\d+.*/, "").replace(/\+.*$/, "");
}

function timeOfDay(iso: string | null | undefined): string {
  if (!iso) return "—";
  const m = iso.match(/T(\d{2}:\d{2}:\d{2})/);
  return m ? m[1] : iso;
}

function channelClass(channel: string): string {
  if (channel === "smartload.anomaly")  return "ch-anomaly";
  if (channel === "smartload.forecast") return "ch-forecast";
  if (channel === "smartload.routing")  return "ch-routing";
  if (channel === "smartload.scale")    return "ch-scale";
  return "";
}

function channelShort(channel: string): string {
  return channel.replace(/^smartload\./, "");
}

// ── status derivation ──────────────────────────────────────────────────────

type Status = "ok" | "warn" | "bad";

function statusOf(svc: EngineStateBody | undefined): Status {
  if (!svc || !svc.reachable) return "bad";
  if (!svc.runloop_enabled) return "warn";
  if (svc.engine && !svc.engine.ready) return "warn";
  return "ok";
}

// ── headline content per service ───────────────────────────────────────────

interface Headline {
  label: string;
  value: React.ReactNode;
  empty: boolean;
}

function headlineFor(name: string, svc: EngineStateBody): Headline {
  if (!svc.reachable) {
    return { label: "Status", value: <span className="headline-empty">unreachable · {svc.error ?? "no /engine/state response"}</span>, empty: true };
  }
  if (!svc.runloop_enabled) {
    return { label: "Status", value: <span className="headline-empty">runloop disabled — set the env flag to start emitting</span>, empty: true };
  }
  const last = svc.last_output;
  if (last === null || last === undefined) {
    return { label: "Awaiting first cycle", value: <span className="headline-empty">no output yet</span>, empty: true };
  }

  if (name === "anomaly-detector" && Array.isArray(last)) {
    const arr = last as Array<{ backend_id?: string; status?: string; score?: number }>;
    if (arr.length === 0) {
      return { label: "Last cycle", value: <span className="headline-empty">no backends in window</span>, empty: true };
    }
    const nonHealthy = arr.filter((s) => s.status && s.status !== "healthy");
    if (nonHealthy.length === 0) {
      return { label: "Last cycle", value: <>{arr.length} backend(s) · all healthy</>, empty: false };
    }
    return {
      label: "Last cycle",
      value: (
        <>
          ⚠ {nonHealthy.length} of {arr.length} unhealthy ·{" "}
          {nonHealthy.slice(0, 3).map((s, i) => (
            <span key={i}>
              {i > 0 ? "  " : ""}
              <code>{s.backend_id ?? "?"}</code> <span style={{ color: "var(--bad)" }}>{s.status}</span> @{(s.score ?? 0).toFixed(2)}
            </span>
          ))}
          {nonHealthy.length > 3 ? <span style={{ color: "var(--muted)" }}> · +{nonHealthy.length - 3} more</span> : null}
        </>
      ),
      empty: false,
    };
  }

  if (name === "forecasting" && typeof last === "object") {
    const f = last as { predicted_rps?: number; confidence_lower?: number; confidence_upper?: number; horizon_minutes?: number };
    if (f.predicted_rps === undefined) {
      return { label: "Last forecast", value: <span className="headline-empty">malformed payload</span>, empty: true };
    }
    return {
      label: "Last forecast",
      value: (
        <>
          <strong>{f.predicted_rps.toFixed(1)} rps</strong> in {f.horizon_minutes ?? "?"}m
          <span style={{ color: "var(--muted)" }}> · band {(f.confidence_lower ?? 0).toFixed(1)} – {(f.confidence_upper ?? 0).toFixed(1)}</span>
        </>
      ),
      empty: false,
    };
  }

  if (name === "rl-engine" && typeof last === "object") {
    const a = last as { mode?: string; server_rankings?: Array<{ backend_id?: string; score?: number }> };
    const top = (a.server_rankings ?? [])
      .slice()
      .sort((x, y) => (y.score ?? 0) - (x.score ?? 0))
      .slice(0, 3);
    return {
      label: "Last recommendation",
      value: (
        <>
          mode <strong>{a.mode ?? "?"}</strong>
          {top.length > 0 ? (
            <span style={{ color: "var(--muted)" }}>
              {" · top "}
              {top.map((r, i) => (
                <span key={i}>
                  {i > 0 ? ", " : ""}
                  <code>{r.backend_id ?? "?"}</code>@{(r.score ?? 0).toFixed(2)}
                </span>
              ))}
            </span>
          ) : null}
        </>
      ),
      empty: false,
    };
  }

  return { label: "Last output", value: <code>{JSON.stringify(last)}</code>, empty: false };
}

// ── event summary per channel ──────────────────────────────────────────────

function summariseEvent(ev: EngineStreamEvent): string {
  const p = ev.payload as Record<string, unknown>;
  if (ev.channel === "smartload.anomaly") {
    const score = (p.score as number | undefined) ?? 0;
    return `${p.backend_id ?? "?"} → ${p.status ?? "?"} (score ${score.toFixed(2)})`;
  }
  if (ev.channel === "smartload.forecast") {
    const rps = (p.predicted_rps as number | undefined) ?? 0;
    return `${rps.toFixed(1)} rps in ${p.horizon_minutes ?? "?"}m`;
  }
  if (ev.channel === "smartload.routing") {
    const rankings = (p.server_rankings as Array<unknown> | undefined) ?? [];
    return `mode ${p.mode ?? "?"} · ${rankings.length} ranked`;
  }
  if (ev.channel === "smartload.scale") {
    return `${p.action ?? "?"} → ${p.instance_count ?? "?"} backends${p.reason ? " · " + p.reason : ""}`;
  }
  return JSON.stringify(p);
}

// ── engine tile ────────────────────────────────────────────────────────────

function EngineTile({
  name,
  body,
  onOpenDetails,
}: {
  name: string;
  body: EngineStateBody | undefined;
  onOpenDetails: () => void;
}) {
  const status = statusOf(body);

  if (!body) {
    return (
      <div className={`engine-tile status-warn`}>
        <div className="tile-head">
          <div className="tile-name">{name}</div>
          <div className="tile-status">loading</div>
        </div>
        <div className="headline">
          <span className="headline-empty">awaiting first snapshot…</span>
        </div>
      </div>
    );
  }

  const headline = headlineFor(name, body);
  const eng = body.engine;
  const policy = body.policy_snapshot as Record<string, unknown> | undefined;
  const safeMode = Boolean(policy?.safe_mode);
  const fallback = Boolean(eng && eng.requested !== eng.loaded);
  const notReady = Boolean(eng && !eng.ready);

  return (
    <div className={`engine-tile status-${status}`}>
      <div className="tile-head">
        <div className="tile-name">{name}</div>
        <div className="tile-status">{status === "ok" ? "healthy" : status === "warn" ? "degraded" : "unreachable"}</div>
      </div>

      <div className="headline">
        <span className="headline-label">{headline.label}</span>
        {headline.value}
      </div>

      <div className="secondary">
        {eng ? <>{eng.kind === "policy" ? "Policy" : "Engine"}: <code>{eng.loaded}</code></> : null}
        {body.channel ? <> · channel <code>{body.channel}</code></> : null}
        {body.rl_mode_env ? <> · rl_mode_env <code>{body.rl_mode_env}</code></> : null}
      </div>

      <div className="tile-footer">
        <div className="tile-chips">
          {safeMode ? <span className="tile-chip bad">safe_mode</span> : null}
          {fallback ? <span className="tile-chip warn">fallback ← {eng?.requested}</span> : null}
          {notReady && !fallback ? <span className="tile-chip warn">not ready</span> : null}
        </div>
        <button className="tile-details-button" onClick={onOpenDetails}>Details ▸</button>
      </div>
    </div>
  );
}

// ── details drawer ─────────────────────────────────────────────────────────

function DetailsDrawer({
  name,
  body,
  onClose,
}: {
  name: string;
  body: EngineStateBody;
  onClose: () => void;
}) {
  const stats = body.stats;
  const policy = body.policy_snapshot as Record<string, unknown> | undefined;
  return (
    <div className="drawer-backdrop" onClick={onClose}>
      <div className="drawer" onClick={(e) => e.stopPropagation()}>
        <button className="drawer-close" onClick={onClose}>Close</button>
        <h3>{name}</h3>
        <p className="meta">
          {body.reachable ? "Reachable" : "Unreachable"}
          {body.channel ? <> · <code>{body.channel}</code></> : null}
        </p>

        <dl>
          {body.engine ? (
            <>
              <dt>{body.engine.kind === "policy" ? "Policy loaded" : "Engine loaded"}</dt>
              <dd><code>{body.engine.loaded}</code> {body.engine.requested !== body.engine.loaded ? <span style={{ color: "var(--warn)" }}>(fallback from <code>{body.engine.requested}</code>)</span> : null}</dd>
              <dt>Ready</dt>
              <dd>{String(body.engine.ready)}{body.engine.error ? <> — <span style={{ color: "var(--bad)" }}>{body.engine.error}</span></> : null}</dd>
            </>
          ) : null}
          <dt>Runloop</dt>
          <dd>{body.runloop_enabled ? "enabled" : <span style={{ color: "var(--warn)" }}>disabled</span>}</dd>
          {body.rl_mode_env ? (<><dt>rl_mode env</dt><dd><code>{body.rl_mode_env}</code></dd></>) : null}
          {stats ? (
            <>
              <dt>Ticks total</dt><dd>{stats.ticks_total}</dd>
              <dt>Publishes total</dt><dd>{stats.publishes_total}</dd>
              <dt>Last tick</dt><dd><code>{shortTime(stats.last_tick_at)}</code> {stats.last_tick_age_seconds !== null && stats.last_tick_age_seconds !== undefined ? `(${stats.last_tick_age_seconds.toFixed(1)}s ago)` : ""}</dd>
              <dt>Last publish</dt><dd><code>{shortTime(stats.last_publish_at)}</code></dd>
            </>
          ) : null}
        </dl>

        {policy ? (
          <>
            <h3 style={{ fontSize: 13, color: "var(--muted)", textTransform: "uppercase", letterSpacing: "0.6px" }}>Policy snapshot</h3>
            <pre>{JSON.stringify(policy, null, 2)}</pre>
          </>
        ) : null}

        {body.last_output !== null && body.last_output !== undefined ? (
          <>
            <h3 style={{ fontSize: 13, color: "var(--muted)", textTransform: "uppercase", letterSpacing: "0.6px" }}>Last output</h3>
            <pre>{JSON.stringify(body.last_output, null, 2)}</pre>
          </>
        ) : null}
      </div>
    </div>
  );
}

// ── page ───────────────────────────────────────────────────────────────────

export default function LiveEnginesPage() {
  const [snapshot, setSnapshot] = useState<EnginesSnapshot | null>(null);
  const [snapshotError, setSnapshotError] = useState<string | null>(null);
  const [feed, setFeed] = useState<EngineStreamEvent[]>([]);
  const [streamState, setStreamState] = useState<"connecting" | "open" | "closed" | "error">("connecting");
  const [filter, setFilter] = useState<ChannelFilter>("all");
  const [drawer, setDrawer] = useState<string | null>(null);
  const sourceRef = useRef<EventSource | null>(null);

  // Snapshot poll.
  useEffect(() => {
    let cancelled = false;
    async function tick() {
      try {
        const s = await api.enginesSnapshot();
        if (!cancelled) {
          setSnapshot(s);
          setSnapshotError(null);
        }
      } catch (err: any) {
        if (!cancelled) setSnapshotError(err?.message || "snapshot fetch failed");
      }
    }
    tick();
    const id = setInterval(tick, SNAPSHOT_REFRESH_MS);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, []);

  // SSE.
  useEffect(() => {
    const src = new EventSource(ENGINES_STREAM_URL);
    sourceRef.current = src;
    setStreamState("connecting");
    src.onopen = () => setStreamState("open");
    src.onerror = () => setStreamState("error");
    src.onmessage = (ev) => {
      try {
        const parsed = JSON.parse(ev.data) as EngineStreamEvent;
        setFeed((prev) => {
          const next = [...prev, parsed];
          return next.length > FEED_MAX ? next.slice(next.length - FEED_MAX) : next;
        });
      } catch {
        /* ignore malformed frames */
      }
    };
    return () => {
      src.close();
      sourceRef.current = null;
      setStreamState("closed");
    };
  }, []);

  const filteredFeed = useMemo(() => {
    const ordered = feed.slice().reverse();             // newest first
    if (filter === "all") return ordered;
    return ordered.filter((e) => e.channel === filter);
  }, [feed, filter]);

  const drawerBody = drawer ? snapshot?.services[drawer] : null;

  return (
    <>
      <div className="engines-page-header">
        <div>
          <h2>Live engines</h2>
          <p className="meta">
            Each tile refreshes every {SNAPSHOT_REFRESH_MS / 1000}s · activity feed live via SSE
            {snapshotError ? <span style={{ color: "var(--bad)" }}> · snapshot: {snapshotError}</span> : null}
          </p>
        </div>
        <div className={`feed-stream-state ${streamState}`}>
          {streamState === "open" ? "● live" : streamState === "connecting" ? "○ connecting…" : streamState === "error" ? "✕ stream error" : "○ idle"}
        </div>
      </div>

      <div className="engines-layout">
        <div className="engine-tiles">
          {ENGINE_ORDER.map((name) => (
            <EngineTile
              key={name}
              name={name}
              body={snapshot?.services[name]}
              onOpenDetails={() => setDrawer(name)}
            />
          ))}
        </div>

        <aside className="feed-panel">
          <div className="feed-head">
            <h3>Activity · {filteredFeed.length}{filter !== "all" ? ` of ${feed.length}` : ""}</h3>
          </div>
          <div className="feed-chips">
            {CHANNEL_FILTERS.map((c) => (
              <button
                key={c.id}
                className={`chip ${filter === c.id ? "on" : ""}`}
                onClick={() => setFilter(c.id)}
              >
                {c.label}
              </button>
            ))}
          </div>

          <div className="feed-stream">
            {filteredFeed.length === 0 ? (
              <div className="feed-empty">
                No events yet for this filter. Confirm the runloop env flags are <code>true</code> for the engines you expect to emit.
              </div>
            ) : (
              filteredFeed.map((ev) => (
                <div key={ev.envelope.event_id} className={`feed-event ${channelClass(ev.channel)}`}>
                  <div className="feed-meta">
                    <span className="feed-channel">{channelShort(ev.channel)}</span>
                    <span>{timeOfDay(ev.envelope.timestamp)}</span>
                  </div>
                  <div className="feed-summary">{summariseEvent(ev)}</div>
                </div>
              ))
            )}
          </div>
        </aside>
      </div>

      {drawer && drawerBody ? (
        <DetailsDrawer name={drawer} body={drawerBody} onClose={() => setDrawer(null)} />
      ) : null}
    </>
  );
}
