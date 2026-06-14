/* ============================================================================
   Topbar -- breadcrumb, live indicator, right-side controls
   ----------------------------------------------------------------------------
   Sticky bar above the content area. Holds a breadcrumb slot, an optional live
   status chip, and a right-aligned slot (clock, kill switch, search).
   ============================================================================ */
import type { ReactNode } from "react";

export interface TopbarProps {
  /** Breadcrumb / page title content. */
  crumb?: ReactNode;
  /** Live indicator label; when set, a pulsing dot is shown. */
  live?: ReactNode;
  /** Right-aligned controls. */
  right?: ReactNode;
  className?: string;
}

export function Topbar({ crumb, live, right, className }: TopbarProps) {
  return (
    <header
      className={className}
      style={{
        display: "flex",
        alignItems: "center",
        gap: 18,
        padding: "0 28px",
        height: 60,
        borderBottom: "1px solid var(--sl-hairline)",
        position: "sticky",
        top: 0,
        zIndex: 20,
        background: "var(--sl-bg)",
      }}
    >
      {crumb != null ? (
        <div
          style={{
            display: "flex",
            alignItems: "center",
            gap: 9,
            fontSize: 13,
            color: "var(--sl-text-mid)",
          }}
        >
          {crumb}
        </div>
      ) : null}

      {live != null ? (
        <span
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
          }}
        >
          <span
            style={{
              width: 8,
              height: 8,
              borderRadius: "50%",
              background: "var(--sl-mint)",
              boxShadow: "0 0 8px var(--sl-mint)",
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
