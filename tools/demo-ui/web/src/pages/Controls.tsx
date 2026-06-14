/**
 * tools/demo-ui/web/src/pages/Controls.tsx  (cockpit "Lab")
 * ──────────────────────────────────────────────────────────
 * The drive console for the demo, rebuilt on the shared kit (dark
 * "Mission Control" theme). Everything that lets an operator steer the
 * cluster by hand lives here:
 *   - Routing algorithm picker (Round-Robin / Least Conn / Random / AI-PPO).
 *   - Demo scenarios (backend failure / latency spike / recovery /
 *     high traffic / AI disabled).
 *   - Manual ops: degrade / recover a backend, traffic-level presets.
 *   - safe_mode kill switch (deliberate, reversible).
 *   - Danger zone: chaos injection + reset-all, both behind a confirm modal.
 *
 * Every action routes through useDemo().action(), which wraps the BFF call
 * with success / failure toast feedback and a shared busy flag — so a BFF
 * that is down surfaces as a red toast, never a crash. Selector state
 * (degrade target, chaos delay, etc.) is local to this page; it is
 * navigation-scoped, not session-scoped.
 */

import { useState } from "react";

import { api, type DemoAlgorithm, type DemoScenario } from "../api";
import { useDemo } from "../state/DemoStateContext";
import {
  Badge,
  Button,
  Card,
  Modal,
  StatusPill,
  Toggle,
} from "../ui";
import { shortName } from "../utils";


/* ── Static config ──────────────────────────────────────────────────────── */

const ALGORITHMS: { id: DemoAlgorithm; label: string; hint: string }[] = [
  { id: "round_robin", label: "Round-Robin", hint: "nginx" },
  { id: "least_conn", label: "Least Conn", hint: "nginx" },
  { id: "random", label: "Random", hint: "nginx" },
  { id: "ppo", label: "AI / PPO", hint: "rl-weighted" },
];

const SCENARIOS: {
  id: DemoScenario;
  label: string;
  desc: string;
  tone: "warn" | "crit" | "ok" | "neutral";
}[] = [
  {
    id: "backend_failure",
    label: "Backend Failure",
    desc: "Knock a backend offline and watch traffic reroute.",
    tone: "crit",
  },
  {
    id: "latency_spike",
    label: "Latency Spike",
    desc: "Inject tail latency on a backend.",
    tone: "warn",
  },
  {
    id: "recovery",
    label: "Recovery",
    desc: "Bring degraded backends back to healthy.",
    tone: "ok",
  },
  {
    id: "high_traffic",
    label: "High Traffic",
    desc: "Ramp the load generator to a heavy profile.",
    tone: "warn",
  },
  {
    id: "ai_disabled",
    label: "AI Disabled",
    desc: "Fall back to classical routing only.",
    tone: "neutral",
  },
];

const TRAFFIC_PRESETS: { label: string; users: number; rate: number }[] = [
  { label: "Off", users: 0, rate: 1 },
  { label: "Low", users: 5, rate: 1 },
  { label: "Med", users: 50, rate: 10 },
  { label: "High", users: 200, rate: 50 },
];

const CHAOS_DELAYS = [0, 200, 500, 800, 1500];

const PORT = ":8080";

/* Shared dark styling for native <select> elements (the kit has no Select). */
const selectStyle: React.CSSProperties = {
  background: "var(--sl-surface-sunk)",
  color: "var(--sl-text)",
  border: "1px solid var(--sl-hairline)",
  borderRadius: "var(--sl-radius-sm)",
  fontFamily: "var(--sl-font-mono)",
  fontSize: 12,
  padding: "8px 10px",
};

const fieldLabelStyle: React.CSSProperties = {
  fontFamily: "var(--sl-font-mono)",
  fontSize: 9.5,
  letterSpacing: "1.2px",
  textTransform: "uppercase",
  color: "var(--sl-text-low)",
};


