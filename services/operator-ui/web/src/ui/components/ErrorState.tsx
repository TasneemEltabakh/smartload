/* ============================================================================
   ErrorState -- calm inline error
   ----------------------------------------------------------------------------
   A composed, non-alarming notice for a panel that could not load. Never a
   stack trace: a short human title, an optional plain-language hint, and an
   optional retry. Tone is a quiet warning band, sized to sit inline inside a
   card rather than take over the page.
   ============================================================================ */
import type { ReactNode } from "react";

export interface ErrorStateProps {
  /** Short human-readable headline. Default "Couldn't load this panel". */
  title?: ReactNode;
  /** Optional plain-language hint (no stack traces). */
  hint?: ReactNode;
  /** Optional retry handler; renders a retry affordance when provided. */
  onRetry?: () => void;
  /** Retry button label. Default "Retry". */
  retryLabel?: string;
  className?: string;
}

export function ErrorState({
  title = "Couldn't load this panel",
  hint,
  onRetry,
  retryLabel = "Retry",
  className,
}: ErrorStateProps) {
  return (
    <div
      className={className}
      role="alert"
      style={{
        display: "flex",
        flexDirection: "column",
        gap: 6,
        padding: "14px 16px",
        borderRadius: "var(--sl-radius-md)",
        background: "var(--sl-warn-tint)",
        border: "1px solid var(--sl-warn)",
        color: "var(--sl-text-mid)",
      }}
    >
      <div style={{ fontSize: 13, fontWeight: 600, color: "var(--sl-warn)" }}>{title}</div>
      {hint != null ? (
        <div style={{ fontSize: 11.5, lineHeight: 1.5, color: "var(--sl-text-mid)" }}>{hint}</div>
      ) : null}
      {onRetry != null ? (
        <div style={{ marginTop: 4 }}>
          <button
            type="button"
            onClick={onRetry}
            style={{
              fontFamily: "var(--sl-font-sans)",
              fontSize: 11.5,
              fontWeight: 600,
              color: "var(--sl-text)",
              background: "var(--sl-surface)",
              border: "1px solid var(--sl-hairline)",
              borderRadius: "var(--sl-radius-sm)",
              padding: "5px 12px",
              cursor: "pointer",
              transition: "border-color var(--sl-dur-fast), color var(--sl-dur-fast)",
            }}
          >
            {retryLabel}
          </button>
        </div>
      ) : null}
    </div>
  );
}
