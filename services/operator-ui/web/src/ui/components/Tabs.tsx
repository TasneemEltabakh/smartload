/* ============================================================================
   Tabs -- segmented control
   ----------------------------------------------------------------------------
   Mono segmented switch (e.g. ACTIVE / SHADOW, 15m / 1h / 6h). Controlled.
   ============================================================================ */
import type { ReactNode } from "react";

export interface TabItem {
  id: string;
  label: ReactNode;
}

export interface TabsProps {
  items: TabItem[];
  value: string;
  onChange: (id: string) => void;
  className?: string;
}

export function Tabs({ items, value, onChange, className }: TabsProps) {
  return (
    <div
      className={className}
      role="tablist"
      style={{
        display: "inline-flex",
        background: "var(--sl-surface-sunk)",
        border: "1px solid var(--sl-hairline)",
        borderRadius: 9,
        padding: 3,
      }}
    >
      {items.map((item) => {
        const on = item.id === value;
        return (
          <button
            key={item.id}
            type="button"
            role="tab"
            aria-selected={on}
            onClick={() => onChange(item.id)}
            style={{
              fontFamily: "var(--sl-font-mono)",
              fontSize: 11,
              fontWeight: 600,
              color: on ? "var(--sl-text)" : "var(--sl-text-low)",
              background: on ? "var(--sl-surface)" : "transparent",
              border: "none",
              borderRadius: 6,
              padding: "5px 11px",
              cursor: "pointer",
              boxShadow: on ? "var(--sl-shadow-1)" : "none",
              transition: "color var(--sl-dur-fast), background var(--sl-dur-fast)",
            }}
          >
            {item.label}
          </button>
        );
      })}
    </div>
  );
}
