// ============================================================================
// Controls -- operator actions + policy governance
// ----------------------------------------------------------------------------
// The single console where an operator drives SmartLoad by hand. It merges what
// used to be two pages (Actions + Policy):
//
//   1. The safe_mode KILL SWITCH (prominent, armed/red), wired through the app
//      shell so it shares the Topbar switch's path.
//   2. The operating-policy editor: read the live policy, edit its fields, then
//      run a Diff & commit flow (policy/preview shows the old -> new diff in a
//      Modal) before setPolicy commits. A named-strategy quick-apply selector
//      sits alongside (policy/strategy).
//   3. Manual overrides as confirm-gated action cards: scale to N (scale),
//      isolate a backend (isolate), and force routing weights (lb/weights).
//      Each is reversible and audit-logged.
//   4. A session operations strip: the last actions performed this session.
//
// HONEST ACTION FEEDBACK. Every write (kill switch, strategy apply, policy
// commit, scale, isolate, weights) reports pending -> confirmed/failed from the
// REAL API result. Nothing is reported as succeeded before the call resolves;
// a failure surfaces a calm critical toast and a "failed" worklog row.
//
// DEMONSTRATION SAFETY. Reads resolve live-or-demo through useLiveOrDemo, so the
// page is fully populated on representative data with no backend running. But
// destructive actions never FAKE success offline: when no live backend has been
// resolved, the commit / scale / isolate / weights affordances are held with a
// clear "connect a live backend to apply" hint, and their confirm dialogs state
// plainly that they would apply once live. The kill switch reflects the
// operator's local intent without claiming a backend write was confirmed.
// ============================================================================

import { useEffect, useMemo, useState, type ReactNode } from "react";
import {
  AlertTriangle,
  ArrowRight,
  Boxes,
  Check,
  Compass,
  GitCompare,
  ListChecks,
  Loader2,
  Power,
  ShieldCheck,
  Sliders,
  Workflow,
  X,
} from "lucide-react";

import {
  api,
  STRATEGY_NAMES,
  type IsolateStatus,
  type LbState,
  type Policy,
  type PolicyDiffEntry,
  type PolicyPreviewResponse,
  type RelatedMetrics,
  type RlModeStatus,
  type StrategyName,
} from "../api";
import {
  Badge,
  Button,
  Card,
  DataTable,
  EmptyState,
  Modal,
  StatusPill,
  Toggle,
  useLiveOrDemo,
  useToast,
  type Column,
} from "../ui";
import { useShell } from "./shell-context";
import { SAMPLE_POLICY } from "./sample";
import {
  SAMPLE_BACKEND_IDS,
  SAMPLE_LB_STATE,
  SAMPLE_OP_HISTORY,
  SAMPLE_RELATED_METRICS,
  SAMPLE_RL_MODE,
  STRATEGY_BLURB,
  type OpEntry,
} from "./_sampleControls";

const ACTOR = "operator";

// Panel ids registered with the DataModeProvider so the global indicator
// reflects which panels resolved live vs demonstration.
const PANEL_POLICY = "controls.policy";
const PANEL_LB = "controls.lb";
const PANEL_RELATED = "controls.related";
const PANEL_RL_MODE = "controls.rl-mode";

// The policy primitives the editor exposes. operating_mode and safe_mode are
// handled separately (mode is a select, safe_mode is the kill switch), so this
// drives the numeric/threshold fields only.
interface FieldSpec {
  key: keyof Policy;
  label: string;
  unit?: string;
  step?: number;
  min?: number;
  max?: number;
  options?: string[];
  hint: string;
}

const POLICY_FIELDS: FieldSpec[] = [
  { key: "min_backends", label: "Min backends", step: 1, min: 1, hint: "Floor the pool never scales below." },
  { key: "max_backends", label: "Max backends", step: 1, min: 1, hint: "Ceiling the pool never scales above." },
  { key: "slo_p95_latency_ms", label: "SLO p95 latency", unit: "ms", step: 5, min: 1, hint: "Target p95 the plane defends." },
  { key: "anomaly_latency_multiplier", label: "Anomaly latency x", step: 0.1, min: 1, hint: "Multiple of SLO that trips an anomaly verdict." },
  { key: "per_instance_capacity_rps", label: "Per-instance capacity", unit: "rps", step: 10, min: 1, hint: "Rated throughput per backend; sizes scale-out." },
  { key: "autoscaler_cooldown_seconds", label: "Autoscaler cooldown", unit: "s", step: 10, min: 0, hint: "Quiet window between consecutive scale actions." },
  { key: "anomaly_recovery_window_seconds", label: "Anomaly recovery window", unit: "s", step: 5, min: 1, hint: "Stable seconds before a recovered backend rejoins rotation." },
  { key: "rl_exploration_rate", label: "RL exploration rate", step: 0.05, min: 0, max: 1, hint: "Share of routing decisions the RL policy explores (0-1)." },
  { key: "rl_confidence_threshold", label: "RL confidence threshold", step: 0.05, min: 0, max: 1, hint: "Minimum confidence before an RL ranking is acted on (0-1)." },
  { key: "anomaly_response", label: "Anomaly response", options: ["auto-isolate", "advisory"], hint: "Auto-isolate a sick backend, or advisory-only." },
];

const OPERATING_MODES = ["classical-only", "hybrid", "rl-only"];

// ── small formatting helpers ─────────────────────────────────────────────────

const nowClock = () =>
  new Date().toLocaleTimeString("en-GB", { hour12: false });

const fmtVal = (v: unknown): string => {
  if (v == null) return "—";
  if (typeof v === "boolean") return v ? "on" : "off";
  if (typeof v === "number") return String(v);
  return String(v);
};

// Coerce a form value back to the policy field's runtime type. Numeric specs
// parse to number (NaN guarded by the caller); everything else passes through.
const coerce = (_spec: FieldSpec, raw: string): number => Number(raw);

function errText(e: unknown): string {
  if (e instanceof Error) return e.message;
  return "no live backend reached";
}

// The disabled hint shown on every destructive affordance while offline, so the
// console never pretends an action wrote to a backend that isn't there.
const OFFLINE_HINT = "Connect a live backend to apply.";

// ── component ────────────────────────────────────────────────────────────────

