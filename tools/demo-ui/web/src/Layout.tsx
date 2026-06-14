/**
 * tools/demo-ui/web/src/Layout.tsx
 * ─────────────────────────────────
 * Mission Control shell for the SmartLoad Dev Console, built on the shared
 * kit (AppShell + Sidebar + Topbar) in the dark theme. The sidebar carries
 * the cockpit nav across the five surfaces; the topbar holds the live mode
 * pill, policy / routing summary, stack-health reading, START / STOP traffic
 * shortcuts, the SSE-connected indicator, and the last-inference age.
 *
 * Navigation is delegated: the kit Sidebar reports the selected item id and
 * this shell drives react-router. The routed page renders via <Outlet />.
 */

import { Outlet, useLocation, useNavigate } from "react-router-dom";

import {
  AppShell,
  Badge,
  Button,
  Logomark,
  Sidebar,
  StatusPill,
  Topbar,
  type NavGroup,
} from "./ui";
import { api } from "./api";
import { useDemo } from "./state/DemoStateContext";
import { modeLabel } from "./utils";


/* ── Cockpit nav. Route paths are unchanged; only labels are cockpit-themed. */
interface NavRoute {
  id: string;       // route path used by react-router
  label: string;    // cockpit label
  hint: string;     // purpose
  icon: JSX.Element;
}

const ICON = {
  deck: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.8}>
      <path d="M3 12h4l3 8 4-16 3 8h4" />
    </svg>
  ),
  drive: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.8}>
      <path d="M3 17c4-6 7-9 9-9s2 6 4 6 3-3 5-7" />
      <circle cx="12" cy="8" r="1.4" fill="currentColor" />
    </svg>
  ),
  lab: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.8}>
      <circle cx="12" cy="12" r="3.2" />
      <path d="M12 3v3M12 18v3M3 12h3M18 12h3M5.5 5.5l2 2M16.5 16.5l2 2M18.5 5.5l-2 2M7.5 16.5l-2 2" />
    </svg>
  ),
  proof: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.8}>
      <path d="M12 3l7 3v6c0 5-3 7-7 9-4-2-7-4-7-9V6z" />
      <path d="M9 12l2 2 4-4" />
    </svg>
  ),
  stream: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.8}>
      <path d="M4 7h16M4 12h16M4 17h16" />
    </svg>
  ),
};

const NAV: NavRoute[] = [
  { id: "/",           label: "Deck",   hint: "Overview — stack health, live metrics, current decision", icon: ICON.deck },
  { id: "/run",        label: "Drive",  hint: "Load profiles + live monitor",                            icon: ICON.drive },
  { id: "/controls",   label: "Lab",    hint: "Scenarios · algorithm · chaos injection",                 icon: ICON.lab },
  { id: "/benchmarks", label: "Proof",  hint: "Benchmark suites — charts & summaries",                    icon: ICON.proof },
  { id: "/feed",       label: "Stream", hint: "Live decision-plane SSE feed",                             icon: ICON.stream },
];

const NAV_GROUPS: NavGroup[] = [
  {
    label: "Console",
    items: NAV.map((n) => ({
      id: n.id,
      label: n.label,
      icon: n.icon,
      tag: n.id === "/" ? "LIVE" : undefined,
    })),
  },
];


/* Mode → status tone for the topbar mode pill. */
function modeStatus(state: ReturnType<typeof useDemo>["state"]) {
  if (!state) return "neutral" as const;
  if (state.safe_mode) return "warn" as const;
  if (state.rl_mode === "active") return "ok" as const;
  return "neutral" as const;
}


