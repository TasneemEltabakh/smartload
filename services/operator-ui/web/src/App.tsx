// ============================================================================
// App -- router shell on the design-system kit
// ----------------------------------------------------------------------------
// Renders the kit AppShell with the redesigned information architecture:
// OPERATE (Flightdeck, Pulse, Foresight, Verdicts) and DECIDE (Helmsman,
// Controls, Ledger). The Topbar carries the breadcrumb, a LIVE chip, a
// sample-data indicator, and the safe_mode kill switch; the Sidebar footer
// carries decision-plane health, connection status, and the operator identity.
// The shell owns the cross-cutting state (safe_mode, data source, plane health)
// and publishes it to the active view through ShellContext.
// ============================================================================

import { useCallback, useMemo, useState, type ReactNode } from "react";
import { Route, Routes, useLocation, useNavigate } from "react-router-dom";
import {
  Activity,
  Compass,
  LayoutDashboard,
  ScrollText,
  ShieldCheck,
  Sliders,
  TrendingUp,
} from "lucide-react";

import { api } from "./api";
import {
  AppShell,
  Sidebar,
  Toaster,
  Toggle,
  Topbar,
  useToast,
  type NavGroup,
} from "./ui";
import Flightdeck from "./views/Flightdeck";
import Pulse from "./views/Pulse";
import Foresight from "./views/Foresight";
import Verdicts from "./views/Verdicts";
import Helmsman from "./views/Helmsman";
import Controls from "./views/Controls";
import Ledger from "./views/Ledger";
import {
  ShellContext,
  type PlaneStatus,
  type ShellState,
} from "./views/shell-context";
import type { DataSource } from "./views/loader";
import { SAMPLE_OPERATOR, SAMPLE_PLANE_NODES } from "./views/sample";

// ── nav model ────────────────────────────────────────────────────────────────

interface RouteDef {
  id: string;
  path: string;
  label: string;
  group: "Operate" | "Decide";
  icon: ReactNode;
}

const ROUTES: RouteDef[] = [
  { id: "flightdeck", path: "/", label: "Flightdeck", group: "Operate", icon: <LayoutDashboard size={17} strokeWidth={1.9} /> },
  { id: "pulse", path: "/pulse", label: "Pulse", group: "Operate", icon: <Activity size={17} strokeWidth={1.9} /> },
  { id: "foresight", path: "/foresight", label: "Foresight", group: "Operate", icon: <TrendingUp size={17} strokeWidth={1.9} /> },
  { id: "verdicts", path: "/verdicts", label: "Verdicts", group: "Operate", icon: <ShieldCheck size={17} strokeWidth={1.9} /> },
  { id: "helmsman", path: "/helmsman", label: "Helmsman", group: "Decide", icon: <Compass size={17} strokeWidth={1.9} /> },
  { id: "controls", path: "/controls", label: "Controls", group: "Decide", icon: <Sliders size={17} strokeWidth={1.9} /> },
  { id: "ledger", path: "/ledger", label: "Ledger", group: "Decide", icon: <ScrollText size={17} strokeWidth={1.9} /> },
];

function buildGroups(): NavGroup[] {
  const order: Array<RouteDef["group"]> = ["Operate", "Decide"];
  return order.map((g) => ({
    label: g,
    items: ROUTES.filter((r) => r.group === g).map((r) => ({
      id: r.id,
      label: r.label,
      icon: r.icon,
    })),
  }));
}

function routeForPath(pathname: string): RouteDef {
  const exact = ROUTES.find((r) => r.path === pathname);
  if (exact) return exact;
  const prefix = ROUTES.filter((r) => r.path !== "/").find((r) => pathname.startsWith(r.path));
  return prefix ?? ROUTES[0];
}

// ── app ──────────────────────────────────────────────────────────────────────

export default function App() {
  return (
    <Toaster>
      <Shell />
    </Toaster>
  );
}

