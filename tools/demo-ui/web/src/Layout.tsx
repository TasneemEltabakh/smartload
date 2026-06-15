/**
 * tools/demo-ui/web/src/Layout.tsx
 * ─────────────────────────────────
 * Shell for the SmartLoad benchmark & audit presentation (light, academic).
 * Strictly read-only: the topbar carries the freshness/provenance reading, not
 * live controls — there is nothing here to trigger or steer a run.
 */

import { Outlet, useLocation, useNavigate } from "react-router-dom";

import { AppShell, Badge, Sidebar, Topbar, type NavGroup } from "./ui";
import { KindBadge } from "./present/Freshness";
import { freshnessText } from "./results/adapter";
import { useResultsCtx } from "./state/ResultsContext";

interface NavRoute {
  id: string;
  label: string;
  icon: JSX.Element;
}

const ICON = {
  overview: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.8}>
      <rect x="3" y="3" width="7" height="7" rx="1.4" />
      <rect x="14" y="3" width="7" height="7" rx="1.4" />
      <rect x="3" y="14" width="7" height="7" rx="1.4" />
      <rect x="14" y="14" width="7" height="7" rx="1.4" />
    </svg>
  ),
  benchmarks: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.8}>
      <path d="M5 20V8M12 20V4M19 20v-8" />
    </svg>
  ),
  audit: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.8}>
      <path d="M12 3l7 3v6c0 5-3 7-7 9-4-2-7-4-7-9V6z" />
      <path d="M9 12l2 2 4-4" />
    </svg>
  ),
  dashboards: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.8}>
      <path d="M3 12h4l3 8 4-16 3 8h4" />
    </svg>
  ),
};

const NAV: NavRoute[] = [
  { id: "/", label: "Overview", icon: ICON.overview },
  { id: "/benchmarks", label: "Benchmarks", icon: ICON.benchmarks },
  { id: "/audit", label: "Audit", icon: ICON.audit },
  { id: "/dashboards", label: "Dashboards", icon: ICON.dashboards },
];

const NAV_GROUPS: NavGroup[] = [
  { label: "Evidence", items: NAV.map((n) => ({ id: n.id, label: n.label, icon: n.icon })) },
];

export default function Layout() {
  const navigate = useNavigate();
  const location = useLocation();
  const { bundle, source } = useResultsCtx();
  const provenance = bundle.provenance;

  const activeId = NAV.find((n) => n.id !== "/" && location.pathname.startsWith(n.id))?.id ?? "/";

  const sidebar = (
    <Sidebar
      brandSub="Benchmark Evidence"
      groups={NAV_GROUPS}
      activeId={activeId}
      onSelect={(id) => navigate(id)}
      footer={
        <div style={{ display: "flex", flexDirection: "column", gap: 9 }}>
          <KindBadge provenance={provenance} />
          <div style={{ fontFamily: "var(--sl-font-sans)", fontSize: 11, lineHeight: 1.5, color: "var(--sl-text-low)" }}>
            Read-only presentation. Numbers load from {source}.
          </div>
        </div>
      }
    />
  );

  const topbar = (
    <Topbar
      crumb={
        <span style={{ display: "inline-flex", alignItems: "center", gap: 10 }}>
          <span style={{ fontFamily: "var(--sl-font-display)", fontSize: 16, fontWeight: 700, color: "var(--sl-text)" }}>SmartLoad</span>
          <Badge tone="neutral">benchmark &amp; audit evidence</Badge>
          <KindBadge provenance={provenance} />
        </span>
      }
      right={
        <span style={{ fontFamily: "var(--sl-font-sans)", fontSize: 12, color: "var(--sl-text-low)" }}>
          results <span style={{ color: "var(--sl-text-mid)" }}>{freshnessText(provenance)}</span>
        </span>
      }
    />
  );

  return (
    <AppShell sidebar={sidebar} topbar={topbar}>
      <Outlet />
    </AppShell>
  );
}