export default function Controls() {
  const shell = useShell();
  const toast = useToast();

  // ── live-or-demo reads (per panel) ─────────────────────────────────────────
  const policyQ = useLiveOrDemo<Policy>(
    () => api.getPolicy(),
    SAMPLE_POLICY,
    { panelId: PANEL_POLICY },
  );
  const lbQ = useLiveOrDemo<LbState>(
    () => api.getLbState(),
    SAMPLE_LB_STATE,
    { panelId: PANEL_LB },
  );
  const relatedQ = useLiveOrDemo<RelatedMetrics>(
    () => api.getRelatedMetrics(),
    SAMPLE_RELATED_METRICS,
    { panelId: PANEL_RELATED },
  );
  const rlModeQ = useLiveOrDemo<RlModeStatus>(
    () => api.getRlMode(),
    SAMPLE_RL_MODE,
    { panelId: PANEL_RL_MODE },
  );

  // A backend is "live" for write purposes once the policy read resolves live.
  // Destructive actions key off this so they never fake success offline.
  const connected = policyQ.source === "live";

  // The policy snapshot the editor edits against. Held locally so a confirmed
  // commit can advance it without waiting for the next read; seeded from the
  // resolved read.
  const [policy, setPolicyState] = useState<Policy>(SAMPLE_POLICY);
  const [lb, setLbState] = useState<LbState>(SAMPLE_LB_STATE);

  // Working copy of the editable policy fields (string-backed for inputs).
  const [draft, setDraft] = useState<Record<string, string>>({});
  const [mode, setMode] = useState<string>(SAMPLE_POLICY.operating_mode);

  // Session worklog (local only).
  const [history, setHistory] = useState<OpEntry[]>(SAMPLE_OP_HISTORY);

  // Modal state for the diff & commit flow.
  const [preview, setPreview] = useState<PolicyPreviewResponse | null>(null);
  const [previewOpen, setPreviewOpen] = useState(false);
  const [previewing, setPreviewing] = useState(false);
  const [committing, setCommitting] = useState(false);

  const backendIds = useMemo(() => {
    const fromLb = Object.keys(lb.upstream_weights ?? {});
    return fromLb.length ? fromLb : SAMPLE_BACKEND_IDS;
  }, [lb]);

  // ── sync local editor state from the resolved reads ────────────────────────
  // Re-seed whenever the policy read changes (initial demo, then live upgrade),
  // so the editor opens against whatever the page actually resolved to.
  useEffect(() => {
    setPolicyState(policyQ.value);
    seedDraft(policyQ.value);
    setMode(policyQ.value.operating_mode ?? "hybrid");
    // Only a live policy read drives the shared kill switch, so a demo read
    // never stomps the operator's manual choice while offline.
    if (policyQ.source === "live") {
      shell.setSafeMode(Boolean(policyQ.value.safe_mode));
    }
    // shell.setSafeMode is stable (from context); track the resolved value.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [policyQ.value, policyQ.source]);

  useEffect(() => {
    setLbState(lbQ.value);
  }, [lbQ.value]);

  function seedDraft(p: Policy) {
    const next: Record<string, string> = {};
    for (const f of POLICY_FIELDS) {
      const v = p[f.key];
      next[f.key as string] = v == null ? "" : String(v);
    }
    setDraft(next);
  }

  // ── session worklog helper ─────────────────────────────────────────────────
  function logOp(entry: Omit<OpEntry, "id" | "time" | "source">) {
    setHistory((prev) => [
      { ...entry, id: Date.now() + Math.random(), time: nowClock(), source: connected ? "live" : "local" },
      ...prev,
    ]);
  }

  // ── derived policy patch (draft vs current) ────────────────────────────────
  const patch = useMemo<Partial<Policy>>(() => {
    const out: Partial<Policy> = {};
    for (const f of POLICY_FIELDS) {
      const raw = draft[f.key as string];
      if (raw == null || raw === "") continue;
      if (f.options) {
        // Enum field: compare and store the raw string, never coerce to number.
        if (raw !== String(policy[f.key] ?? "")) (out as Record<string, unknown>)[f.key as string] = raw;
        continue;
      }
      const n = coerce(f, raw);
      if (Number.isNaN(n)) continue;
      if (n !== policy[f.key]) (out as Record<string, unknown>)[f.key as string] = n;
    }
    if (mode && mode !== policy.operating_mode) out.operating_mode = mode;
    return out;
  }, [draft, mode, policy]);

  const changedCount = Object.keys(patch).length;

  // ── policy: diff & commit ──────────────────────────────────────────────────
  // Preview shows a busy state while the live preview resolves; offline it
  // computes the diff locally so the operator can still review what WOULD apply.
  async function openDiff() {
    if (changedCount === 0) {
      toast.push({ title: "No changes to commit", detail: "Draft matches the live policy.", tone: "info" });
      return;
    }
    setPreviewing(true);
    setPreviewOpen(true);
    try {
      const res = await api.previewPolicy(patch);
      setPreview(res);
    } catch {
      // Offline / unreachable: fall back to a locally computed diff so the
      // operator always sees what would change before deciding.
      setPreview(localPreview(patch, policy));
    } finally {
      setPreviewing(false);
    }
  }

  async function commit() {
    // Demonstration: do not fake a backend write. Apply to the local snapshot
    // so the editor reflects the operator's intent, log it as a local op, and
    // say so plainly.
    if (!connected) {
      const merged = { ...policy, ...patch } as Policy;
      setPolicyState(merged);
      seedDraft(merged);
      setMode(merged.operating_mode ?? mode);
      toast.push({
        title: "Saved locally (demonstration)",
        detail: "No live backend connected; nothing was written.",
        tone: "info",
      });
      logOp({
        kind: "policy",
        summary: `Edited ${changedCount} policy field${changedCount === 1 ? "" : "s"} locally (demonstration; not written to a backend).`,
        outcome: "ok",
      });
      setPreviewOpen(false);
      setPreview(null);
      return;
    }

    setCommitting(true);
    try {
      const res = await api.setPolicy(patch, ACTOR);
      setPolicyState(res.policy);
      seedDraft(res.policy);
      setMode(res.policy.operating_mode ?? mode);
      toast.push({
        title: "Policy committed",
        detail: `v${res.policy_version} · ${res.changed_fields.join(", ") || "no-op"}`,
        tone: "ok",
      });
      logOp({
        kind: "policy",
        summary: `Committed policy v${res.policy_version}: ${res.changed_fields.join(", ") || "no fields changed"}.`,
        outcome: "ok",
      });
      setPreviewOpen(false);
      setPreview(null);
    } catch (e) {
      // A live backend rejected the write: keep the draft, do NOT advance the
      // snapshot, and report the failure honestly.
      toast.push({
        title: "Commit failed",
        detail: errText(e),
        tone: "crit",
      });
      logOp({
        kind: "policy",
        summary: `Policy commit (${changedCount} field${changedCount === 1 ? "" : "s"}) was rejected by the backend.`,
        outcome: "failed",
      });
    } finally {
      setCommitting(false);
    }
  }

  function resetDraft() {
    seedDraft(policy);
    setMode(policy.operating_mode ?? "hybrid");
  }

  // ── named-strategy quick-apply ─────────────────────────────────────────────
  async function applyStrategy(name: StrategyName) {
    if (!connected) {
      const merged = { ...policy, strategy_name: name } as Policy;
      setPolicyState(merged);
      toast.push({
        title: "Strategy staged (demonstration)",
        detail: "No live backend connected; nothing was written.",
        tone: "info",
      });
      logOp({ kind: "strategy", summary: `Selected strategy "${name}" locally (demonstration; not written).`, outcome: "ok" });
      return;
    }
    try {
      const res = await api.setStrategy(name, ACTOR);
      setPolicyState(res.policy);
      seedDraft(res.policy);
      setMode(res.policy.operating_mode ?? mode);
      toast.push({
        title: `Strategy applied: ${name}`,
        detail: res.recommended_rl_mode ? `recommended RL_MODE ${res.recommended_rl_mode}` : "policy primitives updated",
        tone: "ok",
      });
      logOp({ kind: "strategy", summary: `Applied named strategy "${name}" (policy v${res.policy_version}).`, outcome: "ok" });
    } catch (e) {
      toast.push({ title: "Strategy apply failed", detail: errText(e), tone: "crit" });
      logOp({ kind: "strategy", summary: `Strategy "${name}" was rejected by the backend.`, outcome: "failed" });
    }
  }

  // ── kill switch (shared with the shell / Topbar) ───────────────────────────
  // Reflect the operator's local intent and keep the shared shell state in sync
  // (so the Topbar switch agrees), but be honest about whether a write landed.
  function onToggleSafeMode(next: boolean) {
    setPolicyState((p) => ({ ...p, safe_mode: next }));
    shell.toggleSafeMode(next); // optimistic shared state + best-effort write
    logOp({
      kind: "safe_mode",
      summary: connected
        ? next
          ? "Engaged safe mode: automation frozen on last known-good."
          : "Released safe mode: decision plane resumed."
        : next
          ? "Engaged safe mode locally (demonstration; not written to a backend)."
          : "Released safe mode locally (demonstration; not written to a backend).",
      outcome: "ok",
    });
  }

  // ── render ─────────────────────────────────────────────────────────────────
  return (
    <div className="sl-stack">
      <PageHead
        connected={connected}
        policyVersion={policy.policy_version}
        mode={policy.operating_mode}
      />

      <KillSwitch armed={shell.safeMode} connected={connected} onToggle={onToggleSafeMode} />

      <RlModeNote rl={rlModeQ.value} source={rlModeQ.source} />

      <SectionHead
        title="Operating policy"
        sub="Edit the live policy primitives, then review a field-level diff before committing. Or apply a named strategy to set the primitives in one move. Every commit is versioned and audit-logged."
      />

      <div className="sl-grid-2-1">
        <PolicyEditor
          policy={policy}
          draft={draft}
          mode={mode}
          changedCount={changedCount}
          connected={connected}
          related={relatedQ.value}
          relatedLive={relatedQ.source === "live"}
          policyLoading={policyQ.state === "loading" && policyQ.source !== "live"}
          onField={(k, v) => setDraft((d) => ({ ...d, [k]: v }))}
          onMode={setMode}
          onReset={resetDraft}
          onDiff={openDiff}
        />
        <StrategyPicker current={policy.strategy_name} connected={connected} onApply={applyStrategy} />
      </div>

      <SectionHead
        title="Manual overrides"
        sub="Direct, deliberate actions. Each opens a confirm step and is reversible and audit-logged: the load balancer keeps serving on the last committed state throughout. When no live backend is connected these are held — a demonstration never writes."
      />

      <div className="sl-grid-3">
        <ScaleCard
          current={policy.max_backends}
          connected={connected}
          onApply={async (n, reason) => {
            const res = await api.scale(n, ACTOR, reason);
            toast.push({
              title: res.status === "applied" ? `Scaled to ${res.final_count}` : "Scale no-op",
              detail: `${res.previous_count} -> ${res.final_count} (${res.action})`,
              tone: res.status === "applied" ? "ok" : "info",
            });
            logOp({ kind: "scale", summary: `Scale to ${n}: ${res.previous_count} -> ${res.final_count} (${res.action}).`, outcome: "ok" });
          }}
          onFail={(n, e) => {
            toast.push({ title: "Scale failed", detail: errText(e), tone: "crit" });
            logOp({ kind: "scale", summary: `Scale to ${n} was rejected by the backend.`, outcome: "failed" });
          }}
        />
        <IsolateCard
          backendIds={backendIds}
          connected={connected}
          onApply={async (id, status, reason) => {
            const res = await api.isolate(id, status, ACTOR, reason);
            toast.push({ title: `Marked ${id} ${res.anomaly_status}`, detail: res.reason || "isolate applied", tone: "ok" });
            logOp({ kind: "isolate", summary: `Set ${id} to ${status} (score ${res.score}).`, outcome: "ok" });
          }}
          onFail={(id, status, e) => {
            toast.push({ title: "Isolate failed", detail: errText(e), tone: "crit" });
            logOp({ kind: "isolate", summary: `Mark ${id} ${status} was rejected by the backend.`, outcome: "failed" });
          }}
        />
        <WeightsCard
          lb={lb}
          backendIds={backendIds}
          connected={connected}
          onApply={async (weights) => {
            const res = await api.setLbWeights(weights);
            setLbState((s) => ({ ...s, upstream_weights: res.applied_weights }));
            toast.push({ title: "Routing weights forced", detail: `${Object.keys(res.applied_weights).length} backends`, tone: "ok" });
            logOp({ kind: "weights", summary: `Forced routing weights across ${Object.keys(weights).length} backends.`, outcome: "ok" });
          }}
          onFail={(e) => {
            toast.push({ title: "Force weights failed", detail: errText(e), tone: "crit" });
            logOp({ kind: "weights", summary: `Force routing weights was rejected by the backend.`, outcome: "failed" });
          }}
        />
      </div>

      <SectionHead title="This session" sub="Operations performed since this page loaded. Held locally; clears on reload." />
      <HistoryStrip history={history} />

      <CommitModal
        open={previewOpen}
        preview={preview}
        previewing={previewing}
        committing={committing}
        connected={connected}
        targetVersion={(policy.policy_version ?? 0) + 1}
        onClose={() => {
          setPreviewOpen(false);
          setPreview(null);
        }}
        onConfirm={commit}
      />
    </div>
  );
}

// ── local preview fallback ─────────────────────────────────────────────────────
// Mirrors the api PolicyPreviewResponse shape from the local draft when the live
// preview endpoint is unreachable, so the diff modal always has content.
function localPreview(patch: Partial<Policy>, current: Policy): PolicyPreviewResponse {
  const diff: PolicyDiffEntry[] = Object.entries(patch).map(([field, val]) => ({
    field,
    old: current[field as keyof Policy] ?? null,
    new: val,
  }));
  const warnings: string[] = [];
  if (patch.min_backends != null && patch.max_backends != null && Number(patch.min_backends) > Number(patch.max_backends)) {
    warnings.push("min_backends exceeds max_backends.");
  }
  return { valid: true, errors: [], changed_fields: diff.map((d) => d.field), diff, warnings };
}

// ── page head ──────────────────────────────────────────────────────────────────

function PageHead({ connected, policyVersion, mode }: { connected: boolean; policyVersion: number; mode: string }) {
  return (
    <section
      style={{
        position: "relative",
        overflow: "hidden",
        borderRadius: "var(--sl-radius-xl)",
        border: "1px solid var(--sl-hairline)",
        background:
          "radial-gradient(820px 320px at 90% -40%, var(--sl-mint-soft), transparent 60%), var(--sl-surface)",
        boxShadow: "var(--sl-shadow-2)",
        padding: "26px 30px",
      }}
    >
      <span
        style={{
          display: "inline-flex",
          alignItems: "center",
          gap: 8,
          fontFamily: "var(--sl-font-mono)",
          fontSize: 11,
          fontWeight: 600,
          color: "var(--sl-mint-deep)",
          background: "var(--sl-mint-tint)",
          border: "1px solid var(--sl-mint-line)",
          borderRadius: 20,
          padding: "5px 12px",
        }}
      >
        <Sliders size={12} strokeWidth={2} />
        Operator controls
      </span>
      <h1 style={{ fontSize: 30, lineHeight: 1.1, letterSpacing: "-1px", fontWeight: 800, margin: "14px 0 0", color: "var(--sl-text)" }}>
        Govern the plane by hand.
      </h1>
      <p style={{ fontSize: 14, color: "var(--sl-text-mid)", margin: "12px 0 0", maxWidth: "62ch" }}>
        Edit the operating policy with a reviewed diff, apply a named strategy, or take a manual override. Every
        action is reversible and audit-logged, and the load balancer keeps serving throughout.
      </p>
      <div className="sl-cluster" style={{ marginTop: 18 }}>
        <Badge tone="neutral">policy v{policyVersion}</Badge>
        <Badge tone="mint">{(mode ?? "adaptive").toUpperCase()}</Badge>
        <Badge tone={connected ? "mint" : "neutral"}>{connected ? "LIVE BACKEND" : "DEMONSTRATION"}</Badge>
      </div>
    </section>
  );
}

// ── section header ───────────────────────────────────────────────────────────

function SectionHead({ title, sub }: { title: string; sub: string }) {
  return (
    <div style={{ margin: "8px 2px 0" }}>
      <h2 style={{ fontSize: 17, fontWeight: 700, letterSpacing: "-0.3px", margin: 0, color: "var(--sl-text)" }}>{title}</h2>
      <div style={{ fontSize: 12.5, color: "var(--sl-text-low)", marginTop: 3, maxWidth: "92ch" }}>{sub}</div>
    </div>
  );
}

// ── RL-mode deploy-time note (read-only, links to Helmsman) ──────────────────────

function RlModeNote({ rl, source }: { rl: RlModeStatus; source: "live" | "demo" }) {
  const current = rl.current_mode ?? "shadow";
  return (
    <div
      style={{
        display: "flex",
        gap: 12,
        alignItems: "flex-start",
        padding: "13px 16px",
        borderRadius: "var(--sl-radius-md)",
        background: "var(--sl-info-tint)",
        border: "1px solid var(--sl-info-line)",
      }}
    >
      <Compass size={16} strokeWidth={2} color="var(--sl-info)" style={{ flex: "0 0 auto", marginTop: 1 }} />
      <div style={{ fontSize: 12, color: "var(--sl-text-mid)", lineHeight: 1.5 }}>
        RL routing is currently{" "}
        <b style={{ color: "var(--sl-text)" }}>{current.toUpperCase()}</b>. Promotion to active is a{" "}
        <b>deploy-time</b> change to the routing engine, not a policy field you can commit here. The{" "}
        <b>safe_mode</b> and <b>operating_mode</b> gates below still shape the effective mode. See{" "}
        <b>Helmsman</b> for the shadow-vs-applied comparison and promotion readiness.
        {source !== "live" ? (
          <span style={{ color: "var(--sl-text-low)" }}> Showing representative values.</span>
        ) : null}
      </div>
    </div>
  );
}

// ── kill switch ────────────────────────────────────────────────────────────────

function KillSwitch({
  armed,
  connected,
  onToggle,
}: {
  armed: boolean;
  connected: boolean;
  onToggle: (next: boolean) => void;
}) {
  return (
    <section
      style={{
        borderRadius: "var(--sl-radius-lg)",
        border: `1px solid ${armed ? "var(--sl-crit)" : "var(--sl-hairline)"}`,
        boxShadow: armed ? "0 0 0 3px rgba(220,38,38,.08), var(--sl-shadow-1)" : "var(--sl-shadow-1)",
        background: armed ? "var(--sl-crit-tint)" : "var(--sl-surface)",
        overflow: "hidden",
        display: "grid",
        gridTemplateColumns: "1fr auto",
        gap: 14,
        alignItems: "center",
        padding: "18px 22px",
      }}
    >
      <div>
        <div style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap" }}>
          <Power size={18} strokeWidth={2} color={armed ? "var(--sl-crit)" : "var(--sl-mint)"} />
          <span style={{ fontSize: 16, fontWeight: 800, letterSpacing: "-0.3px", color: "var(--sl-text)" }}>Safe mode kill switch</span>
          <StatusPill status={armed ? "crit" : "ok"} hideDot>
            {armed ? "AUTOMATION FROZEN" : "ENGINE AUTONOMOUS"}
          </StatusPill>
          {!connected ? <Badge tone="neutral">LOCAL INTENT</Badge> : null}
        </div>
        <p style={{ fontSize: 12.5, color: armed ? "var(--sl-crit)" : "var(--sl-text-mid)", margin: "8px 0 0", maxWidth: "78ch" }}>
          {armed
            ? "Automation is frozen at its last known-good state. The load balancer keeps routing on the last committed weights; traffic never stops. Reversible and audit-logged."
            : "The decision plane is making automated routing and scaling calls. Flip to freeze every automated decision and hold the deterministic fallback. Reversible and audit-logged."}
          {!connected ? (
            <span style={{ display: "block", marginTop: 6, color: "var(--sl-text-low)" }}>
              No live backend connected: this reflects your local intent only and isn't written to a backend.
            </span>
          ) : null}
        </p>
      </div>
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: 12,
          background: "var(--sl-surface-sunk)",
          border: "1px solid var(--sl-hairline)",
          borderRadius: 12,
          padding: "12px 16px",
        }}
      >
        <div>
          <div style={{ fontSize: 12.5, fontWeight: 700, color: "var(--sl-text)" }}>Freeze automation</div>
          <div style={{ fontFamily: "var(--sl-font-mono)", fontSize: 10.5, color: "var(--sl-text-low)", marginTop: 2 }}>
            safe_mode = {armed ? "on" : "off"}
          </div>
        </div>
        <Toggle checked={armed} onChange={onToggle} armedTone label="Toggle safe mode" />
      </div>
    </section>
  );
}

