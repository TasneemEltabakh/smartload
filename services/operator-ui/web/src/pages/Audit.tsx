import { useEffect, useMemo, useState } from "react";
import {
  AlertTriangle,
  ChevronLeft,
  ChevronRight,
  ChevronsLeft,
  ChevronsRight,
  Clock,
  Download,
  ScrollText,
  ShieldCheck,
  TrendingUp,
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

const LIMIT_CHOICES = [25, 50, 100, 250, 500];
const PAGE_SIZE = 12;

interface UnifiedRow {
  time: string;
  cls: RowClass;
  action: string;
  version: number | null;
  field: string;
  view: string;
  actor: string;
  source: string;
}

function fmt(value: unknown): string {
  if (value === null || value === undefined) return "—";
  if (typeof value === "string") return value;
  return JSON.stringify(value);
}

function shortTime(iso: string): string {
  return iso.replace("T", " ").replace(/\.\d+.*/, "").replace(/\+.*$/, "");
}

function timeAgo(iso: string | null): string {
  if (!iso) return "—";
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return iso;
  const s = Math.max(0, (Date.now() - t) / 1000);
  if (s < 60) return `${Math.floor(s)}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  return `${Math.floor(s / 3600)}h ago`;
}

export default function AuditPage() {
  const [policyRows, setPolicyRows] = useState<AuditRow[]>([]);
  const [scalingRows, setScalingRows] = useState<ScalingAuditRow[]>([]);
  const [counts, setCounts] = useState<AuditCounts | null>(null);

  const [kind, setKind] = useState<"all" | "policy" | "scaling">("all");
  const [actorFilter, setActorFilter] = useState<string>("");
  const [action, setAction] = useState<ActionFilter>("all");
  const [limit, setLimit] = useState<number>(50);
  const [page, setPage] = useState<number>(0);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [lastLoaded, setLastLoaded] = useState<string | null>(null);

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
      setLastLoaded(new Date().toISOString().replace("T", " ").replace(/\..*/, ""));
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

  // Merge into a single time-ordered table.
  const unified: UnifiedRow[] = useMemo(() => {
    const rows: UnifiedRow[] = [];
    for (const r of policyRows) {
      rows.push({
        time: r.time,
        cls: "policy",
        action: "update",
        version: r.policy_version,
        field: r.field,
        view: `${fmt(r.old_value)} → ${fmt(r.new_value)}`,
        actor: r.actor,
        source: "policy-manager",
      });
    }
    for (const r of scalingRows) {
      rows.push({
        time: r.time,
        cls: "scaling",
        action: r.action,
        version: null,
        field: "instance_count",
        view: `${r.instance_count} backend(s)${r.reason ? ` · ${r.reason}` : ""}`,
        actor: "autoscaler",
        source: "autoscaler",
      });
    }
    rows.sort((a, b) => (a.time < b.time ? 1 : a.time > b.time ? -1 : 0));
    return rows;
  }, [policyRows, scalingRows]);

  const filtered = useMemo(() => {
    const needle = actorFilter.trim().toLowerCase();
    return unified.filter((r) => {
      if (kind !== "all" && r.cls !== kind) return false;
      if (action !== "all" && r.action !== action) return false;
      if (needle && !`${r.actor} ${r.field}`.toLowerCase().includes(needle)) return false;
      return true;
    });
  }, [unified, kind, action, actorFilter]);

  const pageCount = Math.max(1, Math.ceil(filtered.length / PAGE_SIZE));
  const pageRows = filtered.slice(page * PAGE_SIZE, (page + 1) * PAGE_SIZE);

  useEffect(() => {
    if (page >= pageCount) setPage(0);
  }, [pageCount, page]);

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
          <span className="refresh-chip">
            <span className="pulse" /> Auto-refresh {REFRESH_MS / 1000}s
          </span>
          <button className="secondary" disabled title="CSV export ships next slice"><Download size={14} /> Export</button>
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
          <div className="kpi-label"><span className="kpi-icon"><AlertTriangle size={14} /></span> Active alerts</div>
          <div className="kpi-value">{counts?.active_alerts ?? 0}</div>
          <div className="kpi-trend">5-min anomaly window</div>
        </div>
        <div className="kpi pink">
          <div className="kpi-label"><span className="kpi-icon"><Clock size={14} /></span> Last event</div>
          <div className="kpi-value" style={{ fontSize: 22 }}>{timeAgo(counts?.last_event_at ?? unified[0]?.time ?? null)}</div>
          <div className="kpi-trend">{lastLoaded ? `loaded ${lastLoaded}` : "—"}</div>
        </div>
      </div>

      {/* ── Toolbar ────────────────────────────────────────────────── */}
      <div className="audit-toolbar">
        <div className="chip-group" role="group" aria-label="Event class">
          <span className="chip-label">Class</span>
          <button className={kind === "all" ? "chip on" : "chip"} onClick={() => setKind("all")}>All</button>
          <button className={kind === "policy" ? "chip on" : "chip"} onClick={() => setKind("policy")}>Policy</button>
          <button className={kind === "scaling" ? "chip on" : "chip"} onClick={() => setKind("scaling")}>Scaling</button>
        </div>

        <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
          <span className="chip-label">Actor / field</span>
          <input
            value={actorFilter}
            onChange={(e) => setActorFilter(e.target.value)}
            placeholder="e.g. demo-test, safe_mode"
            style={{ minWidth: 200 }}
          />
        </div>

        <div className="chip-group" role="group" aria-label="Action">
          <span className="chip-label">Action</span>
          <button className={action === "all" ? "chip on" : "chip"} onClick={() => setAction("all")}>All</button>
          <button className={action === "update" ? "chip on" : "chip"} onClick={() => setAction("update")}>update</button>
          <button className={action === "scale_out" ? "chip on" : "chip"} onClick={() => setAction("scale_out")}>scale_out</button>
          <button className={action === "scale_in" ? "chip on" : "chip"} onClick={() => setAction("scale_in")}>scale_in</button>
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
              Newest first · {filtered.length} matching row{filtered.length === 1 ? "" : "s"}
              {error ? <span style={{ color: "var(--bad)" }}> · {error}</span> : null}
            </p>
          </div>
        </div>
        <table>
          <thead>
            <tr>
              <th style={{ width: 170 }}>Time</th>
              <th style={{ width: 90 }}>Class</th>
              <th style={{ width: 110 }}>Action</th>
              <th style={{ width: 70 }}>Version</th>
              <th style={{ width: 150 }}>Field</th>
              <th>View</th>
              <th style={{ width: 140 }}>Actor</th>
              <th style={{ width: 130 }}>Source</th>
            </tr>
          </thead>
          <tbody>
            {pageRows.length === 0 ? (
              <tr><td colSpan={8} className="empty">No events match the current filter.</td></tr>
            ) : (
              pageRows.map((row, i) => (
                <tr key={`${row.time}-${i}`}>
                  <td><code>{shortTime(row.time)}</code></td>
                  <td><span className={`badge-class ${row.cls}`}>{row.cls}</span></td>
                  <td><span className={`badge-action ${row.action}`}>{row.action}</span></td>
                  <td>{row.version != null ? <code>v{row.version}</code> : <span className="muted">—</span>}</td>
                  <td><code>{row.field}</code></td>
                  <td className="mono small">{row.view}</td>
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
              Page {page + 1} of {pageCount} · showing {pageRows.length} of {filtered.length}
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
