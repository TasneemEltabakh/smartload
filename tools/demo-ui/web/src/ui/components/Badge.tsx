/* ============================================================================
   Badge -- compact mono tag
   ----------------------------------------------------------------------------
   Small mono label for counts, modes, audit refs. Tone controls the accent.
   ============================================================================ */
import type { ReactNode } from "react";

export type BadgeTone = "neutral" | "mint" | "graphite";

export interface BadgeProps {
  tone?: BadgeTone;
  children: ReactNode;
  className?: string;
}

const tones: Record<BadgeTone, React.CSSProperties> = {
  neutral: {
    color: "var(--sl-text-mid)",
    background: "var(--sl-surface-sunk)",
    borderColor: "var(--sl-hairline)",
  },
  mint: {
    color: "var(--sl-on-mint-tint)",
    background: "var(--sl-mint-tint)",
    borderColor: "var(--sl-mint-line)",
  },
  graphite: {
    color: "var(--sl-graphite)",
    background: "transparent",
    borderColor: "var(--sl-hairline)",
  },
};

export function Badge({ tone = "neutral", children, className }: BadgeProps) {
  return (
    <span
      className={className}
      style={{
        fontFamily: "var(--sl-font-mono)",
        fontSize: 10,
        fontWeight: 600,
        letterSpacing: "0.5px",
        padding: "2px 7px",
        borderRadius: "var(--sl-radius-sm)",
        border: "1px solid",
        display: "inline-flex",
        alignItems: "center",
        gap: 6,
        ...tones[tone],
      }}
    >
      {children}
    </span>
  );
}
