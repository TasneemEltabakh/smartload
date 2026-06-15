/**
 * tools/demo-ui/web/src/present/AuditView.tsx
 * ─────────────────────────────────────────────
 * Renders an audit/test section: KPI cards, the before→after stage arc (e.g.
 * the error-rate reduction), and the list of findings with status + severity.
 * All read from the contract; missing data falls back to PENDING.
 */

import { fmtNumber } from "../results/adapter";
import type { AuditItem, AuditSection, Tone } from "../results/schema";
import { StatusPill, type Status } from "../ui";
import { FreshnessLine } from "./Freshness";
import { KpiGrid } from "./KpiCard";
import { PendingBlock } from "./Pending";
import { Section } from "./Section";

function toneStatus(tone?: Tone): Status {
  if (tone === "ok") return "ok";
  if (tone === "warn") return "warn";
  if (tone === "bad") return "crit";
  return "neutral";
}

const STATUS_META: Record<AuditItem["status"], { status: Status; label: string }> = {
  pass: { status: "ok", label: "pass" },
  fixed: { status: "ok", label: "fixed" },
  fail: { status: "crit", label: "fail" },
  warn: { status: "warn", label: "warn" },
  info: { status: "neutral", label: "note" },
  pending: { status: "neutral", label: "pending" },
};

const SEV_COLOR: Record<NonNullable<AuditItem["severity"]>, string> = {
  critical: "var(--sl-crit)",
  high: "var(--sl-warn)",
  medium: "var(--sl-text-mid)",
  low: "var(--sl-text-low)",
};

function StageArc({ section }: { section: AuditSection }) {
  const stages = (section.stages ?? []).filter((s) => s.value != null);
  if (stages.length === 0) return <PendingBlock label="No before/after arc yet" height={90} />;
  const max = Math.max(...stages.map((s) => s.value as number), 1);
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
      {stages.map((s, i) => {
        const pct = Math.max(2, ((s.value as number) / max) * 100);
        const color = s.tone === "ok" ? "var(--sl-ok)" : s.tone === "warn" ? "var(--sl-warn)" : s.tone === "bad" ? "var(--sl-crit)" : "var(--sl-graphite)";
        return (
          <div key={i} style={{ display: "grid", gridTemplateColumns: "minmax(220px, 300px) 1fr 92px", gap: 14, alignItems: "center" }}>
            <span style={{ fontSize: 13.5, color: "var(--sl-text-mid)" }}>
              {s.label}
              {s.note ? <span style={{ color: "var(--sl-text-low)", fontStyle: "italic" }}> — {s.note}</span> : null}
            </span>
            <div style={{ height: 16, background: "var(--sl-surface-sunk)", border: "1px solid var(--sl-hairline)", borderRadius: 5, overflow: "hidden" }}>
              <div style={{ width: `${pct}%`, height: "100%", background: color, transition: "width var(--sl-dur-mid)" }} />
            </div>
            <span style={{ textAlign: "right", fontFamily: "var(--sl-font-display)", fontSize: 16, fontWeight: 700, fontVariantNumeric: "tabular-nums", color }}>
              {fmtNumber(s.value, s.unit)}
            </span>
          </div>
        );
      })}
    </div>
  );
}

function Finding({ item }: { item: AuditItem }) {
  const meta = STATUS_META[item.status];
  return (
    <div style={{ display: "flex", gap: 14, padding: "14px 16px", borderRadius: "var(--sl-radius-md)", background: "var(--sl-surface-sunk)", border: "1px solid var(--sl-hairline)" }}>
      <div style={{ flex: "0 0 auto", paddingTop: 1 }}>
        <StatusPill status={meta.status} hideDot={item.status === "info"}>
          {meta.label}
        </StatusPill>
      </div>
      <div style={{ minWidth: 0, flex: 1 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
          <span style={{ fontSize: 14.5, fontWeight: 600, fontFamily: "var(--sl-font-display)", color: "var(--sl-text)" }}>{item.label}</span>
          {item.severity ? (
            <span style={{ fontFamily: "var(--sl-font-sans)", fontSize: 10, fontWeight: 600, textTransform: "uppercase", letterSpacing: "0.06em", color: SEV_COLOR[item.severity] }}>
              {item.severity}
            </span>
          ) : null}
          {item.metric ? <span style={{ fontFamily: "var(--sl-font-mono)", fontSize: 12, color: "var(--sl-text-mid)" }}>{item.metric}</span> : null}
          {item.ref ? <span style={{ fontFamily: "var(--sl-font-mono)", fontSize: 11, color: "var(--sl-text-low)" }}>· {item.ref}</span> : null}
        </div>
      </div>
    </div>
  );
}

export function AuditView({ section }: { section: AuditSection }) {
  return (
    <>
      <Section
        eyebrow="Audit"
        title={section.title}
        actions={
          <span style={{ display: "inline-flex", gap: 8, alignItems: "center" }}>
            {section.verdict ? <StatusPill status={toneStatus(section.verdict.tone)}>verdict</StatusPill> : null}
            <FreshnessLine provenance={section.provenance} />
          </span>
        }
      >
        {section.verdict ? (
          <div
            style={{
              padding: "12px 16px",
              borderRadius: "var(--sl-radius-md)",
              background: "var(--sl-surface-sunk)",
              borderLeft: `4px solid ${section.verdict.tone === "ok" ? "var(--sl-ok)" : "var(--sl-warn)"}`,
              fontSize: "var(--sl-text-md)",
              fontFamily: "var(--sl-font-display)",
              color: "var(--sl-text)",
              lineHeight: 1.5,
            }}
          >
            {section.verdict.text}
          </div>
        ) : null}
      </Section>

      <Section eyebrow="Headline results" title="At a glance">
        {section.kpis.length > 0 ? <KpiGrid kpis={section.kpis} /> : <PendingBlock height={90} />}
      </Section>

      <Section eyebrow="Proof" title="Error-rate arc — before → after">
        <StageArc section={section} />
      </Section>

      <Section eyebrow="Findings" title="Confirmed defects & fixes">
        {section.items.length > 0 ? (
          <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
            {section.items.map((it) => (
              <Finding key={it.id} item={it} />
            ))}
          </div>
        ) : (
          <PendingBlock label="No findings recorded yet" height={90} />
        )}
      </Section>
    </>
  );
}