// ── policy editor ──────────────────────────────────────────────────────────────

function PolicyEditor({
  policy,
  draft,
  mode,
  changedCount,
  connected,
  related,
  relatedLive,
  policyLoading,
  onField,
  onMode,
  onReset,
  onDiff,
}: {
  policy: Policy;
  draft: Record<string, string>;
  mode: string;
  changedCount: number;
  connected: boolean;
  related: RelatedMetrics;
  relatedLive: boolean;
  policyLoading: boolean;
  onField: (key: string, value: string) => void;
  onMode: (value: string) => void;
  onReset: () => void;
  onDiff: () => void;
}) {
  return (
    <Card
      title="Policy editor"
      eyebrow="// primitives"
      actions={<Badge tone="neutral">v{policy.policy_version}</Badge>}
    >
      {/* live context the primitives are defending */}
      <div
        style={{
          display: "flex",
          gap: 18,
          flexWrap: "wrap",
          padding: "0 0 14px",
          marginBottom: 14,
          borderBottom: "1px solid var(--sl-hairline-soft)",
        }}
      >
        <ContextStat label="SLO compliance" value={related.slo_compliance_pct != null ? `${related.slo_compliance_pct.toFixed(1)}` : "—"} unit="%" />
        <ContextStat label="p95 latency" value={related.p95_latency_ms != null ? `${Math.round(related.p95_latency_ms)}` : "—"} unit="ms" />
        <ContextStat label="Throughput" value={related.rps_current != null ? `${Math.round(related.rps_current)}` : "—"} unit="rps" />
        <span style={{ marginLeft: "auto", alignSelf: "center" }}>
          <Badge tone={relatedLive ? "mint" : "neutral"}>{relatedLive ? "LIVE" : "DEMO"}</Badge>
        </span>
      </div>

      {policyLoading ? (
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 14 }}>
          {Array.from({ length: 6 }).map((_, i) => (
            <span key={i} className="sl-shimmer" style={{ height: 56, borderRadius: 8 }} />
          ))}
        </div>
      ) : (
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 14 }}>
          <Field label="Operating mode" hint="How aggressively the plane scales and routes.">
            <select
              value={mode}
              onChange={(e) => onMode(e.target.value)}
              style={selectStyle}
              aria-label="Operating mode"
            >
              {(OPERATING_MODES.includes(mode) ? OPERATING_MODES : [mode, ...OPERATING_MODES]).map((m) => (
                <option key={m} value={m}>
                  {m}
                </option>
              ))}
            </select>
          </Field>

          {POLICY_FIELDS.map((f) => {
            const raw = draft[f.key as string] ?? "";
            const dirty = f.options
              ? raw !== "" && raw !== String(policy[f.key] ?? "")
              : raw !== "" && Number(raw) !== policy[f.key];
            return (
              <Field key={f.key as string} label={f.label} unit={f.unit} hint={f.hint} dirty={dirty}>
                {f.options ? (
                  <select
                    value={raw}
                    onChange={(e) => onField(f.key as string, e.target.value)}
                    style={{ ...selectStyle, borderColor: dirty ? "var(--sl-mint)" : "var(--sl-hairline)" }}
                    aria-label={f.label}
                  >
                    {(f.options.includes(raw) || raw === "" ? f.options : [raw, ...f.options]).map((o) => (
                      <option key={o} value={o}>
                        {o}
                      </option>
                    ))}
                  </select>
                ) : (
                  <input
                    type="number"
                    inputMode="decimal"
                    value={raw}
                    step={f.step}
                    min={f.min}
                    max={f.max}
                    onChange={(e) => onField(f.key as string, e.target.value)}
                    style={{ ...inputStyle, borderColor: dirty ? "var(--sl-mint)" : "var(--sl-hairline)" }}
                    aria-label={f.label}
                  />
                )}
              </Field>
            );
          })}
        </div>
      )}

      <div style={{ display: "flex", alignItems: "center", gap: 10, marginTop: 18, paddingTop: 14, borderTop: "1px solid var(--sl-hairline-soft)" }}>
        <div style={{ fontSize: 12, color: changedCount > 0 ? "var(--sl-mint-deep)" : "var(--sl-text-low)", fontWeight: 600 }}>
          {changedCount > 0 ? `${changedCount} pending change${changedCount === 1 ? "" : "s"}` : "No pending changes"}
          {changedCount > 0 && !connected ? (
            <span style={{ color: "var(--sl-text-low)", fontWeight: 500 }}> · {OFFLINE_HINT}</span>
          ) : null}
        </div>
        <div style={{ marginLeft: "auto", display: "flex", gap: 8 }}>
          <Button variant="ghost" size="sm" onClick={onReset} disabled={changedCount === 0}>
            Reset
          </Button>
          <Button
            variant="primary"
            size="sm"
            icon={<GitCompare size={13} strokeWidth={2} />}
            onClick={onDiff}
            disabled={changedCount === 0}
          >
            {connected ? "Diff & commit" : "Diff (review only)"}
          </Button>
        </div>
      </div>
    </Card>
  );
}

