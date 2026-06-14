/* ============================================================================
   ForecastChart -- the hero
   ----------------------------------------------------------------------------
   The flagship chart: a graphite "actual" line, a mint "forecast" line leading
   one step ahead, a confidence band, and a dashed "scale decision" marker. Real
   axes, gridlines and units; the forecast line draws on; a hover crosshair with
   a tooltip reports actual / forecast / band at the nearest step.
   ============================================================================ */
import { useEffect, useId, useRef, useState } from "react";
import { smoothPath, linePath, lerp, type Point } from "./svg";

export interface ForecastChartProps {
  /** Actual series (graphite). */
  actual: number[];
  /**
   * Forecast series (mint), leading by one step. Index 0 should align with the
   * last actual point so the lines hand off cleanly.
   */
  forecast: number[];
  /** Lower confidence bound, aligned to the forecast series. */
  confLow?: number[];
  /** Upper confidence bound, aligned to the forecast series. */
  confHigh?: number[];
  /** X-axis tick labels (left to right). */
  xLabels?: string[];
  /** Index in the forecast series where the scale-ahead decision fired. */
  scaleIndex?: number;
  /** Label for the scale decision marker. */
  scaleLabel?: string;
  /** Unit suffix shown in the tooltip / y-axis (e.g. "k rpm"). */
  unit?: string;
  /** Multiply the y-tick value for display (e.g. divide by 10 -> "k"). */
  height?: number;
  className?: string;
}

const W = 720;
const M = { left: 46, right: 16, top: 16, bottom: 34 };

/**
 * Adaptive y-axis tick label. Picks decimals from the tick step so small
 * ranges don't all round to "0", and compacts thousands to "k".
 */
function fmtTick(v: number, step: number): string {
  if (Math.abs(v) >= 1000) {
    const k = v / 1000;
    return `${k.toFixed(Math.abs(k) >= 10 ? 0 : 1)}k`;
  }
  const decimals = step >= 1 ? 0 : step >= 0.1 ? 1 : 2;
  return v.toFixed(decimals);
}

