/* ============================================================================
   Sidebar -- brand, grouped nav, footer status
   ----------------------------------------------------------------------------
   The console rail. Nav items are grouped under mono section labels; the active
   item carries a mint edge + tint. Navigation is delegated: each item declares
   an onSelect, so the host app can wire it to its own router.

   Responsive: pass `collapsed` to render an icon-only rail (labels, tags and
   group headers hide; item titles become tooltips). The AppShell drives the
   width; this component only adapts its contents.
   ============================================================================ */
import type { ReactNode } from "react";
import { Wordmark } from "../brand/Wordmark";
import { Logomark } from "../brand/Logomark";

export interface NavItem {
  id: string;
  label: ReactNode;
  icon?: ReactNode;
  /** Trailing mono tag (e.g. "LIVE", a shortcut key). */
  tag?: ReactNode;
  /** Plain-text title used as the tooltip when the rail is collapsed. */
  title?: string;
}

export interface NavGroup {
  label: string;
  items: NavItem[];
}

export interface SidebarProps {
  groups: NavGroup[];
  /** Currently active item id. */
  activeId?: string;
  onSelect?: (id: string) => void;
  /** Sub-label under the wordmark. */
  brandSub?: string;
  /** Footer content (operator card, plane-health chip). */
  footer?: ReactNode;
  /** Icon-only rail: hide labels, tags and group headers. */
  collapsed?: boolean;
  className?: string;
}

export function Sidebar({
  groups,
  activeId,
  onSelect,
  brandSub,
  footer,
  collapsed = false,
  className,
}: SidebarProps) {
  return (
    <aside
      className={`sl-sidebar${collapsed ? " sl-sidebar-collapsed" : ""}${className ? ` ${className}` : ""}`}
      style={{
        position: "sticky",
        top: 0,
        alignSelf: "start",
        height: "100vh",
        background: "var(--sl-rail-grad)",
        borderRight: "1px solid var(--sl-hairline)",
        display: "flex",
        flexDirection: "column",
        padding: collapsed ? "22px 12px" : "22px 16px",
        gap: 4,
        zIndex: 20,
        overflowY: "auto",
      }}
    >
      <div
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: collapsed ? "center" : "flex-start",
          gap: 11,
          padding: collapsed ? "4px 0 18px" : "4px 8px 18px",
          borderBottom: "1px solid var(--sl-hairline-soft)",
          marginBottom: 6,
        }}
      >
        <Logomark size={34} animated />
        {collapsed ? null : <Wordmark size={19} sub={brandSub} />}
      </div>

      {groups.map((group) => (
        <div key={group.label}>
          {collapsed ? (
            <div style={{ height: 14 }} aria-hidden />
          ) : (
            <div
              style={{
                fontFamily: "var(--sl-font-mono)",
                fontSize: 10,
                fontWeight: 600,
                letterSpacing: "1.8px",
                color: "var(--sl-text-low)",
                textTransform: "uppercase",
                padding: "14px 10px 6px",
              }}
            >
              {group.label}
            </div>
          )}
          <nav style={{ display: "flex", flexDirection: "column", gap: 2 }} aria-label={group.label}>
            {group.items.map((item) => {
              const active = item.id === activeId;
              return (
                <button
                  key={item.id}
                  type="button"
                  className="sl-nav-item"
                  aria-current={active ? "page" : undefined}
                  title={collapsed ? item.title ?? undefined : undefined}
                  onClick={() => onSelect?.(item.id)}
                  style={{
                    position: "relative",
                    display: "flex",
                    alignItems: "center",
                    justifyContent: collapsed ? "center" : "flex-start",
                    gap: 12,
                    padding: collapsed ? "10px 0" : "9px 11px",
                    borderRadius: "var(--sl-radius-sm)",
                    color: active ? "var(--sl-on-mint-tint)" : "var(--sl-text-mid)",
                    background: active ? "var(--sl-mint-tint)" : "transparent",
                    border: "1px solid",
                    borderColor: active ? "var(--sl-mint-line)" : "transparent",
                    fontFamily: "var(--sl-font-sans)",
                    fontSize: 13.5,
                    fontWeight: active ? 600 : 500,
                    cursor: "pointer",
                    textAlign: "left",
                    width: "100%",
                    transition: "background var(--sl-dur-fast), color var(--sl-dur-fast)",
                  }}
                >
                  {active && !collapsed ? (
                    <span
                      aria-hidden
                      style={{
                        position: "absolute",
                        left: -16,
                        top: 8,
                        bottom: 8,
                        width: 3,
                        borderRadius: "0 3px 3px 0",
                        background: "var(--sl-mint)",
                      }}
                    />
                  ) : null}
                  {item.icon ? (
                    <span style={{ width: 17, height: 17, flex: "0 0 auto", display: "inline-flex" }}>
                      {item.icon}
                    </span>
                  ) : null}
                  {collapsed ? null : item.label}
                  {!collapsed && item.tag != null ? (
                    <span
                      style={{
                        marginLeft: "auto",
                        fontFamily: "var(--sl-font-mono)",
                        fontSize: 9,
                        color: "var(--sl-text-low)",
                        border: "1px solid var(--sl-hairline)",
                        borderRadius: 5,
                        padding: "1px 5px",
                        letterSpacing: "0.5px",
                      }}
                    >
                      {item.tag}
                    </span>
                  ) : null}
                </button>
              );
            })}
          </nav>
        </div>
      ))}

      {footer != null && !collapsed ? (
        <div
          style={{
            marginTop: "auto",
            paddingTop: 14,
            borderTop: "1px solid var(--sl-hairline-soft)",
          }}
        >
          {footer}
        </div>
      ) : null}
    </aside>
  );
}