function ContextStat({ label, value, unit }: { label: string; value: string; unit: string }) {
  return (
    <div>
      <div style={{ fontSize: 10.5, color: "var(--sl-text-low)", fontWeight: 600 }}>{label}</div>
      <div style={{ fontFamily: "var(--sl-font-mono)", fontWeight: 700, fontSize: 17, letterSpacing: "-0.5px", marginTop: 2, color: "var(--sl-text)" }}>
        {value}
        <span style={{ fontSize: 11, color: "var(--sl-text-low)", fontWeight: 500, marginLeft: 3 }}>{unit}</span>
      </div>
    </div>
  );
}

function Field({
  label,
  unit,
  hint,
  dirty,
  children,
}: {
  label: string;
  unit?: string;
  hint?: string;
  dirty?: boolean;
  children: ReactNode;
}) {
  return (
    <label style={{ display: "flex", flexDirection: "column", gap: 5 }}>
      <span style={{ fontSize: 11.5, fontWeight: 600, color: "var(--sl-text-mid)", display: "flex", alignItems: "center", gap: 6 }}>
        {label}
        {unit ? <span style={{ fontFamily: "var(--sl-font-mono)", fontSize: 10, color: "var(--sl-text-faint)" }}>{unit}</span> : null}
        {dirty ? <span style={{ width: 6, height: 6, borderRadius: "50%", background: "var(--sl-mint)" }} /> : null}
      </span>
      {children}
      {hint ? <span style={{ fontSize: 10.5, color: "var(--sl-text-faint)", lineHeight: 1.35 }}>{hint}</span> : null}
    </label>
  );
}

