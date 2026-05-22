import { useEffect, useMemo, useState } from "react";

import {
  api,
  type IsolateStatus,
  type ManualIsolateResponse,
  type ManualScaleResponse,
  type Policy,
} from "../api";

type Pending =
  | { kind: "scale"; target_count: number; reason: string }
  | { kind: "isolate"; backend_id: string; status: IsolateStatus; reason: string };

type ResultCard =
  | { kind: "scale"; data: ManualScaleResponse }
  | { kind: "isolate"; data: ManualIsolateResponse };

const STATUSES: IsolateStatus[] = ["healthy", "degraded", "unhealthy"];

function shortId(s: string): string {
  return s ? s.slice(0, 8) : "—";
}

export default function ActionsPage() {
  // ── operator identity (placeholder until OUI.7) ─────────────────────────
  const [actor, setActor] = useState<string>("operator");

  // ── live policy snapshot (drives min/max bounds + current backend count) ─
  const [policy, setPolicy] = useState<Policy | null>(null);
  const [policyError, setPolicyError] = useState<string | null>(null);

  async function loadPolicy() {
    try {
      const p = await api.getPolicy();
      setPolicy(p);
      setPolicyError(null);
    } catch (err: any) {
      setPolicyError(err?.message || "could not load policy");
    }
  }
  useEffect(() => { loadPolicy(); }, []);

  // ── scale form ──────────────────────────────────────────────────────────
  const [scaleTarget, setScaleTarget] = useState<string>("");
  const [scaleReason, setScaleReason] = useState<string>("");

  // ── isolate form ────────────────────────────────────────────────────────
  const [backendId, setBackendId] = useState<string>("");
  const [isolateStatus, setIsolateStatus] = useState<IsolateStatus>("unhealthy");
  const [isolateReason, setIsolateReason] = useState<string>("");

  // ── confirmation modal + result feed ────────────────────────────────────
  const [pending, setPending] = useState<Pending | null>(null);
  const [busy, setBusy] = useState(false);
  const [results, setResults] = useState<ResultCard[]>([]);
  const [error, setError] = useState<string | null>(null);

  function recordResult(card: ResultCard) {
    setResults((prev) => [card, ...prev].slice(0, 10));
  }

  function flashError(msg: string) {
    setError(msg);
    setTimeout(() => setError(null), 6_000);
  }

  // ── scale handlers ──────────────────────────────────────────────────────
  const parsedTarget = useMemo(() => {
    const n = Number(scaleTarget);
    if (!scaleTarget || Number.isNaN(n)) return null;
    return Math.trunc(n);
  }, [scaleTarget]);

  function requestScale() {
    if (parsedTarget === null) {
      flashError("target_count must be an integer");
      return;
    }
    setPending({
      kind: "scale",
      target_count: parsedTarget,
      reason: scaleReason.trim(),
    });
  }

  // ── isolate handlers ────────────────────────────────────────────────────
  function requestIsolate() {
    if (!backendId.trim()) {
      flashError("backend_id is required");
      return;
    }
    setPending({
      kind: "isolate",
      backend_id: backendId.trim(),
      status: isolateStatus,
      reason: isolateReason.trim(),
    });
  }

  // ── modal: confirm / cancel ─────────────────────────────────────────────
  async function confirmPending() {
    if (!pending) return;
    setBusy(true);
    try {
      if (pending.kind === "scale") {
        const r = await api.scale(pending.target_count, actor, pending.reason || undefined);
        recordResult({ kind: "scale", data: r });
        setScaleTarget("");
        setScaleReason("");
      } else {
        const r = await api.isolate(pending.backend_id, pending.status, actor, pending.reason || undefined);
        recordResult({ kind: "isolate", data: r });
        setBackendId("");
        setIsolateReason("");
      }
      await loadPolicy();        // refresh bounds + version
    } catch (err: any) {
      const fieldHint = err?.field ? ` [field: ${err.field}]` : "";
      flashError(`${pending.kind} failed: ${err?.message || err}${fieldHint}`);
    } finally {
      setBusy(false);
      setPending(null);
    }
  }

  // ── render ──────────────────────────────────────────────────────────────
  return (
    <>
      <div className="card">
        <h2>Manual actions</h2>
        <div className="meta">
          Operator overrides. Every action writes an audit row prefixed
          <code> manual:&lt;actor&gt;: </code> so intent is grep-able on the{" "}
          <a href="/audit">Audit</a> page.{" "}
          {policy
            ? <>
                Live policy: <code>min_backends={policy.min_backends}</code>{" "}
                · <code>max_backends={policy.max_backends}</code>{" "}
                · <code>v{policy.policy_version}</code>
              </>
            : policyError
              ? <span style={{ color: "var(--bad)" }}>(could not load policy: {policyError})</span>
              : "loading policy…"}
        </div>

        <div className="form-grid" style={{ marginTop: 16 }}>
          <label>Actor</label>
          <input
            value={actor}
            onChange={(e) => setActor(e.target.value)}
            placeholder="e.g. on-call, oncall@team"
          />
        </div>
      </div>

      {/* ── scale ─────────────────────────────────────────────────────── */}
      <div className="card">
        <h2>Scale to N backends</h2>
        <div className="meta">
          Target must satisfy <code>min_backends &le; N &le; max_backends</code>.
          Bypasses cooldown — the auto-loop's debounce won't immediately undo
          this action, but it will resume normal forecast-driven behaviour
          afterwards.
        </div>
        <div className="form-grid" style={{ marginTop: 12 }}>
          <label>target_count</label>
          <input
            type="number"
            inputMode="numeric"
            value={scaleTarget}
            onChange={(e) => setScaleTarget(e.target.value)}
            placeholder={policy ? `${policy.min_backends}–${policy.max_backends}` : "—"}
            min={policy?.min_backends ?? 1}
            max={policy?.max_backends ?? 99}
          />
          <label>reason</label>
          <input
            value={scaleReason}
            onChange={(e) => setScaleReason(e.target.value)}
            placeholder="e.g. forecast inaccurate, oncall override"
          />
        </div>
        <div style={{ marginTop: 12 }}>
          <button onClick={requestScale} disabled={busy || parsedTarget === null}>
            Scale…
          </button>
        </div>
      </div>

      {/* ── isolate ───────────────────────────────────────────────────── */}
      <div className="card">
        <h2>Isolate backend</h2>
        <div className="meta">
          Publishes a synthetic <code>AnomalyEvent</code> and writes a{" "}
          <code>backend_health</code> row. The load-balancer sidecar (T2.1)
          will react when it lands; until then, the published envelope is
          observable on <code>smartload.anomaly</code> for any subscriber.
        </div>
        <div className="form-grid" style={{ marginTop: 12 }}>
          <label>backend_id</label>
          <input
            value={backendId}
            onChange={(e) => setBackendId(e.target.value)}
            placeholder="e.g. test-backend-3 or 172.18.0.5:8080"
          />
          <label>status</label>
          <select
            value={isolateStatus}
            onChange={(e) => setIsolateStatus(e.target.value as IsolateStatus)}
          >
            {STATUSES.map((s) => (
              <option key={s} value={s}>{s}</option>
            ))}
          </select>
          <label>reason</label>
          <input
            value={isolateReason}
            onChange={(e) => setIsolateReason(e.target.value)}
            placeholder="e.g. demo, failover drill"
          />
        </div>
        <div style={{ marginTop: 12 }}>
          <button onClick={requestIsolate} disabled={busy || !backendId.trim()}>
            Isolate…
          </button>
        </div>
      </div>

      {/* ── force route weights (disabled placeholder) ─────────────────── */}
      <div className="card">
        <h2>Force route weights <span className="muted small">(disabled)</span></h2>
        <div className="meta">
          Requires the LB sidecar (T2.1, <code>#82</code>) before the
          load-balancer reads operator-supplied weights. Form will light up
          when that lands.
        </div>
        <div style={{ marginTop: 12 }}>
          <button disabled title="Disabled until T2.1 sidecar lands">
            Force weights…
          </button>
        </div>
      </div>

      {/* ── result feed ────────────────────────────────────────────────── */}
      {results.length > 0 ? (
        <div className="card">
          <h2>Recent actions ({results.length})</h2>
          <div className="meta">
            Latest at top. Open the <a href="/audit">Audit</a> page to see
            them landed in the persistent audit stream.
          </div>
          <table>
            <thead>
              <tr>
                <th style={{ width: 110 }}>Kind</th>
                <th>Outcome</th>
                <th style={{ width: 110 }}>event_id</th>
              </tr>
            </thead>
            <tbody>
              {results.map((r, i) => {
                if (r.kind === "scale") {
                  return (
                    <tr key={`${r.data.event_id}-${i}`}>
                      <td><span className={`badge-action ${r.data.action}`}>{r.data.action}</span></td>
                      <td>
                        <code>{r.data.previous_count} → {r.data.final_count}</code>
                        {" "}<span className="muted">target={r.data.target_count}, status={r.data.status}</span>
                        <div className="muted small"><code>{r.data.reason}</code></div>
                      </td>
                      <td><code>{shortId(r.data.event_id)}</code></td>
                    </tr>
                  );
                }
                return (
                  <tr key={`${r.data.event_id}-${i}`}>
                    <td><span className="badge-action scale_in">isolate</span></td>
                    <td>
                      <code>{r.data.backend_id}</code>{" "}
                      → <code>{r.data.anomaly_status}</code>{" "}
                      <span className="muted">score={r.data.score}</span>
                      <div className="muted small"><code>{r.data.reason}</code></div>
                    </td>
                    <td><code>{shortId(r.data.event_id)}</code></td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      ) : null}

      {/* ── confirmation modal ─────────────────────────────────────────── */}
      {pending ? (
        <div className="modal-backdrop" onClick={() => !busy && setPending(null)}>
          <div className="modal" onClick={(e) => e.stopPropagation()}>
            <h3>
              {pending.kind === "scale"
                ? `Scale to ${pending.target_count} backends?`
                : `Mark ${pending.backend_id} as ${pending.status}?`}
            </h3>
            <p className="meta">
              {pending.kind === "scale" ? (
                <>
                  This will adjust the running backend count to{" "}
                  <code>{pending.target_count}</code> immediately. The
                  cooldown timer is bypassed. An audit row will be written
                  with actor=<code>{actor}</code>.
                </>
              ) : (
                <>
                  This will publish a synthetic <code>AnomalyEvent</code>{" "}
                  for <code>{pending.backend_id}</code> with status{" "}
                  <code>{pending.status}</code>. A <code>backend_health</code>{" "}
                  row will be written with actor=<code>{actor}</code>.
                </>
              )}
            </p>
            {pending.reason ? (
              <p className="meta">
                Reason: <code>{pending.reason}</code>
              </p>
            ) : null}
            <div style={{ display: "flex", gap: 8, marginTop: 16 }}>
              <button onClick={confirmPending} disabled={busy}>
                {busy ? "applying…" : "Continue"}
              </button>
              <button
                className="secondary"
                onClick={() => setPending(null)}
                disabled={busy}
              >
                Cancel
              </button>
            </div>
          </div>
        </div>
      ) : null}

      {error ? <div className="toast bad">{error}</div> : null}
    </>
  );
}