export function ForecastChart({
  actual,
  forecast,
  confLow,
  confHigh,
  xLabels,
  scaleIndex,
  scaleLabel = "scale-ahead",
  unit = "",
  height = 300,
  className,
}: ForecastChartProps) {
  const id = useId().replace(/:/g, "");
  const foreRef = useRef<SVGPathElement>(null);
  const svgRef = useRef<SVGSVGElement>(null);
  const [hover, setHover] = useState<number | null>(null);

  const H = height;
  const iw = W - M.left - M.right;
  const ih = H - M.top - M.bottom;

  // forecast is offset to start at the last actual index
  const offset = actual.length - 1;
  const foreEnd = offset + forecast.length - 1;
  const total = foreEnd + 1;

  const all = [...actual, ...forecast, ...(confHigh ?? []), ...(confLow ?? [])];
  const rawMin = Math.min(...all);
  const rawMax = Math.max(...all);
  const pad = (rawMax - rawMin) * 0.12 || 1;
  const yMin = rawMin - pad;
  const yMax = rawMax + pad;

  const x = (i: number) => (total <= 1 ? M.left : M.left + (i / (total - 1)) * iw);
  const y = (v: number) => M.top + (1 - (v - yMin) / (yMax - yMin || 1)) * ih;

  const aPts: Point[] = actual.map((v, i) => [x(i), y(v)]);
  const fPts: Point[] = forecast.map((v, i) => [x(offset + i), y(v)]);

  // gridlines
  const ticks = 4;
  const tickValues = Array.from({ length: ticks + 1 }, (_, i) => lerp(yMin, yMax, i / ticks));
  const tickStep = (yMax - yMin) / ticks;

  // confidence band polygon
  let bandPath = "";
  if (confLow && confHigh && confLow.length === confHigh.length) {
    const topPts: Point[] = confHigh.map((v, i) => [x(offset + i), y(v)]);
    const botPts: Point[] = [...confLow]
      .map((v, i): Point => [x(offset + i), y(v)])
      .reverse();
    bandPath = smoothPath([...topPts, ...botPts]) + " Z";
  }

  // area under actual
  const aArea =
    linePath(aPts) + ` L ${x(offset)} ${y(yMin)} L ${x(0)} ${y(yMin)} Z`;

  useEffect(() => {
    const fore = foreRef.current;
    if (!fore) return;
    const len = fore.getTotalLength();
    fore.style.transition = "none";
    fore.style.strokeDasharray = String(len);
    fore.style.strokeDashoffset = String(len);
    void fore.getBoundingClientRect();
    const raf = requestAnimationFrame(() => {
      fore.style.transition = "stroke-dashoffset 1.4s var(--sl-ease)";
      fore.style.strokeDashoffset = "0";
    });
    return () => cancelAnimationFrame(raf);
  }, [actual, forecast]);

  const onMove = (e: React.MouseEvent<SVGSVGElement>) => {
    const svg = svgRef.current;
    if (!svg) return;
    const r = svg.getBoundingClientRect();
    const px = ((e.clientX - r.left) / r.width) * W;
    let i = Math.round(((px - M.left) / iw) * (total - 1));
    i = Math.max(0, Math.min(total - 1, i));
    setHover(i);
  };

  const hoverActual = hover != null && hover < actual.length ? actual[hover] : null;
  const hoverFore = hover != null && hover >= offset ? forecast[hover - offset] : null;
  const hoverX = hover != null ? x(hover) : 0;

  return (
    <div className={className} style={{ position: "relative", width: "100%" }}>
      <svg
        ref={svgRef}
        width="100%"
        viewBox={`0 0 ${W} ${H}`}
        style={{ display: "block", cursor: "crosshair" }}
        onMouseMove={onMove}
        onMouseLeave={() => setHover(null)}
        role="img"
        aria-label="forecast leading actual throughput with confidence band"
      >
        <defs>
          <linearGradient id={`${id}-band`} x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="var(--sl-mint)" stopOpacity="0.22" />
            <stop offset="100%" stopColor="var(--sl-mint)" stopOpacity="0.04" />
          </linearGradient>
          <linearGradient id={`${id}-area`} x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="var(--sl-graphite)" stopOpacity="0.12" />
            <stop offset="100%" stopColor="var(--sl-graphite)" stopOpacity="0" />
          </linearGradient>
          <filter id={`${id}-glow`} x="-10%" y="-10%" width="120%" height="120%">
            <feDropShadow dx="0" dy="0" stdDeviation="2" floodColor="var(--sl-mint)" floodOpacity="0.5" />
          </filter>
        </defs>

        {/* gridlines + y labels */}
        {tickValues.map((v, i) => (
          <g key={i}>
            <line
              x1={M.left}
              x2={W - M.right}
              y1={y(v)}
              y2={y(v)}
              stroke="var(--sl-grid)"
              strokeWidth={1}
              strokeDasharray={i === 0 ? "0" : "2 5"}
            />
            <text
              x={M.left - 9}
              y={y(v) + 3.5}
              textAnchor="end"
              fill="var(--sl-text-faint)"
              fontSize={10}
              fontFamily="var(--sl-font-mono)"
            >
              {fmtTick(v, tickStep)}
            </text>
          </g>
        ))}

        {/* x labels */}
        {(xLabels ?? []).map((lab, i, arr) => (
          <text
            key={i}
            x={lerp(M.left, W - M.right, arr.length <= 1 ? 0 : i / (arr.length - 1))}
            y={H - 12}
            textAnchor="middle"
            fill="var(--sl-text-faint)"
            fontSize={10.5}
            fontFamily="var(--sl-font-mono)"
          >
            {lab}
          </text>
        ))}

        {/* confidence band */}
        {bandPath ? <path d={bandPath} fill={`url(#${id}-band)`} /> : null}

        {/* actual area + line */}
        <path d={aArea} fill={`url(#${id}-area)`} />
        <path
          d={smoothPath(aPts)}
          fill="none"
          stroke="var(--sl-graphite)"
          strokeWidth={2.4}
          strokeLinecap="round"
          strokeLinejoin="round"
        />

        {/* forecast line (draws on) */}
        <path
          ref={foreRef}
          d={smoothPath(fPts)}
          fill="none"
          stroke="var(--sl-mint)"
          strokeWidth={2.8}
          strokeLinecap="round"
          strokeLinejoin="round"
          filter={`url(#${id}-glow)`}
        />

        {/* scale-ahead decision marker */}
        {scaleIndex != null ? (
          <g>
            <line
              x1={x(offset + scaleIndex)}
              x2={x(offset + scaleIndex)}
              y1={M.top}
              y2={H - M.bottom}
              stroke="var(--sl-text-faint)"
              strokeWidth={1.4}
              strokeDasharray="3 4"
            />
            <circle
              cx={x(offset + scaleIndex)}
              cy={y(forecast[scaleIndex])}
              r={4.5}
              fill="var(--sl-mint)"
              stroke="var(--sl-surface)"
              strokeWidth={2}
            />
            {(() => {
              const sx = x(offset + scaleIndex);
              const nearRight = sx > W - M.right - 76;
              return (
                <text
                  x={nearRight ? sx - 6 : sx + 6}
                  y={M.top + 12}
                  textAnchor={nearRight ? "end" : "start"}
                  fill="var(--sl-text-low)"
                  fontSize={10}
                  fontFamily="var(--sl-font-mono)"
                >
                  {scaleLabel}
                </text>
              );
            })()}
          </g>
        ) : null}

        {/* hover crosshair + dots */}
        {hover != null ? (
          <g>
            <line
              x1={hoverX}
              x2={hoverX}
              y1={M.top}
              y2={H - M.bottom}
              stroke="var(--sl-graphite-soft)"
              strokeWidth={1}
            />
            {hoverActual != null ? (
              <circle cx={hoverX} cy={y(hoverActual)} r={4.5} fill="var(--sl-graphite)" stroke="var(--sl-surface)" strokeWidth={2} />
            ) : null}
            {hoverFore != null ? (
              <circle cx={hoverX} cy={y(hoverFore)} r={4.5} fill="var(--sl-mint)" stroke="var(--sl-surface)" strokeWidth={2} />
            ) : null}
          </g>
        ) : null}
      </svg>

      {/* tooltip */}
      {hover != null ? (
        <div
          style={{
            position: "absolute",
            left: `min(${(hoverX / W) * 100}%, calc(100% - 160px))`,
            top: 8,
            pointerEvents: "none",
            background: "var(--sl-text)",
            color: "var(--sl-surface)",
            borderRadius: "var(--sl-radius-sm)",
            padding: "8px 11px",
            fontFamily: "var(--sl-font-mono)",
            fontSize: 11,
            boxShadow: "var(--sl-shadow-2)",
            whiteSpace: "nowrap",
            zIndex: 4,
          }}
        >
          <div style={{ opacity: 0.6, fontSize: 10, marginBottom: 3 }}>
            {xLabels && xLabels[hover] ? xLabels[hover] : `step ${hover}`}
          </div>
          {hoverActual != null ? (
            <div style={{ display: "flex", gap: 8, justifyContent: "space-between" }}>
              <span>actual</span>
              <span>{`${hoverActual}${unit ? " " + unit : ""}`}</span>
            </div>
          ) : null}
          {hoverFore != null ? (
            <div style={{ display: "flex", gap: 8, justifyContent: "space-between", color: "var(--sl-mint)" }}>
              <span>forecast</span>
              <span>{`${hoverFore}${unit ? " " + unit : ""}`}</span>
            </div>
          ) : null}
          {confLow && confHigh && hover >= offset ? (
            <div style={{ display: "flex", gap: 8, justifyContent: "space-between", opacity: 0.6 }}>
              <span>band</span>
              <span>{`${confLow[hover - offset]}–${confHigh[hover - offset]}`}</span>
            </div>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}
