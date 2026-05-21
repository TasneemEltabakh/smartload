import { useEffect, useMemo, useState } from "react";

import { api, type AuditRow, type ScalingAuditRow } from "../api";

const REFRESH_MS = 10_000;

type KindFilter = "all" | "policy" | "scaling";
type ActionFilter = "all" | "scale_out" | "scale_in";

const LIMIT_CHOICES = [25, 50, 100, 250, 500];

function fmt(value: unknown): string {
  if (value === null || value === undefined) return "—";
  if (typeof value === "string") return value;
  return JSON.stringify(value);
}

function shortTime(iso: string): string {
  // 2026-05-21T19:06:40.522650+00:00 → 2026-05-21 19:06:40
  return iso.replace("T", " ").replace(/\.\d+.*/, "").replace(/\+.*$/, "");
}

export default function AuditPage() {
  const [policyRows, setPolicyRows] = useState<AuditRow[]>([]);
  const [scalingRows, setScalingRows] = useState<ScalingAuditRow[]>([]);
  const [kind, setKind] = useState<KindFilter>("all");
  const [actor, setActor] = useState<string>("");
  const [action, setAction] = useState<ActionFilter>("all");
  const [limit, setLimit] = useState<number>(50);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [lastLoaded, setLastLoaded] = useState<string | null>(null);

  async function loadAll() {
    setBusy(true);
    try {
      const [pol, sca] = await Promise.all([
        api.auditPolicy(limit),
        api.auditScaling(limit),
      ]);
      setPolicyRows(pol);
      setScalingRows(sca);
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

  const filteredPolicy = useMemo(() => {
    const needle = actor.trim().toLowerCase();
    if (!needle) return policyRows;
    return policyRows.filter(
      (r) =>
        r.actor?.toLowerCase().includes(needle) ||
        r.field?.toLowerCase().includes(needle),
    );
  }, [policyRows, actor]);

  const filteredScaling = useMemo(() => {
    let rows = scalingRows;
    if (action !== "all") rows = rows.filter((r) => r.action === action);
    return rows;
  }, [scalingRows, action]);

  const showPolicy  = kind === "all" || kind === "policy";
  const showScaling = kind === "all" || kind === "scaling";

  return (
    <>
      <div className="card">
        <h2>Audit log</h2>
        <div className="meta">
          One row per audited event. Newest first.{" "}
          {lastLoaded ? <>Last refreshed: <code>{lastLoaded}</code>.</> : null}{" "}
          {error ? <span style={{ color: "var(--bad)" }}>· {error}</span> : null}
        </div>

        <div className="audit-toolbar" style={{ display: "flex", flexWrap: "wrap", gap: 12, marginTop: 12, alignItems: "center" }}>
          <div className="chip-group" role="group" aria-label="kind">
            <span className="chip-label">Kind:</span>
            <button className={kind === "all" ? "chip on" : "chip"}     onClick={() => setKind("all")}>All</button>
            <button className={kind === "policy" ? "chip on" : "chip"}  onClick={() => setKind("policy")}>Policy</button>
            <button className={kind === "scaling" ? "chip on" : "chip"} onClick={() => setKind("scaling")}>Scaling</button>
          </div>

          {showPolicy ? (
            <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
              <span className="chip-label">Filter (actor/field):</span>
              <input
                value={actor}
                onChange={(e) => setActor(e.target.value)}
                placeholder="e.g. demo-test, safe_mode"
                style={{ minWidth: 200 }}
              />
            </div>
          ) : null}

          {showScaling ? (
            <div className="chip-group" role="group" aria-label="action">
              <span className="chip-label">Action:</span>
              <button className={action === "all"        ? "chip on" : "chip"} onClick={() => setAction("all")}>All</button>
              <button className={action === "scale_out"  ? "chip on" : "chip"} onClick={() => setAction("scale_out")}>scale_out</button>
              <button className={action === "scale_in"   ? "chip on" : "chip"} onClick={() => setAction("scale_in")}>scale_in</button>
            </div>
          ) : null}

          <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
            <span className="chip-label">Limit:</span>
            <select value={limit} onChange={(e) => setLimit(Number(e.target.value))}>
              {LIMIT_CHOICES.map((n) => (
                <option key={n} value={n}>{n}</option>
              ))}
            </select>
          </div>

          <button onClick={loadAll} disabled={busy} style={{ marginLeft: "auto" }}>
            {busy ? "loading…" : "Refresh"}
          </button>
        </div>
      </div>

      {showPolicy ? (
        <div className="card">
          <h2>Policy changes ({filteredPolicy.length}{actor ? ` of ${policyRows.length}` : ""})</h2>
          <div className="meta">
            One row per changed field. Source:{" "}
            <code>GET /api/ui/audit/policy</code> → policy-manager.
          </div>
          <table>
            <thead>
              <tr>
                <th style={{ width: 170 }}>Time</th>
                <th style={{ width: 80 }}>Version</th>
                <th style={{ width: 200 }}>Field</th>
                <th>Old</th>
                <th>New</th>
                <th style={{ width: 140 }}>Actor</th>
              </tr>
            </thead>
            <tbody>
              {filteredPolicy.length === 0 ? (
                <tr><td colSpan={6} className="empty">No policy changes match the current filter.</td></tr>
              ) : (
                filteredPolicy.map((row, idx) => (
                  <tr key={`p-${row.time}-${idx}`}>
                    <td><code>{shortTime(row.time)}</code></td>
                    <td>{row.policy_version}</td>
                    <td><code>{row.field}</code></td>
                    <td><code>{fmt(row.old_value)}</code></td>
                    <td><code>{fmt(row.new_value)}</code></td>
                    <td>{row.actor}</td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>
      ) : null}

      {showScaling ? (
        <div className="card">
          <h2>Scaling events ({filteredScaling.length}{action !== "all" ? ` of ${scalingRows.length}` : ""})</h2>
          <div className="meta">
            One row per autoscaler action. Source:{" "}
            <code>GET /api/ui/audit/scaling</code> → autoscaler.
          </div>
          <table>
            <thead>
              <tr>
                <th style={{ width: 170 }}>Time</th>
                <th style={{ width: 110 }}>Action</th>
                <th style={{ width: 90 }}>Backends</th>
                <th>Reason</th>
              </tr>
            </thead>
            <tbody>
              {filteredScaling.length === 0 ? (
                <tr><td colSpan={4} className="empty">No scaling events match the current filter.</td></tr>
              ) : (
                filteredScaling.map((row, idx) => (
                  <tr key={`s-${row.time}-${idx}`}>
                    <td><code>{shortTime(row.time)}</code></td>
                    <td>
                      <span className={`badge-action ${row.action}`}>{row.action}</span>
                    </td>
                    <td>{row.instance_count}</td>
                    <td>{row.reason ?? <span className="muted">—</span>}</td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>
      ) : null}
    </>
  );
}
