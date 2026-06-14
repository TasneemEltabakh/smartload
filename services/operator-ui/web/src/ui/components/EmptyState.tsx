/* ============================================================================
   EmptyState -- "nothing yet" panel
   ----------------------------------------------------------------------------
   A clean, centered placeholder for a panel with no rows to show (no active
   alerts, an empty feed, an unfiltered table). Calm and intentional -- this is
   a healthy state, not an error.
   ============================================================================ */
import type { ReactNode } from "react";

export interface EmptyStateProps {
  /** Optional leading glyph (e.g. a lucide icon). */
  icon?: ReactNode;
  /** Primary line. */
  title: ReactNode;
  /** Optional secondary hint. */
  hint?: ReactNode;
  /** Optional action (e.g. a Button). */
  action?: ReactNode;
  className?: string;
}

export function EmptyState({ icon, title, hint, action, className }: EmptyStateProps) {
  return (
    <div
      className={className}
      style={{
        display: "flex",
        flexDirection: "column",
        alignItems: "center",
        justifyContent: "center",
        textAlign: "center",
        gap: 8,
        padding: "32px 16px",
        color: "var(--sl-text-low)",
      }}
    >
      {icon != null ? (
        <span
          aria-hidden
          style={{
            display: "inline-flex",
            color: "var(--sl-text-faint)",
            marginBottom: 2,
          }}
        >
          {icon}
        </span>
      ) : null}
      <div style={{ fontSize: 13.5, fontWeight: 600, color: "var(--sl-text-mid)" }}>{title}</div>
      {hint != null ? (
        <div style={{ fontSize: 11.5, lineHeight: 1.5, maxWidth: 300, color: "var(--sl-text-low)" }}>
          {hint}
        </div>
      ) : null}
      {action != null ? <div style={{ marginTop: 6 }}>{action}</div> : null}
    </div>
  );
}
