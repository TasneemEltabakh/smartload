import { useEffect, useMemo, useState } from "react";
import ReactDiffViewer from "react-diff-viewer-continued";

import { api, type AuditRow, type Policy } from "../api";

const REFRESH_MS = 10_000;

function formatJson(v: unknown): string {
  return JSON.stringify(v, null, 2);
}

export default function PolicyPage() {
  const [current, setCurrent] = useState<Policy | null>(null);
  const [draft, setDraft] = useState<string>("");
  const [audit, setAudit] = useState<AuditRow[]>([]);
  const [busy, setBusy] = useState(false);
  const [toast, setToast] = useState<{ msg: string; kind: "ok" | "bad" } | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function loadAll() {
    try {
      const [p, a] = await Promise.all([api.getPolicy(), api.auditPolicy(20)]);
      setCurrent(p);
      setAudit(a);
      setDraft((prev) => (prev ? prev : formatJson(p)));
      setError(null);
    } catch (err: any) {
      setError(err.message || "could not load policy");
    }
  }

  useEffect(() => {
    loadAll();
    const id = setInterval(loadAll, REFRESH_MS);
    return () => clearInterval(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const parsedDraft = useMemo(() => {
    try {
      return { ok: true as const, value: JSON.parse(draft) as Partial<Policy> };
    } catch (err: any) {
      return { ok: false as const, error: err.message as string };
    }
  }, [draft]);

  const diffOldStr = current ? formatJson(current) : "";
  const diffNewStr = parsedDraft.ok ? formatJson(parsedDraft.value) : "(invalid JSON)";

  function flash(msg: string, kind: "ok" | "bad") {
    setToast({ msg, kind });
    setTimeout(() => setToast(null), 4_000);
  }

  async function commit() {
    if (!parsedDraft.ok) {
      flash(`draft is not valid JSON: ${parsedDraft.error}`, "bad");
      return;
    }
    setBusy(true);
    try {
      const patch = { ...(parsedDraft.value as Partial<Policy>) } as Record<string, unknown>;
      delete patch.policy_version; // server bumps it
      const result = await api.setPolicy(patch, "operator-ui");
      flash(
        result.status === "updated"
          ? `updated (v${result.policy_version}; changed ${result.changed_fields.join(", ")})`
          : "no change committed",
        "ok",
      );
      await loadAll();
    } catch (err: any) {
      const fieldHint = err.field ? ` [field: ${err.field}]` : "";
      flash(`commit failed: ${err.message || err}${fieldHint}`, "bad");
    } finally {
      setBusy(false);
    }
  }

  function resetDraft() {
    if (current) setDraft(formatJson(current));
  }

  return (
    <>
      <div className="card">
        <h2>Current policy</h2>
        <div className="meta">
          {current
            ? `policy_version = ${current.policy_version} · operating_mode = ${current.operating_mode} · safe_mode = ${String(current.safe_mode)}`
            : "loading…"}
          {error ? ` · last error: ${error}` : null}
        </div>
        <pre>{current ? formatJson(current) : ""}</pre>
      </div>

      <div className="card">
        <h2>Edit (JSON)</h2>
        <div className="meta">
          Edit policy fields below. Unknown fields are ignored by the service.
          policy_version is set server-side.
        </div>
        <textarea value={draft} onChange={(e) => setDraft(e.target.value)} />
        <div style={{ marginTop: 12, display: "flex", gap: 8 }}>
          <button onClick={commit} disabled={busy || !parsedDraft.ok}>
            {busy ? "committing…" : "Commit"}
          </button>
          <button className="secondary" onClick={resetDraft} disabled={busy}>
            Reset
          </button>
          {!parsedDraft.ok ? (
            <span style={{ color: "var(--bad)", alignSelf: "center", marginLeft: 8 }}>
              {parsedDraft.error}
            </span>
          ) : null}
        </div>
      </div>

      <div className="card">
        <h2>Diff preview</h2>
        <div className="meta">Left: current on-disk policy. Right: your draft.</div>
        <ReactDiffViewer
          oldValue={diffOldStr}
          newValue={diffNewStr}
          splitView
          useDarkTheme
          hideLineNumbers={false}
        />
      </div>

      <div className="card">
        <h2>Recent audit ({audit.length})</h2>
        <div className="meta">Newest first. Each row corresponds to one changed field.</div>
        <table>
          <thead>
            <tr>
              <th>Time</th>
              <th>Version</th>
              <th>Field</th>
              <th>Old</th>
              <th>New</th>
              <th>Actor</th>
            </tr>
          </thead>
          <tbody>
            {audit.map((row, idx) => (
              <tr key={idx}>
                <td>{row.time}</td>
                <td>{row.policy_version}</td>
                <td>{row.field}</td>
                <td><code>{JSON.stringify(row.old_value)}</code></td>
                <td><code>{JSON.stringify(row.new_value)}</code></td>
                <td>{row.actor}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {toast ? <div className={`toast ${toast.kind}`}>{toast.msg}</div> : null}
    </>
  );
}
