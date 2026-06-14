/* ============================================================================
   ThemeToggle -- light / dark switch
   ----------------------------------------------------------------------------
   A compact icon button that flips Daylight <-> Mission Control. It drives the
   shared useTheme hook, so the choice persists to localStorage and the document
   attribute updates in one place. Drop it anywhere in the chrome; it owns its
   own state.
   ============================================================================ */
import { Moon, Sun } from "lucide-react";
import { useTheme } from "../theme";

export interface ThemeToggleProps {
  /** Square edge length in px. */
  size?: number;
  className?: string;
}

export function ThemeToggle({ size = 34, className }: ThemeToggleProps) {
  const { theme, toggle } = useTheme();
  const isDark = theme === "dark";
  const glyph = Math.round(size * 0.47);

  return (
    <button
      type="button"
      className={className}
      onClick={toggle}
      aria-label={isDark ? "Switch to light theme" : "Switch to dark theme"}
      title={isDark ? "Switch to light theme" : "Switch to dark theme"}
      style={{
        width: size,
        height: size,
        display: "inline-flex",
        alignItems: "center",
        justifyContent: "center",
        borderRadius: "var(--sl-radius-sm)",
        border: "1px solid var(--sl-hairline)",
        background: "var(--sl-surface)",
        color: "var(--sl-text-mid)",
        cursor: "pointer",
        padding: 0,
        transition:
          "color var(--sl-dur-fast), border-color var(--sl-dur-fast), background var(--sl-dur-fast)",
      }}
    >
      {isDark ? (
        <Sun size={glyph} strokeWidth={1.9} aria-hidden />
      ) : (
        <Moon size={glyph} strokeWidth={1.9} aria-hidden />
      )}
    </button>
  );
}
