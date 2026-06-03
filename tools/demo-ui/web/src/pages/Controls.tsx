/**
 * tools/demo-ui/web/src/pages/Controls.tsx
 * ─────────────────────────────────────────
 * All the demo orchestration buttons in one place:
 *   - Routing algorithm picker (Round-Robin / Least Conn / Random / AI-PPO)
 *   - Demo scenarios (backend failure / latency spike / recovery / etc.)
 *   - Manual controls: degrade a backend / recover / safe-mode toggle /
 *     traffic level presets / chaos injection / reset-all
 *
 * Stateful selectors (degrade target, chaos delay, etc.) are local to
 * this page — they're navigation-scoped, not session-scoped.
 */

import { useState } from "react";

import { api, type DemoAlgorithm, type DemoScenario } from "../api";
import { useDemo } from "../state/DemoStateContext";
import { shortName } from "../utils";


export default function Controls() {
  const { state, busy, action } = useDemo();

  const [degradeTarget, setDegradeTarget] = useState("");
  const [degradeLevel, setDegradeLevel] = useState<"degraded" | "unhealthy">("unhealthy");
  const [recoverTarget, setRecoverTarget] = useState("");
  const [chaosTarget, setChaosTarget] = useState("");
  const [chaosDelayMs, setChaosDelayMs] = useState(800);

  const backends: string[] = state?.backend_names ?? [];
  const firstBackend = backends[0] ?? "";

  return (
    <div className="card">
      {/* Routing algorithm */}
      <div style={{ marginBottom: 16 }}>
        <div className="muted" style={{ fontSize: 11, marginBottom: 8 }}>ROUTING ALGORITHM</div>
        <div style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
          {(
            [
              ["round_robin", "Round-Robin"],
              ["least_conn", "Least Conn"],
              ["random", "Random"],
              ["ppo", "AI / PPO"],
            ] as [DemoAlgorithm, string][]
          ).map(([algo, label]) => (
            <button
              key={algo}
              className="secondary"
              disabled={busy}
              onClick={() => action(`Algorithm: ${label}`, () => api.demoAlgorithm(algo))}
              style={{
                fontSize: 12, padding: "6px 12px",
                fontWeight: algo === "ppo" ? 700 : 400,
              }}
            >
              {label}
            </button>
          ))}
        </div>
        <div className="muted" style={{ fontSize: 10, marginTop: 4 }}>
          Round-Robin / Least Conn / Random are handled natively by NGINX.
          AI / PPO lets the RL engine control weights.
        </div>
      </div>

      <hr style={{ border: "none", borderTop: "1px solid var(--border)", margin: "12px 0" }} />

      {/* Scenarios */}
      <div style={{ marginBottom: 16 }}>
        <div className="muted" style={{ fontSize: 11, marginBottom: 8 }}>DEMO SCENARIOS</div>
        <div style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
          {(
            [
              ["backend_failure", "Backend Failure"],
              ["latency_spike", "Latency Spike"],
              ["recovery", "Recovery"],
              ["high_traffic", "High Traffic"],
              ["ai_disabled", "AI Disabled"],
            ] as [DemoScenario, string][]
          ).map(([id, label]) => (
            <button
              key={id}
              className="secondary"
              disabled={busy}
              onClick={() => action(label, () => api.demoScenario(id))}
              style={{ fontSize: 12, padding: "6px 12px" }}
            >
              {label}
            </button>
          ))}
        </div>
      </div>

      <hr style={{ border: "none", borderTop: "1px solid var(--border)", margin: "12px 0" }} />

      <div className="muted" style={{ fontSize: 11, marginBottom: 8 }}>MANUAL CONTROLS</div>
      <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>

        {/* Degrade */}
        <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
          <span style={{ minWidth: 64, fontSize: 12, color: "var(--muted)" }}>Degrade</span>
          <select
            value={degradeTarget || firstBackend}
            onChange={(e) => setDegradeTarget(e.target.value)}
            style={{ flex: 1 }}
          >
            {backends.map((b) => (
              <option key={b} value={b}>{shortName(b)}</option>
            ))}
          </select>
          <select
            value={degradeLevel}
            onChange={(e) => setDegradeLevel(e.target.value as "degraded" | "unhealthy")}
            style={{ width: 100 }}
          >
            <option value="unhealthy">unhealthy</option>
            <option value="degraded">degraded</option>
          </select>
          <button
            disabled={busy}
            onClick={() => {
              const target = (degradeTarget || firstBackend) + ":8080";
              action(`Degrade ${shortName(target)}`, () => api.demoDegrade(target, degradeLevel));
            }}
            style={{ padding: "6px 12px", fontSize: 12 }}
          >
            Go
          </button>
        </div>

        {/* Recover */}
        <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
          <span style={{ minWidth: 64, fontSize: 12, color: "var(--muted)" }}>Recover</span>
          <select
            value={recoverTarget || firstBackend}
            onChange={(e) => setRecoverTarget(e.target.value)}
            style={{ flex: 1 }}
          >
            {backends.map((b) => (
              <option key={b} value={b}>{shortName(b)}</option>
            ))}
          </select>
          <button
            disabled={busy}
            onClick={() => {
              const target = (recoverTarget || firstBackend) + ":8080";
              action(`Recover ${shortName(target)}`, () => api.demoRecover(target));
            }}
            style={{ padding: "6px 12px", fontSize: 12 }}
          >
            Go
          </button>
        </div>

        {/* Safe-mode toggle */}
        <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
          <span style={{ minWidth: 64, fontSize: 12, color: "var(--muted)" }}>Mode</span>
          <button
            disabled={busy}
            onClick={() => action(
              state?.safe_mode ? "Enable AI Active" : "Enable Safe Mode",
              () => api.demoMode(!state?.safe_mode),
            )}
            style={{
              padding: "6px 12px", fontSize: 12,
              background: state?.safe_mode ? "var(--ok)" : "var(--warn)",
              color: "#0d1117",
            }}
          >
            {state?.safe_mode ? "Activate AI" : "Enable Safe Mode"}
          </button>
        </div>

        {/* Traffic presets */}
        <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
          <span style={{ minWidth: 64, fontSize: 12, color: "var(--muted)" }}>Traffic</span>
          {(
            [["Off", 0, 1], ["Low", 5, 1], ["Med", 50, 10], ["High", 200, 50]] as [string, number, number][]
          ).map(([label, users, rate]) => (
            <button
              key={label}
              className="secondary"
              disabled={busy}
              onClick={() => action(`Traffic ${label}`, () => api.demoTraffic(users, rate))}
              style={{ padding: "6px 10px", fontSize: 12 }}
            >
              {label}
            </button>
          ))}
        </div>

        {/* Chaos */}
        <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
          <span style={{ minWidth: 64, fontSize: 12, color: "var(--muted)" }}>Chaos</span>
          <select
            value={chaosTarget || firstBackend}
            onChange={(e) => setChaosTarget(e.target.value)}
            style={{ flex: 1 }}
          >
            {backends.map((b) => (
              <option key={b} value={b}>{shortName(b)}</option>
            ))}
          </select>
          <select
            value={chaosDelayMs}
            onChange={(e) => setChaosDelayMs(Number(e.target.value))}
            style={{ width: 80 }}
          >
            <option value={0}>0ms</option>
            <option value={200}>200ms</option>
            <option value={500}>500ms</option>
            <option value={800}>800ms</option>
            <option value={1500}>1500ms</option>
          </select>
          <button
            disabled={busy}
            onClick={() => {
              const target = chaosTarget || firstBackend;
              action(`Chaos ${shortName(target)} ${chaosDelayMs}ms`, () =>
                api.demoChaos(target, chaosDelayMs),
              );
            }}
            style={{ padding: "6px 12px", fontSize: 12 }}
          >
            Go
          </button>
        </div>

        {/* Reset */}
        <div style={{ marginTop: 4 }}>
          <button
            disabled={busy}
            onClick={() => action("Reset All", () => api.demoReset())}
            style={{
              width: "100%", padding: "8px", fontSize: 12,
              background: "var(--bad)", color: "#fff",
            }}
          >
            Reset All
          </button>
        </div>
      </div>
    </div>
  );
}
