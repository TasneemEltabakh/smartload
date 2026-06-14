/* ============================================================================
   Logomark -- the predictive curve, in miniature
   ----------------------------------------------------------------------------
   A graphite "actual" line, a mint "forecast" line leading one step ahead, and
   a pulsing decision point. The animated variant echoes the hero motion as a
   live heartbeat for the brand.
   ============================================================================ */
import { useEffect, useId, useRef } from "react";
import { smoothPath, type Point } from "../charts/svg";

export interface LogomarkProps {
  /** Square edge length in px. */
  size?: number;
  /** When true, the forecast line redraws and the point pulses on a loop. */
  animated?: boolean;
  className?: string;
}

const PAD = 7;

function curve(size: number) {
  const span = size - PAD * 2;
  const actual = [6, 7, 9, 8, 11, 13];
  const forecast = [11, 13, 17, 19];
  const total = actual.length + forecast.length - 1;
  const sx = (i: number) => PAD + (i / (total - 1)) * span;
  const sy = (v: number) => size - PAD - (v / 20) * span;
  const aPts: Point[] = actual.map((v, i) => [sx(i), sy(v)]);
  const offset = actual.length - 1;
  const fPts: Point[] = forecast.map((v, i) => [sx(offset + i), sy(v)]);
  return { aPts, fPts };
}

export function Logomark({ size = 34, animated = false, className }: LogomarkProps) {
  const id = useId().replace(/:/g, "");
  const foreRef = useRef<SVGPathElement>(null);
  const pointRef = useRef<SVGCircleElement>(null);
  const { aPts, fPts } = curve(size);
  const tip = fPts[fPts.length - 1];

  useEffect(() => {
    if (!animated) return;
    const fore = foreRef.current;
    const point = pointRef.current;
    if (!fore || !point) return;
    let cancelled = false;
    const timers: number[] = [];
    const len = fore.getTotalLength();

    const run = () => {
      if (cancelled) return;
      fore.style.transition = "none";
      fore.style.strokeDasharray = String(len);
      fore.style.strokeDashoffset = String(len);
      point.style.opacity = "0";
      // force reflow so the reset takes before the draw
      void fore.getBoundingClientRect();
      requestAnimationFrame(() => {
        if (cancelled) return;
        fore.style.transition = "stroke-dashoffset 1.2s var(--sl-ease)";
        fore.style.strokeDashoffset = "0";
      });
      timers.push(
        window.setTimeout(() => {
          point.style.transition = "opacity 0.3s ease";
          point.style.opacity = "1";
        }, 1100),
      );
      timers.push(window.setTimeout(run, 1200 + 800 + 1500));
    };
    run();

    return () => {
      cancelled = true;
      timers.forEach((t) => window.clearTimeout(t));
    };
  }, [animated, size]);

  return (
    <svg
      className={className}
      width={size}
      height={size}
      viewBox={`0 0 ${size} ${size}`}
      fill="none"
      role="img"
      aria-label="smartload logomark"
    >
      <defs>
        <filter id={`${id}-glow`} x="-40%" y="-40%" width="180%" height="180%">
          <feDropShadow dx="0" dy="0" stdDeviation="1.2" floodColor="var(--sl-mint)" floodOpacity="0.7" />
        </filter>
      </defs>
      <path
        d={smoothPath(aPts)}
        stroke="var(--sl-graphite)"
        strokeWidth={1.8}
        strokeLinecap="round"
        strokeLinejoin="round"
      />
      <path
        ref={foreRef}
        d={smoothPath(fPts)}
        stroke="var(--sl-mint)"
        strokeWidth={2}
        strokeLinecap="round"
        strokeLinejoin="round"
        filter={`url(#${id}-glow)`}
      />
      <circle
        ref={pointRef}
        cx={tip[0]}
        cy={tip[1]}
        r={2.4}
        fill="var(--sl-mint)"
        opacity={animated ? 0 : 1}
      />
    </svg>
  );
}
