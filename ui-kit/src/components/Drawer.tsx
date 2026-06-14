/* ============================================================================
   Drawer -- side panel over a scrim
   ----------------------------------------------------------------------------
   Slides in from the right for detail / diff views (policy diff, decision
   detail). Escape and scrim click both request close.
   ============================================================================ */
import { useEffect, type ReactNode } from "react";

export interface DrawerProps {
  open: boolean;
  onClose: () => void;
  title?: ReactNode;
  /** Width of the panel in px. */
  width?: number;
  children?: ReactNode;
}

export function Drawer({ open, onClose, title, width = 420, children }: DrawerProps) {
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  return (
    <div
      aria-hidden={!open}
      style={{
        position: "fixed",
        inset: 0,
        zIndex: 80,
        pointerEvents: open ? "auto" : "none",
      }}
    >
      <div
        role="presentation"
        onClick={onClose}
        style={{
          position: "absolute",
          inset: 0,
          background: "rgba(5, 7, 10, 0.45)",
          opacity: open ? 1 : 0,
          transition: "opacity var(--sl-dur-mid) var(--sl-ease)",
        }}
      />
      <aside
        role="dialog"
        aria-modal="true"
        style={{
          position: "absolute",
          top: 0,
          right: 0,
          height: "100%",
          width,
          maxWidth: "92vw",
          background: "var(--sl-surface)",
          borderLeft: "1px solid var(--sl-hairline)",
          boxShadow: "var(--sl-shadow-2)",
          transform: open ? "translateX(0)" : "translateX(100%)",
          transition: "transform var(--sl-dur-mid) var(--sl-ease)",
          display: "flex",
          flexDirection: "column",
        }}
      >
        {title != null ? (
          <header
            style={{
              padding: "16px 20px",
              borderBottom: "1px solid var(--sl-hairline-soft)",
              fontSize: 15,
              fontWeight: 700,
              color: "var(--sl-text)",
            }}
          >
            {title}
          </header>
        ) : null}
        <div style={{ padding: "16px 20px", overflow: "auto", flex: 1 }}>{children}</div>
      </aside>
    </div>
  );
}
