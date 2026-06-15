/**
 * tools/demo-ui/web/src/pages/Dashboards.tsx
 * ────────────────────────────────────────────
 * The live Grafana surface — read-only kiosk embeds of the project dashboards.
 * Configured entirely from the contract (grafana.baseUrl + dashboards); shows a
 * clean pending state when no base URL is set or the stack isn't running.
 */

import { useResultsCtx } from "../state/ResultsContext";
import { GrafanaView } from "../present/GrafanaEmbed";
import { Section } from "../present/Section";

export default function Dashboards() {
  const { bundle } = useResultsCtx();
  const cfg = bundle.grafana;

  return (
    <>
      <Section eyebrow="Grafana · read-only" title="Live dashboards" />

      <GrafanaView cfg={cfg} />
    </>
  );
}