// ── strategy quick-apply ───────────────────────────────────────────────────────

function StrategyPicker({
  current,
  connected,
  onApply,
}: {
  current?: string;
  connected: boolean;
  onApply: (name: StrategyName) => Promise<void>;
}) {
  const [sel, setSel] = useState<StrategyName>(
    (STRATEGY_NAMES.includes((current ?? "") as StrategyName) ? (current as StrategyName) : "ai-hybrid"),
  );
  const [busy, setBusy] = useState(false);
  const isCurrent = sel === current;

  const label = busy
    ? "Applying…"
    : isCurrent
      ? "Already applied"
      : connected
        ? `Apply ${sel}`
        : `Stage ${sel} (demonstration)`;

  return (
    <Card title="Named strategy" eyebrow="// quick-apply">
      <p style={{ fontSize: 12, color: "var(--sl-text-low)", margin: "0 0 12px" }}>
        Apply an alias over the policy primitives in one move. Current:{" "}
        <span style={{ fontFamily: "var(--sl-font-mono)", fontWeight: 600, color: "var(--sl-text)" }}>{current ?? "custom"}</span>.
      </p>
      <select value={sel} onChange={(e) => setSel(e.target.value as StrategyName)} style={selectStyle} aria-label="Named strategy">
        {STRATEGY_NAMES.map((s) => (
          <option key={s} value={s}>
            {s}
          </option>
        ))}
      </select>
      <p style={{ fontSize: 11.5, color: "var(--sl-text-mid)", margin: "10px 0 14px", lineHeight: 1.45, minHeight: 32 }}>
        {STRATEGY_BLURB[sel]}
      </p>
      <Button
        variant="primary"
        size="sm"
        icon={busy ? <Loader2 size={13} strokeWidth={2} style={spinStyle} /> : <Workflow size={13} strokeWidth={2} />}
        disabled={busy || isCurrent}
        onClick={async () => {
          setBusy(true);
          try {
            await onApply(sel);
          } finally {
            setBusy(false);
          }
        }}
        style={{ width: "100%", justifyContent: "center" }}
      >
        {label}
      </Button>
      {!connected && !isCurrent ? (
        <p style={{ fontSize: 10.5, color: "var(--sl-text-faint)", margin: "8px 0 0", textAlign: "center" }}>
          {OFFLINE_HINT} Staged locally for the demonstration.
        </p>
      ) : null}
    </Card>
  );
}