export default function Layout() {
  const navigate = useNavigate();
  const location = useLocation();
  const { state, services, error, sseConnected, busy, action, toast } = useDemo();

  // Resolve active nav id from the current path (exact for "/", prefix otherwise).
  const activeId =
    NAV.find((n) => n.id !== "/" && location.pathname.startsWith(n.id))?.id ?? "/";

  const stackTone =
    services == null ? "neutral"
      : services.healthy === services.total ? "ok"
      : services.healthy === 0 ? "crit"
      : "warn";
  const stackLabel =
    services == null ? "stack —" : `stack ${services.healthy}/${services.total}`;

  const sidebar = (
    <Sidebar
      brandSub="Dev Console"
      groups={NAV_GROUPS}
      activeId={activeId}
      onSelect={(id) => navigate(id)}
      footer={
        <div style={{ display: "flex", flexDirection: "column", gap: 9 }}>
          <StatusPill status={stackTone}>{stackLabel}</StatusPill>
          <StatusPill status={sseConnected ? "ok" : "neutral"}>
            {sseConnected ? "live · sse" : "polling"}
          </StatusPill>
          <div
            style={{
              fontFamily: "var(--sl-font-mono)",
              fontSize: 9.5,
              lineHeight: 1.5,
              color: "var(--sl-text-low)",
              marginTop: 2,
            }}
          >
            BFF :8091 — developer harness, separate from the operator UI on :8090.
          </div>
        </div>
      }
    />
  );

  const topbar = (
    <Topbar
      crumb={
        <span style={{ display: "inline-flex", alignItems: "center", gap: 10 }}>
          <Logomark size={20} animated />
          <StatusPill status={modeStatus(state)} hideDot={false}>
            {modeLabel(state)}
          </StatusPill>
          <Badge tone="neutral">policy {state?.policy_type ?? "—"}
            {state?.policy_ready === false ? " · not ready" : ""}
          </Badge>
          <Badge tone="neutral">routing {state?.algorithm ?? "round_robin"}</Badge>
          <StatusPill status={stackTone}>
            {services == null ? "stack —" : `stack ${services.healthy}/${services.total} healthy`}
          </StatusPill>
          {error ? (
            <StatusPill status="crit">{error}</StatusPill>
          ) : null}
        </span>
      }
      right={
        <>
          <span
            style={{
              fontFamily: "var(--sl-font-mono)",
              fontSize: 11,
              color: "var(--sl-text-low)",
            }}
          >
            last inference{" "}
            <span style={{ color: "var(--sl-text-mid)" }}>
              {state?.last_inference_age_seconds != null
                ? `${state.last_inference_age_seconds}s ago`
                : "—"}
            </span>
          </span>
          <Button
            variant="primary"
            size="sm"
            disabled={busy}
            onClick={() => action("Start Traffic", () => api.demoTraffic(20, 5))}
          >
            ▶ Start traffic
          </Button>
          <Button
            variant="ghost"
            size="sm"
            disabled={busy}
            onClick={() => action("Stop Traffic", () => api.demoTraffic(0, 1))}
          >
            ■ Stop
          </Button>
        </>
      }
    />
  );

  return (
    <>
      <AppShell sidebar={sidebar} topbar={topbar}>
        <Outlet />
      </AppShell>

      {/* Toast — preserves the existing DemoStateContext toast behaviour. */}
      {toast ? (
        <div
          role="status"
          style={{
            position: "fixed",
            bottom: 24,
            left: "50%",
            transform: "translateX(-50%)",
            zIndex: 90,
            background: "var(--sl-text)",
            color: "var(--sl-surface)",
            borderRadius: "var(--sl-radius-md)",
            padding: "13px 18px",
            boxShadow: "var(--sl-shadow-2)",
            display: "flex",
            alignItems: "center",
            gap: 12,
            maxWidth: 560,
            fontSize: 13,
          }}
        >
          <span
            style={{
              width: 8,
              height: 8,
              borderRadius: "50%",
              flex: "0 0 auto",
              background: toast.ok ? "var(--sl-ok)" : "var(--sl-crit)",
              boxShadow: `0 0 8px ${toast.ok ? "var(--sl-ok)" : "var(--sl-crit)"}`,
            }}
          />
          {toast.msg}
        </div>
      ) : null}
    </>
  );
}
