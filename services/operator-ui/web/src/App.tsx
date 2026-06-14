// ============================================================================
// App -- router shell on the design-system kit
// ----------------------------------------------------------------------------
// Renders the kit AppShell with the operator information architecture:
// OVERVIEW (Flightdeck, System), OPERATE (Pulse, Foresight, Verdicts, Traffic,
// Capacity) and DECIDE (Helmsman, Controls, Ledger). The Topbar carries the
// breadcrumb, the calm live/demonstration badge, the theme toggle, and the
// safe_mode kill switch; the Sidebar footer carries decision-plane service
// health and the operator identity. The shell owns the cross-cutting state
// (safe_mode, data source, plane health) and publishes it to the active view
// through ShellContext.
// ============================================================================

import { useCallback, useMemo, useState, type ReactNode } from "react";
import { Route, Routes, useLocation, useNavigate } from "react-router-dom";
import {
  Activity,
  Boxes,
  Compass,
  LayoutDashboard,
  Network,
  Route as RouteIcon,
  ScrollText,
  ShieldCheck,
  Sliders,
  TrendingUp,
} from "lucide-react";

import { api } from "./api";
import {
  AppShell,
  DataModeBadge,
  DataModeProvider,
  Sidebar,
  ThemeToggle,
  Toaster,
  Toggle,
  Topbar,
  useToast,
  type NavGroup,
} from "./ui";
import Flightdeck from "./views/Flightdeck";
import System from "./views/System";
import Pulse from "./views/Pulse";
import Foresight from "./views/Foresight";
import Verdicts from "./views/Verdicts";
import Traffic from "./views/Traffic";
import Capacity from "./views/Capacity";
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

type Group = "Overview" | "Operate" | "Decide";

interface RouteDef {
  id: string;
  path: string;
  label: string;
  group: Group;
  icon: ReactNode;
}

const ROUTES: RouteDef[] = [
  { id: "flightdeck", path: "/", label: "Flightdeck", group: "Overview", icon: <LayoutDashboard size={17} strokeWidth={1.9} /> },
  { id: "system", path: "/system", label: "System", group: "Overview", icon: <Network size={17} strokeWidth={1.9} /> },
  { id: "pulse", path: "/pulse", label: "Pulse", group: "Operate", icon: <Activity size={17} strokeWidth={1.9} /> },
  { id: "foresight", path: "/foresight", label: "Foresight", group: "Operate", icon: <TrendingUp size={17} strokeWidth={1.9} /> },
  { id: "verdicts", path: "/verdicts", label: "Verdicts", group: "Operate", icon: <ShieldCheck size={17} strokeWidth={1.9} /> },
  { id: "traffic", path: "/traffic", label: "Traffic", group: "Operate", icon: <RouteIcon size={17} strokeWidth={1.9} /> },
  { id: "capacity", path: "/capacity", label: "Capacity", group: "Operate", icon: <Boxes size={17} strokeWidth={1.9} /> },
  { id: "helmsman", path: "/helmsman", label: "Helmsman", group: "Decide", icon: <Compass size={17} strokeWidth={1.9} /> },
  { id: "controls", path: "/controls", label: "Controls", group: "Decide", icon: <Sliders size={17} strokeWidth={1.9} /> },
  { id: "ledger", path: "/ledger", label: "Ledger", group: "Decide", icon: <ScrollText size={17} strokeWidth={1.9} /> },
];

