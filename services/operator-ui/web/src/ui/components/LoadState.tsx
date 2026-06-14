/* ============================================================================
   LoadState -- skeleton / shimmer placeholder
   ----------------------------------------------------------------------------
   A tasteful loading placeholder for a panel that is resolving its data. Renders
   a stack of shimmer bars sized to suggest the content to come. The shimmer
   animation is defined in styles.css (.sl-shimmer) and is disabled under
   prefers-reduced-motion, where it degrades to a flat, static block.
   ============================================================================ */
import type { CSSProperties } from "react";

export interface LoadStateProps {
  /** Number of placeholder lines. Default 3. */
  lines?: number;
  /** Height of each line in px. Default 14. */
  lineHeight?: number;
  /** Accessible label announced while loading. */
  label?: string;
  className?: string;
  style?: CSSProperties;
}

export function LoadState({
  lines = 3,
  lineHeight = 14,
  label = "Loading…",
  className,
  style,
}: LoadStateProps) {
  return (
    <div
      className={className}
      role="status"
      aria-busy="true"
      aria-live="polite"
      style={{ display: "flex", flexDirection: "column", gap: 10, padding: "4px 0", ...style }}
    >
      {Array.from({ length: lines }).map((_, i) => (
        <span
          key={i}
          className="sl-shimmer"
          aria-hidden
          style={{
            height: lineHeight,
            // last bar is shorter to read as a paragraph tail
            width: i === lines - 1 ? "62%" : "100%",
            borderRadius: 6,
          }}
        />
      ))}
      <span style={{ position: "absolute", width: 1, height: 1, overflow: "hidden", clip: "rect(0 0 0 0)" }}>
        {label}
      </span>
    </div>
  );
}
