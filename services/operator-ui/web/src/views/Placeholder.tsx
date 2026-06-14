// ============================================================================
// Placeholder -- intentional "in progress" panel for not-yet-built sections
// ----------------------------------------------------------------------------
// Each of the six secondary routes renders this: a kit Card with the section
// name, its one-line purpose, the signature Heartbeat motion, and an in-progress
// note. Keeps the nav working and the app feeling complete while the section is
// built next.
// ============================================================================

import { Badge, Card, Heartbeat } from "../ui";

export interface PlaceholderProps {
  name: string;
  purpose: string;
}

export function Placeholder({ name, purpose }: PlaceholderProps) {
  return (
    <Card
      title={name}
      eyebrow="// section"
      actions={<Badge tone="neutral">IN PROGRESS</Badge>}
    >
      <p
        style={{
          fontSize: 13.5,
          color: "var(--sl-text-mid)",
          margin: "4px 0 0",
          maxWidth: "60ch",
        }}
      >
        {purpose}
      </p>

      <div
        style={{
          marginTop: 22,
          height: 160,
          borderRadius: "var(--sl-radius-md)",
          border: "1px solid var(--sl-hairline)",
          background: "linear-gradient(180deg, var(--sl-surface), var(--sl-surface-sunk))",
          display: "grid",
          placeItems: "center",
          overflow: "hidden",
        }}
      >
        <div style={{ width: 320, height: 130 }}>
          <Heartbeat width={320} height={130} />
        </div>
      </div>

      <p
        style={{
          fontFamily: "var(--sl-font-mono)",
          fontSize: 11,
          color: "var(--sl-text-low)",
          margin: "16px 0 0",
        }}
      >
        This section is on the build queue. The decision plane is live; the
        view is being assembled next.
      </p>
    </Card>
  );
}
