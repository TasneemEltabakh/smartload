/* ============================================================================
   useFocusTrap -- contain Tab focus inside an overlay
   ----------------------------------------------------------------------------
   For modal dialogs and drawers. While `active`, focus is moved into the
   container (first focusable element, else the container itself), Tab / Shift+Tab
   wrap within it, and on deactivation focus is restored to whatever element was
   focused when the trap engaged (typically the trigger). Escape is left to the
   host component, which already handles close.

   Returns a ref to attach to the overlay's container element.
   ============================================================================ */
import { useEffect, useRef } from "react";

const FOCUSABLE =
  'a[href], area[href], button:not([disabled]), input:not([disabled]):not([type="hidden"]),' +
  'select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"]),' +
  "[contenteditable='true']";

export function useFocusTrap<T extends HTMLElement>(active: boolean) {
  const containerRef = useRef<T | null>(null);

  useEffect(() => {
    if (!active) return;
    const container = containerRef.current;
    if (!container) return;

    const previouslyFocused = document.activeElement as HTMLElement | null;

    const focusables = () =>
      Array.from(container.querySelectorAll<HTMLElement>(FOCUSABLE)).filter(
        (el) => el.offsetParent !== null || el === document.activeElement,
      );

    // Move initial focus into the overlay.
    const first = focusables()[0];
    if (first) {
      first.focus();
    } else {
      container.setAttribute("tabindex", "-1");
      container.focus();
    }

    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Tab") return;
      const items = focusables();
      if (items.length === 0) {
        e.preventDefault();
        return;
      }
      const firstEl = items[0];
      const lastEl = items[items.length - 1];
      const activeEl = document.activeElement as HTMLElement | null;

      if (e.shiftKey) {
        if (activeEl === firstEl || !container.contains(activeEl)) {
          e.preventDefault();
          lastEl.focus();
        }
      } else if (activeEl === lastEl || !container.contains(activeEl)) {
        e.preventDefault();
        firstEl.focus();
      }
    };

    document.addEventListener("keydown", onKey, true);
    return () => {
      document.removeEventListener("keydown", onKey, true);
      // Restore focus to the trigger on close.
      if (previouslyFocused && typeof previouslyFocused.focus === "function") {
        previouslyFocused.focus();
      }
    };
  }, [active]);

  return containerRef;
}
