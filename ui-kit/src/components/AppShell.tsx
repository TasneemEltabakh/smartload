/* ============================================================================
   AppShell -- sidebar + topbar + content outlet
   ----------------------------------------------------------------------------
   The page frame. A fixed-width rail on the left, a sticky topbar, and the
   scrolling content area. Sidebar and topbar are passed in so the host app
   keeps control of nav and chrome.
   ============================================================================ */
import type { CSSProperties, ReactNode } from "react";

export interface AppShellProps {
  sidebar: ReactNode;
  topbar?: ReactNode;
  children?: ReactNode;
  /** Sidebar column width in px. */
  railWidth?: number;
  /** Optional max content width; centers the content when set. */
  contentMaxWidth?: number;
  className?: string;
  style?: CSSProperties;
}

export function AppShell({
  sidebar,
  topbar,
  children,
  railWidth = 248,
  contentMaxWidth,
  className,
  style,
}: AppShellProps) {
  return (
    <div
      className={className}
      style={{
        display: "grid",
        gridTemplateColumns: `${railWidth}px 1fr`,
        minHeight: "100vh",
        background: "var(--sl-bg)",
        color: "var(--sl-text)",
        fontFamily: "var(--sl-font-sans)",
        fontSize: 14,
        lineHeight: 1.5,
        ...style,
      }}
    >
      {sidebar}
      <div style={{ minWidth: 0, display: "flex", flexDirection: "column" }}>
        {topbar}
        <main
          style={{
            padding: "24px 28px 56px",
            width: "100%",
            maxWidth: contentMaxWidth,
            margin: contentMaxWidth ? "0 auto" : undefined,
            display: "flex",
            flexDirection: "column",
            gap: 22,
          }}
        >
          {children}
        </main>
      </div>
    </div>
  );
}
