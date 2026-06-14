import { useEffect, useMemo, useState } from "react";
import ReactDiffViewer from "react-diff-viewer-continued";
import {
  Activity,
  AlertCircle,
  AlertTriangle,
  ArrowRight,
  CheckCircle2,
  Clock,
  Cpu,
  GitBranch,
  Pencil,
  RefreshCw,
  ShieldAlert,
  ShieldCheck,
  Sliders,
  Sparkles,
  Target,
  TrendingUp,
  XCircle,
} from "lucide-react";

import {
  api,
  STRATEGY_NAMES,
  type AuditRow,
  type EnvironmentScope,
  type Policy,
  type PolicyPreviewResponse,
  type RelatedMetrics,
  type StrategyName,
} from "../api";

const REFRESH_MS = 10_000;

function formatJson(v: unknown): string {
  return JSON.stringify(v, null, 2);
}

function timeAgo(iso: string | null | undefined): string {
  if (!iso) return "—";
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return iso;
  const s = Math.max(0, (Date.now() - t) / 1000);
  if (s < 60) return `${Math.floor(s)}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

function fmt(v: unknown): string {
  if (v === null || v === undefined) return "—";
  if (typeof v === "string") return v;
  return JSON.stringify(v);
}

export default function PolicyPage() {
  const [current, setCurrent] = useState<Policy | null>(null);
  const [draft, setDraft] = useState<string>("");
  const [audit, setAudit] = useState<AuditRow[]>([]);
  const [env, setEnv] = useState<EnvironmentScope | null>(null);
  const [related, setRelated] = useState<RelatedMetrics | null>(null);
  const [preview, setPreview] = useState<PolicyPreviewResponse | null>(null);
  const [showDiff, setShowDiff] = useState(false);
  const [busy, setBusy] = useState(false);
  const [toast, setToast] = useState<{ msg: string; kind: "ok" | "bad" } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [strategyChoice, setStrategyChoice] = useState<StrategyName | "">("");

  async function loadAll() {
    try {
      const [p, a, e, r] = await Promise.all([
        api.getPolicy(),
        api.auditPolicy(30),
        api.getEnvironmentScope().catch(() => null),
        api.getRelatedMetrics().catch(() => null),
      ]);
      setCurrent(p);
      setAudit(a);
      setEnv(e);
      setRelated(r);
      setDraft((prev) => (prev ? prev : formatJson(p)));
      setError(null);
    } catch (err: any) {
      setError(err.message || "could not load policy");
    }
  }

  useEffect(() => {
    loadAll();
    const id = setInterval(loadAll, REFRESH_MS);
    return () => clearInterval(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const parsedDraft = useMemo(() => {
    try {
      return { ok: true as const, value: JSON.parse(draft) as Partial<Policy> };
    } catch (err: any) {
      return { ok: false as const, error: err.message as string };
    }
  }, [draft]);

  const diffOldStr = current ? formatJson(current) : "";
  const diffNewStr = parsedDraft.ok ? formatJson(parsedDraft.value) : "(invalid JSON)";

  function flash(msg: string, kind: "ok" | "bad") {
    setToast({ msg, kind });
    setTimeout(() => setToast(null), 4_000);
  }

  async function runPreview() {
    if (!parsedDraft.ok) {
      flash(`draft is not valid JSON: ${parsedDraft.error}`, "bad");
      return;
    }
    setBusy(true);
    try {
      const patch = { ...(parsedDraft.value as Partial<Policy>) } as Record<string, unknown>;
      delete patch.policy_version;
      const r = await api.previewPolicy(patch as Partial<Policy>);
      setPreview(r);
      setShowDiff(true);
    } catch (err: any) {
      flash(`preview failed: ${err.message || err}`, "bad");
    } finally {
      setBusy(false);
    }
  }

  async function commit() {
    if (!parsedDraft.ok) {
      flash(`draft is not valid JSON: ${parsedDraft.error}`, "bad");
      return;
    }
    setBusy(true);
    try {
      const patch = { ...(parsedDraft.value as Partial<Policy>) } as Record<string, unknown>;
      delete patch.policy_version;
      const result = await api.setPolicy(patch, "operator-ui");
      flash(
        result.status === "updated"
          ? `updated (v${result.policy_version}; changed ${result.changed_fields.join(", ")})`
          : "no change committed",
        "ok",
      );
      setPreview(null);
      await loadAll();
    } catch (err: any) {
      const fieldHint = err.field ? ` [field: ${err.field}]` : "";
      flash(`commit failed: ${err.message || err}${fieldHint}`, "bad");
    } finally {
      setBusy(false);
    }
  }

  async function applyStrategy() {
    if (!strategyChoice) {
      flash("pick a strategy first", "bad");
      return;
    }
    setBusy(true);
    try {
      const r = await api.setStrategy(strategyChoice, "operator-ui");
      const rl = r.recommended_rl_mode ? `; recommended RL_MODE=${r.recommended_rl_mode}` : "";
      flash(
        r.status === "updated"
          ? `strategy ${r.strategy} applied (v${r.policy_version})${rl}`
          : `already on ${r.strategy}${rl}`,
        "ok",
      );
      setPreview(null);
      await loadAll();
    } catch (err: any) {
      const fieldHint = err.field ? ` [field: ${err.field}]` : "";
      flash(`strategy apply failed: ${err.message || err}${fieldHint}`, "bad");
    } finally {
      setBusy(false);
    }
  }

  function resetDraft() {
    if (current) {
      setDraft(formatJson(current));
      setPreview(null);
      setShowDiff(false);
    }
  }

  function formatBuffer() {
    if (parsedDraft.ok) setDraft(formatJson(parsedDraft.value));
  }

  function copyBuffer() {
    navigator.clipboard?.writeText(draft).then(
      () => flash("copied to clipboard", "ok"),
      () => flash("copy failed", "bad"),
    );
  }

  const versions = useMemo(() => {
    const map = new Map<number, { fields: string[]; time: string; actor: string }>();
    for (const row of audit) {
      const v = map.get(row.policy_version);
      if (v) v.fields.push(row.field);
      else map.set(row.policy_version, { fields: [row.field], time: row.time, actor: row.actor });
    }
    return Array.from(map.entries()).sort((a, b) => b[0] - a[0]).slice(0, 6);
  }, [audit]);

  // Validation status drives the "Validation" KPI and the Guardrails panel.
  // After a preview it reflects the server dry-run; before then it falls back
  // to whether the draft parses as JSON at all.
  const validationOk = preview ? preview.valid : parsedDraft.ok;
  const lastUpdated = audit[0]?.time ?? null;

  return (
    <>
      <div className="page-header">
        <div>
          <h2>Policy Management</h2>
          <div className="subtitle">
            Configure routing, autoscaling, anomaly response, and safety guardrails.
          </div>
        </div>
        <div className="header-actions">
          <span className="refresh-chip">
            <span className="pulse" /> Auto-refresh {REFRESH_MS / 1000}s
          </span>
        </div>
      </div>

      {/* ── KPI row ────────────────────────────────────────────────── */}
      <div className="kpi-row">
        <div className="kpi cyan">
          <div className="kpi-label"><span className="kpi-icon"><Sliders size={14} /></span> Policy Version</div>
          <div className="kpi-value">v{current?.policy_version ?? "—"}</div>
          <div className="kpi-trend">current version</div>
        </div>
        <div className="kpi violet">
          <div className="kpi-label"><span className="kpi-icon"><Cpu size={14} /></span> Operating Mode</div>
          <div className="kpi-value" style={{ fontSize: 22 }}>{current?.operating_mode ?? "—"}</div>
          <div className="kpi-trend">routing strategy</div>
        </div>
        <div className={`kpi ${current?.safe_mode ? "amber" : "green"}`}>
          <div className="kpi-label"><span className="kpi-icon"><ShieldAlert size={14} /></span> Safe Mode</div>
          <div className="kpi-value" style={{ fontSize: 22 }}>
            {current ? (current.safe_mode ? "on / true" : "off / false") : "—"}
          </div>
          <div className="kpi-trend">{current?.safe_mode ? "guardrails enforced" : "guardrails relaxed"}</div>
        </div>
        <div className={`kpi ${validationOk ? "green" : "bad"}`}>
          <div className="kpi-label">
            <span className="kpi-icon">{validationOk ? <CheckCircle2 size={14} /> : <XCircle size={14} />}</span> Validation
          </div>
          <div className="kpi-value" style={{ fontSize: 22 }}>
            {validationOk ? "valid" : "invalid"}
          </div>
          <div className="kpi-trend">
            {preview ? "dry-run preview" : parsedDraft.ok ? "draft parses cleanly" : "draft does not parse"}
          </div>
        </div>
        <div className="kpi pink">
          <div className="kpi-label"><span className="kpi-icon"><Clock size={14} /></span> Last Updated</div>
          <div className="kpi-value" style={{ fontSize: 22 }}>{timeAgo(lastUpdated)}</div>
          <div className="kpi-trend">most recent change</div>
        </div>
      </div>

      {/* ── Named-strategy quick apply (#150) ─────────────────────── */}
      <div className="card">
        <div className="card-head">
          <h2>Strategy</h2>
          <span className="meta">
            Apply a named load-balancing strategy. Translates to the underlying
            primitives (operating mode + safe mode) through the same audited path
            as the editor below. The primitives editor stays available for
            advanced changes.
          </span>
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: 12, flexWrap: "wrap" }}>
          <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
            <Sparkles size={14} /> Active:
            <strong>{current?.strategy_name ?? "—"}</strong>
          </span>
          <select
            value={strategyChoice}
            onChange={(e) => setStrategyChoice(e.target.value as StrategyName | "")}
            disabled={busy}
            aria-label="Named strategy"
          >
            <option value="">Select a strategy…</option>
            {STRATEGY_NAMES.map((s) => (
              <option key={s} value={s}>{s}</option>
            ))}
          </select>
          <button onClick={applyStrategy} disabled={busy || !strategyChoice}>
            <ArrowRight size={14} /> Apply strategy
          </button>
        </div>
      </div>

      {/* ── Summary cards ─────────────────────────────────────────── */}
      <div className="card">
        <div className="card-head">
          <h2>Current Policy Summary</h2>
          <span className="meta">High-level overview of key policy settings and guardrails.</span>
        </div>
        <div className="policy-summary-grid">
          <div className="policy-summary-card anomaly">
            <div className="sc-head">
              <span className="sc-icon"><AlertTriangle size={14} /></span>
              <h4>Anomaly Detection</h4>
            </div>
            <div className="sc-row"><span className="k">latency multiplier</span><span className="v">{current?.anomaly_latency_multiplier ?? "—"}</span></div>
            <div className="sc-row"><span className="k">recovery window</span><span className="v">{current?.anomaly_recovery_window_seconds != null ? `${current.anomaly_recovery_window_seconds}s` : "—"}</span></div>
            <div className="sc-row"><span className="k">response</span><span className="v">{current?.anomaly_response ?? "—"}</span></div>
          </div>

          <div className="policy-summary-card scaling">
            <div className="sc-head">
              <span className="sc-icon"><TrendingUp size={14} /></span>
              <h4>Autoscaling</h4>
            </div>
            <div className="sc-row"><span className="k">cooldown</span><span className="v">{current?.autoscaler_cooldown_seconds != null ? `${current.autoscaler_cooldown_seconds}s` : "—"}</span></div>
            <div className="sc-row"><span className="k">min backends</span><span className="v">{current?.min_backends ?? "—"}</span></div>
            <div className="sc-row"><span className="k">max backends</span><span className="v">{current?.max_backends ?? "—"}</span></div>
          </div>

          <div className="policy-summary-card decision">
            <div className="sc-head">
              <span className="sc-icon"><Sparkles size={14} /></span>
              <h4>RL / Decision Layer</h4>
            </div>
            <div className="sc-row"><span className="k">operating mode</span><span className="v">{current?.operating_mode ?? "—"}</span></div>
            <div className="sc-row"><span className="k">confidence threshold</span><span className="v">{current?.rl_confidence_threshold ?? "—"}</span></div>
            <div className="sc-row"><span className="k">exploration rate</span><span className="v">{current?.rl_exploration_rate ?? "—"}</span></div>
          </div>

          <div className="policy-summary-card slo">
            <div className="sc-head">
              <span className="sc-icon"><Target size={14} /></span>
              <h4>Service Objective</h4>
            </div>
            <div className="sc-row"><span className="k">p95 latency target</span><span className="v">{current?.slo_p95_latency_ms != null ? `${current.slo_p95_latency_ms}ms` : "—"}</span></div>
            <div className="sc-row"><span className="k">per-instance capacity</span><span className="v">{current?.per_instance_capacity_rps != null ? `${current.per_instance_capacity_rps} rps` : "—"}</span></div>
            <div className="sc-row"><span className="k">policy version</span><span className="v">v{current?.policy_version ?? "—"}</span></div>
          </div>
        </div>
      </div>

      {/* ── Editor + validation/impact (60/40) ─────────────────────── */}
      <div className="grid-2 grid-stretch">
        <div className="card card-fill">
          <div className="card-head">
            <h2>Edit Policy JSON</h2>
            <span className="meta">Unknown fields are ignored. policy_version is server-managed.</span>
          </div>
          <div className="policy-editor-toolbar">
            <div className="left">{draft.length.toLocaleString()} chars · {draft.split("\n").length} lines</div>
            <button onClick={formatBuffer} disabled={!parsedDraft.ok}>Format</button>
            <button onClick={copyBuffer}>Copy</button>
          </div>
          <textarea
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            style={{ minHeight: 380 }}
          />

          {showDiff && parsedDraft.ok ? (
            <div style={{ marginTop: 16 }}>
              <div className="card-head" style={{ marginBottom: 8 }}>
                <h3>Diff preview</h3>
                <span className="meta">left: current · right: draft</span>
              </div>
              <ReactDiffViewer
                oldValue={diffOldStr}
                newValue={diffNewStr}
                splitView
                useDarkTheme
                hideLineNumbers={false}
              />
            </div>
          ) : null}
        </div>

        <div className="stack card-fill">
          <div className="card">
            <div className="card-head">
              <h2>Validation &amp; Guardrails</h2>
            </div>
            {!parsedDraft.ok ? (
              <div className="alert-row bad">
                <span className="icon"><XCircle size={16} /></span>
                <div>
                  <div className="title">Draft does not parse as JSON</div>
                  <div className="meta">{parsedDraft.error}</div>
                </div>
              </div>
            ) : preview ? (
              <>
                <div className={`alert-row ${preview.valid ? "ok" : "bad"}`}>
                  <span className="icon">{preview.valid ? <CheckCircle2 size={16} /> : <XCircle size={16} />}</span>
                  <div>
                    <div className="title">
                      {preview.valid ? "Validation passes" : `${preview.errors.length} validation error(s)`}
                    </div>
                    <div className="meta">
                      {preview.valid
                        ? `${preview.changed_fields.length} field(s) would change`
                        : "Fix the errors below before committing."}
                    </div>
                  </div>
                </div>
                {preview.errors.map((e, i) => (
                  <div key={`e${i}`} className="alert-row bad">
                    <span className="icon"><AlertCircle size={16} /></span>
                    <div><div className="title">{e}</div></div>
                  </div>
                ))}
                {preview.warnings.map((w, i) => (
                  <div key={`w${i}`} className="alert-row warn">
                    <span className="icon"><AlertTriangle size={16} /></span>
                    <div><div className="title">{w}</div></div>
                  </div>
                ))}
                {current ? (
                  <div className="alert-row warn">
                    <span className="icon"><ShieldAlert size={16} /></span>
                    <div>
                      <div className="title">
                        {current.safe_mode
                          ? "Safe mode is ON. Guardrails are enforced."
                          : "Safe mode is OFF. Guardrails are relaxed."}
                      </div>
                    </div>
                  </div>
                ) : null}
              </>
            ) : (
              <div className="empty-state">
                <ShieldCheck size={26} strokeWidth={1.5} />
                <div>Ready to validate</div>
                <div className="empty-sub">Click "Validate" below to run the draft through the dry-run preview.</div>
              </div>
            )}
          </div>

          <div className="card stretch">
            <div className="card-head">
              <h2>Change Impact Preview</h2>
              {preview ? <span className="meta">{preview.diff.length} field(s)</span> : null}
            </div>
            {preview && preview.diff.length > 0 ? (
              <>
                {preview.diff.slice(0, 10).map((d, i) => (
                  <div className="svc-row" key={i}>
                    <div className="svc-name mono">{d.field}</div>
                    <span className="muted small">{fmt(d.old)}</span>
                    <span className="muted-2 small">→</span>
                    <span className="mono small" style={{ color: "var(--cyan)" }}>{fmt(d.new)}</span>
                  </div>
                ))}
              </>
            ) : preview ? (
              <div className="alert-row ok">
                <span className="icon"><CheckCircle2 size={16} /></span>
                <div><div className="title">No changes detected.</div></div>
              </div>
            ) : (
              <div className="empty-state">
                <Activity size={26} strokeWidth={1.5} />
                <div>No pending changes</div>
                <div className="empty-sub">Edit the policy on the left and click "Preview Diff".</div>
              </div>
            )}
          </div>
        </div>
      </div>

      {/* ── Versions + Environment + Related metrics (3-up) ───────── */}
      <div className="grid-3">
        <div className="card">
          <div className="card-head">
            <h2>Recent Versions</h2>
            <span className="meta">{versions.length} shown</span>
          </div>
          {versions.length === 0 ? (
            <div className="meta">No audited version history yet.</div>
          ) : (
            versions.map(([v, info]) => (
              <div className="version-row" key={v}>
                <span className="v-tag">v{v}</span>
                <div>
                  <div>{info.fields.slice(0, 3).join(", ")}{info.fields.length > 3 ? ` · +${info.fields.length - 3}` : ""}</div>
                  <div className="v-meta">{info.actor} · {timeAgo(info.time)}</div>
                </div>
                <button className="ghost small" title="View this version's diff">
                  <GitBranch size={12} />
                </button>
              </div>
            ))
          )}
        </div>

        <div className="card">
          <div className="card-head">
            <h2>Environment Scope</h2>
          </div>
          <div style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
            {(env?.available ?? []).length === 0 ? (
              <span className="muted small">—</span>
            ) : (
              env!.available.map((name) => {
                const active = name === env!.active;
                return (
                  <span key={name} className={`svc-pill ${active ? "ok" : "degraded"}`}>
                    {name}
                  </span>
                );
              })
            )}
          </div>
          <div className="meta" style={{ marginTop: 10 }}>
            {env?.active
              ? `This policy is active in ${env.active}.`
              : "No environment scope reported."}
          </div>
        </div>

        <div className="card">
          <div className="card-head">
            <h2>Related Metrics</h2>
            <span className="meta">live</span>
          </div>
          <div className="svc-row">
            <div className="svc-name">SLO compliance</div>
            <span className="mono">{related?.slo_compliance_pct != null ? `${related.slo_compliance_pct}%` : "—"}</span>
            <span />
            <span />
          </div>
          <div className="svc-row">
            <div className="svc-name">p95 latency</div>
            <span className="mono">{related?.p95_latency_ms != null ? `${related.p95_latency_ms} ms` : "—"}</span>
            <span />
            <span />
          </div>
          <div className="svc-row">
            <div className="svc-name">RPS (1m)</div>
            <span className="mono">{related?.rps_current != null ? related.rps_current.toFixed(2) : "—"}</span>
            <span />
            <span />
          </div>
        </div>
      </div>

      {/* ── Action bar ────────────────────────────────────────────── */}
      <div className="policy-action-bar">
        <button onClick={runPreview} disabled={busy || !parsedDraft.ok}>
          <CheckCircle2 size={14} /> Validate
        </button>
        <button className="secondary" onClick={() => setShowDiff((v) => !v)} disabled={!parsedDraft.ok}>
          <GitBranch size={14} /> {showDiff ? "Hide diff" : "Preview Diff"}
        </button>
        <button className="ghost" onClick={resetDraft} disabled={busy}>
          <RefreshCw size={14} /> Reset
        </button>
        <span className="grow" />
        {error ? <span style={{ color: "var(--bad)", fontSize: 12 }}>{error}</span> : null}
        <button onClick={commit} disabled={busy || !parsedDraft.ok}>
          {busy ? "committing…" : <>Commit Changes <ArrowRight size={14} /></>}
        </button>
      </div>

      {/* ── Workflow ──────────────────────────────────────────────── */}
      <div className="workflow">
        <div className="step done"><span className="dot" /><Pencil size={11} /> Edit</div>
        <span className="arrow"><ArrowRight size={12} /></span>
        <div className={`step ${preview ? "done" : "active"}`}><span className="dot" /><CheckCircle2 size={11} /> Validate</div>
        <span className="arrow"><ArrowRight size={12} /></span>
        <div className={`step ${preview ? "active" : ""}`}><span className="dot" /><Activity size={11} /> Preview</div>
        <span className="arrow"><ArrowRight size={12} /></span>
        <div className="step"><span className="dot" /><GitBranch size={11} /> Commit</div>
        <span className="arrow"><ArrowRight size={12} /></span>
        <div className="step"><span className="dot" /><Sparkles size={11} /> Apply</div>
        <span className="arrow"><ArrowRight size={12} /></span>
        <div className="step"><span className="dot" /><Activity size={11} /> Observe</div>
      </div>

      {toast ? <div className={`toast ${toast.kind}`}>{toast.msg}</div> : null}
    </>
  );
}
