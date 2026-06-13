import { useEffect, useMemo, useState } from "react";
import {
  Calendar,
  ChevronLeft,
  ChevronRight,
  ChevronsLeft,
  ChevronsRight,
  Clock,
  Download,
  ScrollText,
  ShieldCheck,
  TrendingUp,
  Users,
} from "lucide-react";

import {
  api,
  type AuditCounts,
  type AuditRow,
  type ScalingAuditRow,
} from "../api";

const REFRESH_MS = 10_000;

type RowClass = "policy" | "scaling";
type ActionFilter = "all" | "scale_out" | "scale_in" | "update";
type TimeRange = "1h" | "24h" | "7d" | "30d" | "all";

const LIMIT_CHOICES = [25, 50, 100, 250, 500];
const PAGE_SIZE = 12;

const TIME_RANGES: Array<{ value: TimeRange; label: string; ms: number | null }> = [
  { value: "1h", label: "Last hour", ms: 60 * 60 * 1000 },
  { value: "24h", label: "Last 24 hours", ms: 24 * 60 * 60 * 1000 },
  { value: "7d", label: "Last 7 days", ms: 7 * 24 * 60 * 60 * 1000 },
  { value: "30d", label: "Last 30 days", ms: 30 * 24 * 60 * 60 * 1000 },
  { value: "all", label: "All", ms: null },
];

const EMDASH = "—";

interface UnifiedRow {
  time: string;
  cls: RowClass;
  action: string;
  version: number | null;
  field: string;
  oldValue: string | null;
  newValue: string | null;
  actor: string;
  source: string;
}

function fmt(value: unknown): string | null {
  if (value === null || value === undefined) return null;
  if (typeof value === "string") return value;
  return JSON.stringify(value);
}

function shortTime(iso: string): string {
  return iso.replace("T", " ").replace(/\.\d+.*/, "").replace(/\+.*$/, "");
}

// Live wall-clock (HH:MM:SS) for the "Last updated" KPI, matching the mockup.
function clockNow(): string {
  return new Date().toLocaleTimeString([], { hour12: false });
}

