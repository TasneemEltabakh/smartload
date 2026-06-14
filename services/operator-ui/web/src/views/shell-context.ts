// ============================================================================
// Shell context -- bridge between a route view and the app chrome
// ----------------------------------------------------------------------------
// The Topbar (kill switch, LIVE chip, sample-data indicator) and the Sidebar
// footer (decision-plane health, connection, operator) live in App, but their
// state is owned by the active view (the Flightdeck reads safe_mode and learns
// whether it is on live or sample data). This context lets a view publish that
// state up to the chrome without prop-drilling through the router.
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
  /** Decision-plane / connection health for the footer + LIVE chip. */
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
