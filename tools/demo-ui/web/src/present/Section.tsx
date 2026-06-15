/**
 * tools/demo-ui/web/src/present/Section.tsx
 * ───────────────────────────────────────────
 * Academic section panel. Wraps the kit Card but renders a serif heading and a
 * quiet sans eyebrow (no "// mono" dev-console styling), an optional lead
 * sentence, and a right-aligned actions slot. This is the standard container
 * for every presentation surface so headings read as a rigorous evaluation.
 */

import type { CSSProperties, ReactNode } from "react";

import { Card } from "../ui";

export interface SectionProps {
  title: ReactNode;
  /** Small, quiet category label above the title (sans, tracked — no "//"). */
  eyebrow?: ReactNode;
  /** One-sentence lead under the title. */
  lead?: ReactNode;
  actions?: ReactNode;
  flush?: boolean;
  children?: ReactNode;
  style?: CSSProperties;
}

export function Heading({ children, size = "lg" }: { children: ReactNode; size?: "lg" | "xl" | "2xl" }) {
  const fontSize = size === "2xl" ? "var(--sl-text-2xl)" : size === "xl" ? "var(--sl-text-xl)" : "var(--sl-text-lg)";
  return (
    <span
      style={{
        fontFamily: "var(--sl-font-display)",
        fontSize,
        fontWeight: 700,
        letterSpacing: "-0.01em",
        color: "var(--sl-text)",
        lineHeight: 1.2,
      }}
    >
      {children}
    </span>
  );
}

export function Eyebrow({ children }: { children: ReactNode }) {
  return (
    <span
      style={{
        fontFamily: "var(--sl-font-sans)",
        fontSize: 11,
        fontWeight: 600,
        letterSpacing: "0.12em",
        textTransform: "uppercase",
        color: "var(--sl-text-low)",
      }}
    >
      {children}
    </span>
  );
}

export function Section({ title, eyebrow, lead, actions, flush, children, style }: SectionProps) {
  return (
    <Card
      flush={flush}
      style={style}
      actions={actions}
      title={
        <span style={{ display: "inline-flex", flexDirection: "column", gap: 4 }}>
          {eyebrow ? <Eyebrow>{eyebrow}</Eyebrow> : null}
          <Heading>{title}</Heading>
        </span>
      }
    >
      {lead ? (
        <p style={{ margin: "0 0 4px", fontSize: "var(--sl-text-base)", color: "var(--sl-text-mid)", lineHeight: 1.6, maxWidth: 900 }}>
          {lead}
        </p>
      ) : null}
      {children}
    </Card>
  );
}
