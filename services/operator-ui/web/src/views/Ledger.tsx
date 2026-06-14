// ============================================================================
// Ledger -- the unified, immutable audit trail
// ----------------------------------------------------------------------------
// One time-ordered record of every committed decision: policy changes (field,
// old -> new, actor) and scaling / isolation actions (action, resulting pool
// size, evidence reason). The trail is read-only -- rows are never edited, only
// appended -- so this view is purely a lens over the audit endpoints.
//
// It tries the live API for each source (counts, policy audit, scaling audit)
// and falls back to realistic sample rows on error or timeout, so the page
// renders complete with no backend running, then publishes its data source up to
// the app shell exactly like the Flightdeck. Filters (kind / action / actor /
// time range) narrow the merged set; the CSV export writes whatever is currently
// shown, built client-side.
// ============================================================================

import { useEffect, useMemo, useState } from "react";
import {
  ArrowRight,
  BookOpen,
  Download,
  Filter,
  Scale,
  ShieldAlert,
  SlidersHorizontal,
  Users,
} from "lucide-react";

import {
  api,
  type AuditCounts,
  type AuditRow,
  type ScalingAuditRow,
} from "../api";
import {
  Badge,
  Button,
  Card,
  DataTable,
  KpiStat,
  StatusPill,
  Tabs,
  type Column,
  type Status,
} from "../ui";
import { loadWithFallback, type DataSource } from "./loader";
import { useShell } from "./shell-context";
import {
  SAMPLE_AUDIT_COUNTS,
  SAMPLE_AUDIT_ISOLATION,
  SAMPLE_AUDIT_POLICY,
  SAMPLE_AUDIT_SCALING,
  SAMPLE_LB_CHANGES,
} from "./_sampleLedger";

const REFRESH_MS = 20_000;
const ROW_LIMIT = 200;

// ── unified row model ─────────────────────────────────────────────────────────
// Policy and scaling/isolation rows fold into one shape the table can sort and
// filter as a single stream. `kind` drives the left badge; `action` is the
// filterable verb (the policy field name, or scale_out / scale_in / isolate).

type LedgerKind = "policy" | "scaling";

interface LedgerRow {
  id: string;
  ts: number; // epoch ms for sorting / range filtering
  iso: string; // original ISO timestamp
  kind: LedgerKind;
  action: string;
  actor: string;
  // policy specifics
  field?: string;
  oldValue?: unknown;
  newValue?: unknown;
  policyVersion?: number;
  // scaling / isolation specifics
  instanceCount?: number;
  reason?: string | null;
}

// ── time helpers ──────────────────────────────────────────────────────────────

function tsOf(iso: string): number {
  const n = Date.parse(iso);
  return Number.isNaN(n) ? 0 : n;
}

// Compact mono timestamp: "Jun 14 14:28:41". Falls back to the raw string when
// the value is not parseable (the scaling endpoint can return a bare clock).
function fmtTime(iso: string): string {
  const t = tsOf(iso);
  if (t === 0) return iso;
  const d = new Date(t);
  const date = d.toLocaleDateString("en-US", { month: "short", day: "2-digit" });
  const time = d.toLocaleTimeString("en-GB", { hour12: false });
  return `${date} ${time}`;
}

const RANGE_MS: Record<string, number> = {
  "1h": 3_600_000,
  "24h": 86_400_000,
  "7d": 604_800_000,
  "30d": 2_592_000_000,
  all: Number.POSITIVE_INFINITY,
};

// ── source -> unified row mappers ─────────────────────────────────────────────

function policyToRows(rows: AuditRow[]): LedgerRow[] {
  return rows.map((r, i) => ({
    id: `policy-${r.policy_version}-${r.field}-${r.time}-${i}`,
    ts: tsOf(r.time),
    iso: r.time,
    kind: "policy",
    action: r.field,
    actor: r.actor || "system",
    field: r.field,
    oldValue: r.old_value,
    newValue: r.new_value,
    policyVersion: r.policy_version,
  }));
}

