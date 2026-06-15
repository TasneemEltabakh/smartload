/**
 * tools/demo-ui/web/src/pages/Overview.tsx
 * ──────────────────────────────────────────
 * The landing surface. Frames what SmartLoad is, makes the freshness of the
 * loaded results unmistakable, and presents the benchmark suites grouped by
 * category (the hierarchy leads into the benchmark detail), plus the audit
 * headline. Suites are read from the bundle — never hard-coded — so the layout
 * holds for any number of suites, populated or fully pending.
 */

import { Link } from "react-router-dom";

import { fmtNumber, suiteGroups, toneForKpi } from "../results/adapter";
import type { AuditSection, Suite, Tone } from "../results/schema";
import { useResultsCtx } from "../state/ResultsContext";
import { StatusPill, type Status } from "../ui";
import { FreshnessBanner, FreshnessLine } from "../present/Freshness";
import { toneColor } from "../present/palette";
import { PendingBlock } from "../present/Pending";
import { Section } from "../present/Section";

function vStatus(tone?: Tone): Status {
  if (tone === "ok") return "ok";
  if (tone === "warn") return "warn";
  if (tone === "bad") return "crit";
  return "neutral";
}
function vLabel(verdict?: { tone: Tone; text: string }): string {
  const tone = verdict?.tone;
  if (tone === "ok") return "win";
  if (tone === "warn") return "honest finding";
  if (tone === "bad") return "no lift";
  // Neutral/muted: a deferred or not-yet-run verdict. Use the verdict's own
  // short label text when given (e.g. "Future work"); otherwise "pending".
  const text = verdict?.text?.trim();
  return text ? text : "pending";
}

/** Suites in a single list, ordered by group so the dense grid still reads grouped. */
function orderedSuites(bundle: ReturnType<typeof useResultsCtx>["bundle"], groups: string[]) {
  const rank = (g: string) => {
    const i = groups.indexOf(g);
    return i === -1 ? groups.length : i;
  };
  return [...bundle.suites].sort((a, b) => rank(a.group ?? "Benchmarks") - rank(b.group ?? "Benchmarks"));
}

function SuiteSummary({ suite }: { suite: Suite }) {
  const kpi = suite.kpis[0];
  const tone = kpi ? toneForKpi(kpi) : "muted";
  return (
    <Link to={`/benchmarks?suite=${encodeURIComponent(suite.id)}`} style={{ textDecoration: "none" }}>
      <div
        style={{
          height: "100%",
          display: "flex",
          flexDirection: "column",
          gap: 10,
          padding: "16px 18px",
          borderRadius: "var(--sl-radius-md)",
          background: "var(--sl-surface)",
          border: "1px solid var(--sl-hairline)",
          boxShadow: "var(--sl-shadow-1)",
        }}
      >
        <div style={{ fontSize: 10, fontWeight: 600, letterSpacing: "0.1em", textTransform: "uppercase", color: "var(--sl-text-faint)" }}>
          {suite.group ?? "Benchmarks"}
        </div>
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 8 }}>
          <span style={{ fontFamily: "var(--sl-font-display)", fontSize: 17, fontWeight: 700, color: "var(--sl-text)", lineHeight: 1.2 }}>{suite.label}</span>
          {suite.verdict ? <StatusPill status={vStatus(suite.verdict.tone)} hideDot>{vLabel(suite.verdict)}</StatusPill> : null}
        </div>
        <div style={{ flex: 1, display: "flex", alignItems: "flex-end" }}>
          {kpi ? (
            <div style={{ display: "flex", alignItems: "baseline", gap: 8 }}>
              <span style={{ fontFamily: "var(--sl-font-display)", fontSize: 34, fontWeight: 700, color: toneColor(tone), letterSpacing: "-0.02em", fontVariantNumeric: "tabular-nums", lineHeight: 1 }}>
                {kpi.value == null ? "—" : fmtNumber(kpi.value, "")}
                {kpi.value != null && kpi.unit ? <span style={{ fontSize: 15, color: "var(--sl-text-low)", marginLeft: 3, fontFamily: "var(--sl-font-sans)" }}>{kpi.unit}</span> : null}
              </span>
              <span style={{ fontSize: 12, color: "var(--sl-text-low)" }}>{kpi.label}</span>
            </div>
          ) : null}
        </div>
        <span style={{ fontSize: 12, fontWeight: 600, color: "var(--sl-on-mint-tint)" }}>view →</span>
      </div>
    </Link>
  );
}

function AuditSummary({ section }: { section: AuditSection }) {
  const kpi = section.kpis[0];
  return (
    <Link to="/audit" style={{ textDecoration: "none" }}>
      <div style={{ display: "flex", alignItems: "center", gap: 20, padding: "16px 18px", borderRadius: "var(--sl-radius-md)", background: "var(--sl-surface)", border: "1px solid var(--sl-hairline)", boxShadow: "var(--sl-shadow-1)" }}>
        {kpi ? (
          <div style={{ flex: "0 0 auto" }}>
            <div style={{ fontFamily: "var(--sl-font-display)", fontSize: 30, fontWeight: 700, color: "var(--sl-ok)", fontVariantNumeric: "tabular-nums" }}>
              {kpi.value == null ? "—" : fmtNumber(kpi.value, kpi.unit)}
            </div>
            <div style={{ fontSize: 11.5, color: "var(--sl-text-low)" }}>{kpi.label}</div>
          </div>
        ) : null}
        <div style={{ minWidth: 0 }}>
          <div style={{ fontFamily: "var(--sl-font-display)", fontSize: 16, fontWeight: 700, color: "var(--sl-text)" }}>{section.title}</div>
          {section.verdict ? <div style={{ fontSize: 12.5, color: "var(--sl-text-mid)", lineHeight: 1.55, marginTop: 4 }}>{section.verdict.text}</div> : null}
          <span style={{ fontSize: 12, fontWeight: 600, color: "var(--sl-text-low)" }}>view audit →</span>
        </div>
      </div>
    </Link>
  );
}

export default function Overview() {
  const { bundle, loading, error, source } = useResultsCtx();
  const groups = suiteGroups(bundle);

  return (
    <>
      <Section eyebrow="Read-only evidence" title="SmartLoad — benchmark & audit" actions={<FreshnessLine provenance={bundle.provenance} source={source} />} />

      <FreshnessBanner provenance={bundle.provenance} />

      {error ? (
        <Section eyebrow="Data layer" title="Results unavailable">
          <div style={{ fontSize: "var(--sl-text-base)", color: "var(--sl-text-mid)", lineHeight: 1.6 }}>
            Could not load the results bundle from <code>{source}</code> ({error}). Every surface is showing its
            pending state. Drop a results bundle at <code>public/results/results.json</code> or set{" "}
            <code>VITE_RESULTS_URL</code>.
          </div>
        </Section>
      ) : null}

      <Section eyebrow="Benchmarks" title="Comparison suites">
        {bundle.suites.length === 0 ? (
          <PendingBlock label={loading ? "Loading results…" : "No comparison suites loaded"} height={120} />
        ) : (
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(290px, 1fr))", gap: 14 }}>
            {orderedSuites(bundle, groups).map((s) => (
              <SuiteSummary key={s.id} suite={s} />
            ))}
          </div>
        )}
      </Section>

      {bundle.audit.length > 0 ? (
        <Section eyebrow="Control loop" title="Audit & hardening">
          <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
            {bundle.audit.map((a) => (
              <AuditSummary key={a.key} section={a} />
            ))}
          </div>
        </Section>
      ) : null}
    </>
  );
}
