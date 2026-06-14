/* ============================================================================
   Modal -- centered dialog over a scrim
   ----------------------------------------------------------------------------
   Used for deliberate confirmations (arming safe_mode, committing policy).
   Renders nothing when closed; Escape and scrim click both request close.
   ============================================================================ */
import { useEffect, type ReactNode } from "react";
import { useFocusTrap } from "../../lib/useFocusTrap";

export interface ModalProps {
  open: boolean;
  onClose: () => void;
  title?: ReactNode;
  /** Footer actions (buttons). */
  footer?: ReactNode;
  children?: ReactNode;
  /** Max width of the dialog in px. */
  width?: number;
}

export function Modal({ open, onClose, title, footer, children, width = 460 }: ModalProps) {
  const trapRef = useFocusTrap<HTMLDivElement>(open);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  if (!open) return null;

  return (
    <div
      role="presentation"
      onClick={onClose}
      style={{
        position: "fixed",
        inset: 0,
        zIndex: 80,
        background: "var(--sl-scrim)",
        backdropFilter: "blur(2px)",
        display: "grid",
        placeItems: "center",
        padding: "var(--sl-space-4)",
      }}
    >
      <div
        ref={trapRef}
        role="dialog"
        aria-modal="true"
        onClick={(e) => e.stopPropagation()}
        style={{
          width: "100%",
          maxWidth: width,
          background: "var(--sl-surface)",
          border: "1px solid var(--sl-hairline)",
          borderRadius: "var(--sl-radius-lg)",
          boxShadow: "var(--sl-shadow-2)",
          overflow: "hidden",
        }}
      >
        {title != null ? (
          <header
            style={{
              padding: "16px 20px 12px",
              borderBottom: "1px solid var(--sl-hairline-soft)",
              fontSize: 15,
              fontWeight: 700,
              color: "var(--sl-text)",
            }}
          >
            {title}
          </header>
        ) : null}
        <div style={{ padding: "16px 20px", color: "var(--sl-text-mid)", fontSize: 13 }}>
          {children}
        </div>
        {footer != null ? (
          <footer
            style={{
              padding: "12px 20px 16px",
              borderTop: "1px solid var(--sl-hairline-soft)",
              display: "flex",
              justifyContent: "flex-end",
              gap: 10,
            }}
          >
            {footer}
          </footer>
        ) : null}
      </div>
    </div>
  );
}
