/* ============================================================================
   Donut -- proportion ring
   ----------------------------------------------------------------------------
   A hand-built ring for share / utilisation. Each segment is an arc; the center
   carries an optional value + caption. Theme-token colored.
   ============================================================================ */
import type { ReactNode } from "react";

export interface DonutSegment {
  id: string;
  value: number;
  /** CSS color (defaults walk the mint -> graphite ramp). */
  color?: string;
  label?: string;
}

export interface DonutProps {
  segments: DonutSegment[];
  size?: number;
  thickness?: number;
  /** Big center value. */
  centerValue?: ReactNode;
  /** Small center caption. */
  centerLabel?: ReactNode;
  className?: string;
}

const RAMP = [
  "var(--sl-mint)",
  "var(--sl-mint-deep)",
  "var(--sl-graphite)",
  "var(--sl-graphite-soft)",
  "var(--sl-warn)",
  "var(--sl-crit)",
];

function arc(cx: number, cy: number, r: number, a0: number, a1: number): string {
  const p0x = cx + r * Math.cos(a0);
  const p0y = cy + r * Math.sin(a0);
  const p1x = cx + r * Math.cos(a1);
  const p1y = cy + r * Math.sin(a1);
  const large = a1 - a0 > Math.PI ? 1 : 0;
  return `M ${p0x.toFixed(2)} ${p0y.toFixed(2)} A ${r} ${r} 0 ${large} 1 ${p1x.toFixed(2)} ${p1y.toFixed(2)}`;
}

export function Donut({
  segments,
  size = 120,
  thickness = 14,
  centerValue,
  centerLabel,
  className,
}: DonutProps) {
  const total = segments.reduce((s, seg) => s + seg.value, 0) || 1;
  const r = (size - thickness) / 2;
  const cx = size / 2;
  const cy = size / 2;
  let angle = -Math.PI / 2;
  const gap = 0.04;

  return (
    <div className={className} style={{ position: "relative", width: size, height: size }}>
      <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`} role="img" aria-label="proportion">
        <circle cx={cx} cy={cy} r={r} fill="none" stroke="var(--sl-surface-sunk)" strokeWidth={thickness} />
        {segments.map((seg, i) => {
          const frac = seg.value / total;
          const a0 = angle + gap / 2;
          const a1 = angle + frac * Math.PI * 2 - gap / 2;
          angle += frac * Math.PI * 2;
          if (a1 <= a0) return null;
          return (
            <path
              key={seg.id}
              d={arc(cx, cy, r, a0, a1)}
              fill="none"
              stroke={seg.color ?? RAMP[i % RAMP.length]}
              strokeWidth={thickness}
              strokeLinecap="round"
            />
          );
        })}
      </svg>
      {(centerValue != null || centerLabel != null) && (
        <div
          style={{
            position: "absolute",
            inset: 0,
            display: "grid",
            placeItems: "center",
            textAlign: "center",
          }}
        >
          <div>
            {centerValue != null ? (
              <div
                style={{
                  fontFamily: "var(--sl-font-mono)",
                  fontWeight: 700,
                  fontSize: 18,
                  color: "var(--sl-text)",
                  letterSpacing: "-0.5px",
                }}
              >
                {centerValue}
              </div>
            ) : null}
            {centerLabel != null ? (
              <div style={{ fontSize: 10, color: "var(--sl-text-low)", marginTop: 2 }}>{centerLabel}</div>
            ) : null}
          </div>
        </div>
      )}
    </div>
  );
}
