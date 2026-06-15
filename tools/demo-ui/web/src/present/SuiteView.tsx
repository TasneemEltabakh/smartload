/**
 * tools/demo-ui/web/src/present/SuiteView.tsx
 * ─────────────────────────────────────────────
 * One suite, presentation-first and scannable: a hero card (title + one-line
 * verdict + KPI numbers), the comparison charts, the systems × parameters grid,
 * and the systems × metrics matrix. Minimal prose — numbers, graphs, tables.
 */

import { useState } from "react";

import { hasParamAxis } from "../results/adapter";
import type { Suite, Tone } from "../results/schema";
import { Card, StatusPill, type Status } from "../ui";
import { ComparisonChart } from "./ComparisonChart";
import { ComparisonMatrix } from "./ComparisonMatrix";
import { KindBadge } from "./Freshness";
import { KpiGrid } from "./KpiCard";
import { ParameterGrid } from "./ParameterGrid";
import { Section, Heading, Eyebrow } from "./Section";

function verdictStatus(tone: Tone): Status {
  if (tone === "ok") return "ok";
  if (tone === "warn") return "warn";
  if (tone === "bad") return "crit";
  return "neutral";
}
function verdictWord(tone: Tone): string {
  if (tone === "ok") return "win";
  if (tone === "warn") return "finding";
  if (tone === "bad") return "no lift";
  return "pending";
}

export function SuiteView({ suite }: { suite: Suite }) {
  const [configId, setConfigId] = useState<string>(suite.defaultConfigId || suite.configurations[0]?.id || "");
  const activeConfig = suite.configurations.find((c) => c.id === configId) ?? suite.configurations[0];
  const v = suite.verdict;

  return (
    <>
      {/* Hero: title + one-line verdict + KPI numbers */}
      <Card>
        <div style={{ display: "flex", alignItems: "flex-start", justifyContent: "space-between", gap: 16, flexWrap: "wrap" }}>
          <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
            <Eyebrow>{suite.group ?? "Benchmark"}</Eyebrow>
            <Heading size="2xl">{suite.label}</Heading>
          </div>
          <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
            {v ? <StatusPill status={verdictStatus(v.tone)}>{verdictWord(v.tone)}</StatusPill> : null}
            <KindBadge provenance={suite.provenance} />
          </div>
        </div>

        {v ? (
          <div
            style={{
              marginTop: 12,
              padding: "12px 16px",
              borderRadius: "var(--sl-radius-md)",
              background: "var(--sl-surface-sunk)",
              borderLeft: `4px solid ${v.tone === "ok" ? "var(--sl-ok)" : v.tone === "warn" ? "var(--sl-warn)" : v.tone === "bad" ? "var(--sl-crit)" : "var(--sl-hairline)"}`,
              fontSize: "var(--sl-text-md)",
              fontFamily: "var(--sl-font-display)",
              color: "var(--sl-text)",
              lineHeight: 1.5,
            }}
          >
            {v.text}
          </div>
        ) : null}

        {suite.kpis.length > 0 ? (
          <div style={{ marginTop: 16 }}>
            <KpiGrid kpis={suite.kpis} />
          </div>
        ) : null}
      </Card>

      {/* Charts — visual first */}
      {suite.charts.length > 0 ? (
        <div style={{ display: "grid", gridTemplateColumns: suite.charts.length > 1 ? "1fr 1fr" : "1fr", gap: 18, alignItems: "start" }}>
          {suite.charts.map((chart) => (
            <Section key={chart.key} title={chart.title}>
              <ComparisonChart chart={chart} />
            </Section>
          ))}
        </div>
      ) : null}

      {/* Systems × parameters grid */}
      {hasParamAxis(suite) ? (
        <Section title="Across configurations">
          <ParameterGrid suite={suite} />
        </Section>
      ) : null}

      {/* Systems × metrics matrix for a selected configuration */}
      <Section
        title="By metric"
        actions={suite.configurations.length > 1 ? <ConfigSelect suite={suite} value={configId} onChange={setConfigId} /> : null}
      >
        {suite.systems.length > 0 && suite.metrics.length > 0 && activeConfig ? (
          <ComparisonMatrix suite={suite} configId={activeConfig.id} />
        ) : null}
      </Section>
    </>
  );
}

/** Parameter-configuration selector (chips). */
function ConfigSelect({ suite, value, onChange }: { suite: Suite; value: string; onChange: (id: string) => void }) {
  return (
    <div style={{ display: "flex", gap: 6, flexWrap: "wrap", alignItems: "center", justifyContent: "flex-end" }}>
      {suite.configurations.map((c) => {
        const on = c.id === value;
        return (
          <button
            key={c.id}
            type="button"
            onClick={() => onChange(c.id)}
            title={c.params ? JSON.stringify(c.params) : undefined}
            style={{
              cursor: "pointer",
              fontFamily: "var(--sl-font-sans)",
              fontSize: 12.5,
              fontWeight: on ? 700 : 500,
              color: on ? "var(--sl-text)" : "var(--sl-text-mid)",
              background: on ? "var(--sl-mint-tint)" : "var(--sl-surface)",
              border: `1px solid ${on ? "var(--sl-mint-line)" : "var(--sl-hairline)"}`,
              borderRadius: 999,
              padding: "4px 11px",
            }}
          >
            {c.label}
          </button>
        );
      })}
    </div>
  );
}
