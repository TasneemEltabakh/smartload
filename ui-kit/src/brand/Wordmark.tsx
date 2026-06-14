/* ============================================================================
   Wordmark -- smartload/
   ----------------------------------------------------------------------------
   The wordmark is set in mono; the trailing slash (mint) is part of the mark
   and reads as routing downstream.
   ============================================================================ */
import type { CSSProperties } from "react";

export interface WordmarkProps {
  /** Font size of the wordmark in px. */
  size?: number;
  /** Optional sub-label rendered beneath the mark (e.g. "decision plane"). */
  sub?: string;
  className?: string;
  style?: CSSProperties;
}

export function Wordmark({ size = 19, sub, className, style }: WordmarkProps) {
  return (
    <div className={className} style={{ lineHeight: 1.1, ...style }}>
      <span
        style={{
          fontFamily: "var(--sl-font-mono)",
          fontWeight: 700,
          fontSize: size,
          letterSpacing: "-0.5px",
          color: "var(--sl-text)",
        }}
      >
        smartload<span style={{ color: "var(--sl-mint)" }}>/</span>
      </span>
      {sub ? (
        <div
          style={{
            fontFamily: "var(--sl-font-mono)",
            fontSize: 9.5,
            letterSpacing: "1.5px",
            color: "var(--sl-text-low)",
            textTransform: "uppercase",
            marginTop: 2,
          }}
        >
          {sub}
        </div>
      ) : null}
    </div>
  );
}
