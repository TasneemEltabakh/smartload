/* ============================================================================
   Topbar -- breadcrumb, live indicator, right-side controls
   ----------------------------------------------------------------------------
   Sticky bar above the content area. Holds an optional menu button (shown only
   on the mobile drawer breakpoint), a breadcrumb slot, an optional live status
   chip, and a right-aligned slot (clock, kill switch, theme toggle, search).
   ============================================================================ */
import { Menu } from "lucide-react";
import type { ReactNode } from "react";

export interface TopbarProps {
  /** Breadcrumb / page title content. */
  crumb?: ReactNode;
  /** Live indicator label; when set, a pulsing dot is shown. */
  live?: ReactNode;
  /** Right-aligned controls. */
  right?: ReactNode;
  /**
   * When provided, a menu button is rendered (visible only below the drawer
   * breakpoint) that calls this to open/close the off-canvas Sidebar.
   */
  onMenuToggle?: () => void;
  /** Reflects the off-canvas menu state for aria-expanded. */
  menuOpen?: boolean;
  className?: string;
}

export function Topbar({ crumb, live, right, onMenuToggle, menuOpen, className }: TopbarProps) {
  return (
    <header
      className={`sl-topbar${className ? ` ${className}` : ""}`}
      style={{
        display: "flex",
        alignItems: "center",
        gap: 14,
        padding: "0 28px",
        height: "var(--sl-topbar-height)",
        borderBottom: "1px solid var(--sl-hairline)",
        position: "sticky",
        top: 0,
        zIndex: 20,
        background: "var(--sl-bg)",
      }}
    >
      {onMenuToggle ? (
        <button
          type="button"
          className="sl-topbar-menu"
          aria-label={menuOpen ? "Close navigation" : "Open navigation"}
          aria-expanded={menuOpen ?? false}
          onClick={onMenuToggle}
          style={{
            display: "none", // shown via .sl-topbar-menu media query in styles.css
            alignItems: "center",
            justifyContent: "center",
            width: 36,
            height: 36,
            flex: "0 0 auto",
            borderRadius: "var(--sl-radius-sm)",
            border: "1px solid var(--sl-hairline)",
            background: "var(--sl-surface)",
            color: "var(--sl-text-mid)",
            cursor: "pointer",
            padding: 0,
          }}
        >
          <Menu size={18} strokeWidth={1.9} aria-hidden />
        </button>
      ) : null}

      {crumb != null ? (
        <div
          className="sl-topbar-crumb"
          style={{
            display: "flex",
            alignItems: "center",
            gap: 9,
            fontSize: 13,
            color: "var(--sl-text-mid)",
            minWidth: 0,
          }}
        >
          {crumb}
        </div>
      ) : null}

      {live != null ? (
        <span
          className="sl-topbar-live"
          style={{
            display: "inline-flex",
            alignItems: "center",
            gap: 8,
            fontFamily: "var(--sl-font-mono)",
            fontSize: 11,
            fontWeight: 600,
            color: "var(--sl-on-mint-tint)",
            background: "var(--sl-mint-tint)",
            border: "1px solid var(--sl-mint-line)",
            borderRadius: 20,
            padding: "5px 12px 5px 9px",
            flex: "0 0 auto",
          }}
        >
          <span
            className="sl-pulse"
            style={{
              width: 8,
              height: 8,
              borderRadius: "50%",
              background: "var(--sl-mint)",
            }}
          />
          {live}
        </span>
      ) : null}

      {right != null ? (
        <div style={{ marginLeft: "auto", display: "flex", alignItems: "center", gap: 14 }}>
          {right}
        </div>
      ) : null}
    </header>
  );
}
