// ============================================================================
// Shell context -- bridge between a route view and the app chrome
// ----------------------------------------------------------------------------
// The Topbar (kill switch, safe_mode) and the Sidebar footer (decision-plane
// service health, operator identity) live in App, but their state is owned by
// the active view: a view reads safe_mode and reports the real service health
// and data source it resolved to. This context lets a view publish that state
// up to the chrome without prop-drilling through the router.
//
// Note: the calm live / demonstration indicator in the Topbar is driven
// independently by the DataModeProvider, not by this context. `plane` here is a
// separate concept -- the reachability of the SmartLoad services themselves --
// and it defaults to healthy so the chrome never opens in a degraded state.
// ============================================================================

import { createContext, useContext } from "react";
import type { DataSource } from "./loader";

export type PlaneStatus = "ok" | "warn" | "bad";

export interface ShellState {
  /** safe_mode kill switch state (armed = automation frozen). */
  safeMode: boolean;
  setSafeMode: (next: boolean) => void;
  /**
   * Engage / release safe_mode. Owned by the app so the Topbar switch and any
   * in-view control drive the same path: optimistic local state, a toast, and a
   * best-effort policy write (no-op offline).
   */
  toggleSafeMode: (next: boolean) => void;
  /** Whether the view is rendering live API data or the sample fallback. */
  dataSource: DataSource;
  setDataSource: (next: DataSource) => void;
  /**
   * Real decision-plane service health for the Sidebar footer. Defaults to
   * "ok"; a view raises it only from genuine service health when live. This is
   * intentionally distinct from the live/demonstration data mode.
   */
  plane: PlaneStatus;
  setPlane: (next: PlaneStatus) => void;
  /** Decision-plane node count for the footer chip. */
  planeNodes: number;
  setPlaneNodes: (next: number) => void;
}

export const ShellContext = createContext<ShellState | null>(null);

export function useShell(): ShellState {
  const ctx = useContext(ShellContext);
  if (!ctx) {
    throw new Error("useShell must be used within the AppShell provider");
  }
  return ctx;
}