function buildGroups(): NavGroup[] {
  const order: Group[] = ["Overview", "Operate", "Decide"];
  return order.map((g) => ({
    label: g,
    items: ROUTES.filter((r) => r.group === g).map((r) => ({
      id: r.id,
      label: r.label,
      icon: r.icon,
      title: r.label,
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
      <DataModeProvider>
        <Shell />
      </DataModeProvider>
    </Toaster>
  );
}

function Shell() {
  const navigate = useNavigate();
  const location = useLocation();
  const toast = useToast();

  const [safeMode, setSafeMode] = useState(false);
  const [dataSource, setDataSource] = useState<DataSource>("sample");
  // Decision-plane service health. Defaults to healthy: the console is built to
  // present cleanly on representative data, so the footer never opens degraded.
  // Views raise this from real service health once they resolve live.
  const [plane, setPlane] = useState<PlaneStatus>("ok");
  const [planeNodes, setPlaneNodes] = useState<number>(SAMPLE_PLANE_NODES);

  // Mobile off-canvas navigation state. Below the drawer breakpoint the Topbar
  // shows a menu button that toggles this; the AppShell renders the scrim.
  const [menuOpen, setMenuOpen] = useState(false);

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

  // Decision-plane health is a separate concept from live/demonstration: it
  // reflects whether the SmartLoad services themselves are reachable. Only an
  // unreachable plane reads critically; anything else stays calm.
  const planeBad = plane === "bad";
  const planeWord = plane === "bad" ? "unreachable" : plane === "warn" ? "degraded" : "healthy";

  const sidebar = (
    <Sidebar
      groups={groups}
      activeId={active.id}
      brandSub="Routing learns. Safety doesn't."
      onSelect={(id) => {
        const r = ROUTES.find((x) => x.id === id);
        if (r) navigate(r.path);
        setMenuOpen(false);
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
              background: planeBad ? "var(--sl-crit-tint)" : "var(--sl-mint-tint)",
              border: `1px solid ${planeBad ? "var(--sl-crit)" : "var(--sl-mint-line)"}`,
            }}
          >
            <div style={{ flex: 1 }}>
              <div style={{ fontSize: 12, fontWeight: 700, color: "var(--sl-text)" }}>Decision plane</div>
              <div
                style={{
                  fontFamily: "var(--sl-font-mono)",
                  fontSize: 10.5,
                  color: planeBad ? "var(--sl-crit)" : "var(--sl-mint-deep)",
                  marginTop: 1,
                }}
              >
                {planeWord} - {planeNodes} nodes
              </div>
            </div>
            <span
              style={{
                width: 8,
                height: 8,
                borderRadius: "50%",
                background: planeBad ? "var(--sl-crit)" : "var(--sl-mint)",
                boxShadow: `0 0 8px ${planeBad ? "var(--sl-crit)" : "var(--sl-mint)"}`,
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
              <div style={{ fontSize: 10.5, color: "var(--sl-text-low)" }}>{SAMPLE_OPERATOR.role}</div>
            </div>
          </div>
        </div>
      }
    />
  );

  const topbar = (
    <Topbar
      menuOpen={menuOpen}
      onMenuToggle={() => setMenuOpen((v) => !v)}
      crumb={
        <>
          <span style={{ fontFamily: "var(--sl-font-mono)" }}>smartload</span>
          <span style={{ color: "var(--sl-text-faint)" }}>/</span>
          <b style={{ color: "var(--sl-text)", fontWeight: 600 }}>{active.label}</b>
        </>
      }
      right={
        <>
          <DataModeBadge />
          <ThemeToggle />

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
      <AppShell
        sidebar={sidebar}
        topbar={topbar}
        contentMaxWidth={1480}
        menuOpen={menuOpen}
        onMenuClose={() => setMenuOpen(false)}
      >
        <Routes>
          <Route path="/" element={<Flightdeck />} />
          <Route path="/system" element={<System />} />
          <Route path="/pulse" element={<Pulse />} />
          <Route path="/foresight" element={<Foresight />} />
          <Route path="/verdicts" element={<Verdicts />} />
          <Route path="/traffic" element={<Traffic />} />
          <Route path="/capacity" element={<Capacity />} />
          <Route path="/helmsman" element={<Helmsman />} />
          <Route path="/controls" element={<Controls />} />
          <Route path="/ledger" element={<Ledger />} />
          <Route path="*" element={<Flightdeck />} />
        </Routes>
      </AppShell>
    </ShellContext.Provider>
  );
}
