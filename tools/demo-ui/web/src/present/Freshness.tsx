/**
 * tools/demo-ui/web/src/present/Freshness.tsx
 * ─────────────────────────────────────────────
 * The provenance / freshness surface. Every results-bearing surface shows a
 * "results as of <timestamp> from <host>" line plus a kind badge (FINAL /
 * STALE / SAMPLE / PENDING) so a viewer can never mistake stale or sample
 * numbers for the final VPS run.
 */

import { freshnessText, kindTone } from "../results/adapter";
import type { Provenance } from "../results/schema";
import { StatusPill, type Status } from "../ui";

const KIND_LABEL: Record<Provenance["kind"], string> = {
  final: "FINAL",
  stale: "STALE · PRE-VPS",
  sample: "SAMPLE DATA",
  pending: "AWAITING RESULTS",
};

function toStatus(tone: ReturnType<typeof kindTone>): Status {
  if (tone === "ok") return "ok";
  if (tone === "warn") return "warn";
  if (tone === "bad") return "crit";
  return "neutral";
}

export function KindBadge({ provenance }: { provenance: Provenance }) {
  return (
    <StatusPill status={toStatus(kindTone(provenance.kind))} hideDot={provenance.kind === "pending"}>
      {KIND_LABEL[provenance.kind]}
    </StatusPill>
  );
}

export function FreshnessLine({
  provenance,
  source,
}: {
  provenance: Provenance;
  source?: string;
}) {
  return (
    <div
      style={{
        display: "flex",
        alignItems: "center",
        gap: 10,
        flexWrap: "wrap",
        fontFamily: "var(--sl-font-sans)",
        fontSize: 11,
        color: "var(--sl-text-low)",
      }}
    >
      <KindBadge provenance={provenance} />
      <span>
        results as of{" "}
        <span style={{ color: "var(--sl-text-mid)" }}>{freshnessText(provenance)}</span>
        {provenance.gitCommit ? (
          <span style={{ color: "var(--sl-text-low)" }}> · git {provenance.gitCommit}</span>
        ) : null}
      </span>
      {provenance.note ? (
        <span style={{ color: "var(--sl-text-low)", fontStyle: "italic" }}>— {provenance.note}</span>
      ) : null}
      {source ? <span style={{ color: "var(--sl-text-low)" }}>· src {source}</span> : null}
    </div>
  );
}

/** A loud banner shown app-wide when the active bundle is not FINAL. */
export function FreshnessBanner({ provenance }: { provenance: Provenance }) {
  if (provenance.kind === "final") return null;
  const tone = toStatus(kindTone(provenance.kind));
  const bg =
    tone === "warn"
      ? "var(--sl-warn-tint)"
      : tone === "ok"
        ? "var(--sl-ok-tint)"
        : "var(--sl-surface-sunk)";
  const line =
    tone === "warn" ? "var(--sl-warn)" : tone === "crit" ? "var(--sl-crit)" : "var(--sl-hairline)";
  const msg =
    provenance.kind === "pending"
      ? "No benchmark results loaded yet — every surface is showing its pending state. Drop in the VPS results bundle to populate."
      : "These are sample / pre-VPS numbers and are NOT final. They show the layout and the exact data shape; the VPS re-run will replace them.";
  return (
    <div
      style={{
        display: "flex",
        alignItems: "center",
        gap: 12,
        padding: "10px 16px",
        borderRadius: "var(--sl-radius-md)",
        background: bg,
        border: `1px solid ${line}`,
        marginBottom: 16,
        fontSize: 12.5,
        color: "var(--sl-text-mid)",
      }}
    >
      <KindBadge provenance={provenance} />
      <span>{msg}</span>
    </div>
  );
}