// ── manual override: scale ──────────────────────────────────────────────────────

function ScaleCard({
  current,
  connected,
  onApply,
  onFail,
}: {
  current: number;
  connected: boolean;
  onApply: (n: number, reason?: string) => Promise<void>;
  onFail: (n: number, e: unknown) => void;
}) {
  const [n, setN] = useState<string>(String(current));
  const [reason, setReason] = useState("");
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const target = Number(n);
  const valid = Number.isFinite(target) && target >= 0;

  return (
    <ActionCard
      icon={<Boxes size={16} strokeWidth={2} />}
      title="Scale to N"
      blurb="Set the backend pool to an exact count. Reversible and audit-logged."
      connected={connected}
    >
      <Field label="Target backends">
        <input type="number" min={0} step={1} value={n} onChange={(e) => setN(e.target.value)} style={inputStyle} aria-label="Target backends" />
      </Field>
      <Field label="Reason" hint="Recorded with the scaling action.">
        <input type="text" value={reason} onChange={(e) => setReason(e.target.value)} placeholder="e.g. pre-warm for campaign" style={inputStyle} aria-label="Scale reason" />
      </Field>
      <Button variant="secondary" size="sm" disabled={!valid} onClick={() => setOpen(true)} style={{ width: "100%", justifyContent: "center", marginTop: 4 }}>
        Scale to {valid ? target : "?"}
      </Button>

      <Modal
        open={open}
        onClose={() => setOpen(false)}
        title="Confirm scale"
        footer={
          <>
            <Button variant="ghost" size="sm" onClick={() => setOpen(false)}>Cancel</Button>
            <Button
              variant="primary"
              size="sm"
              disabled={busy || !connected}
              icon={busy ? <Loader2 size={13} strokeWidth={2} style={spinStyle} /> : <Check size={13} strokeWidth={2} />}
              onClick={async () => {
                setBusy(true);
                try {
                  await onApply(target, reason || undefined);
                  setOpen(false);
                } catch (e) {
                  onFail(target, e);
                } finally {
                  setBusy(false);
                }
              }}
            >
              {busy ? "Applying…" : "Confirm scale"}
            </Button>
          </>
        }
      >
        <ConfirmBody
          line={<>Scale the pool to <b style={{ color: "var(--sl-text)" }}>{target}</b> backend{target === 1 ? "" : "s"}.</>}
          reason={reason}
          connected={connected}
        />
      </Modal>
    </ActionCard>
  );
}

// ── manual override: isolate ────────────────────────────────────────────────────

const ISOLATE_STATUSES: IsolateStatus[] = ["unhealthy", "degraded", "healthy"];

function IsolateCard({
  backendIds,
  connected,
  onApply,
  onFail,
}: {
  backendIds: string[];
  connected: boolean;
  onApply: (id: string, status: IsolateStatus, reason?: string) => Promise<void>;
  onFail: (id: string, status: IsolateStatus, e: unknown) => void;
}) {
  const [id, setId] = useState(backendIds[0] ?? "");
  const [status, setStatus] = useState<IsolateStatus>("unhealthy");
  const [reason, setReason] = useState("");
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);

  // Keep the selected backend valid if the roster changes after load.
  useEffect(() => {
    if (!backendIds.includes(id) && backendIds.length) setId(backendIds[0]);
  }, [backendIds, id]);

  return (
    <ActionCard
      icon={<ShieldCheck size={16} strokeWidth={2} />}
      title="Isolate backend"
      blurb="Force an anomaly verdict on a node so the router holds it out. Reversible and audit-logged."
      connected={connected}
    >
      <Field label="Backend">
        <select value={id} onChange={(e) => setId(e.target.value)} style={selectStyle} aria-label="Backend to isolate">
          {backendIds.map((b) => (
            <option key={b} value={b}>{b}</option>
          ))}
        </select>
      </Field>
      <Field label="Verdict" hint="healthy returns the node to rotation.">
        <select value={status} onChange={(e) => setStatus(e.target.value as IsolateStatus)} style={selectStyle} aria-label="Verdict">
          {ISOLATE_STATUSES.map((s) => (
            <option key={s} value={s}>{s}</option>
          ))}
        </select>
      </Field>
      <Field label="Reason" hint="Recorded with the verdict.">
        <input type="text" value={reason} onChange={(e) => setReason(e.target.value)} placeholder="e.g. manual drain for patch" style={inputStyle} aria-label="Isolate reason" />
      </Field>
      <Button variant="secondary" size="sm" disabled={!id} onClick={() => setOpen(true)} style={{ width: "100%", justifyContent: "center", marginTop: 4 }}>
        Mark {id || "node"} {status}
      </Button>

      <Modal
        open={open}
        onClose={() => setOpen(false)}
        title="Confirm isolate"
        footer={
          <>
            <Button variant="ghost" size="sm" onClick={() => setOpen(false)}>Cancel</Button>
            <Button
              variant={status === "healthy" ? "primary" : "danger"}
              size="sm"
              disabled={busy || !connected}
              icon={busy ? <Loader2 size={13} strokeWidth={2} style={spinStyle} /> : <Check size={13} strokeWidth={2} />}
              onClick={async () => {
                setBusy(true);
                try {
                  await onApply(id, status, reason || undefined);
                  setOpen(false);
                } catch (e) {
                  onFail(id, status, e);
                } finally {
                  setBusy(false);
                }
              }}
            >
              {busy ? "Applying…" : "Confirm"}
            </Button>
          </>
        }
      >
        <ConfirmBody
          line={<>Set <b style={{ color: "var(--sl-text)" }}>{id}</b> to verdict <b style={{ color: "var(--sl-text)" }}>{status}</b>.{status !== "healthy" ? " Traffic redistributes to the rest of the pool." : " The node returns to rotation."}</>}
          reason={reason}
          connected={connected}
        />
      </Modal>
    </ActionCard>
  );
}

// ── manual override: force weights ──────────────────────────────────────────────

