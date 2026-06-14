/* ============================================================================
   SmartLoad design-system kit -- barrel
   ----------------------------------------------------------------------------
   Single entry point. Apps import "./ui" (from web/src/ui) to reach everything.
   The matching tokens.css must be imported once at app boot.
   ============================================================================ */

/* theme */
export { setTheme, getTheme, getInitialTheme, useTheme } from "./theme";
export type { Theme } from "./theme";
export { ThemeToggle } from "./components/ThemeToggle";
export type { ThemeToggleProps } from "./components/ThemeToggle";

/* brand */
export { Wordmark } from "./brand/Wordmark";
export type { WordmarkProps } from "./brand/Wordmark";
export { Logomark } from "./brand/Logomark";
export type { LogomarkProps } from "./brand/Logomark";

/* motion */
export { Heartbeat } from "./motion/Heartbeat";
export type { HeartbeatProps } from "./motion/Heartbeat";

/* components */
export { AppShell } from "./components/AppShell";
export type { AppShellProps } from "./components/AppShell";
export { Sidebar } from "./components/Sidebar";
export type { SidebarProps, NavItem, NavGroup } from "./components/Sidebar";
export { Topbar } from "./components/Topbar";
export type { TopbarProps } from "./components/Topbar";
export { Card } from "./components/Card";
export type { CardProps } from "./components/Card";
export { KpiStat } from "./components/KpiStat";
export type { KpiStatProps, DeltaDir } from "./components/KpiStat";
export { StatusPill } from "./components/StatusPill";
export type { StatusPillProps, Status } from "./components/StatusPill";
export { Badge } from "./components/Badge";
export type { BadgeProps, BadgeTone } from "./components/Badge";
export { Button } from "./components/Button";
export type { ButtonProps, ButtonVariant, ButtonSize } from "./components/Button";
export { Toggle } from "./components/Toggle";
export type { ToggleProps } from "./components/Toggle";
export { DataTable } from "./components/DataTable";
export type { DataTableProps, Column, SortDir } from "./components/DataTable";
export { EvidenceLine } from "./components/EvidenceLine";
export type { EvidenceLineProps } from "./components/EvidenceLine";
export { Drawer } from "./components/Drawer";
export type { DrawerProps } from "./components/Drawer";
export { Modal } from "./components/Modal";
export type { ModalProps } from "./components/Modal";
export { Toaster, useToast } from "./components/Toast";
export type { ToastInput, ToastTone } from "./components/Toast";
export { Tabs } from "./components/Tabs";
export type { TabsProps, TabItem } from "./components/Tabs";

/* data-mode + load/empty/error states (the "robust either way" surface) */
export { DataModeBadge } from "./components/DataModeBadge";
export type { DataModeBadgeProps } from "./components/DataModeBadge";
export { LoadState } from "./components/LoadState";
export type { LoadStateProps } from "./components/LoadState";
export { EmptyState } from "./components/EmptyState";
export type { EmptyStateProps } from "./components/EmptyState";
export { ErrorState } from "./components/ErrorState";
export type { ErrorStateProps } from "./components/ErrorState";

/* data-mode infrastructure (re-exported from lib for one-stop import) */
export {
  DataModeProvider,
  useDataMode,
  useLiveOrDemo,
} from "../lib/datamode";
export type {
  DataMode,
  DataSource,
  ConnectionState,
  LoadStatus,
  DataModeContextValue,
  DataModeProviderProps,
  LiveOrDemo,
  UseLiveOrDemoOptions,
} from "../lib/datamode";
export { useFocusTrap } from "../lib/useFocusTrap";

/* charts */
export { ForecastChart } from "./charts/ForecastChart";
export type { ForecastChartProps } from "./charts/ForecastChart";
export { Sparkline } from "./charts/Sparkline";
export type { SparklineProps } from "./charts/Sparkline";
export { Donut } from "./charts/Donut";
export type { DonutProps, DonutSegment } from "./charts/Donut";
export { ShareBars } from "./charts/ShareBars";
export type { ShareBarsProps, ShareRow } from "./charts/ShareBars";

/* svg helpers (handy for app-side custom visuals) */
export { smoothPath, linePath, xScale, yScale, lerp, clamp } from "./charts/svg";
export type { Point } from "./charts/svg";