function Shell() {
  const navigate = useNavigate();
  const location = useLocation();
  const toast = useToast();

  const [safeMode, setSafeMode] = useState(false);
  const [dataSource, setDataSource] = useState<DataSource>("sample");
  const [plane, setPlane] = useState<PlaneStatus>("warn");
  const [planeNodes, setPlaneNodes] = useState<number>(SAMPLE_PLANE_NODES);

  const toggleSafeMode = useCallback(
    (next: boolean) => {
      setSafeMode(next);
      if (next) {
        toast.push({
          title: "Safe mode engaged",
          detail: "safe_mode = on - automation frozen on last known-good",
          tone: "crit",
        });
      } else {
        toast.push({
          title: "Safe mode released",
          detail: "safe_mode = off - decision plane resumed",
          tone: "ok",
        });
      }
      // Best-effort policy write; offline this is a no-op and local state stands.
      api.setPolicy({ safe_mode: next }, "operator").catch(() => undefined);
    },
    [toast],
  );

  const shell: ShellState = useMemo(
    () => ({
      safeMode,
      setSafeMode,
      toggleSafeMode,
      dataSource,
      setDataSource,
      plane,
      setPlane,
      planeNodes,
      setPlaneNodes,
    }),
    [safeMode, toggleSafeMode, dataSource, plane, planeNodes],
  );

  const active = routeForPath(location.pathname);
  const groups = useMemo(buildGroups, []);

  const planeLabel = plane === "ok" ? "Live connected" : plane === "warn" ? "Degraded" : "Disconnected";

  const sidebar = (
    <Sidebar
      groups={groups}
      activeId={active.id}
      brandSub="Routing learns. Safety doesn't."
      onSelect={(id) => {
        const r = ROUTES.find((x) => x.id === id);
        if (r) navigate(r.path);
      }}
      footer={
        <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
          <div
            style={{
              display: "flex",
              alignItems: "center",
              gap: 10,
              padding: "10px 12px",
              borderRadius: 12,
              background: plane === "bad" ? "var(--sl-crit-tint)" : "var(--sl-mint-tint)",
              border: `1px solid ${plane === "bad" ? "var(--sl-crit)" : "var(--sl-mint-line)"}`,
            }}
          >
            <div style={{ flex: 1 }}>
              <div style={{ fontSize: 12, fontWeight: 700, color: "var(--sl-text)" }}>Decision plane</div>
              <div
                style={{
                  fontFamily: "var(--sl-font-mono)",
                  fontSize: 10.5,
                  color: plane === "bad" ? "var(--sl-crit)" : "var(--sl-mint-deep)",
                  marginTop: 1,
                }}
              >
                {plane === "bad" ? "unreachable" : plane === "warn" ? "degraded" : "healthy"} - {planeNodes} nodes
              </div>
            </div>
            <span
              style={{
                width: 8,
                height: 8,
                borderRadius: "50%",
                background: plane === "bad" ? "var(--sl-crit)" : "var(--sl-mint)",
                boxShadow: `0 0 8px ${plane === "bad" ? "var(--sl-crit)" : "var(--sl-mint)"}`,
                flex: "0 0 auto",
              }}
            />
          </div>

          <div style={{ display: "flex", alignItems: "center", gap: 10, padding: "4px 4px 0" }}>
            <div
              style={{
                width: 30,
                height: 30,
                borderRadius: 9,
                background: "linear-gradient(135deg, var(--sl-mint), var(--sl-mint-deep))",
                color: "#fff",
                display: "grid",
                placeItems: "center",
                fontWeight: 700,
                fontSize: 12,
                fontFamily: "var(--sl-font-mono)",
                flex: "0 0 auto",
              }}
            >
              {SAMPLE_OPERATOR.initials}
            </div>
            <div style={{ minWidth: 0 }}>
              <div style={{ fontSize: 12.5, fontWeight: 600, color: "var(--sl-text)" }}>{SAMPLE_OPERATOR.name}</div>
              <div style={{ fontSize: 10.5, color: "var(--sl-text-low)" }}>
                {SAMPLE_OPERATOR.role} - {planeLabel.toLowerCase()}
              </div>
            </div>
          </div>
        </div>
      }
    />
  );

  const topbar = (
    <Topbar
      crumb={
        <>
          <span style={{ fontFamily: "var(--sl-font-mono)" }}>smartload</span>
          <span style={{ color: "var(--sl-text-faint)" }}>/</span>
          <b style={{ color: "var(--sl-text)", fontWeight: 600 }}>{active.label}</b>
        </>
      }
      live={`LIVE - ${plane === "bad" ? "offline" : plane === "warn" ? "degraded" : "connected"}`}
      right={
        <>
          {dataSource === "sample" ? (
            <span
              style={{
                fontFamily: "var(--sl-font-mono)",
                fontSize: 10.5,
                fontWeight: 600,
                color: "var(--sl-warn)",
                background: "var(--sl-warn-tint)",
                border: "1px solid var(--sl-warn)",
                borderRadius: 20,
                padding: "4px 10px",
              }}
              title="No live backend reached; showing representative sample data."
            >
              SAMPLE DATA
            </span>
          ) : null}

          <div style={{ display: "flex", alignItems: "center", gap: 9 }}>
            <span
              style={{
                fontFamily: "var(--sl-font-mono)",
                fontSize: 10.5,
                fontWeight: 600,
                letterSpacing: "0.4px",
                color: safeMode ? "var(--sl-crit)" : "var(--sl-text-low)",
                textTransform: "uppercase",
              }}
            >
              safe_mode
            </span>
            <Toggle checked={safeMode} onChange={toggleSafeMode} armedTone label="Toggle safe mode" />
          </div>
        </>
      }
    />
  );

  return (
    <ShellContext.Provider value={shell}>
      <AppShell sidebar={sidebar} topbar={topbar} contentMaxWidth={1480}>
        <Routes>
          <Route path="/" element={<Flightdeck />} />
          <Route path="/pulse" element={<Pulse />} />
          <Route path="/foresight" element={<Foresight />} />
          <Route path="/verdicts" element={<Verdicts />} />
          <Route path="/helmsman" element={<Helmsman />} />
          <Route path="/controls" element={<Controls />} />
          <Route path="/ledger" element={<Ledger />} />
        </Routes>
      </AppShell>
    </ShellContext.Provider>
  );
}