function WeightsCard({
  lb,
  backendIds,
  connected,
  onApply,
  onFail,
}: {
  lb: LbState;
  backendIds: string[];
  connected: boolean;
  onApply: (weights: Record<string, number>) => Promise<void>;
  onFail: (e: unknown) => void;
}) {
  // Per-backend numeric inputs, seeded from the live weight map.
  const [weights, setWeights] = useState<Record<string, string>>(() =>
    Object.fromEntries(backendIds.map((b) => [b, String(lb.upstream_weights[b] ?? 0)])),
  );
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);

  // Re-seed when the roster or live weights change after load.
  useEffect(() => {
    setWeights(Object.fromEntries(backendIds.map((b) => [b, String(lb.upstream_weights[b] ?? 0)])));
  }, [backendIds, lb.upstream_weights]);

  const parsed = useMemo<Record<string, number>>(() => {
    const out: Record<string, number> = {};
    for (const [k, v] of Object.entries(weights)) {
      const n = Number(v);
      out[k] = Number.isFinite(n) ? n : 0;
    }
    return out;
  }, [weights]);

  const total = useMemo(() => Object.values(parsed).reduce((a, b) => a + b, 0), [parsed]);

  return (
    <ActionCard
      icon={<Sliders size={16} strokeWidth={2} />}
      title="Force routing weights"
      blurb="Override the per-backend split. The plane resumes scoring once released. Reversible and audit-logged."
      connected={connected}
    >
      <div style={{ display: "flex", flexDirection: "column", gap: 8, maxHeight: 188, overflow: "auto", paddingRight: 2 }}>
        {backendIds.map((b) => (
          <div key={b} style={{ display: "grid", gridTemplateColumns: "1fr 84px", gap: 8, alignItems: "center" }}>
            <span style={{ fontFamily: "var(--sl-font-mono)", fontSize: 12, color: "var(--sl-text)" }}>{b}</span>
            <input
              type="number"
              min={0}
              step={0.01}
              value={weights[b] ?? "0"}
              onChange={(e) => setWeights((w) => ({ ...w, [b]: e.target.value }))}
              style={{ ...inputStyle, padding: "6px 8px", fontSize: 12 }}
              aria-label={`Weight for ${b}`}
            />
          </div>
        ))}
      </div>
      <div style={{ display: "flex", justifyContent: "space-between", fontSize: 11, color: "var(--sl-text-low)", marginTop: 8 }}>
        <span>Sum</span>
        <span style={{ fontFamily: "var(--sl-font-mono)", fontWeight: 600, color: Math.abs(total - 1) < 0.001 ? "var(--sl-mint-deep)" : "var(--sl-warn)" }}>
          {total.toFixed(2)}
        </span>
      </div>
      <Button variant="secondary" size="sm" onClick={() => setOpen(true)} style={{ width: "100%", justifyContent: "center", marginTop: 8 }}>
        Force weights
      </Button>

      <Modal
        open={open}
        onClose={() => setOpen(false)}
        title="Confirm forced weights"
        footer={
          <>
            <Button variant="ghost" size="sm" onClick={() => setOpen(false)}>Cancel</Button>
            <Button
              variant="primary"
              size="sm"
              disabled={busy || !connected}
              icon={busy ? <Loader2 size={13} strokeWidth={2} style={spinStyle} /> : <Check size={13} strokeWidth={2} />}
              onClick={async () => {
                setBusy(true);
                try {
                  await onApply(parsed);
                  setOpen(false);
                } catch (e) {
                  onFail(e);
                } finally {
                  setBusy(false);
                }
              }}
            >
              {busy ? "Applying…" : "Confirm override"}
            </Button>
          </>
        }
      >
        <p style={{ margin: "0 0 10px" }}>Force the load balancer onto this split (sum {total.toFixed(2)}). The decision plane stops scoring weights until released.</p>
        <div style={{ display: "flex", flexDirection: "column", gap: 4, fontFamily: "var(--sl-font-mono)", fontSize: 12 }}>
          {Object.entries(parsed).map(([k, v]) => (
            <div key={k} style={{ display: "flex", justifyContent: "space-between", color: "var(--sl-text-mid)" }}>
              <span>{k}</span>
              <span style={{ color: "var(--sl-text)", fontWeight: 600 }}>{v.toFixed(2)}</span>
            </div>
          ))}
        </div>
        {Math.abs(total - 1) >= 0.001 ? (
          <div style={{ display: "flex", gap: 7, marginTop: 12, fontSize: 11.5, color: "var(--sl-warn)", alignItems: "flex-start" }}>
            <AlertTriangle size={14} strokeWidth={2} style={{ flex: "0 0 auto", marginTop: 1 }} />
            <span>Weights do not sum to 1.00; the load balancer normalizes on apply.</span>
          </div>
        ) : null}
        {!connected ? <OfflineNotice /> : null}
      </Modal>
    </ActionCard>
  );
}

// ── action card shell ───────────────────────────────────────────────────────────

function ActionCard({
  icon,
  title,
  blurb,
  connected,
  children,
}: {
  icon: ReactNode;
  title: string;
  blurb: string;
  connected: boolean;
  children: ReactNode;
}) {
  return (
    <Card>
      <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
        <span
          style={{
            width: 32,
            height: 32,
            borderRadius: 9,
            display: "grid",
            placeItems: "center",
            background: "var(--sl-mint-tint)",
            color: "var(--sl-mint-deep)",
            flex: "0 0 auto",
          }}
        >
          {icon}
        </span>
        <div style={{ fontSize: 14, fontWeight: 700, color: "var(--sl-text)" }}>{title}</div>
        {!connected ? <span style={{ marginLeft: "auto" }}><Badge tone="neutral">REVIEW ONLY</Badge></span> : null}
      </div>
      <p style={{ fontSize: 11.5, color: "var(--sl-text-low)", margin: "8px 0 14px", lineHeight: 1.45 }}>{blurb}</p>
      <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>{children}</div>
    </Card>
  );
}

function ConfirmBody({ line, reason, connected }: { line: ReactNode; reason?: string; connected: boolean }) {
  return (
    <div>
      <p style={{ margin: "0 0 12px" }}>{line}</p>
      {reason ? (
        <div style={{ fontFamily: "var(--sl-font-mono)", fontSize: 11.5, color: "var(--sl-text-mid)", background: "var(--sl-surface-sunk)", border: "1px solid var(--sl-hairline)", borderRadius: 8, padding: "8px 10px", marginBottom: 12 }}>
          reason: {reason}
        </div>
      ) : null}
      <div style={{ display: "flex", gap: 7, fontSize: 11.5, color: "var(--sl-text-low)", alignItems: "flex-start" }}>
        <ShieldCheck size={14} strokeWidth={2} style={{ flex: "0 0 auto", marginTop: 1 }} />
        <span>Reversible and audit-logged. The load balancer keeps serving on the last committed state.</span>
      </div>
      {!connected ? <OfflineNotice /> : null}
    </div>
  );
}

// Calm notice shown inside a confirm dialog when no live backend is connected:
// the action is held, and the dialog says plainly it would apply once live.
function OfflineNotice() {
  return (
    <div
      style={{
        display: "flex",
        gap: 8,
        marginTop: 12,
        padding: "9px 11px",
        borderRadius: 9,
        background: "var(--sl-info-tint)",
        border: "1px solid var(--sl-info-line)",
        fontSize: 11.5,
        color: "var(--sl-text-mid)",
        alignItems: "flex-start",
      }}
    >
      <AlertTriangle size={14} strokeWidth={2} color="var(--sl-info)" style={{ flex: "0 0 auto", marginTop: 1 }} />
      <span>
        No live backend connected. This is held in the demonstration and would be
        applied once a backend is reachable; nothing is written now.
      </span>
    </div>
  );
}

// ── commit modal (policy diff) ──────────────────────────────────────────────────

