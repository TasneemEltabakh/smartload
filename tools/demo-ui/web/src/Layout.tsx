/**
 * tools/demo-ui/web/src/Layout.tsx
 * ─────────────────────────────────
 * App shell. Sidebar nav across the four demo-ui surfaces (Overview /
 * Controls / Feed / Benchmark) + a top bar with the live mode pill,
 * Start/Stop traffic shortcuts, and SSE-connected indicator. Renders
 * the routed page via <Outlet />.
 */

import { NavLink, Outlet } from "react-router-dom";

import { api } from "./api";
import { useDemo } from "./state/DemoStateContext";
import { CLR_MUTED, CLR_OK, modeBadgeClass, modeLabel } from "./utils";


const NAV: { to: string; label: string; hint: string }[] = [
  { to: "/",          label: "Overview",  hint: "Decision · weights · rankings · metrics" },
  { to: "/controls",  label: "Controls",  hint: "Algorithm · scenarios · manual ops" },
  { to: "/feed",      label: "Live Feed", hint: "SSE event stream (routing / anomaly / policy)" },
  { to: "/benchmark", label: "Benchmark", hint: "Baseline-vs-SmartLoad results (#148)" },
];


export default function Layout() {
  const { state, error, sseConnected, busy, action, toast } = useDemo();

  return (
    <div style={{ display: "grid", gridTemplateColumns: "200px 1fr", minHeight: "100vh" }}>
      {/* ── Sidebar ──────────────────────────────────────────────────────── */}
      <aside style={{
        borderRight: "1px solid var(--border)",
        background: "#0d1117",
        padding: "16px 12px",
        display: "flex", flexDirection: "column", gap: 4,
      }}>
        <div style={{ marginBottom: 16 }}>
          <div style={{ fontWeight: 700, fontSize: 16, color: "var(--text)" }}>SmartLoad</div>
          <div className="muted" style={{ fontSize: 11 }}>Developer Demo</div>
        </div>

        {NAV.map((item) => (
          <NavLink
            key={item.to}
            to={item.to}
            end={item.to === "/"}
            style={({ isActive }) => ({
              padding: "8px 10px",
              borderRadius: 4,
              fontSize: 13,
              fontWeight: isActive ? 600 : 400,
              background: isActive ? "var(--accent)" : "transparent",
              color: isActive ? "#0d1117" : "var(--text)",
              textDecoration: "none",
              borderLeft: isActive ? "3px solid var(--ok)" : "3px solid transparent",
              transition: "background 0.1s",
            })}
            title={item.hint}
          >
            {item.label}
          </NavLink>
        ))}

        <div style={{ marginTop: "auto", paddingTop: 16, fontSize: 11, color: CLR_MUTED, lineHeight: 1.5 }}>
          <div style={{ color: sseConnected ? CLR_OK : CLR_MUTED }}>
            {sseConnected ? "● Live" : "○ Polling"}
          </div>
          <div style={{ marginTop: 6 }}>
            BFF :8091 — separate from the operator UI on :8090.
          </div>
        </div>
      </aside>

      {/* ── Main column ──────────────────────────────────────────────────── */}
      <div style={{ display: "flex", flexDirection: "column" }}>

        {/* Top bar (mode pill + start/stop traffic shortcuts) */}
        <header className="card" style={{
          margin: 0,
          borderRadius: 0,
          borderBottom: "1px solid var(--border)",
          borderLeft: 0, borderRight: 0, borderTop: 0,
        }}>
          <div style={{ display: "flex", alignItems: "center", gap: 16, flexWrap: "wrap" }}>
            <div className={modeBadgeClass(state)} style={{ margin: 0, padding: "6px 14px" }}>
              <div className="name" style={{ fontSize: 13 }}>{modeLabel(state)}</div>
            </div>
            <span className="muted" style={{ fontSize: 12 }}>
              Policy: {state?.policy_type ?? "—"}
              {state?.policy_ready === false ? " (not ready)" : ""}
            </span>
            <span className="muted" style={{ fontSize: 12 }}>
              Routing: {state?.algorithm ?? "round_robin"}
            </span>
            <button
              style={{ padding: "8px 20px", fontSize: 13, background: "var(--ok)", color: "#0d1117", fontWeight: 700 }}
              disabled={busy}
              onClick={() => action("Start Traffic", () => api.demoTraffic(20, 5))}
            >
              ▶ START DEMO
            </button>
            <button
              className="secondary"
              style={{ padding: "8px 16px", fontSize: 13 }}
              disabled={busy}
              onClick={() => action("Stop Traffic", () => api.demoTraffic(0, 1))}
            >
              ■ STOP
            </button>
            {error && <span style={{ color: "var(--bad)", fontSize: 12 }}>⚠ {error}</span>}
            <span className="muted" style={{ fontSize: 12, marginLeft: "auto" }}>
              Last inference:{" "}
              {state?.last_inference_age_seconds != null
                ? `${state.last_inference_age_seconds}s ago`
                : "—"}
            </span>
          </div>
        </header>

        {/* Routed page */}
        <main style={{ padding: 12, flex: 1, overflow: "auto" }}>
          <Outlet />
        </main>
      </div>

      {/* Toast — sits over everything */}
      {toast && (
        <div className={`toast ${toast.ok ? "ok" : "bad"}`}>{toast.msg}</div>
      )}
    </div>
  );
}
