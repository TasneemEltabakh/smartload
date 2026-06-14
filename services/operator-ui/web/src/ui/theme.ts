/* ============================================================================
   Theme control
   ----------------------------------------------------------------------------
   The active theme is held on <html data-theme="...">. tokens.css swaps the
   palette off that attribute, so switching is a single attribute write.
   ============================================================================ */
import { useCallback, useEffect, useState } from "react";

export type Theme = "light" | "dark";

const ATTR = "data-theme";

/** Read the theme currently applied to the document, defaulting to light. */
export function getTheme(): Theme {
  if (typeof document === "undefined") return "light";
  const value = document.documentElement.getAttribute(ATTR);
  return value === "dark" ? "dark" : "light";
}

/** Apply a theme by writing it onto the document root. */
export function setTheme(theme: Theme): void {
  if (typeof document === "undefined") return;
  document.documentElement.setAttribute(ATTR, theme);
}

/**
 * React binding for the theme. Tracks the document attribute and exposes a
 * setter plus a toggle. Each app sets its own default at boot; this hook keeps
 * any in-app theme switch in sync with the document.
 */
export function useTheme(): {
  theme: Theme;
  setTheme: (theme: Theme) => void;
  toggle: () => void;
} {
  const [theme, setThemeState] = useState<Theme>(getTheme);

  useEffect(() => {
    setThemeState(getTheme());
  }, []);

  const apply = useCallback((next: Theme) => {
    setTheme(next);
    setThemeState(next);
  }, []);

  const toggle = useCallback(() => {
    apply(getTheme() === "dark" ? "light" : "dark");
  }, [apply]);

  return { theme, setTheme: apply, toggle };
}
