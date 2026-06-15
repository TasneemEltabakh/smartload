/**
 * tools/demo-ui/web/src/present/GrafanaEmbed.tsx
 * ────────────────────────────────────────────────
 * Read-only Grafana dashboard embeds. URLs are built from the data contract
 * (grafana.baseUrl + uid) in kiosk mode, which hides Grafana's edit/share
 * chrome; anonymous Viewer access (enabled in docker-compose) means no login
 * and no edit rights are exposed. When no base URL is configured, or the stack
 * isn't up, each dashboard renders a clean "awaiting live stack" pending card.
 */

import { useState } from "react";

import { grafanaEmbedUrl } from "../results/adapter";
import type { GrafanaConfig, GrafanaDashboard } from "../results/schema";
import { StatusPill } from "../ui";
import { PendingBlock } from "./Pending";
import { Section } from "./Section";

function Embed({ url, title }: { url: string; title: string }) {
  const [show, setShow] = useState(false);
  if (!show) {
    return (
      <div
        style={{
          display: "flex",
          flexDirection: "column",
          alignItems: "center",
          justifyContent: "center",
          gap: 12,
          height: 360,
          border: "1px solid var(--sl-hairline)",
          borderRadius: "var(--sl-radius-md)",
          background: "var(--sl-surface-sunk)",
        }}
      >
        <div style={{ fontSize: 13.5, color: "var(--sl-text-mid)" }}>Live read-only dashboard — loads from the running stack.</div>
        <button
          type="button"
          onClick={() => setShow(true)}
          style={{
            cursor: "pointer",
            fontFamily: "var(--sl-font-sans)",
            fontSize: 13,
            fontWeight: 600,
            color: "var(--sl-on-mint-tint)",
            background: "var(--sl-mint-tint)",
            border: "1px solid var(--sl-mint-line)",
            borderRadius: "var(--sl-radius-md)",
            padding: "9px 18px",
          }}
        >
          ▶ Load embed
        </button>
        <a href={url} target="_blank" rel="noreferrer" style={{ fontSize: 12, color: "var(--sl-text-low)" }}>
          or open in a new tab ↗
        </a>
      </div>
    );
  }
  return (
    <iframe
      title={title}
      src={url}
      loading="lazy"
      style={{ width: "100%", height: 460, border: "1px solid var(--sl-hairline)", borderRadius: "var(--sl-radius-md)", background: "var(--sl-surface-sunk)" }}
    />
  );
}

function DashboardCard({ cfg, dash }: { cfg: GrafanaConfig; dash: GrafanaDashboard }) {
  const url = grafanaEmbedUrl(cfg, dash.uid);
  return (
    <Section
      eyebrow="Grafana"
      title={dash.title}
      lead={dash.description}
      actions={
        url ? (
          <a href={url} target="_blank" rel="noreferrer" style={{ textDecoration: "none" }}>
            <StatusPill status="ok" hideDot>
              open ↗
            </StatusPill>
          </a>
        ) : (
          <StatusPill status="neutral">pending</StatusPill>
        )
      }
    >
      {url ? <Embed url={url} title={dash.title} /> : <PendingBlock label="Grafana base URL not configured — set grafana.baseUrl in the results bundle" height={200} />}
    </Section>
  );
}

export function GrafanaView({ cfg }: { cfg: GrafanaConfig }) {
  if (!cfg.dashboards.length) {
    return (
      <Section eyebrow="Grafana" title="Dashboards">
        <PendingBlock label="No dashboards configured" height={120} />
      </Section>
    );
  }
  return (
    <>
      {cfg.dashboards.map((d) => (
        <DashboardCard key={d.uid} cfg={cfg} dash={d} />
      ))}
    </>
  );
}
