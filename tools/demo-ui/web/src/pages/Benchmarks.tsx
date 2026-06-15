/**
 * tools/demo-ui/web/src/pages/Benchmarks.tsx
 * ────────────────────────────────────────────
 * The centerpiece: the benchmark comparison surface. A grouped suite selector
 * (suites are read from the bundle — never hard-coded; any number, any groups)
 * drives the full SuiteView below: systems × parameters grid, systems × metrics
 * matrix per configuration, KPIs and charts. Active suite is reflected in the
 * URL (?suite=…) for deep-linking from the overview.
 */

import { useEffect } from "react";
import { useSearchParams } from "react-router-dom";

import { suiteGroups } from "../results/adapter";
import type { Tone } from "../results/schema";
import { useResultsCtx } from "../state/ResultsContext";
import { StatusPill, type Status } from "../ui";
import { FreshnessBanner } from "../present/Freshness";
import { PendingBlock } from "../present/Pending";
import { Section } from "../present/Section";
import { SuiteView } from "../present/SuiteView";

function vStatus(tone?: Tone): Status {
  if (tone === "ok") return "ok";
  if (tone === "warn") return "warn";
  if (tone === "bad") return "crit";
  return "neutral";
}

export default function Benchmarks() {
  const { bundle, loading } = useResultsCtx();
  const [params, setParams] = useSearchParams();
  const suites = bundle.suites;
  const groups = suiteGroups(bundle);

  const requested = params.get("suite");
  const active = suites.find((s) => s.id === requested)?.id ?? suites[0]?.id ?? "";

  useEffect(() => {
    if (active && requested !== active) {
      const next = new URLSearchParams(params);
      next.set("suite", active);
      setParams(next, { replace: true });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [active]);

  if (suites.length === 0) {
    return (
      <Section eyebrow="Benchmarks" title="Comparison suites">
        <PendingBlock label={loading ? "Loading results…" : "No comparison suites loaded"} height={160} />
      </Section>
    );
  }

  const suite = suites.find((s) => s.id === active) ?? suites[0];

  return (
    <>
      <FreshnessBanner provenance={bundle.provenance} />

      <Section eyebrow="Benchmarks" title="Choose a comparison">
        <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
          {groups.map((group) => {
            const groupSuites = suites.filter((s) => (s.group ?? "Benchmarks") === group);
            if (groupSuites.length === 0) return null;
            return (
              <div key={group}>
                <div style={{ fontSize: 11, fontWeight: 600, letterSpacing: "0.1em", textTransform: "uppercase", color: "var(--sl-text-low)", marginBottom: 8 }}>{group}</div>
                <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
                  {groupSuites.map((s) => {
                    const on = s.id === active;
                    return (
                      <button
                        key={s.id}
                        type="button"
                        onClick={() => {
                          const next = new URLSearchParams(params);
                          next.set("suite", s.id);
                          setParams(next);
                        }}
                        style={{
                          cursor: "pointer",
                          textAlign: "left",
                          display: "inline-flex",
                          alignItems: "center",
                          gap: 8,
                          fontFamily: "var(--sl-font-sans)",
                          fontSize: 13,
                          fontWeight: on ? 700 : 500,
                          color: on ? "var(--sl-text)" : "var(--sl-text-mid)",
                          background: on ? "var(--sl-mint-tint)" : "var(--sl-surface)",
                          border: `1px solid ${on ? "var(--sl-mint-line)" : "var(--sl-hairline)"}`,
                          borderRadius: 999,
                          padding: "7px 14px",
                        }}
                      >
                        {s.label}
                        {s.verdict ? <StatusPill status={vStatus(s.verdict.tone)} hideDot>{s.verdict.tone === "ok" ? "win" : s.verdict.tone === "warn" ? "finding" : s.verdict.tone === "bad" ? "no lift" : "pending"}</StatusPill> : null}
                      </button>
                    );
                  })}
                </div>
              </div>
            );
          })}
        </div>
      </Section>

      <SuiteView key={suite.id} suite={suite} />
    </>
  );
}
