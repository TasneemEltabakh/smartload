/* ============================================================================
   SVG geometry helpers
   ----------------------------------------------------------------------------
   Pure math shared by the hand-built charts, the logomark, and the signature
   motion. No DOM, no dependencies.
   ============================================================================ */

export type Point = [number, number];

/** Smallest / largest of a list (empty-safe). */
export const minOf = (xs: number[]): number => (xs.length ? Math.min(...xs) : 0);
export const maxOf = (xs: number[]): number => (xs.length ? Math.max(...xs) : 1);

/** Linear interpolation. */
export const lerp = (a: number, b: number, t: number): number => a + (b - a) * t;

/** Clamp helper. */
export const clamp = (v: number, lo: number, hi: number): number =>
  Math.max(lo, Math.min(hi, v));

/**
 * Catmull-Rom -> cubic-bezier smoothing. Produces a flowing line through every
 * point, matching the prototype chart curves.
 */
export function smoothPath(pts: Point[]): string {
  if (pts.length === 0) return "";
  if (pts.length === 1) return `M ${pts[0][0]} ${pts[0][1]}`;
  let d = `M ${pts[0][0].toFixed(2)} ${pts[0][1].toFixed(2)}`;
  for (let i = 0; i < pts.length - 1; i++) {
    const p0 = pts[i - 1] || pts[i];
    const p1 = pts[i];
    const p2 = pts[i + 1];
    const p3 = pts[i + 2] || p2;
    const c1x = p1[0] + (p2[0] - p0[0]) / 6;
    const c1y = p1[1] + (p2[1] - p0[1]) / 6;
    const c2x = p2[0] - (p3[0] - p1[0]) / 6;
    const c2y = p2[1] - (p3[1] - p1[1]) / 6;
    d += ` C ${c1x.toFixed(2)} ${c1y.toFixed(2)}, ${c2x.toFixed(2)} ${c2y.toFixed(
      2,
    )}, ${p2[0].toFixed(2)} ${p2[1].toFixed(2)}`;
  }
  return d;
}

/** Straight polyline through points. */
export function linePath(pts: Point[]): string {
  return pts
    .map((p, i) => `${i ? "L" : "M"} ${p[0].toFixed(2)} ${p[1].toFixed(2)}`)
    .join(" ");
}

/** Build a linear x-scale across a plot's inner width. */
export function xScale(count: number, left: number, innerWidth: number) {
  return (i: number): number =>
    count <= 1 ? left : left + (i / (count - 1)) * innerWidth;
}

/** Build an inverted y-scale (SVG y grows downward). */
export function yScale(
  min: number,
  max: number,
  top: number,
  innerHeight: number,
) {
  const span = max - min || 1;
  return (v: number): number => top + (1 - (v - min) / span) * innerHeight;
}
