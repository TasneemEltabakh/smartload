/* ============================================================================
   Toggle -- the safe_mode kill switch
   ----------------------------------------------------------------------------
   A deliberate switch. The default is a neutral on/off; the "armed" tone turns
   the track red to read as a kill switch (freeze automation, hold fallback).
   Controlled component: caller owns `checked`.
   ============================================================================ */
export interface ToggleProps {
  checked: boolean;
  onChange: (next: boolean) => void;
  /** When true the engaged state reads as a destructive kill switch (red). */
  armedTone?: boolean;
  disabled?: boolean;
  /** Accessible label. */
  label?: string;
  className?: string;
}

export function Toggle({
  checked,
  onChange,
  armedTone = false,
  disabled = false,
  label = "Toggle",
  className,
}: ToggleProps) {
  const onColor = armedTone ? "var(--sl-crit)" : "var(--sl-mint)";
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={label}
      disabled={disabled}
      className={className}
      onClick={() => !disabled && onChange(!checked)}
      style={{
        position: "relative",
        width: 54,
        height: 30,
        borderRadius: 30,
        border: "1px solid var(--sl-hairline)",
        background: checked ? onColor : "var(--sl-graphite-soft)",
        cursor: disabled ? "not-allowed" : "pointer",
        flex: "0 0 auto",
        padding: 0,
        transition: "background var(--sl-dur-mid) var(--sl-ease)",
        opacity: disabled ? 0.5 : 1,
      }}
    >
      <span
        style={{
          position: "absolute",
          top: 3,
          left: 3,
          width: 22,
          height: 22,
          borderRadius: "50%",
          background: "#ffffff",
          boxShadow: "0 1px 3px rgba(0,0,0,.3)",
          transform: checked ? "translateX(24px)" : "translateX(0)",
          transition: "transform var(--sl-dur-mid) var(--sl-ease)",
        }}
      />
    </button>
  );
}
