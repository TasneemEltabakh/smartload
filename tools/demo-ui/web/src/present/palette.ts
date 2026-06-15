/**
 * tools/demo-ui/web/src/present/palette.ts
 * ──────────────────────────────────────────
 * Chart/SVG colour literals for the presentation surfaces, tuned for the LIGHT
 * (default) theme — strong, saturated values that hold contrast on white for
 * projector legibility. recharts paints SVG that can't reliably read CSS custom
 * properties off its own attributes, so the values are mirrored here as
 * literals. Restrained, credible palette: one accent (green) for subject/winner,
 * a quiet graphite for baselines, amber for references, red for "worse".
 */

import type { Tone } from "../results/schema";

export const C = {
  accent: "#15803d", // subject / winner / good (deep green)
  accentSoft: "rgba(21,128,61,0.10)",
  blue: "#2563eb", // secondary series
  amber: "#b45309", // reference (ceiling/floor)
  red: "#b91c1c", // worse / bad
  graphite: "#64748b", // baseline / muted series
  graphiteSoft: "#94a3b8",
  grid: "#e5e8ee",
  axis: "#475569",
  textLow: "#6a7384",
  text: "#0e1116",
  surface: "#ffffff",
  hair: "#e5e8ee",
} as const;

/** Multi-series line palette (subject first). */
export const SERIES_COLORS = [C.accent, C.blue, C.amber, C.graphite, C.red];

export function toneColor(tone: Tone): string {
  if (tone === "ok") return "var(--sl-ok)";
  if (tone === "warn") return "var(--sl-warn)";
  if (tone === "bad") return "var(--sl-crit)";
  return "var(--sl-text)";
}

export function toneChartColor(tone: Tone): string {
  if (tone === "ok") return C.accent;
  if (tone === "warn") return C.amber;
  if (tone === "bad") return C.red;
  return C.graphite;
}

export const TOOLTIP_STYLE: React.CSSProperties = {
  background: C.surface,
  border: `1px solid ${C.hair}`,
  borderRadius: 8,
  color: C.text,
  fontFamily: "var(--sl-font-sans)",
  fontSize: 12,
  boxShadow: "0 4px 14px rgba(14,17,22,0.10)",
};
