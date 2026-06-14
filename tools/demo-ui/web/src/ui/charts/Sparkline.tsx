/* ============================================================================
   Sparkline -- inline trend, no axes
   ----------------------------------------------------------------------------
   Compact filled line for KPI cards. Mint by default; pass tone="graphite"
   for restrained "actual" series.
   ============================================================================ */
import { useId } from "react";
import { smoothPath, minOf, maxOf, type Point } from "./svg";

export interface SparklineProps {
  data: number[];
  width?: number;
  height?: number;
  tone?: "mint" | "graphite";
  /** Fill the area under the line. */
  fill?: boolean;
  className?: string;
}

export function Sparkline({
  data,
  width = 120,
  height = 28,
  tone = "mint",
  fill = true,
  className,
}: SparklineProps) {
  const id = useId().replace(/:/g, "");
  const pad = 2;
  const lo = minOf(data);
  const hi = maxOf(data);
  const span = hi - lo || 1;
  const x = (i: number) =>
    data.length <= 1 ? pad : pad + (i / (data.length - 1)) * (width - pad * 2);
  const y = (v: number) => pad + (1 - (v - lo) / span) * (height - pad * 2);
  const pts: Point[] = data.map((v, i) => [x(i), y(v)]);
  const line = smoothPath(pts);
  const stroke = tone === "mint" ? "var(--sl-mint)" : "var(--sl-graphite)";
  const last = data.length ? data[data.length - 1] : 0;

  return (
    <svg
      className={className}
      width={width}
      height={height}
      viewBox={`0 0 ${width} ${height}`}
      preserveAspectRatio="none"
      role="img"
      aria-hidden="true"
    >
      {fill ? (
        <>
          <defs>
            <linearGradient id={`${id}-fill`} x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor={stroke} stopOpacity="0.22" />
              <stop offset="100%" stopColor={stroke} stopOpacity="0" />
            </linearGradient>
          </defs>
          <path
            d={`${line} L ${x(data.length - 1)} ${height} L ${x(0)} ${height} Z`}
            fill={`url(#${id}-fill)`}
          />
        </>
      ) : null}
      <path d={line} fill="none" stroke={stroke} strokeWidth={1.6} strokeLinecap="round" strokeLinejoin="round" />
      {data.length ? <circle cx={x(data.length - 1)} cy={y(last)} r={2.2} fill={stroke} /> : null}
    </svg>
  );
}