// Scaling and isolation share the ScalingAuditRow shape. Isolation rows carry an
// "isolated" / "flagged" cue in their reason, so derive a friendlier action verb
// from the reason when present; otherwise keep the raw scale verb.
function scalingToRows(rows: ScalingAuditRow[], origin: string): LedgerRow[] {
  return rows.map((r, i) => {
    const reason = r.reason ?? "";
    const isIsolation = /isolated|flagged|anomaly|degraded/i.test(reason);
    const action = isIsolation ? "isolate" : r.action;
    return {
      id: `${origin}-${r.time}-${r.action}-${i}`,
      ts: tsOf(r.time),
      iso: r.time,
      kind: "scaling",
      action,
      actor: isIsolation ? "anomaly-detector" : "autoscaler",
      instanceCount: r.instance_count,
      reason: r.reason,
    };
  });
}

// ── value rendering ───────────────────────────────────────────────────────────

function valueText(v: unknown): string {
  if (v == null) return "—";
  if (typeof v === "string") return v;
  if (typeof v === "boolean") return v ? "true" : "false";
  return String(v);
}

// ── CSV export (client-side, current filtered rows) ──────────────────────────

function csvCell(v: string): string {
  // Quote when the value carries a comma, quote, or newline; double interior
  // quotes per RFC 4180.
  if (/[",\n]/.test(v)) return `"${v.replace(/"/g, '""')}"`;
  return v;
}

function rowsToCsv(rows: LedgerRow[]): string {
  const header = [
    "timestamp",
    "kind",
    "action",
    "actor",
    "field",
    "old_value",
    "new_value",
    "policy_version",
    "instance_count",
    "reason",
  ];
  const lines = rows.map((r) =>
    [
      r.iso,
      r.kind,
      r.action,
      r.actor,
      r.field ?? "",
      r.field != null ? valueText(r.oldValue) : "",
      r.field != null ? valueText(r.newValue) : "",
      r.policyVersion != null ? String(r.policyVersion) : "",
      r.instanceCount != null ? String(r.instanceCount) : "",
      r.reason ?? "",
    ]
      .map((c) => csvCell(c))
      .join(","),
  );
  return [header.join(","), ...lines].join("\r\n");
}

function downloadCsv(rows: LedgerRow[]) {
  const csv = rowsToCsv(rows);
  const blob = new Blob([csv], { type: "text/csv;charset=utf-8;" });
  const url = URL.createObjectURL(blob);
  const stamp = new Date().toISOString().slice(0, 19).replace(/[:T]/g, "-");
  const a = document.createElement("a");
  a.href = url;
  a.download = `smartload-ledger-${stamp}.csv`;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}

// ── component ────────────────────────────────────────────────────────────────

export default function Ledger() {
  const shell = useShell();
  const { setDataSource, setPlane } = shell;

  const [counts, setCounts] = useState<AuditCounts>(SAMPLE_AUDIT_COUNTS);
  const [policyRows, setPolicyRows] = useState<AuditRow[]>(SAMPLE_AUDIT_POLICY);
  const [scalingRows, setScalingRows] = useState<ScalingAuditRow[]>(SAMPLE_AUDIT_SCALING);

  // filters
  const [kind, setKind] = useState<"all" | LedgerKind>("all");
  const [range, setRange] = useState<keyof typeof RANGE_MS>("7d");
  const [action, setAction] = useState<string>("all");
  const [actor, setActor] = useState<string>("all");

  // ── data load (live, with sample fallback) ─────────────────────────────────
  useEffect(() => {
    let cancelled = false;

    async function tick() {
      const results = await Promise.all([
        loadWithFallback(() => api.getAuditCounts(), SAMPLE_AUDIT_COUNTS),
        loadWithFallback(() => api.auditPolicy(ROW_LIMIT), SAMPLE_AUDIT_POLICY),
        loadWithFallback(() => api.auditScaling(ROW_LIMIT), SAMPLE_AUDIT_SCALING),
      ]);

      if (cancelled) return;

      const [coR, poR, scR] = results;
      setCounts(coR.value);
      setPolicyRows(poR.value);
      setScalingRows(scR.value);

      const sources: DataSource[] = results.map((r) => r.source);
      const anySample = sources.some((s) => s === "sample");
      const allSample = sources.every((s) => s === "sample");
      setDataSource(anySample ? "sample" : "live");
      setPlane(allSample ? "bad" : anySample ? "warn" : "ok");
    }

    tick();
    const id = window.setInterval(tick, REFRESH_MS);
    return () => {
      cancelled = true;
      window.clearInterval(id);
    };
  }, [setDataSource, setPlane]);

  // ── merge into the unified, time-ordered trail ─────────────────────────────
  // Isolation events are sample-only today (no dedicated list endpoint), so they
  // join the merge unconditionally; they read as operational "scaling"-kind rows
  // carrying the anomaly evidence in the reason.
  const allRows = useMemo<LedgerRow[]>(() => {
    const merged = [
      ...policyToRows(policyRows),
      ...scalingToRows(scalingRows, "scaling"),
      ...scalingToRows(SAMPLE_AUDIT_ISOLATION, "isolation"),
    ];
    merged.sort((a, b) => b.ts - a.ts);
    return merged;
  }, [policyRows, scalingRows]);

  // distinct actions / actors for the dropdowns, derived from the live set
  const actionOptions = useMemo(
    () => ["all", ...Array.from(new Set(allRows.map((r) => r.action))).sort()],
    [allRows],
  );
  const actorOptions = useMemo(
    () => ["all", ...Array.from(new Set(allRows.map((r) => r.actor))).sort()],
    [allRows],
  );

  // ── apply filters ───────────────────────────────────────────────────────────
  const filtered = useMemo(() => {
    const span = RANGE_MS[range];
    const floor = span === Number.POSITIVE_INFINITY ? -Infinity : Date.now() - span;
    return allRows.filter((r) => {
      if (kind !== "all" && r.kind !== kind) return false;
      if (action !== "all" && r.action !== action) return false;
      if (actor !== "all" && r.actor !== actor) return false;
      if (r.ts < floor) return false;
      return true;
    });
  }, [allRows, kind, range, action, actor]);

  // ── derived KPI readings (prefer live counts, else derive from rows) ────────
  const kpis = useMemo(() => {
    const policyCount = allRows.filter((r) => r.kind === "policy").length;
    const scalingCount = allRows.filter((r) => r.kind === "scaling").length;
    const actors = new Set(allRows.map((r) => r.actor)).size;
    const last = allRows.length > 0 ? allRows[0].iso : counts.last_event_at;
    return {
      total: counts.total_events || allRows.length,
      policy: counts.policy_changes || policyCount,
      scaling: counts.scaling_actions || scalingCount,
      actors: counts.actors_unique || actors,
      lastUpdated: last ? fmtTime(last) : "—",
    };
  }, [allRows, counts]);

  // ── render ─────────────────────────────────────────────────────────────────

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 22 }}>
      <SectionHead
        title="Ledger"
        sub="The unified, immutable audit trail. Every committed policy change and scaling or isolation action, time-ordered, with the actor and the evidence behind it. Rows are append-only; nothing here is ever edited."
      />

      <KpiRail kpis={kpis} />

      <FilterBar
        kind={kind}
        setKind={setKind}
        range={range}
        setRange={setRange}
        action={action}
        setAction={setAction}
        actor={actor}
        setActor={setActor}
        actionOptions={actionOptions}
        actorOptions={actorOptions}
        shown={filtered.length}
        total={allRows.length}
        onExport={() => downloadCsv(filtered)}
      />

      <TrailCard rows={filtered} />

      <SectionHead
        title="Load-balancer changes"
        sub="Upstream-weight and algorithm change history. A dedicated history endpoint is planned; the slot below shows the intended shape with a single illustrative row."
      />

      <LbChangesCard />
    </div>
  );
}

// ── section header ───────────────────────────────────────────────────────────

function SectionHead({ title, sub }: { title: string; sub: string }) {
  return (
    <div style={{ margin: "8px 2px 0" }}>
      <h2 style={{ fontSize: 17, fontWeight: 700, letterSpacing: "-0.3px", margin: 0, color: "var(--sl-text)" }}>
        {title}
      </h2>
      <div style={{ fontSize: 12.5, color: "var(--sl-text-low)", marginTop: 3, maxWidth: "92ch" }}>{sub}</div>
    </div>
  );
}

// ── KPI rail ─────────────────────────────────────────────────────────────────

function KpiRail({
  kpis,
}: {
  kpis: { total: number; policy: number; scaling: number; actors: number; lastUpdated: string };
}) {
  const fmtInt = (n: number) => n.toLocaleString("en-US", { maximumFractionDigits: 0 });
  return (
    <div style={{ display: "grid", gridTemplateColumns: "repeat(5, 1fr)", gap: 16 }}>
      <KpiStat
        label={<><BookOpen size={12} strokeWidth={2} /> Total events</>}
        value={fmtInt(kpis.total)}
        footnote="audited"
      />
      <KpiStat
        label={<><SlidersHorizontal size={12} strokeWidth={2} /> Policy changes</>}
        value={fmtInt(kpis.policy)}
        footnote="field commits"
      />
      <KpiStat
        label={<><Scale size={12} strokeWidth={2} /> Scaling actions</>}
        value={fmtInt(kpis.scaling)}
        footnote="incl. isolations"
      />
      <KpiStat
        label={<><Users size={12} strokeWidth={2} /> Unique actors</>}
        value={fmtInt(kpis.actors)}
        footnote="operators + engine"
      />
      <KpiStat
        label={<><Filter size={12} strokeWidth={2} /> Last updated</>}
        value={<span style={{ fontSize: 16 }}>{kpis.lastUpdated}</span>}
        footnote="most recent entry"
      />
    </div>
  );
}

// ── filter bar ───────────────────────────────────────────────────────────────

function FilterBar({
  kind,
  setKind,
  range,
  setRange,
  action,
  setAction,
  actor,
  setActor,
  actionOptions,
  actorOptions,
  shown,
  total,
  onExport,
}: {
  kind: "all" | LedgerKind;
  setKind: (v: "all" | LedgerKind) => void;
  range: keyof typeof RANGE_MS;
  setRange: (v: keyof typeof RANGE_MS) => void;
  action: string;
  setAction: (v: string) => void;
  actor: string;
  setActor: (v: string) => void;
  actionOptions: string[];
  actorOptions: string[];
  shown: number;
  total: number;
  onExport: () => void;
}) {
  return (
    <Card flush>
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: 16,
          flexWrap: "wrap",
          padding: "14px 18px",
        }}
      >
        <FilterGroup label="Kind">
          <Tabs
            items={[
              { id: "all", label: "All" },
              { id: "policy", label: "Policy" },
              { id: "scaling", label: "Scaling" },
            ]}
            value={kind}
            onChange={(id) => setKind(id as "all" | LedgerKind)}
          />
        </FilterGroup>

        <FilterGroup label="Range">
          <Tabs
            items={[
              { id: "1h", label: "1h" },
              { id: "24h", label: "24h" },
              { id: "7d", label: "7d" },
              { id: "30d", label: "30d" },
              { id: "all", label: "All" },
            ]}
            value={range}
            onChange={(id) => setRange(id as keyof typeof RANGE_MS)}
          />
        </FilterGroup>

        <FilterGroup label="Action">
          <Select value={action} onChange={setAction} options={actionOptions} />
        </FilterGroup>

        <FilterGroup label="Actor">
          <Select value={actor} onChange={setActor} options={actorOptions} />
        </FilterGroup>

        <div style={{ marginLeft: "auto", display: "flex", alignItems: "center", gap: 12 }}>
          <span style={{ fontFamily: "var(--sl-font-mono)", fontSize: 11, color: "var(--sl-text-low)" }}>
            {shown} / {total} rows
          </span>
          <Button
            variant="secondary"
            size="sm"
            icon={<Download size={13} strokeWidth={2} />}
            onClick={onExport}
            disabled={shown === 0}
          >
            Export CSV
          </Button>
        </div>
      </div>
    </Card>
  );
}

function FilterGroup({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 5 }}>
      <span
        style={{
          fontFamily: "var(--sl-font-mono)",
          fontSize: 9,
          letterSpacing: "1px",
          textTransform: "uppercase",
          color: "var(--sl-text-low)",
          fontWeight: 600,
        }}
      >
        {label}
      </span>
      {children}
    </div>
  );
}

// Native select, styled to the kit so it reads as part of the design language.
function Select({
  value,
  onChange,
  options,
}: {
  value: string;
  onChange: (v: string) => void;
  options: string[];
}) {
  return (
    <select
      value={value}
      onChange={(e) => onChange(e.target.value)}
      style={{
        fontFamily: "var(--sl-font-mono)",
        fontSize: 11,
        fontWeight: 600,
        color: "var(--sl-text)",
        background: "var(--sl-surface)",
        border: "1px solid var(--sl-hairline)",
        borderRadius: 9,
        padding: "7px 11px",
        cursor: "pointer",
        minWidth: 150,
        maxWidth: 220,
      }}
    >
      {options.map((o) => (
        <option key={o} value={o}>
          {o === "all" ? "All" : o}
        </option>
      ))}
    </select>
  );
}

// ── trail table ──────────────────────────────────────────────────────────────

const KIND_META: Record<LedgerKind, { label: string; status: Status }> = {
  policy: { label: "POLICY", status: "neutral" },
  scaling: { label: "ACTION", status: "ok" },
};

function actionStatus(row: LedgerRow): Status {
  if (row.action === "isolate") return "crit";
  if (row.action === "scale_in") return "warn";
  if (row.kind === "scaling") return "ok";
  return "neutral";
}

function TrailCard({ rows }: { rows: LedgerRow[] }) {
  const columns: Column<LedgerRow>[] = [
    {
      key: "time",
      header: "Timestamp",
      render: (r) => (
        <span style={{ fontFamily: "var(--sl-font-mono)", fontSize: 11.5, color: "var(--sl-text-mid)", whiteSpace: "nowrap" }}>
          {fmtTime(r.iso)}
        </span>
      ),
    },
    {
      key: "kind",
      header: "Kind",
      render: (r) => {
        const meta = KIND_META[r.kind];
        return <StatusPill status={meta.status} hideDot>{meta.label}</StatusPill>;
      },
    },
    {
      key: "change",
      header: "Change",
      render: (r) => {
        if (r.kind === "policy") {
          return (
            <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
              <span style={{ fontFamily: "var(--sl-font-mono)", fontSize: 12.5, fontWeight: 600, color: "var(--sl-text)" }}>
                {r.field}
                {r.policyVersion != null ? (
                  <span style={{ marginLeft: 8 }}>
                    <Badge tone="neutral">v{r.policyVersion}</Badge>
                  </span>
                ) : null}
              </span>
              <span style={{ display: "inline-flex", alignItems: "center", gap: 8, fontFamily: "var(--sl-font-mono)", fontSize: 12 }}>
                <span style={{ color: "var(--sl-text-faint)", textDecoration: "line-through" }}>
                  {valueText(r.oldValue)}
                </span>
                <ArrowRight size={12} strokeWidth={2} color="var(--sl-text-low)" />
                <span
                  style={{
                    color: "var(--sl-mint-deep)",
                    fontWeight: 700,
                    background: "var(--sl-mint-tint)",
                    border: "1px solid var(--sl-mint-line)",
                    borderRadius: 6,
                    padding: "1px 7px",
                  }}
                >
                  {valueText(r.newValue)}
                </span>
              </span>
            </div>
          );
        }
        // scaling / isolation
        const s = actionStatus(r);
        const verb =
          r.action === "isolate"
            ? "ISOLATE"
            : r.action === "scale_in"
              ? "SCALE IN"
              : "SCALE OUT";
        return (
          <div style={{ display: "flex", flexDirection: "column", gap: 5 }}>
            <span style={{ display: "inline-flex", alignItems: "center", gap: 9 }}>
              <StatusPill status={s} hideDot>{verb}</StatusPill>
              {r.instanceCount != null ? (
                <span style={{ fontFamily: "var(--sl-font-mono)", fontSize: 12, color: "var(--sl-text-mid)" }}>
                  pool ={" "}
                  <span style={{ fontWeight: 700, color: "var(--sl-text)" }}>{r.instanceCount}</span>
                </span>
              ) : null}
            </span>
            {r.reason ? (
              <span style={{ fontSize: 11.5, color: "var(--sl-text-mid)", lineHeight: 1.45, maxWidth: "72ch" }}>
                {r.reason}
              </span>
            ) : null}
          </div>
        );
      },
    },
    {
      key: "actor",
      header: "Actor",
      render: (r) => (
        <span style={{ fontFamily: "var(--sl-font-mono)", fontSize: 12, color: "var(--sl-text-mid)", whiteSpace: "nowrap" }}>
          {r.actor}
        </span>
      ),
    },
  ];

  return (
    <Card flush>
      {rows.length === 0 ? (
        <div style={{ padding: 28, textAlign: "center", fontSize: 13, color: "var(--sl-text-low)" }}>
          No audit entries match the current filters.
        </div>
      ) : (
        <DataTable columns={columns} rows={rows} rowKey={(r) => r.id} />
      )}
    </Card>
  );
}

// ── planned: load-balancer change history ────────────────────────────────────

function LbChangesCard() {
  const rows = SAMPLE_LB_CHANGES;
  return (
    <Card
      title="Weight & algorithm history"
      eyebrow="// planned endpoint"
      actions={<Badge tone="neutral">PREVIEW</Badge>}
      flush
    >
      <div
        style={{
          margin: "0 18px 14px",
          marginTop: 14,
          display: "flex",
          alignItems: "flex-start",
          gap: 9,
          padding: "10px 12px",
          borderRadius: "var(--sl-radius-md)",
          background: "var(--sl-surface-sunk)",
          border: "1px dashed var(--sl-hairline)",
          fontSize: 11.5,
          color: "var(--sl-text-low)",
        }}
      >
        <ShieldAlert size={14} strokeWidth={2} style={{ flex: "0 0 auto", marginTop: 1 }} />
        <span>
          Load-balancer weight and algorithm changes are applied today but not yet
          surfaced as a history endpoint. The row below is an illustrative sample
          of the intended shape; it will read live once the endpoint lands.
        </span>
      </div>

      <div style={{ display: "flex", flexDirection: "column" }}>
        {rows.map((r, i) => (
          <div
            key={`${r.time}-${i}`}
            style={{
              display: "grid",
              gridTemplateColumns: "150px auto 1fr auto",
              gap: 14,
              alignItems: "center",
              padding: "13px 18px",
              borderTop: "1px solid var(--sl-hairline-soft)",
            }}
          >
            <span style={{ fontFamily: "var(--sl-font-mono)", fontSize: 11.5, color: "var(--sl-text-mid)", whiteSpace: "nowrap" }}>
              {fmtTime(r.time)}
            </span>
            <StatusPill status="neutral" hideDot>LB</StatusPill>
            <span style={{ fontFamily: "var(--sl-font-mono)", fontSize: 12, color: "var(--sl-text)" }}>
              <span style={{ fontWeight: 700 }}>{r.change}</span>
              <span style={{ color: "var(--sl-text-mid)" }}> · {r.detail}</span>
            </span>
            <span style={{ fontFamily: "var(--sl-font-mono)", fontSize: 12, color: "var(--sl-text-mid)", whiteSpace: "nowrap" }}>
              {r.actor}
            </span>
          </div>
        ))}
      </div>
    </Card>
  );
}