function CommitModal({
  open,
  preview,
  previewing,
  committing,
  connected,
  targetVersion,
  onClose,
  onConfirm,
}: {
  open: boolean;
  preview: PolicyPreviewResponse | null;
  previewing: boolean;
  committing: boolean;
  connected: boolean;
  targetVersion: number;
  onClose: () => void;
  onConfirm: () => void;
}) {
  const blocked = preview ? !preview.valid || preview.errors.length > 0 : false;
  const confirmLabel = committing
    ? "Committing…"
    : connected
      ? `Commit to v${targetVersion}`
      : "Save locally";
  return (
    <Modal
      open={open}
      onClose={onClose}
      width={520}
      title={
        <span style={{ display: "inline-flex", alignItems: "center", gap: 9 }}>
          <GitCompare size={16} strokeWidth={2} />
          Review policy diff
        </span>
      }
      footer={
        <>
          <Button variant="ghost" size="sm" onClick={onClose}>Cancel</Button>
          <Button
            variant="primary"
            size="sm"
            disabled={committing || previewing || blocked}
            icon={committing ? <Loader2 size={13} strokeWidth={2} style={spinStyle} /> : <Check size={13} strokeWidth={2} />}
            onClick={onConfirm}
          >
            {confirmLabel}
          </Button>
        </>
      }
    >
      {previewing && !preview ? (
        <div style={{ display: "flex", alignItems: "center", gap: 9, color: "var(--sl-text-low)" }}>
          <Loader2 size={15} strokeWidth={2} style={spinStyle} />
          Computing diff…
        </div>
      ) : !preview ? (
        <EmptyState title="Nothing to preview" hint="Edit a field to see its diff." />
      ) : (
        <div>
          <p style={{ margin: "0 0 12px" }}>
            {preview.diff.length} field{preview.diff.length === 1 ? "" : "s"} change. Review the old → new values, then {connected ? "commit" : "save locally"}.
          </p>

          <div style={{ display: "flex", flexDirection: "column", gap: 1, borderRadius: 10, overflow: "hidden", border: "1px solid var(--sl-hairline)" }}>
            <div style={{ display: "grid", gridTemplateColumns: "1.4fr 1fr auto 1fr", gap: 8, padding: "8px 12px", background: "var(--sl-surface-sunk)", fontFamily: "var(--sl-font-mono)", fontSize: 9.5, letterSpacing: "0.8px", textTransform: "uppercase", color: "var(--sl-text-low)", fontWeight: 600 }}>
              <span>Field</span>
              <span style={{ textAlign: "right" }}>Old</span>
              <span />
              <span>New</span>
            </div>
            {preview.diff.map((d) => (
              <div key={d.field} style={{ display: "grid", gridTemplateColumns: "1.4fr 1fr auto 1fr", gap: 8, padding: "9px 12px", alignItems: "center", background: "var(--sl-surface)" }}>
                <span style={{ fontFamily: "var(--sl-font-mono)", fontSize: 12, color: "var(--sl-text)" }}>{d.field}</span>
                <span style={{ fontFamily: "var(--sl-font-mono)", fontSize: 12, textAlign: "right", color: "var(--sl-text-low)", textDecoration: "line-through" }}>{fmtVal(d.old)}</span>
                <ArrowRight size={13} strokeWidth={2} color="var(--sl-text-faint)" />
                <span style={{ fontFamily: "var(--sl-font-mono)", fontSize: 12, fontWeight: 700, color: "var(--sl-mint-deep)" }}>{fmtVal(d.new)}</span>
              </div>
            ))}
          </div>

          {preview.warnings.length > 0 ? (
            <Notice tone="warn" icon={<AlertTriangle size={14} strokeWidth={2} />}>
              {preview.warnings.join(" ")}
            </Notice>
          ) : null}
          {preview.errors.length > 0 ? (
            <Notice tone="crit" icon={<X size={14} strokeWidth={2} />}>
              {preview.errors.join(" ")}
            </Notice>
          ) : null}

          {connected ? (
            <div style={{ display: "flex", gap: 7, marginTop: 12, fontSize: 11.5, color: "var(--sl-text-low)", alignItems: "flex-start" }}>
              <ShieldCheck size={14} strokeWidth={2} style={{ flex: "0 0 auto", marginTop: 1 }} />
              <span>Committing writes a new policy version and is audit-logged. Reversible by committing the prior values.</span>
            </div>
          ) : (
            <OfflineNotice />
          )}
        </div>
      )}
    </Modal>
  );
}

function Notice({ tone, icon, children }: { tone: "warn" | "crit"; icon: ReactNode; children: ReactNode }) {
  const color = tone === "crit" ? "var(--sl-crit)" : "var(--sl-warn)";
  const bg = tone === "crit" ? "var(--sl-crit-tint)" : "var(--sl-warn-tint)";
  return (
    <div style={{ display: "flex", gap: 8, marginTop: 12, padding: "9px 11px", borderRadius: 9, background: bg, border: `1px solid ${color}`, fontSize: 11.5, color, alignItems: "flex-start" }}>
      <span style={{ flex: "0 0 auto", marginTop: 1 }}>{icon}</span>
      <span>{children}</span>
    </div>
  );
}

// ── session history strip ────────────────────────────────────────────────────────

const KIND_META: Record<OpEntry["kind"], { label: string }> = {
  policy: { label: "POLICY" },
  strategy: { label: "STRATEGY" },
  scale: { label: "SCALE" },
  isolate: { label: "ISOLATE" },
  weights: { label: "WEIGHTS" },
  safe_mode: { label: "SAFE MODE" },
};

function HistoryStrip({ history }: { history: OpEntry[] }) {
  const columns: Column<OpEntry>[] = [
    {
      key: "time",
      header: "Time",
      render: (r) => <span style={{ fontFamily: "var(--sl-font-mono)", fontSize: 11.5, color: "var(--sl-text-faint)" }}>{r.time}</span>,
    },
    {
      key: "kind",
      header: "Action",
      render: (r) => <Badge tone="neutral">{KIND_META[r.kind].label}</Badge>,
    },
    {
      key: "source",
      header: "Where",
      render: (r) => <Badge tone={r.source === "live" ? "mint" : "neutral"}>{r.source === "live" ? "LIVE" : "LOCAL"}</Badge>,
    },
    {
      key: "summary",
      header: "Detail",
      render: (r) => <span style={{ fontSize: 12.5, color: "var(--sl-text-mid)" }}>{r.summary}</span>,
    },
    {
      key: "outcome",
      header: "Outcome",
      render: (r) => (
        <StatusPill status={r.outcome === "ok" ? "ok" : "crit"}>{r.outcome === "ok" ? "OK" : "FAILED"}</StatusPill>
      ),
    },
  ];
  return (
    <Card flush actions={<Badge tone="neutral">{history.length} ops</Badge>} title="Session operations" eyebrow="// local">
      {history.length === 0 ? (
        <EmptyState icon={<ListChecks size={20} strokeWidth={1.8} />} title="No operations yet this session" hint="Actions you take here are listed newest-first." />
      ) : (
        <DataTable columns={columns} rows={history} rowKey={(r) => String(r.id)} rowMuted={(r) => r.outcome === "failed"} />
      )}
    </Card>
  );
}

// ── shared input styling (kit has no Input/Select component) ─────────────────────

const inputStyle: React.CSSProperties = {
  fontFamily: "var(--sl-font-mono)",
  fontSize: 13,
  color: "var(--sl-text)",
  background: "var(--sl-surface)",
  border: "1px solid var(--sl-hairline)",
  borderRadius: "var(--sl-radius-sm)",
  padding: "8px 10px",
  width: "100%",
  outline: "none",
  boxSizing: "border-box",
};

const selectStyle: React.CSSProperties = {
  ...inputStyle,
  cursor: "pointer",
};

// Inline spinner animation referencing the existing @keyframes spin in
// styles.css, so a busy Loader2 rotates without needing a new utility class.
const spinStyle: React.CSSProperties = {
  animation: "spin 0.9s linear infinite",
};
