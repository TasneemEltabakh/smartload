/**
 * tools/demo-ui/web/src/pages/Audit.tsx
 * ───────────────────────────────────────
 * The audit / test results surface. Renders every audit section from the
 * contract (verdict, KPIs, before→after error-rate arc, findings list).
 */

import { useResultsCtx } from "../state/ResultsContext";
import { AuditView } from "../present/AuditView";
import { FreshnessBanner } from "../present/Freshness";
import { PendingBlock } from "../present/Pending";
import { Section } from "../present/Section";

export default function Audit() {
  const { bundle, loading } = useResultsCtx();

  if (bundle.audit.length === 0) {
    return (
      <Section eyebrow="Findings" title="Audit & tests">
        <PendingBlock label={loading ? "Loading results…" : "No audit results loaded"} height={160} />
      </Section>
    );
  }

  return (
    <>
      <FreshnessBanner provenance={bundle.provenance} />
      {bundle.audit.map((section) => (
        <AuditView key={section.key} section={section} />
      ))}
    </>
  );
}
