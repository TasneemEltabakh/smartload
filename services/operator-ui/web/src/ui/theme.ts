/* ============================================================================
   Theme control
   ----------------------------------------------------------------------------
   The active theme is held on <html data-theme="...">. tokens.css swaps the
   palette off that attribute, so switching is a single attribute write.

   Resolution order at boot (getInitialTheme):
     1. a previously persisted choice in localStorage,
     2. the OS prefers-color-scheme,
     3. the product default (light -- best for a projected discussion).
   setTheme persists the choice so it survives reloads.
   ============================================================================ */
import { useCallback, useEffect, useState } from "react";

export type Theme = "light" | "dark";

const ATTR = "data-theme";
const STORAGE_KEY = "smartload.theme";
const DEFAULT_THEME: Theme = "light";

function isTheme(value: unknown): value is Theme {
  return value === "light" || value === "dark";
}

/** Read a persisted theme choice, if any. */
function readStored(): Theme | null {
  if (typeof localStorage === "undefined") return null;
  try {
    const value = localStorage.getItem(STORAGE_KEY);
    return isTheme(value) ? value : null;
  } catch {
    return null;
  }
}

/** The OS-level color-scheme preference, when expressed. */
function systemTheme(): Theme | null {
  if (typeof window === "undefined" || typeof window.matchMedia !== "function") {
    return null;
  }
  if (window.matchMedia("(prefers-color-scheme: dark)").matches) return "dark";
  if (window.matchMedia("(prefers-color-scheme: light)").matches) return "light";
  return null;
}

/**
 * The theme to apply at first paint: a stored choice, else the OS preference,
 * else the product default. Pure (no DOM writes) so the app can call it once at
 * boot and hand the result to setTheme.
 */
export function getInitialTheme(): Theme {
  return readStored() ?? systemTheme() ?? DEFAULT_THEME;
}

/** Read the theme currently applied to the document, defaulting to light. */
export function getTheme(): Theme {
  if (typeof document === "undefined") return DEFAULT_THEME;
  const value = document.documentElement.getAttribute(ATTR);
  return value === "dark" ? "dark" : "light";
}

/** Apply a theme by writing it onto the document root and persisting it. */
export function setTheme(theme: Theme): void {
  if (typeof document !== "undefined") {
    document.documentElement.setAttribute(ATTR, theme);
  }
  if (typeof localStorage !== "undefined") {
    try {
      localStorage.setItem(STORAGE_KEY, theme);
    } catch {
      /* storage unavailable (private mode / quota) -- the attribute still holds */
    }
  }
}

/**
 * React binding for the theme. Tracks the document attribute and exposes a
 * setter plus a toggle. On mount it reconciles to the resolved initial theme
 * (stored -> system -> default) so a fresh tab respects prior choice / OS, and
 * keeps any in-app theme switch in sync with the document.
 */
export function useTheme(): {
  theme: Theme;
  setTheme: (theme: Theme) => void;
  toggle: () => void;
} {
  const [theme, setThemeState] = useState<Theme>(getTheme);

  useEffect(() => {
    const resolved = readStored() ?? getTheme();
    if (resolved !== getTheme()) setTheme(resolved);
    setThemeState(resolved);
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