export default function AuditPage() {
  const [policyRows, setPolicyRows] = useState<AuditRow[]>([]);
  const [scalingRows, setScalingRows] = useState<ScalingAuditRow[]>([]);
  const [counts, setCounts] = useState<AuditCounts | null>(null);

  const [kind, setKind] = useState<"all" | "policy" | "scaling">("all");
  const [actorFilter, setActorFilter] = useState<string>("");
  const [action, setAction] = useState<ActionFilter>("all");
  const [range, setRange] = useState<TimeRange>("7d");
  const [limit, setLimit] = useState<number>(50);
  const [page, setPage] = useState<number>(0);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [clock, setClock] = useState<string>(clockNow());

  async function loadAll() {
    setBusy(true);
    try {
      const [pol, sca, cts] = await Promise.all([
        api.auditPolicy(limit),
        api.auditScaling(limit),
        api.getAuditCounts().catch(() => null),
      ]);
      setPolicyRows(pol);
      setScalingRows(sca);
      setCounts(cts);
      setError(null);
      setClock(clockNow());
    } catch (err: any) {
      setError(err?.message || "could not load audit data");
    } finally {
      setBusy(false);
    }
  }

  useEffect(() => {
    loadAll();
    const id = setInterval(loadAll, REFRESH_MS);
    return () => clearInterval(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [limit]);

  // Live clock tick (independent of the data poll) for the "Last updated" KPI.
  useEffect(() => {
    const id = setInterval(() => setClock(clockNow()), 1000);
    return () => clearInterval(id);
  }, []);

  // Merge policy + scaling audit rows into one time-ordered table. Each kind
  // maps onto a common shape; the change is carried as discrete old/new values
  // so the table can render them as two color-coded columns.
  const unified: UnifiedRow[] = useMemo(() => {
    const rows: UnifiedRow[] = [];
    for (const r of policyRows) {
      rows.push({
        time: r.time,
        cls: "policy",
        action: "update",
        version: r.policy_version,
        field: r.field,
        oldValue: fmt(r.old_value),
        newValue: fmt(r.new_value),
        actor: r.actor,
        source: "policy-manager",
      });
    }
    for (const r of scalingRows) {
      // Scaling rows have no field-level diff; the new value is the resulting
      // instance count and the reason rides along on the field cell.
      rows.push({
        time: r.time,
        cls: "scaling",
        action: r.action,
        version: null,
        field: r.reason ? `instance_count (${r.reason})` : "instance_count",
        oldValue: null,
        newValue: String(r.instance_count),
        actor: "autoscaler",
        source: "autoscaler",
      });
    }
    rows.sort((a, b) => (a.time < b.time ? 1 : a.time > b.time ? -1 : 0));
    return rows;
  }, [policyRows, scalingRows]);

  const filtered = useMemo(() => {
    const needle = actorFilter.trim().toLowerCase();
    const rangeMs = TIME_RANGES.find((t) => t.value === range)?.ms ?? null;
    const floor = rangeMs == null ? null : Date.now() - rangeMs;
    return unified.filter((r) => {
      if (kind !== "all" && r.cls !== kind) return false;
      if (action !== "all" && r.action !== action) return false;
      if (needle && !`${r.actor} ${r.field}`.toLowerCase().includes(needle)) return false;
      if (floor != null) {
        const t = Date.parse(r.time);
        if (!Number.isNaN(t) && t < floor) return false;
      }
      return true;
    });
  }, [unified, kind, action, actorFilter, range]);

  const pageCount = Math.max(1, Math.ceil(filtered.length / PAGE_SIZE));
  const pageRows = filtered.slice(page * PAGE_SIZE, (page + 1) * PAGE_SIZE);

  useEffect(() => {
    if (page >= pageCount) setPage(0);
  }, [pageCount, page]);

  // Client-side CSV export of the currently-filtered rows.
  function exportCsv() {
    const headers = [
      "time",
      "kind",
      "action",
      "version",
      "field",
      "old",
      "new",
      "actor",
      "source",
    ];
    const esc = (v: string | number | null) => {
      const s = v == null ? "" : String(v);
      return `"${s.replace(/"/g, '""')}"`;
    };
    const lines = [headers.map(esc).join(",")];
    for (const r of filtered) {
      lines.push(
        [
          shortTime(r.time),
          r.cls,
          r.action,
          r.version,
          r.field,
          r.oldValue,
          r.newValue,
          r.actor,
          r.source,
        ]
          .map(esc)
          .join(","),
      );
    }
    const blob = new Blob([lines.join("\r\n")], { type: "text/csv;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `smartload-audit-${filtered.length}.csv`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  }

  return (
    <>
      <div className="page-header">
        <div>
          <h2>Audit</h2>
          <div className="subtitle">
            Track changes, decisions and scaling actions across SmartLoad
          </div>
        </div>
        <div className="header-actions">
          <button
            className="secondary"
            onClick={exportCsv}
            disabled={filtered.length === 0}
            title="Download the currently-filtered rows as CSV"
          >
            <Download size={14} /> Export
          </button>
        </div>
      </div>

      {/* ── KPI row ────────────────────────────────────────────────── */}
      <div className="kpi-row">
        <div className="kpi cyan">
          <div className="kpi-label"><span className="kpi-icon"><ScrollText size={14} /></span> Total events</div>
          <div className="kpi-value">{counts?.total_events ?? unified.length}</div>
          <div className="kpi-trend">policy + scaling + anomaly</div>
        </div>
        <div className="kpi violet">
          <div className="kpi-label"><span className="kpi-icon"><ShieldCheck size={14} /></span> Policy changes</div>
          <div className="kpi-value">{counts?.policy_changes ?? policyRows.length}</div>
          <div className="kpi-trend">field-level rows</div>
        </div>
        <div className="kpi green">
          <div className="kpi-label"><span className="kpi-icon"><TrendingUp size={14} /></span> Scaling actions</div>
          <div className="kpi-value">{counts?.scaling_actions ?? scalingRows.length}</div>
          <div className="kpi-trend">autoscaler decisions</div>
        </div>
        <div className="kpi amber">
          <div className="kpi-label"><span className="kpi-icon"><Users size={14} /></span> Actors</div>
          <div className="kpi-value">{counts?.actors_unique ?? EMDASH}</div>
          <div className="kpi-trend">unique users / services</div>
        </div>
        <div className="kpi pink">
          <div className="kpi-label"><span className="kpi-icon"><Clock size={14} /></span> Last updated</div>
          <div className="kpi-value">{clock}</div>
          <div className="kpi-trend">{busy ? "refreshing…" : `auto-refresh ${REFRESH_MS / 1000}s`}</div>
        </div>
      </div>

      {/* ── Toolbar ────────────────────────────────────────────────── */}
      <div className="audit-toolbar">
        <div className="chip-group" role="group" aria-label="Kind">
          <span className="chip-label">Kind</span>
          <button className={kind === "all" ? "chip on" : "chip"} onClick={() => setKind("all")}>All</button>
          <button className={kind === "policy" ? "chip on" : "chip"} onClick={() => setKind("policy")}>Policy</button>
          <button className={kind === "scaling" ? "chip on" : "chip"} onClick={() => setKind("scaling")}>Scaling</button>
        </div>

        <div className="chip-group" role="group" aria-label="Action">
          <span className="chip-label">Action</span>
          <button className={action === "all" ? "chip on" : "chip"} onClick={() => setAction("all")}>All</button>
          <button className={action === "scale_out" ? "chip on" : "chip"} onClick={() => setAction("scale_out")}>scale_out</button>
          <button className={action === "scale_in" ? "chip on" : "chip"} onClick={() => setAction("scale_in")}>scale_in</button>
        </div>

        <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
          <span className="chip-label">Actor / field</span>
          <input
            value={actorFilter}
            onChange={(e) => setActorFilter(e.target.value)}
            placeholder="e.g. demo-test, safe_mode, max_backends"
            style={{ minWidth: 220 }}
          />
        </div>

        <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
          <span className="chip-label"><Calendar size={13} style={{ verticalAlign: "-2px", marginRight: 4 }} />Time range</span>
          <select value={range} onChange={(e) => setRange(e.target.value as TimeRange)}>
            {TIME_RANGES.map((t) => (<option key={t.value} value={t.value}>{t.label}</option>))}
          </select>
        </div>

        <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
          <span className="chip-label">Limit</span>
          <select value={limit} onChange={(e) => setLimit(Number(e.target.value))}>
            {LIMIT_CHOICES.map((n) => (<option key={n} value={n}>{n}</option>))}
          </select>
        </div>

        <span className="toolbar-spacer" />

        <button onClick={loadAll} disabled={busy}>{busy ? "loading…" : "Refresh"}</button>
      </div>

      {/* ── Table ──────────────────────────────────────────────────── */}
      <div className="audit-table-wrap">
        <div className="head">
          <div>
            <h3>Audit log</h3>
            <p className="meta">
              Showing {filtered.length} event{filtered.length === 1 ? "" : "s"} (one row per event, newest first)
              {error ? <span style={{ color: "var(--bad)" }}> · {error}</span> : null}
            </p>
          </div>
        </div>
        <table>
          <thead>
            <tr>
              <th style={{ width: 170 }}>Time</th>
              <th style={{ width: 90 }}>Kind</th>
              <th style={{ width: 110 }}>Action</th>
              <th style={{ width: 70 }}>Version</th>
              <th style={{ width: 180 }}>Field</th>
              <th style={{ width: 110 }}>Old</th>
              <th style={{ width: 110 }}>New</th>
              <th style={{ width: 150 }}>Actor</th>
              <th style={{ width: 150 }}>Source</th>
            </tr>
          </thead>
          <tbody>
            {pageRows.length === 0 ? (
              <tr><td colSpan={9} className="empty">No events match the current filter.</td></tr>
            ) : (
              pageRows.map((row, i) => (
                <tr key={`${row.time}-${i}`}>
                  <td><code>{shortTime(row.time)}</code></td>
                  <td><span className={`badge-class ${row.cls}`}>{row.cls}</span></td>
                  <td><span className={`badge-action ${row.action}`}>{row.action}</span></td>
                  <td>{row.version != null ? <code>v{row.version}</code> : <span className="muted">{EMDASH}</span>}</td>
                  <td><code>{row.field}</code></td>
                  <td>
                    {row.oldValue != null
                      ? <span className="audit-val old">{row.oldValue}</span>
                      : <span className="audit-val empty">{EMDASH}</span>}
                  </td>
                  <td>
                    {row.newValue != null
                      ? <span className="audit-val new">{row.newValue}</span>
                      : <span className="audit-val empty">{EMDASH}</span>}
                  </td>
                  <td>{row.actor}</td>
                  <td><code>{row.source}</code></td>
                </tr>
              ))
            )}
          </tbody>
        </table>

        {filtered.length > PAGE_SIZE ? (
          <div className="pager" style={{ padding: "10px 18px" }}>
            <span>
              {page * PAGE_SIZE + 1}{EMDASH}{Math.min(filtered.length, (page + 1) * PAGE_SIZE)} of {filtered.length} event{filtered.length === 1 ? "" : "s"}
            </span>
            <div className="pager-pages">
              <button onClick={() => setPage(0)} disabled={page === 0}><ChevronsLeft size={14} /></button>
              <button onClick={() => setPage((p) => Math.max(0, p - 1))} disabled={page === 0}><ChevronLeft size={14} /></button>
              {Array.from({ length: Math.min(5, pageCount) }, (_, i) => {
                const base = Math.max(0, Math.min(pageCount - 5, page - 2));
                const idx = base + i;
                if (idx >= pageCount) return null;
                return (
                  <button
                    key={idx}
                    className={idx === page ? "on" : ""}
                    onClick={() => setPage(idx)}
                  >
                    {idx + 1}
                  </button>
                );
              })}
              <button onClick={() => setPage((p) => Math.min(pageCount - 1, p + 1))} disabled={page >= pageCount - 1}><ChevronRight size={14} /></button>
              <button onClick={() => setPage(pageCount - 1)} disabled={page >= pageCount - 1}><ChevronsRight size={14} /></button>
            </div>
          </div>
        ) : null}
      </div>
    </>
  );
}
