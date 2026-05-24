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

function shortTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  return iso.replace("T", " ").replace(/\.\d+.*/, "").replace(/\+.*$/, "");
}

function ageString(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return "—";
  if (seconds < 60) return `${seconds.toFixed(1)}s ago`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m ago`;
  return `${(seconds / 3600).toFixed(1)}h ago`;
}

function statusOf(svc: EngineStateBody): "ok" | "warn" | "bad" {
  if (!svc.reachable) return "bad";
  if (!svc.runloop_enabled) return "warn";
  if (svc.engine && !svc.engine.ready) return "warn";
  return "ok";
}

function summariseLastOutput(name: string, last: unknown): string {
  if (last === null || last === undefined) return "no output yet";
  if (name === "anomaly-detector" && Array.isArray(last)) {
    const arr = last as Array<{ backend_id?: string; status?: string; score?: number }>;
    if (arr.length === 0) return "no backends in last cycle";
    const nonHealthy = arr.filter((s) => s.status && s.status !== "healthy");
    if (nonHealthy.length === 0) {
      return `${arr.length} backend(s), all healthy`;
    }
    return nonHealthy
      .map((s) => `${s.backend_id ?? "?"}=${s.status}@${(s.score ?? 0).toFixed(2)}`)
      .join(" · ");
  }
  if (name === "forecasting" && typeof last === "object") {
    const f = last as { predicted_rps?: number; confidence_lower?: number; confidence_upper?: number; horizon_minutes?: number };
    if (f.predicted_rps === undefined) return JSON.stringify(last);
    return `${f.predicted_rps.toFixed(1)} rps in ${f.horizon_minutes ?? "?"}m (${(f.confidence_lower ?? 0).toFixed(1)}–${(f.confidence_upper ?? 0).toFixed(1)})`;
  }
  if (name === "rl-engine" && typeof last === "object") {
    const a = last as { mode?: string; server_rankings?: Array<{ backend_id?: string; score?: number }> };
    const top = (a.server_rankings ?? [])
      .slice()
      .sort((x, y) => (y.score ?? 0) - (x.score ?? 0))
      .slice(0, 3)
      .map((r) => `${r.backend_id ?? "?"}@${(r.score ?? 0).toFixed(2)}`)
      .join(", ");
    return `${a.mode ?? "?"} · ${top || "no rankings"}`;
  }
  return JSON.stringify(last);
}

function summariseEvent(ev: EngineStreamEvent): string {
  const p = ev.payload as Record<string, unknown>;
  if (ev.channel === "smartload.anomaly") {
    return `${p.backend_id ?? "?"} ${p.status ?? "?"} score=${(p.score as number ?? 0).toFixed(2)}`;
  }
  if (ev.channel === "smartload.forecast") {
    return `predicted ${(p.predicted_rps as number ?? 0).toFixed(1)} rps in ${p.horizon_minutes ?? "?"}m`;
  }
  if (ev.channel === "smartload.routing") {
    const rankings = (p.server_rankings as Array<{ backend_id?: string; score?: number }> | undefined) ?? [];
    return `${p.mode ?? "?"} (${rankings.length} backends)`;
  }
  if (ev.channel === "smartload.scale") {
    return `${p.action ?? "?"} → ${p.instance_count ?? "?"} backends · ${p.reason ?? ""}`;
  }
  return JSON.stringify(p);
}

function EngineCard({ name, body }: { name: string; body: EngineStateBody | undefined }) {
  if (!body) {
    return (
      <div className="card">
        <h3>{name}</h3>
        <div className="meta">no snapshot yet</div>
      </div>
    );
  }
  if (!body.reachable) {
    return (
      <div className="card">
        <h3>{name}</h3>
        <div className="meta" style={{ color: "var(--bad)" }}>
          unreachable · {body.error ?? "no /engine/state response"}
        </div>
      </div>
    );
  }

  const status = statusOf(body);
  const eng = body.engine!;
  const stats = body.stats!;
  const policySnap = body.policy_snapshot as Record<string, unknown> | undefined;
  const safeMode = Boolean(policySnap?.safe_mode);
  const fallback = eng.requested !== eng.loaded;

  return (
    <div className="card">
      <h3>
        {name}{" "}
        <span className={`badge-action ${status === "ok" ? "scale_out" : status === "warn" ? "scale_in" : "scale_in"}`} style={{ marginLeft: 8 }}>
          {status}
        </span>
      </h3>
      <div className="meta">
        <code>{body.channel}</code> · runloop {body.runloop_enabled ? "enabled" : "disabled"}
        {body.rl_mode_env ? <> · rl_mode_env=<code>{body.rl_mode_env}</code></> : null}
        {safeMode ? <> · <strong style={{ color: "var(--bad)" }}>safe_mode</strong></> : null}
      </div>

      <table style={{ marginTop: 8 }}>
        <tbody>
          <tr>
            <td style={{ width: 160 }}>{eng.kind === "policy" ? "Policy" : "Engine"}</td>
            <td>
              <code>{eng.loaded}</code>
              {fallback ? <> · fell back from <code>{eng.requested}</code> ({eng.error ?? "unknown error"})</> : null}
              {!eng.ready ? <> · <strong>not ready</strong></> : null}
            </td>
          </tr>
          <tr>
            <td>Policy version</td>
            <td>{(policySnap?.policy_version as number) ?? "—"}</td>
          </tr>
          <tr>
            <td>Ticks / publishes</td>
            <td>{stats.ticks_total} / {stats.publishes_total}</td>
          </tr>
          <tr>
            <td>Last tick</td>
            <td>
              <code>{shortTime(stats.last_tick_at)}</code> · {ageString(stats.last_tick_age_seconds)}
            </td>
          </tr>
          <tr>
            <td>Last publish</td>
            <td><code>{shortTime(stats.last_publish_at)}</code></td>
          </tr>
          <tr>
            <td>Last output</td>
            <td>{summariseLastOutput(name, body.last_output)}</td>
          </tr>
        </tbody>
      </table>
    </div>
  );
}

export default function LiveEnginesPage() {
  const [snapshot, setSnapshot] = useState<EnginesSnapshot | null>(null);
  const [snapshotError, setSnapshotError] = useState<string | null>(null);
  const [feed, setFeed] = useState<EngineStreamEvent[]>([]);
  const [streamState, setStreamState] = useState<"connecting" | "open" | "closed" | "error">("connecting");
  const sourceRef = useRef<EventSource | null>(null);

  // Snapshot poll: per-engine cards refresh from /api/ui/engines/snapshot.
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

  // SSE: live event feed. EventSource handles reconnect on its own.
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
        // ignore malformed frames — the BFF only emits JSON
      }
    };
    return () => {
      src.close();
      sourceRef.current = null;
      setStreamState("closed");
    };
  }, []);

  // Newest-first for display.
  const displayedFeed = useMemo(() => feed.slice().reverse(), [feed]);

  return (
    <>
      <div className="card">
        <h2>Live engines</h2>
        <div className="meta">
          Per-engine cards refresh every {SNAPSHOT_REFRESH_MS / 1000}s · live feed via SSE ({streamState})
          {snapshotError ? <span style={{ color: "var(--bad)" }}> · snapshot: {snapshotError}</span> : null}
        </div>
      </div>

      <div className="engines-grid" style={{ display: "grid", gap: 16, gridTemplateColumns: "repeat(auto-fit, minmax(360px, 1fr))" }}>
        {ENGINE_ORDER.map((name) => (
          <EngineCard key={name} name={name} body={snapshot?.services[name]} />
        ))}
      </div>

      <div className="card">
        <h2>Event feed ({feed.length})</h2>
        <div className="meta">
          Newest-first · subscribed to <code>smartload.{`{anomaly,forecast,routing,scale}`}</code> · capped at {FEED_MAX} on the client.
        </div>
        <table>
          <thead>
            <tr>
              <th style={{ width: 170 }}>Time</th>
              <th style={{ width: 160 }}>Channel</th>
              <th style={{ width: 140 }}>Source</th>
              <th>Payload</th>
            </tr>
          </thead>
          <tbody>
            {displayedFeed.length === 0 ? (
              <tr><td colSpan={4} className="empty">Feed is empty — waiting for the engines to emit. Confirm runloop envs are enabled.</td></tr>
            ) : (
              displayedFeed.map((ev) => (
                <tr key={ev.envelope.event_id}>
                  <td><code>{shortTime(ev.envelope.timestamp)}</code></td>
                  <td><code>{ev.channel}</code></td>
                  <td>{ev.envelope.source}</td>
                  <td>{summariseEvent(ev)}</td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>
    </>
  );
}