export default function Controls() {
  const { state, busy, action } = useDemo();

  const backends: string[] = state?.backend_names ?? [];
  const firstBackend = backends[0] ?? "";
  const hasBackends = backends.length > 0;
  const safeMode = !!state?.safe_mode;
  const algorithm = state?.algorithm ?? null;

  // Local selector state — navigation-scoped.
  const [degradeTarget, setDegradeTarget] = useState("");
  const [degradeLevel, setDegradeLevel] = useState<"degraded" | "unhealthy">("unhealthy");
  const [recoverTarget, setRecoverTarget] = useState("");
  const [chaosTarget, setChaosTarget] = useState("");
  const [chaosDelayMs, setChaosDelayMs] = useState(800);

  // Deliberate-confirm modals for the destructive actions.
  const [confirmChaos, setConfirmChaos] = useState(false);
  const [confirmReset, setConfirmReset] = useState(false);

  const dTarget = degradeTarget || firstBackend;
  const rTarget = recoverTarget || firstBackend;
  const cTarget = chaosTarget || firstBackend;

  function runChaos() {
    setConfirmChaos(false);
    action(`Chaos ${shortName(cTarget)} ${chaosDelayMs}ms`, () =>
      api.demoChaos(cTarget, chaosDelayMs),
    );
  }

  function runReset() {
    setConfirmReset(false);
    action("Reset All", () => api.demoReset());
  }

  return (
    <>
      {/* ── Routing algorithm ───────────────────────────────────────────── */}
      <Card
        title="Routing algorithm"
        eyebrow="// load balancer"
        actions={
          <Badge tone={algorithm === "ppo" ? "mint" : "neutral"}>
            {algorithm ? `active: ${algorithm}` : "—"}
          </Badge>
        }
      >
        <div
          style={{
            display: "grid",
            gridTemplateColumns: "repeat(auto-fit, minmax(150px, 1fr))",
            gap: 10,
          }}
        >
          {ALGORITHMS.map((a) => {
            const active = algorithm === a.id;
            return (
              <button
                key={a.id}
                type="button"
                disabled={busy}
                onClick={() => action(`Algorithm: ${a.label}`, () => api.demoAlgorithm(a.id))}
                style={{
                  textAlign: "left",
                  cursor: busy ? "not-allowed" : "pointer",
                  background: active ? "var(--sl-mint-tint)" : "var(--sl-surface-sunk)",
                  border: `1px solid ${active ? "var(--sl-mint-line)" : "var(--sl-hairline)"}`,
                  borderRadius: "var(--sl-radius-md)",
                  padding: "12px 14px",
                  opacity: busy ? 0.6 : 1,
                  transition: "border-color var(--sl-dur-fast), background var(--sl-dur-fast)",
                }}
              >
                <div
                  style={{
                    display: "flex",
                    alignItems: "center",
                    justifyContent: "space-between",
                    gap: 8,
                  }}
                >
                  <span
                    style={{
                      fontSize: 13,
                      fontWeight: a.id === "ppo" ? 700 : 600,
                      color: active ? "var(--sl-mint)" : "var(--sl-text)",
                    }}
                  >
                    {a.label}
                  </span>
                  {active ? <StatusPill status="ok" hideDot>on</StatusPill> : null}
                </div>
                <div
                  style={{
                    fontFamily: "var(--sl-font-mono)",
                    fontSize: 9.5,
                    letterSpacing: "0.6px",
                    textTransform: "uppercase",
                    color: "var(--sl-text-low)",
                    marginTop: 6,
                  }}
                >
                  {a.hint}
                </div>
              </button>
            );
          })}
        </div>
        <p style={{ fontSize: 12, color: "var(--sl-text-low)", margin: "12px 0 0", lineHeight: 1.5 }}>
          Round-Robin, Least Conn and Random are served natively by NGINX. AI / PPO
          hands weight control to the RL engine.
        </p>
      </Card>

      {/* ── Scenarios ───────────────────────────────────────────────────── */}
      <Card title="Scenarios" eyebrow="// one-click playbooks">
        <div
          style={{
            display: "grid",
            gridTemplateColumns: "repeat(auto-fit, minmax(220px, 1fr))",
            gap: 12,
          }}
        >
          {SCENARIOS.map((s) => (
            <div
              key={s.id}
              style={{
                display: "flex",
                flexDirection: "column",
                gap: 10,
                background: "var(--sl-surface-sunk)",
                border: "1px solid var(--sl-hairline)",
                borderRadius: "var(--sl-radius-md)",
                padding: "14px",
              }}
            >
              <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                <StatusPill status={s.tone}>{s.label}</StatusPill>
              </div>
              <p
                style={{
                  fontSize: 12,
                  color: "var(--sl-text-mid)",
                  margin: 0,
                  lineHeight: 1.45,
                  flex: 1,
                }}
              >
                {s.desc}
              </p>
              <Button
                size="sm"
                variant={s.tone === "ok" ? "primary" : "secondary"}
                disabled={busy}
                onClick={() => action(s.label, () => api.demoScenario(s.id))}
                style={{ justifyContent: "center" }}
              >
                Run
              </Button>
            </div>
          ))}
        </div>
      </Card>

      {/* ── Manual controls + safe_mode ─────────────────────────────────── */}
      <div style={{ display: "grid", gridTemplateColumns: "1.6fr 1fr", gap: 18 }}>
        <Card
          title="Manual controls"
          eyebrow="// fault injection"
          actions={
            <Badge tone="neutral">
              {hasBackends ? `${backends.length} backends` : "no backends"}
            </Badge>
          }
        >
          <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
            {/* Degrade */}
            <div>
              <div style={fieldLabelStyle}>Degrade backend</div>
              <div style={{ display: "flex", gap: 8, alignItems: "center", marginTop: 6, flexWrap: "wrap" }}>
                <select
                  value={dTarget}
                  disabled={!hasBackends}
                  onChange={(e) => setDegradeTarget(e.target.value)}
                  style={{ ...selectStyle, flex: 1, minWidth: 130 }}
                >
                  {backends.map((b) => (
                    <option key={b} value={b}>{shortName(b)}</option>
                  ))}
                  {!hasBackends ? <option value="">—</option> : null}
                </select>
                <select
                  value={degradeLevel}
                  onChange={(e) => setDegradeLevel(e.target.value as "degraded" | "unhealthy")}
                  style={{ ...selectStyle, width: 120 }}
                >
                  <option value="unhealthy">unhealthy</option>
                  <option value="degraded">degraded</option>
                </select>
                <Button
                  size="sm"
                  variant="secondary"
                  disabled={busy || !hasBackends}
                  onClick={() => {
                    const target = dTarget + PORT;
                    action(`Degrade ${shortName(target)}`, () =>
                      api.demoDegrade(target, degradeLevel),
                    );
                  }}
                >
                  Apply
                </Button>
              </div>
            </div>

            {/* Recover */}
            <div>
              <div style={fieldLabelStyle}>Recover backend</div>
              <div style={{ display: "flex", gap: 8, alignItems: "center", marginTop: 6, flexWrap: "wrap" }}>
                <select
                  value={rTarget}
                  disabled={!hasBackends}
                  onChange={(e) => setRecoverTarget(e.target.value)}
                  style={{ ...selectStyle, flex: 1, minWidth: 130 }}
                >
                  {backends.map((b) => (
                    <option key={b} value={b}>{shortName(b)}</option>
                  ))}
                  {!hasBackends ? <option value="">—</option> : null}
                </select>
                <Button
                  size="sm"
                  variant="primary"
                  disabled={busy || !hasBackends}
                  onClick={() => {
                    const target = rTarget + PORT;
                    action(`Recover ${shortName(target)}`, () => api.demoRecover(target));
                  }}
                >
                  Recover
                </Button>
              </div>
            </div>

            {/* Traffic presets */}
            <div>
              <div style={fieldLabelStyle}>Traffic level</div>
              <div style={{ display: "flex", gap: 8, marginTop: 6, flexWrap: "wrap" }}>
                {TRAFFIC_PRESETS.map((t) => (
                  <Button
                    key={t.label}
                    size="sm"
                    variant={t.users === 0 ? "ghost" : "secondary"}
                    disabled={busy}
                    onClick={() =>
                      action(`Traffic ${t.label}`, () => api.demoTraffic(t.users, t.rate))
                    }
                  >
                    {t.label}
                  </Button>
                ))}
              </div>
            </div>
          </div>
        </Card>

        {/* safe_mode kill switch */}
        <Card title="safe_mode" eyebrow="// kill switch">
          <div
            style={{
              display: "flex",
              alignItems: "center",
              gap: 14,
              padding: "12px 14px",
              borderRadius: "var(--sl-radius-md)",
              border: `1px solid ${safeMode ? "var(--sl-crit)" : "var(--sl-hairline)"}`,
              background: safeMode ? "var(--sl-crit-tint)" : "var(--sl-surface-sunk)",
              boxShadow: safeMode ? "0 0 24px -8px var(--sl-crit)" : "none",
              transition: "border-color var(--sl-dur-mid), background var(--sl-dur-mid)",
            }}
          >
            <Toggle
              checked={safeMode}
              armedTone
              disabled={busy || state == null}
              label="Toggle safe_mode"
              onChange={(next) =>
                action(next ? "Enable Safe Mode" : "Activate AI", () => api.demoMode(next))
              }
            />
            <div style={{ lineHeight: 1.2 }}>
              <div style={{ fontSize: 13, fontWeight: 600, color: "var(--sl-text)" }}>
                {safeMode ? "Engaged" : "Automation engaged"}
              </div>
              <div
                style={{
                  fontFamily: "var(--sl-font-mono)",
                  fontSize: 9.5,
                  letterSpacing: "1px",
                  textTransform: "uppercase",
                  color: safeMode ? "var(--sl-crit)" : "var(--sl-text-low)",
                  marginTop: 3,
                }}
              >
                {safeMode ? "classical routing only" : "rl weights live"}
              </div>
            </div>
          </div>
          <p style={{ fontSize: 12, color: "var(--sl-text-low)", margin: "12px 0 0", lineHeight: 1.5 }}>
            Freeze automation, hold last-known-good weights and route classical only.
            Always reversible.
          </p>
        </Card>
      </div>

      {/* ── Danger zone ─────────────────────────────────────────────────── */}
      <Card
        title="Danger zone"
        eyebrow="// destructive · requires confirm"
        actions={<StatusPill status="crit" hideDot>handle with care</StatusPill>}
        style={{ borderColor: "var(--sl-crit)" }}
      >
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 16 }}>
          {/* Chaos */}
          <div
            style={{
              display: "flex",
              flexDirection: "column",
              gap: 10,
              border: "1px solid var(--sl-hairline)",
              borderRadius: "var(--sl-radius-md)",
              padding: 14,
            }}
          >
            <div style={{ fontSize: 13, fontWeight: 600, color: "var(--sl-text)" }}>
              Chaos injection
            </div>
            <p style={{ fontSize: 12, color: "var(--sl-text-low)", margin: 0, lineHeight: 1.45 }}>
              Force artificial delay onto a backend to stress the router.
            </p>
            <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
              <select
                value={cTarget}
                disabled={!hasBackends}
                onChange={(e) => setChaosTarget(e.target.value)}
                style={{ ...selectStyle, flex: 1, minWidth: 120 }}
              >
                {backends.map((b) => (
                  <option key={b} value={b}>{shortName(b)}</option>
                ))}
                {!hasBackends ? <option value="">—</option> : null}
              </select>
              <select
                value={chaosDelayMs}
                onChange={(e) => setChaosDelayMs(Number(e.target.value))}
                style={{ ...selectStyle, width: 96 }}
              >
                {CHAOS_DELAYS.map((d) => (
                  <option key={d} value={d}>{d}ms</option>
                ))}
              </select>
            </div>
            <Button
              size="sm"
              variant="danger"
              disabled={busy || !hasBackends}
              onClick={() => setConfirmChaos(true)}
              style={{ justifyContent: "center" }}
            >
              Inject chaos
            </Button>
          </div>

          {/* Reset all */}
          <div
            style={{
              display: "flex",
              flexDirection: "column",
              gap: 10,
              border: "1px solid var(--sl-hairline)",
              borderRadius: "var(--sl-radius-md)",
              padding: 14,
            }}
          >
            <div style={{ fontSize: 13, fontWeight: 600, color: "var(--sl-text)" }}>
              Reset everything
            </div>
            <p style={{ fontSize: 12, color: "var(--sl-text-low)", margin: 0, lineHeight: 1.45 }}>
              Recover all backends, clear chaos, stop traffic and return to defaults.
            </p>
            <div style={{ flex: 1 }} />
            <Button
              size="sm"
              variant="danger"
              disabled={busy}
              onClick={() => setConfirmReset(true)}
              style={{ justifyContent: "center" }}
            >
              Reset all
            </Button>
          </div>
        </div>
      </Card>

      {/* ── Confirm modals ──────────────────────────────────────────────── */}
      <Modal
        open={confirmChaos}
        onClose={() => setConfirmChaos(false)}
        title="Inject chaos?"
        footer={
          <>
            <Button variant="ghost" size="sm" onClick={() => setConfirmChaos(false)}>
              Cancel
            </Button>
            <Button variant="danger" size="sm" disabled={busy} onClick={runChaos}>
              Inject {chaosDelayMs}ms
            </Button>
          </>
        }
      >
        Force a {chaosDelayMs}ms delay onto{" "}
        <b style={{ color: "var(--sl-text)" }}>{shortName(cTarget) || "—"}</b>. This degrades
        live traffic to that backend until reset. Reversible via Recover or Reset all.
      </Modal>

      <Modal
        open={confirmReset}
        onClose={() => setConfirmReset(false)}
        title="Reset everything?"
        footer={
          <>
            <Button variant="ghost" size="sm" onClick={() => setConfirmReset(false)}>
              Cancel
            </Button>
            <Button variant="danger" size="sm" disabled={busy} onClick={runReset}>
              Reset all
            </Button>
          </>
        }
      >
        This recovers every backend, clears chaos and latency injection, stops the load
        generator and restores default routing. Cannot be undone, but is non-destructive
        to the cluster.
      </Modal>
    </>
  );
}
