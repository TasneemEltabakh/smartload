/* ============================================================================
   Heartbeat -- the signature predictive-curve motion
   ----------------------------------------------------------------------------
   The product's signature animation: a muted graphite "actual" line draws in,
   a mint "forecast" line catches up and overtakes one step ahead, and the
   decision point pulses once. ~1.6s draw on cubic-bezier(0.22,1,0.36,1) with
   an ~800ms pause between loops. Used as a hero visual and as a live indicator.
   ============================================================================ */
import { useEffect, useId, useRef } from "react";
import { smoothPath, yScale, type Point } from "../charts/svg";

export interface HeartbeatProps {
  /** Width in px. */
  width?: number;
  /** Height in px. */
  height?: number;
  /** Loop the animation; when false it draws once. */
  loop?: boolean;
  /** Render the confidence band under the forecast tail. */
  showBand?: boolean;
  className?: string;
}

const ACTUAL = [42, 44, 41, 48, 52, 50, 58, 62, 60, 66, 70];
const FORECAST = [70, 74, 80, 88, 96, 104, 110];

export function Heartbeat({
  width = 480,
  height = 200,
  loop = true,
  showBand = true,
  className,
}: HeartbeatProps) {
  const id = useId().replace(/:/g, "");
  const actualRef = useRef<SVGPathElement>(null);
  const foreRef = useRef<SVGPathElement>(null);
  const pointRef = useRef<SVGCircleElement>(null);
  const haloRef = useRef<SVGCircleElement>(null);

  const W = width;
  const H = height;
  const pad = 14;
  const all = [...ACTUAL, ...FORECAST];
  const yMin = Math.min(...all) - 8;
  const yMax = Math.max(...all) + 10;
  const total = ACTUAL.length + FORECAST.length - 1;
  const x = (i: number) => pad + (i / (total - 1)) * (W - pad * 2);
  const y = yScale(yMin, yMax, pad, H - pad * 2);

  const aPts: Point[] = ACTUAL.map((v, i) => [x(i), y(v)]);
  const offset = ACTUAL.length - 1;
  const fPts: Point[] = FORECAST.map((v, i) => [x(offset + i), y(v)]);
  const tip = fPts[fPts.length - 1];

  const bandTop: Point[] = FORECAST.map((v, i) => [x(offset + i), y(v + 6)]);
  const bandBot: Point[] = [...FORECAST]
    .map((v, i): Point => [x(offset + i), y(v - 6)])
    .reverse();
  const bandPath = smoothPath([...bandTop, ...bandBot]) + " Z";

  useEffect(() => {
    const actual = actualRef.current;
    const fore = foreRef.current;
    const point = pointRef.current;
    const halo = haloRef.current;
    if (!actual || !fore || !point || !halo) return;

    let cancelled = false;
    const timers: number[] = [];
    const aLen = actual.getTotalLength();
    const fLen = fore.getTotalLength();

    const run = () => {
      if (cancelled) return;
      actual.style.transition = "none";
      fore.style.transition = "none";
      actual.style.strokeDasharray = String(aLen);
      actual.style.strokeDashoffset = String(aLen);
      fore.style.strokeDasharray = String(fLen);
      fore.style.strokeDashoffset = String(fLen);
      point.style.opacity = "0";
      halo.style.opacity = "0";
      void actual.getBoundingClientRect();

      requestAnimationFrame(() => {
        if (cancelled) return;
        actual.style.transition = "stroke-dashoffset 1.0s var(--sl-ease)";
        actual.style.strokeDashoffset = "0";
      });
      // forecast catches up and overtakes, slightly delayed but quicker
      timers.push(
        window.setTimeout(() => {
          fore.style.transition = "stroke-dashoffset 0.9s var(--sl-ease)";
          fore.style.strokeDashoffset = "0";
        }, 650),
      );
      // decision point pulses once
      timers.push(
        window.setTimeout(() => {
          point.style.transition = "opacity 0.3s ease";
          point.style.opacity = "1";
          halo.style.opacity = "0.8";
          halo.style.transition = "r 0.7s ease, opacity 0.7s ease";
          halo.setAttribute("r", "16");
          halo.style.opacity = "0";
        }, 1500),
      );
      if (loop) timers.push(window.setTimeout(run, 1600 + 800));
    };
    run();

    return () => {
      cancelled = true;
      timers.forEach((t) => window.clearTimeout(t));
    };
  }, [loop, width, height]);

  return (
    <svg
      className={className}
      width="100%"
      height="100%"
      viewBox={`0 0 ${W} ${H}`}
      preserveAspectRatio="none"
      role="img"
      aria-label="forecast leading actual throughput"
    >
      <defs>
        <linearGradient id={`${id}-band`} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor="var(--sl-mint)" stopOpacity="0.18" />
          <stop offset="100%" stopColor="var(--sl-mint)" stopOpacity="0" />
        </linearGradient>
        <filter id={`${id}-glow`} x="-20%" y="-20%" width="140%" height="140%">
          <feDropShadow dx="0" dy="0" stdDeviation="2.2" floodColor="var(--sl-mint)" floodOpacity="0.55" />
        </filter>
      </defs>

      {showBand ? <path d={bandPath} fill={`url(#${id}-band)`} /> : null}

      <path
        ref={actualRef}
        d={smoothPath(aPts)}
        fill="none"
        stroke="var(--sl-graphite)"
        strokeWidth={2}
        strokeLinecap="round"
        strokeLinejoin="round"
      />
      <path
        ref={foreRef}
        d={smoothPath(fPts)}
        fill="none"
        stroke="var(--sl-mint)"
        strokeWidth={2.4}
        strokeLinecap="round"
        strokeLinejoin="round"
        filter={`url(#${id}-glow)`}
      />
      <circle ref={haloRef} cx={tip[0]} cy={tip[1]} r={5} fill="none" stroke="var(--sl-mint)" strokeWidth={2} opacity={0} />
      <circle ref={pointRef} cx={tip[0]} cy={tip[1]} r={3.5} fill="var(--sl-mint)" opacity={0} />
    </svg>
  );
}
