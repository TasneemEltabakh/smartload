/* ============================================================================
   StatusPill -- ok / warn / crit / neutral
   ----------------------------------------------------------------------------
   A status reading with a colored LED. Color is information: ok shares the
   mint family, warn is amber, crit is red.
   ============================================================================ */
import type { ReactNode } from "react";

export type Status = "ok" | "warn" | "crit" | "neutral";

export interface StatusPillProps {
  status: Status;
  children: ReactNode;
  /** Hide the leading LED dot. */
  hideDot?: boolean;
  className?: string;
}

const palette: Record<Status, { fg: string; bg: string; line: string; led: string }> = {
  ok: {
    fg: "var(--sl-ok)",
    bg: "var(--sl-ok-tint)",
    line: "var(--sl-mint-line)",
    led: "var(--sl-ok)",
  },
  warn: {
    fg: "var(--sl-warn)",
    bg: "var(--sl-warn-tint)",
    line: "var(--sl-warn)",
    led: "var(--sl-warn)",
  },
  crit: {
    fg: "var(--sl-crit)",
    bg: "var(--sl-crit-tint)",
    line: "var(--sl-crit)",
    led: "var(--sl-crit)",
  },
  neutral: {
    fg: "var(--sl-text-mid)",
    bg: "var(--sl-surface-sunk)",
    line: "var(--sl-hairline)",
    led: "var(--sl-text-low)",
  },
};

export function StatusPill({ status, children, hideDot, className }: StatusPillProps) {
  const p = palette[status];
  return (
    <span
      className={className}
      style={{
        fontFamily: "var(--sl-font-mono)",
        fontSize: 9.5,
        fontWeight: 600,
        letterSpacing: "0.6px",
        textTransform: "uppercase",
        padding: "3px 8px",
        borderRadius: 6,
        border: `1px solid ${p.line}`,
        color: p.fg,
        background: p.bg,
        display: "inline-flex",
        alignItems: "center",
        gap: 6,
      }}
    >
      {hideDot ? null : (
        <span
          style={{
            width: 6,
            height: 6,
            borderRadius: "50%",
            background: p.led,
            boxShadow: `0 0 8px ${p.led}`,
            flex: "0 0 auto",
          }}
        />
      )}
      {children}
    </span>
  );
}
