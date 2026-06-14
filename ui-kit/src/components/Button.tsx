/* ============================================================================
   Button -- primary / secondary / ghost / danger
   ----------------------------------------------------------------------------
   Token-driven button. Primary carries the mint brand fill; danger is reserved
   for destructive intent (drain, arm safe_mode).
   ============================================================================ */
import type { ButtonHTMLAttributes, ReactNode } from "react";

export type ButtonVariant = "primary" | "secondary" | "ghost" | "danger";
export type ButtonSize = "sm" | "md";

export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: ButtonVariant;
  size?: ButtonSize;
  /** Optional leading glyph. */
  icon?: ReactNode;
  children?: ReactNode;
}

const base: React.CSSProperties = {
  fontFamily: "var(--sl-font-sans)",
  fontWeight: 600,
  borderRadius: "var(--sl-radius-sm)",
  border: "1px solid transparent",
  cursor: "pointer",
  display: "inline-flex",
  alignItems: "center",
  gap: 7,
  transition: "background var(--sl-dur-fast), border-color var(--sl-dur-fast), color var(--sl-dur-fast)",
  lineHeight: 1.2,
};

const sizes: Record<ButtonSize, React.CSSProperties> = {
  sm: { fontSize: 11, padding: "6px 11px" },
  md: { fontSize: 13.5, padding: "9px 15px" },
};

const variants: Record<ButtonVariant, React.CSSProperties> = {
  primary: {
    background: "var(--sl-mint)",
    color: "#ffffff",
    boxShadow: "var(--sl-shadow-1)",
  },
  secondary: {
    background: "var(--sl-surface)",
    color: "var(--sl-text)",
    borderColor: "var(--sl-hairline)",
    boxShadow: "var(--sl-shadow-1)",
  },
  ghost: {
    background: "transparent",
    color: "var(--sl-text-mid)",
    borderColor: "var(--sl-hairline)",
  },
  danger: {
    background: "transparent",
    color: "var(--sl-crit)",
    borderColor: "var(--sl-crit)",
  },
};

export function Button({
  variant = "secondary",
  size = "md",
  icon,
  children,
  style,
  ...rest
}: ButtonProps) {
  return (
    <button
      type="button"
      style={{ ...base, ...sizes[size], ...variants[variant], ...style }}
      {...rest}
    >
      {icon}
      {children}
    </button>
  );
}
