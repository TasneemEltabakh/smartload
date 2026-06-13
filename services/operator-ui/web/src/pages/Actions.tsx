import { useEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";
import {
  ArrowLeftRight,
  ArrowRight,
  Ban,
  Info,
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
  type LbState,
  type LbWeightOverrideResponse,
  type ManualIsolateResponse,
  type ManualScaleResponse,
  type Policy,
} from "../api";

type Pending =
  | { kind: "scale";     target_count: number; reason: string }
  | { kind: "isolate";   backend_id: string; status: IsolateStatus; reason: string }
  | { kind: "lb_weights"; weights: Record<string, number> }
  | { kind: "safe-mode"; enable: boolean; reason: string };

type ResultCard =
  | { kind: "scale"; data: ManualScaleResponse }
  | { kind: "isolate"; data: ManualIsolateResponse }
  | { kind: "lb_weights"; data: LbWeightOverrideResponse }
  | { kind: "safe-mode"; data: { enable: boolean; policy_version: number } };

const STATUSES: IsolateStatus[] = ["healthy", "degraded", "unhealthy"];

// Clickable JSON snippets for the force-weights editor.
const EXAMPLE_EQUAL = JSON.stringify(
  { "test-backend-1:8080": 1, "test-backend-2:8080": 1 },
  null,
  2,
);
const EXAMPLE_PREFER = JSON.stringify(
  { "test-backend-1:8080": 70, "test-backend-2:8080": 30 },
  null,
  2,
);

function shortId(s: string): string {
  return s ? s.slice(0, 8) : "—";
}

export default function ActionsPage() {
  const [actor, setActor] = useState<string>("operator");

  const [policy, setPolicy] = useState<Policy | null>(null);
  const [policyError, setPolicyError] = useState<string | null>(null);

  const [lbState, setLbState] = useState<LbState | null>(null);
  const [lbStateError, setLbStateError] = useState<string | null>(null);

  async function loadPolicy() {
    try {
      const p = await api.getPolicy();
      setPolicy(p);
      setPolicyError(null);
    } catch (err: any) {
      setPolicyError(err?.message || "could not load policy");
    }
  }

  async function loadLbState() {
    try {
      const s = await api.getLbState();
      setLbState(s);
      setLbStateError(null);
    } catch (err: any) {
      setLbStateError(err?.message || "could not load load-balancer state");
    }
  }

  // Re-fetch all page data: live policy + current upstream weights.
  async function refreshAll() {
    await Promise.all([loadPolicy(), loadLbState()]);
  }

  useEffect(() => { refreshAll(); }, []);

  const [scaleTarget, setScaleTarget] = useState<string>("");
  const [scaleReason, setScaleReason] = useState<string>("");
  const scaleTargetRef = useRef<HTMLInputElement>(null);

  const [backendId, setBackendId] = useState<string>("");
  const [isolateStatus, setIsolateStatus] = useState<IsolateStatus>("unhealthy");
  const [isolateReason, setIsolateReason] = useState<string>("");

  // ── lb weight override form (JSON editor) ───────────────────────────────
  // Accepts a flat JSON object of backend → weight, e.g.
  //   {"test-backend-1:8080": 5, "test-backend-2:8080": 1}
  const [lbWeightsRaw, setLbWeightsRaw] = useState<string>("");

  // Parse + validate the textarea into a flat Record<string, number>.
  // Returns { weights } on success or { error } with a human-readable reason.
  function parseLbWeights(
    raw: string,
  ): { weights: Record<string, number>; error?: undefined } | { error: string; weights?: undefined } {
    const text = raw.trim();
    if (!text) return { error: "Enter weights as a JSON object, e.g. {\"backend-1:8080\": 5}" };
    let value: unknown;
    try {
      value = JSON.parse(text);
    } catch {
      return { error: "Weights are not valid JSON — check for trailing commas or missing quotes" };
    }
    if (value === null || typeof value !== "object" || Array.isArray(value)) {
      return { error: "Weights must be a JSON object of backend → number" };
    }
    const out: Record<string, number> = {};
    for (const [key, v] of Object.entries(value as Record<string, unknown>)) {
      if (typeof v !== "number" || !Number.isFinite(v)) {
        return { error: `Weight for "${key}" must be a number` };
      }
      if (v < 0) {
        return { error: `Weight for "${key}" must not be negative` };
      }
      out[key] = v;
    }
    if (Object.keys(out).length === 0) {
      return { error: "Provide at least one backend weight" };
    }
    return { weights: out };
  }

  function requestLbWeights() {
    const result = parseLbWeights(lbWeightsRaw);
    if (result.error || !result.weights) {
      flashError(result.error ?? "Provide at least one backend weight");
      return;
    }
    setPending({ kind: "lb_weights", weights: result.weights });
  }


  const [pending, setPending] = useState<Pending | null>(null);
  const [busy, setBusy] = useState(false);
  const [results, setResults] = useState<ResultCard[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  function recordResult(card: ResultCard) {
    setResults((prev) => [card, ...prev].slice(0, 10));
  }

  function flashError(msg: string) {
    setError(msg);
    setTimeout(() => setError(null), 6_000);
  }

  function flashSuccess(msg: string) {
    setNotice(msg);
    setTimeout(() => setNotice(null), 5_000);
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

  // Prefill the "Scale to N backends" form's target with the current cluster
  // size ± 1 (clamped to policy bounds), then scroll/focus it so the operator
  // reviews + confirms rather than firing a scale blindly.
  function quickScale(delta: number) {
    const min = policy?.min_backends ?? 1;
    const max = policy?.max_backends ?? 99;
    const current = lbState ? Object.keys(lbState.upstream_weights).length : min;
    const base = current > 0 ? current : min;
    const target = Math.max(min, Math.min(max, base + delta));
    setScaleTarget(String(target));
    setScaleReason(delta > 0 ? "scale up" : "scale down");
    requestAnimationFrame(() => {
      scaleTargetRef.current?.scrollIntoView({ behavior: "smooth", block: "center" });
      scaleTargetRef.current?.focus();
      scaleTargetRef.current?.select();
    });
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
      } else if (pending.kind === "lb_weights") {
        const r = await api.setLbWeights(pending.weights);
        recordResult({ kind: "lb_weights", data: r });
        setLbWeightsRaw("");
      } else if (pending.kind === "safe-mode") {
        const r = await api.setPolicy({ safe_mode: pending.enable }, actor);
        recordResult({ kind: "safe-mode", data: { enable: pending.enable, policy_version: r.policy_version } });
        flashSuccess(`Safe mode ${pending.enable ? "enabled" : "disabled"} · policy v${r.policy_version}`);
      }
      await refreshAll();
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
          <button className="qa-tile" onClick={refreshAll} disabled={busy}>
            <span className="qa-icon"><RefreshCw size={16} /></span>
            <span>Refresh backends</span>
            <span className="qa-sub">Reload backend list from registry</span>
          </button>
          <button className="qa-tile green" onClick={() => quickScale(+1)} disabled={busy || !policy}>
            <span className="qa-icon"><Plus size={16} /></span>
            <span>Scale up</span>
            <span className="qa-sub">Increase backend replicas</span>
          </button>
          <button className="qa-tile amber" onClick={() => quickScale(-1)} disabled={busy || !policy}>
            <span className="qa-icon"><Minus size={16} /></span>
            <span>Scale down</span>
            <span className="qa-sub">Decrease backend replicas</span>
          </button>
          <button
            className="qa-tile violet"
            onClick={requestToggleSafeMode}
            disabled={busy || !policy}
          >
            <span className="qa-icon"><ShieldAlert size={16} /></span>
            <span>{policy?.safe_mode ? "Disable safe mode" : "Enable safe mode"}</span>
            <span className="qa-sub">Fallback to classical algorithms</span>
          </button>
        </div>
      </div>

      {/* ── Manual actions ─────────────────────────────────────────── */}
      <div className="card">
        <div className="card-head">
          <h2>Manual actions</h2>
          <span className="meta">
            Override SmartLoad behaviour by executing manual operations. All actions are
            audited in the <Link to="/audit" className="link">Audit log</Link>.
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
        <div className="meta" style={{ marginTop: 8 }}>
          {policy
            ? <>Live policy: <code>min={policy.min_backends}</code> · <code>max={policy.max_backends}</code> · <code>v{policy.policy_version}</code></>
            : policyError
              ? <span style={{ color: "var(--bad)" }}>(could not load policy: {policyError})</span>
              : "loading policy…"}
        </div>
      </div>

      {/* Scale */}
      <div className="manual-action scale">
        <div className="ma-head">
          <span className="ma-icon"><TrendingUp size={14} /></span>
          <div>
            <h3>Scale to N backends</h3>
            <div className="ma-sub">Target total number of backends. SmartLoad will scale up or down automatically.</div>
          </div>
        </div>
        <div className="ma-body">
          <div className="field">
            <label>Target count</label>
            <input
              ref={scaleTargetRef}
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
              placeholder="e.g. traffic spike, maintenance, test"
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
            <div className="ma-sub">Temporarily remove a backend from receiving traffic. The backend will be marked as unhealthy until isolation is removed.</div>
          </div>
        </div>
        <div className="ma-body">
          <div className="field">
            <label>Backend ID / IP</label>
            <input
              value={backendId}
              onChange={(e) => setBackendId(e.target.value)}
              placeholder="e.g. test-backend-3 or 172.18.0.3:8080"
            />
          </div>
          <div className="field" style={{ flex: 0.7 }}>
            <label>Status after isolation</label>
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
              placeholder="e.g. high latency, error rate"
            />
          </div>
          <button className="danger" onClick={requestIsolate} disabled={busy || !backendId.trim()}>Isolate</button>
        </div>
      </div>

      {/* Force route weights (T2.1 — live) */}
      <div className="manual-action weights">
        <div className="ma-head">
          <span className="ma-icon"><ArrowLeftRight size={14} /></span>
          <div>
            <h3>Force route weights <span className="ma-sub" style={{ marginTop: 0 }}>(optional)</span></h3>
            <div className="ma-sub">Override routing distribution by setting custom weights for backends. Leave empty to clear overrides.</div>
          </div>
        </div>

        <div className="ma-body" style={{ alignItems: "stretch" }}>
          <div className="field" style={{ flex: 2 }}>
            <label style={{ display: "inline-flex", alignItems: "center", gap: 5 }}>
              Backend weights (JSON) <Info size={11} />
            </label>
            <textarea
              value={lbWeightsRaw}
              onChange={(e) => setLbWeightsRaw(e.target.value)}
              rows={6}
              spellCheck={false}
              placeholder={'{\n  "test-backend-1:8080": 50,\n  "test-backend-2:8080": 30,\n  "test-backend-3:8080": 20\n}'}
              style={{ fontFamily: "ui-monospace, monospace", fontSize: 12, resize: "vertical" }}
            />
          </div>

          <div className="field" style={{ flex: 1.4, justifyContent: "flex-start" }}>
            <div className="json-examples">
              <h4>Examples</h4>
              <pre
                title="Click to fill the editor"
                onClick={() => setLbWeightsRaw(EXAMPLE_EQUAL)}
              >{`• Equal weights: { "backend-1": 1, "backend-2": 1 }`}</pre>
              <pre
                title="Click to fill the editor"
                onClick={() => setLbWeightsRaw(EXAMPLE_PREFER)}
              >{`• Prefer backend-1: { "backend-1": 70, "backend-2": 30 }`}</pre>
              <p className="note">Weights are normalized automatically.</p>
            </div>
          </div>

          <button onClick={requestLbWeights} disabled={busy || !lbWeightsRaw.trim()}>
            Apply weights
          </button>
        </div>

        {/* Current load-balancer state for reference. */}
        <div className="meta" style={{ marginTop: 12 }}>
          {lbStateError ? (
            <span style={{ color: "var(--bad)" }}>(could not load load-balancer state: {lbStateError})</span>
          ) : lbState ? (
            <>
              <div>
                Current upstream weights:{" "}
                {Object.keys(lbState.upstream_weights).length === 0 ? (
                  <span className="muted">none (default round-robin)</span>
                ) : (
                  Object.entries(lbState.upstream_weights).map(([b, w]) => (
                    <span key={b} style={{ marginRight: 8 }}><code>{b}={w}</code></span>
                  ))
                )}
              </div>
              <div style={{ marginTop: 4 }}>
                Excluded backends:{" "}
                {lbState.excluded_backends.length === 0 ? (
                  <span className="muted">none</span>
                ) : (
                  lbState.excluded_backends.map((b) => (
                    <span key={b} style={{ marginRight: 8 }}><code>{b}</code></span>
                  ))
                )}
              </div>
            </>
          ) : (
            "loading load-balancer state…"
          )}
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
                    <tr key={`scale-${r.data.event_id}-${i}`}>
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
                if (r.kind === "lb_weights") {
                  const entries = Object.entries(r.data.applied_weights);
                  return (
                    <tr key={`lb-${i}`}>
                      <td><span className="badge-action scale_out">lb weights</span></td>
                      <td>
                        {entries.map(([b, w]) => (
                          <span key={b} style={{ marginRight: 8 }}>
                            <code>{b}={w}</code>
                          </span>
                        ))}
                      </td>
                      <td>—</td>
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
                : pending.kind === "lb_weights"
                  ? `Force route weights (${Object.keys(pending.weights).length} backends)?`
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
              ) : pending.kind === "lb_weights" ? (
                <>
                  This will rewrite the NGINX upstream block via the lb-sidecar
                  and trigger <code>nginx -s reload</code> immediately.
                  Weights: {Object.entries(pending.weights).map(([b, w]) => (
                    <span key={b} style={{ marginRight: 6 }}><code>{b}={w}</code></span>
                  ))}
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

      {/* ── Footer note ───────────────────────────────────────────── */}
      <div className="meta" style={{ marginTop: 16, display: "flex", alignItems: "center", gap: 6 }}>
        <Info size={12} />
        <span>
          All manual actions are recorded in the{" "}
          <Link to="/audit" className="link">Audit log</Link>.
        </span>
      </div>

      {notice ? <div className="toast ok">{notice}</div> : null}
      {error ? <div className="toast bad">{error}</div> : null}
    </>
  );
}
