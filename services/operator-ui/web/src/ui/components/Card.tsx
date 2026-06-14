/* ============================================================================
   Card -- raised surface with optional header
   ----------------------------------------------------------------------------
   The base panel. Header carries a title, an optional mono eyebrow, and a
   right-aligned slot for pills / actions.
   ============================================================================ */
import type { CSSProperties, ReactNode } from "react";

export interface CardProps {
  /** Card heading. */
  title?: ReactNode;
  /** Mono eyebrow rendered next to the title (e.g. "// cluster"). */
  eyebrow?: ReactNode;
  /** Right-aligned header slot for pills or buttons. */
  actions?: ReactNode;
  /** Remove default body padding (for tables / charts that pad themselves). */
  flush?: boolean;
  children?: ReactNode;
  className?: string;
  style?: CSSProperties;
}

export function Card({
  title,
  eyebrow,
  actions,
  flush,
  children,
  className,
  style,
}: CardProps) {
  const hasHeader = title != null || eyebrow != null || actions != null;
  return (
    <section
      className={className}
      style={{
        background: "var(--sl-surface)",
        border: "1px solid var(--sl-hairline)",
        borderRadius: "var(--sl-radius-lg)",
        boxShadow: "var(--sl-shadow-1)",
        overflow: "hidden",
        ...style,
      }}
    >
      {hasHeader ? (
        <header
          style={{
            display: "flex",
            alignItems: "center",
            gap: 10,
            padding: "15px 18px 12px",
          }}
        >
          {title != null ? (
            <h3
              style={{
                fontSize: 14,
                fontWeight: 700,
                letterSpacing: "-0.2px",
                color: "var(--sl-text)",
                margin: 0,
              }}
            >
              {title}
            </h3>
          ) : null}
          {eyebrow != null ? (
            <span
              style={{
                fontFamily: "var(--sl-font-mono)",
                fontSize: 10,
                letterSpacing: "1.4px",
                color: "var(--sl-text-low)",
                textTransform: "uppercase",
              }}
            >
              {eyebrow}
            </span>
          ) : null}
          {actions != null ? (
            <div style={{ marginLeft: "auto", display: "flex", alignItems: "center", gap: 8 }}>
              {actions}
            </div>
          ) : null}
        </header>
      ) : null}
      <div style={{ padding: flush ? 0 : "4px 18px 18px" }}>{children}</div>
    </section>
  );
}
