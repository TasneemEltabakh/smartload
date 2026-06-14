/**
 * tools/demo-ui/web/src/pages/_sampleDrive.ts
 * ────────────────────────────────────────────
 * Static fall-back catalogue for the Drive page. When the BFF is unreachable
 * the profile picker would otherwise be empty; these shapes mirror the live
 * /bench/profiles surface so the page still reads as a real cockpit (clearly
 * flagged "offline preview"). They are never started — the run button is
 * disabled while the BFF is down.
 */

import type { BenchProfile } from "../api";

export const SAMPLE_PROFILES: BenchProfile[] = [
  {
    id: "adaptive-5phase",
    label: "Adaptive 5-phase",
    description:
      "Warm-up, ramp, sustain, anomaly-under-load, then drain. The headline shape: the pool grows ahead of load, isolates the slow node, then shrinks.",
    total_secs: 300,
    phases: [
      { name: "warm-up", secs: 30, users: 10, anomaly: false },
      { name: "ramp", secs: 60, users: 60, anomaly: false },
      { name: "sustain", secs: 90, users: 90, anomaly: false },
      { name: "anomaly", secs: 60, users: 90, anomaly: true },
      { name: "drain", secs: 60, users: 10, anomaly: false },
    ],
  },
  {
    id: "spike",
    label: "Flash spike",
    description:
      "A quiet baseline then a sharp burst. Stresses scale-ahead reaction time before p95 crosses the SLO.",
    total_secs: 150,
    phases: [
      { name: "baseline", secs: 45, users: 15, anomaly: false },
      { name: "spike", secs: 45, users: 120, anomaly: false },
      { name: "settle", secs: 60, users: 20, anomaly: false },
    ],
  },
  {
    id: "anomaly-under-load",
    label: "Anomaly under load",
    description:
      "Sustained heavy traffic with one backend degraded mid-run. Watch p95 spike then recover as the slow node is isolated.",
    total_secs: 180,
    phases: [
      { name: "load", secs: 60, users: 80, anomaly: false },
      { name: "degrade", secs: 60, users: 80, anomaly: true },
      { name: "recover", secs: 60, users: 80, anomaly: false },
    ],
  },
];
