import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import {
  ArrowLeftRight,
  ArrowRight,
  Ban,
  Minus,
  Plus,
  RefreshCw,
  ScrollText,
  ShieldAlert,
  TrendingUp,
} from "lucide-react";

import {
  api,
  type IsolateStatus,
  type ManualIsolateResponse,
  type ManualScaleResponse,
  type Policy,
} from "../api";

type Pending =
  | { kind: "scale";   target_count: number; reason: string }
  | { kind: "isolate"; backend_id: string; status: IsolateStatus; reason: string }
  | { kind: "safe-mode"; enable: boolean; reason: string };

type ResultCard =
  | { kind: "scale"; data: ManualScaleResponse }
  | { kind: "isolate"; data: ManualIsolateResponse }
  | { kind: "safe-mode"; data: { enable: boolean; policy_version: number } };

const STATUSES: IsolateStatus[] = ["healthy", "degraded", "unhealthy"];

function shortId(s: string): string {
  return s ? s.slice(0, 8) : "—";
}

export default function ActionsPage() {
  const [actor, setActor] = useState<string>("operator");

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

  const [scaleTarget, setScaleTarget] = useState<string>("");
  const [scaleReason, setScaleReason] = useState<string>("");

  const [backendId, setBackendId] = useState<string>("");
  const [isolateStatus, setIsolateStatus] = useState<IsolateStatus>("unhealthy");
  const [isolateReason, setIsolateReason] = useState<string>("");

  const [weightsBuffer, setWeightsBuffer] = useState<string>(`{\n  "backend-1": 0.5,\n  "backend-2": 0.3,\n  "backend-3": 0.2\n}`);

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
    setPending({ kind: "scale", target_count: parsedTarget, reason: scaleReason.trim() });
  }

  function quickScale(delta: number) {
    const cur = policy?.min_backends ?? 1;
    const target = Math.max(policy?.min_backends ?? 1, Math.min(policy?.max_backends ?? 99, cur + delta));
    setPending({ kind: "scale", target_count: target, reason: delta > 0 ? "quick scale up" : "quick scale down" });
  }

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

  function requestToggleSafeMode() {
    if (!policy) return;
    setPending({ kind: "safe-mode", enable: !policy.safe_mode, reason: "manual toggle from Actions page" });
  }

  async function confirmPending() {
    if (!pending) return;
    setBusy(true);
    try {
      if (pending.kind === "scale") {
        const r = await api.scale(pending.target_count, actor, pending.reason || undefined);
        recordResult({ kind: "scale", data: r });
        setScaleTarget("");
        setScaleReason("");
      } else if (pending.kind === "isolate") {
        const r = await api.isolate(pending.backend_id, pending.status, actor, pending.reason || undefined);
        recordResult({ kind: "isolate", data: r });
        setBackendId("");
        setIsolateReason("");
      } else if (pending.kind === "safe-mode") {
        const r = await api.setPolicy({ safe_mode: pending.enable }, actor);
        recordResult({ kind: "safe-mode", data: { enable: pending.enable, policy_version: r.policy_version } });
      }
      await loadPolicy();
    } catch (err: any) {
      const fieldHint = err?.field ? ` [field: ${err.field}]` : "";
      flashError(`${pending.kind} failed: ${err?.message || err}${fieldHint}`);
    } finally {
      setBusy(false);
      setPending(null);
    }
  }

  return (
    <>
      <div className="page-header">
        <div>
          <h2>Actions</h2>
          <div className="subtitle">
            Execute manual operations and control SmartLoad behaviour in real time
          </div>
        </div>
        <div className="header-actions">
          <Link to="/audit" className="refresh-chip"><ScrollText size={12} /> Audit log</Link>
        </div>
      </div>

      {/* ── Quick actions ─────────────────────────────────────────── */}
      <div className="card">
        <div className="card-head">
          <h2>Quick actions</h2>
          <span className="meta">One-tap shortcuts for common operator flows</span>
        </div>
        <div className="quick-actions">
          <button className="qa-tile" onClick={loadPolicy} disabled={busy}>
            <span className="qa-icon"><RefreshCw size={16} /></span>
            <span>Refresh state</span>
            <span className="qa-sub">Reload policy &amp; bounds</span>
          </button>
          <button className="qa-tile green" onClick={() => quickScale(+1)} disabled={busy || !policy}>
            <span className="qa-icon"><Plus size={16} /></span>
            <span>Scale up</span>
            <span className="qa-sub">Add one backend</span>
          </button>
          <button className="qa-tile amber" onClick={() => quickScale(-1)} disabled={busy || !policy}>
            <span className="qa-icon"><Minus size={16} /></span>
            <span>Scale down</span>
            <span className="qa-sub">Remove one backend</span>
          </button>
          <button
            className={`qa-tile ${policy?.safe_mode ? "violet" : "bad"}`}
            onClick={requestToggleSafeMode}
            disabled={busy || !policy}
          >
            <span className="qa-icon"><ShieldAlert size={16} /></span>
            <span>{policy?.safe_mode ? "Disable safe mode" : "Enable safe mode"}</span>
            <span className="qa-sub">Fail-safe routing override</span>
          </button>
        </div>
      </div>

      {/* ── Manual actions ─────────────────────────────────────────── */}
      <div className="card">
        <div className="card-head">
          <h2>Manual actions</h2>
          <span className="meta">
            {policy
              ? <>Live policy: <code>min={policy.min_backends}</code> · <code>max={policy.max_backends}</code> · <code>v{policy.policy_version}</code></>
              : policyError
                ? <span style={{ color: "var(--bad)" }}>(could not load policy: {policyError})</span>
                : "loading policy…"}
          </span>
        </div>

        <div className="form-grid" style={{ marginTop: 4, gridTemplateColumns: "80px 1fr" }}>
          <label>Actor</label>
          <input
            value={actor}
            onChange={(e) => setActor(e.target.value)}
            placeholder="e.g. on-call, oncall@team"
          />
        </div>
      </div>

      {/* Scale */}
      <div className="manual-action scale">
        <div className="ma-head">
          <span className="ma-icon"><TrendingUp size={14} /></span>
          <div>
            <h3>Scale to N backends</h3>
            <div className="ma-sub">Direct backend count override · bypasses cooldown · forecast resumes after</div>
          </div>
        </div>
        <div className="ma-body">
          <div className="field">
            <label>Target count</label>
            <input
              type="number"
              inputMode="numeric"
              value={scaleTarget}
              onChange={(e) => setScaleTarget(e.target.value)}
              placeholder={policy ? `${policy.min_backends}–${policy.max_backends}` : "—"}
              min={policy?.min_backends ?? 1}
              max={policy?.max_backends ?? 99}
            />
          </div>
          <div className="field" style={{ flex: 2 }}>
            <label>Reason (optional)</label>
            <input
              value={scaleReason}
              onChange={(e) => setScaleReason(e.target.value)}
              placeholder="e.g. traffic spike, oncall override"
            />
          </div>
          <button onClick={requestScale} disabled={busy || parsedTarget === null}>Scale…</button>
        </div>
      </div>

      {/* Isolate */}
      <div className="manual-action isolate">
        <div className="ma-head">
          <span className="ma-icon"><Ban size={14} /></span>
          <div>
            <h3>Isolate backend</h3>
            <div className="ma-sub">Temporarily remove a backend by publishing a synthetic AnomalyEvent</div>
          </div>
        </div>
        <div className="ma-body">
          <div className="field">
            <label>Backend ID</label>
            <input
              value={backendId}
              onChange={(e) => setBackendId(e.target.value)}
              placeholder="e.g. test-backend-3 or 172.18.0.5:8080"
            />
          </div>
          <div className="field" style={{ flex: 0.7 }}>
            <label>Status</label>
            <select
              value={isolateStatus}
              onChange={(e) => setIsolateStatus(e.target.value as IsolateStatus)}
            >
              {STATUSES.map((s) => (<option key={s} value={s}>{s}</option>))}
            </select>
          </div>
          <div className="field">
            <label>Reason (optional)</label>
            <input
              value={isolateReason}
              onChange={(e) => setIsolateReason(e.target.value)}
              placeholder="e.g. demo, failover drill"
            />
          </div>
          <button className="danger" onClick={requestIsolate} disabled={busy || !backendId.trim()}>Isolate</button>
        </div>
      </div>

      {/* Force weights (disabled until LB sidecar lands) */}
      <div className="manual-action weights">
        <div className="ma-head">
          <span className="ma-icon"><ArrowLeftRight size={14} /></span>
          <div>
            <h3>Force route weights <span className="muted small">(disabled)</span></h3>
            <div className="ma-sub">Override routing by setting per-backend weights · awaiting LB sidecar (T2.1)</div>
          </div>
        </div>
        <div className="ma-body" style={{ alignItems: "flex-start" }}>
          <div className="field" style={{ flex: 2 }}>
            <label>Backend weights JSON</label>
            <textarea
              value={weightsBuffer}
              onChange={(e) => setWeightsBuffer(e.target.value)}
              style={{ minHeight: 110 }}
              disabled
            />
          </div>
          <div className="field" style={{ flex: 1 }}>
            <label>Example</label>
            <div className="weights-example">
              <div className="ex-label">Equal split:</div>
              <pre style={{ background: "transparent", border: 0, padding: 0, margin: 0 }}>{`{
  "backend-1": 0.34,
  "backend-2": 0.33,
  "backend-3": 0.33
}`}</pre>
            </div>
          </div>
          <button className="violet" disabled title="Disabled until T2.1 sidecar lands">Apply weights</button>
        </div>
      </div>

      {/* ── Result feed ───────────────────────────────────────────── */}
      {results.length > 0 ? (
        <div className="card">
          <div className="card-head">
            <h2>Recent actions ({results.length})</h2>
            <Link to="/audit" className="link">Open audit log <ArrowRight size={12} /></Link>
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
                if (r.kind === "isolate") {
                  return (
                    <tr key={`${r.data.event_id}-${i}`}>
                      <td><span className="badge-action isolate">isolate</span></td>
                      <td>
                        <code>{r.data.backend_id}</code>{" "}
                        → <code>{r.data.anomaly_status}</code>{" "}
                        <span className="muted">score={r.data.score}</span>
                        <div className="muted small"><code>{r.data.reason}</code></div>
                      </td>
                      <td><code>{shortId(r.data.event_id)}</code></td>
                    </tr>
                  );
                }
                return (
                  <tr key={`safe-${i}`}>
                    <td><span className="badge-action policy">policy</span></td>
                    <td>
                      safe_mode → <code>{r.data.enable ? "true" : "false"}</code>{" "}
                      <span className="muted">v{r.data.policy_version}</span>
                    </td>
                    <td>—</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      ) : null}

      {/* ── Confirmation modal ────────────────────────────────────── */}
      {pending ? (
        <div className="modal-backdrop" onClick={() => !busy && setPending(null)}>
          <div className="modal" onClick={(e) => e.stopPropagation()}>
            <h3>
              {pending.kind === "scale"
                ? `Scale to ${pending.target_count} backends?`
                : pending.kind === "isolate"
                  ? `Mark ${pending.backend_id} as ${pending.status}?`
                  : `${pending.enable ? "Enable" : "Disable"} safe mode?`}
            </h3>
            <p className="meta">
              {pending.kind === "scale" ? (
                <>
                  Adjust backend count to <code>{pending.target_count}</code> immediately.
                  Cooldown is bypassed. Audit row written with actor=<code>{actor}</code>.
                </>
              ) : pending.kind === "isolate" ? (
                <>
                  Publish a synthetic <code>AnomalyEvent</code> for <code>{pending.backend_id}</code> with status{" "}
                  <code>{pending.status}</code>. Audit row written with actor=<code>{actor}</code>.
                </>
              ) : (
                <>
                  Setting <code>safe_mode = {String(pending.enable)}</code> on the live policy. The decision layer
                  will react on the next tick. Audit row written with actor=<code>{actor}</code>.
                </>
              )}
            </p>
            {("reason" in pending) && pending.reason ? (
              <p className="meta">Reason: <code>{pending.reason}</code></p>
            ) : null}
            <div style={{ display: "flex", gap: 8, marginTop: 16 }}>
              <button onClick={confirmPending} disabled={busy}>
                {busy ? "applying…" : "Continue"}
              </button>
              <button className="secondary" onClick={() => setPending(null)} disabled={busy}>Cancel</button>
            </div>
          </div>
        </div>
      ) : null}

      {error ? <div className="toast bad">{error}</div> : null}
    </>
  );
}
