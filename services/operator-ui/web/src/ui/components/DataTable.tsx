/* ============================================================================
   DataTable -- sticky header, sortable-ready, mono numerics
   ----------------------------------------------------------------------------
   A typed table. Columns declare their own cell renderer; numeric columns
   right-align and render in mono. Clicking a sortable header reports the sort
   request via onSort (sorting itself is left to the caller's data layer).
   ============================================================================ */
import type { ReactNode } from "react";

export type SortDir = "asc" | "desc";

export interface Column<Row> {
  key: string;
  header: ReactNode;
  /** Cell renderer for this column. */
  render: (row: Row) => ReactNode;
  /** Right-align + mono (for measurements). */
  numeric?: boolean;
  /** Mark this column sortable (emits onSort on header click). */
  sortable?: boolean;
}

export interface DataTableProps<Row> {
  columns: Column<Row>[];
  rows: Row[];
  /** Stable key for each row. */
  rowKey: (row: Row) => string;
  /** Visually de-emphasize a row (e.g. an isolated node). */
  rowMuted?: (row: Row) => boolean;
  /** Current sort, for the header indicator. */
  sort?: { key: string; dir: SortDir };
  onSort?: (key: string) => void;
  className?: string;
}

export function DataTable<Row>({
  columns,
  rows,
  rowKey,
  rowMuted,
  sort,
  onSort,
  className,
}: DataTableProps<Row>) {
  return (
    <div className={className} style={{ width: "100%", overflow: "auto" }}>
      <table style={{ width: "100%", borderCollapse: "collapse" }}>
        <thead>
          <tr>
            {columns.map((col) => {
              const active = sort?.key === col.key;
              const interactive = Boolean(col.sortable && onSort);
              const ariaSort = !col.sortable
                ? undefined
                : active
                  ? sort?.dir === "asc"
                    ? "ascending"
                    : "descending"
                  : "none";
              return (
                <th
                  key={col.key}
                  scope="col"
                  role="columnheader"
                  aria-sort={ariaSort}
                  tabIndex={interactive ? 0 : undefined}
                  onClick={interactive ? () => onSort?.(col.key) : undefined}
                  onKeyDown={
                    interactive
                      ? (e) => {
                          if (e.key === "Enter" || e.key === " ") {
                            e.preventDefault();
                            onSort?.(col.key);
                          }
                        }
                      : undefined
                  }
                  style={{
                    position: "sticky",
                    top: 0,
                    zIndex: 1,
                    background: "var(--sl-surface)",
                    fontFamily: "var(--sl-font-mono)",
                    fontSize: 9,
                    letterSpacing: "1px",
                    color: active ? "var(--sl-text-mid)" : "var(--sl-text-low)",
                    textTransform: "uppercase",
                    textAlign: col.numeric ? "right" : "left",
                    padding: "10px 14px",
                    borderBottom: "1px solid var(--sl-hairline)",
                    fontWeight: 600,
                    cursor: col.sortable && onSort ? "pointer" : "default",
                    userSelect: "none",
                    whiteSpace: "nowrap",
                  }}
                >
                  {col.header}
                  {active ? <span aria-hidden> {sort?.dir === "asc" ? "▲" : "▼"}</span> : null}
                </th>
              );
            })}
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => {
            const muted = rowMuted?.(row) ?? false;
            return (
              <tr key={rowKey(row)} style={{ opacity: muted ? 0.55 : 1 }}>
                {columns.map((col) => (
                  <td
                    key={col.key}
                    style={{
                      padding: "11px 14px",
                      borderBottom: "1px solid var(--sl-hairline-soft)",
                      textAlign: col.numeric ? "right" : "left",
                      fontFamily: col.numeric ? "var(--sl-font-mono)" : "var(--sl-font-sans)",
                      fontSize: col.numeric ? 12 : 13,
                      color: "var(--sl-text)",
                      fontVariantNumeric: col.numeric ? "tabular-nums" : undefined,
                    }}
                  >
                    {col.render(row)}
                  </td>
                ))}
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
