/* ============================================================================
   AppShell -- sidebar + topbar + content outlet (responsive)
   ----------------------------------------------------------------------------
   The page frame. A rail on the left, a sticky topbar, and the scrolling
   content area. Sidebar and topbar are passed in so the host app keeps control
   of nav and chrome.

   Responsive behaviour (driven by the .sl-shell* classes in styles.css):
     >= 1024px  full rail (var(--sl-rail-width)), grid two-column.
     768-1023px collapsed icon-rail (var(--sl-rail-width-collapsed)). The host
                may pass `collapsed` to its Sidebar to hide labels.
     < 768px    rail leaves the flow; it becomes an off-canvas drawer toggled by
                the Topbar's menu button (host wires onMenuToggle -> menuOpen).

   The grid template lives in CSS (media queries), so railWidth is exposed as
   the --sl-rail-width custom property rather than an inline column width.
   ============================================================================ */
import type { CSSProperties, ReactNode } from "react";

export interface AppShellProps {
  sidebar: ReactNode;
  topbar?: ReactNode;
  children?: ReactNode;
  /** Sidebar column width in px (full rail). */
  railWidth?: number;
  /** Optional max content width; centers the content when set. */
  contentMaxWidth?: number;
  /**
   * Mobile off-canvas state. When the viewport is below the drawer breakpoint
   * the rail is hidden until `menuOpen` is true; a scrim is shown over the
   * content. The host toggles this from the Topbar menu button.
   */
  menuOpen?: boolean;
  /** Called when the mobile scrim is clicked (host should close the menu). */
  onMenuClose?: () => void;
  className?: string;
  style?: CSSProperties;
}

export function AppShell({
  sidebar,
  topbar,
  children,
  railWidth,
  contentMaxWidth,
  menuOpen = false,
  onMenuClose,
  className,
  style,
}: AppShellProps) {
  const shellStyle: CSSProperties = {
    ...(railWidth ? ({ ["--sl-rail-width" as string]: `${railWidth}px` } as CSSProperties) : null),
    ...style,
  };

  return (
    <div
      className={`sl-shell${menuOpen ? " sl-shell-menu-open" : ""}${className ? ` ${className}` : ""}`}
      style={shellStyle}
    >
      <div className="sl-shell-rail">{sidebar}</div>

      {/* Scrim shown only on the mobile drawer breakpoint while the menu is open. */}
      <div
        className="sl-shell-scrim"
        role="presentation"
        aria-hidden={!menuOpen}
        onClick={onMenuClose}
      />

      <div className="sl-shell-main">
        {topbar}
        <main
          className="sl-shell-content"
          style={{
            maxWidth: contentMaxWidth,
            margin: contentMaxWidth ? "0 auto" : undefined,
          }}
        >
          {children}
        </main>
      </div>
    </div>
  );
}
