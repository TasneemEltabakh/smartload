#!/usr/bin/env python3
"""experiments/adaptive-advantage/ablation_compare.py

Aggregate an ablation batch (ablation.sh) into a contribution table.

Input: a manifest TSV with rows `<config>\t<side>\t<run_root>` (written by
ablation.sh). For each (config, side) it averages the per-phase error rate across
every `run-NN/<side>/locust_stats.csv` under <run_root>, then prints a Markdown
report:

  * baseline (NGINX RR, from the `full` config's baseline side) — the floor,
  * full SmartLoad — the ceiling,
  * each leave-one-out config and, per phase, the DELTA vs full = "the cost of
    removing this fix" (positive = errors got worse without it).

Usage:  python3 ablation_compare.py <manifest.tsv>
"""
from __future__ import annotations

import csv
import os
import sys
from collections import defaultdict

PHASES = ["A_ramp", "B_degrade", "C_spike", "D_slow", "E_tail"]
# Config -> human label + which fix it removes (for the report).
CONFIG_NOTE = {
    "full": "full stack (all fixes on)",
    "no-clamp": "− anti-concentration clamp (T0.1/T0.2)",
    "no-guard": "− #3 absolute-overload guard (T0.3)",
    "no-pin": "− equal-capacity pin (autoscaler free to flap) (T0.6a)",
    "no-reset": "− per-side routing reset (T0.6b)",
}


def _phase_errs_one_csv(path: str) -> dict[str, tuple[int, int]]:
    """Return {phase: (requests, failures)} from one locust_stats.csv."""
    out: dict[str, tuple[int, int]] = {}
    try:
        with open(path, newline="") as fh:
            for r in csv.DictReader(fh):
                name = r.get("Name", "")
                if not name.startswith("GET-/-"):
                    continue
                phase = name[len("GET-/-"):]
                try:
                    req = int(r["Request Count"])
                    fail = int(r["Failure Count"])
                except (KeyError, ValueError):
                    continue
                out[phase] = (req, fail)
    except OSError:
        pass
    return out


def _avg_phase_errs(run_root: str, side: str) -> dict[str, float]:
    """Average per-phase error% across every run-NN/<side> under run_root."""
    acc: dict[str, list[float]] = defaultdict(list)
    if not os.path.isdir(run_root):
        return {}
    for entry in sorted(os.listdir(run_root)):
        csv_path = os.path.join(run_root, entry, side, "locust_stats.csv")
        if not os.path.isfile(csv_path):
            continue
        for phase, (req, fail) in _phase_errs_one_csv(csv_path).items():
            acc[phase].append(100.0 * fail / req if req else 0.0)
    return {p: sum(v) / len(v) for p, v in acc.items() if v}


def _overall(phase_errs: dict[str, float]) -> float:
    vals = [phase_errs[p] for p in PHASES if p in phase_errs]
    return sum(vals) / len(vals) if vals else float("nan")


def _fmt(x: float) -> str:
    return "  —  " if x != x else f"{x:5.1f}%"  # x!=x => NaN


def main(manifest: str) -> int:
    # Parse manifest -> {config: {side: run_root}}
    entries: dict[str, dict[str, str]] = defaultdict(dict)
    try:
        with open(manifest, newline="") as fh:
            for row in csv.reader(fh, delimiter="\t"):
                if len(row) != 3:
                    continue
                cfg, side, root = row
                entries[cfg][side] = root
    except OSError as exc:
        print(f"cannot read manifest {manifest}: {exc}", file=sys.stderr)
        return 1

    # Collect per-config smartload error profiles + the baseline (from `full`).
    profiles: dict[str, dict[str, float]] = {}
    for cfg, sides in entries.items():
        if "smartload" in sides:
            profiles[cfg] = _avg_phase_errs(sides["smartload"], "smartload")
    baseline = {}
    if "full" in entries and "baseline" in entries["full"]:
        baseline = _avg_phase_errs(entries["full"]["baseline"], "baseline")

    full = profiles.get("full", {})

    # ── Report ────────────────────────────────────────────────────────────────
    lines: list[str] = []
    lines.append("# Ablation — SmartLoad C_spike fix stack (5v5 equal-capacity)\n")
    lines.append("Per-phase **error rate**; `Δfull` = error% of the ablated config "
                 "minus the full stack = **the cost of removing that fix** "
                 "(positive ⇒ worse without it).\n")

    # 1) Absolute table: baseline, full, each config.
    header = "| Phase | baseline | " + " | ".join(
        c for c in ("full", "no-clamp", "no-guard", "no-pin", "no-reset") if c in profiles
    ) + " |"
    sep = "|" + "---|" * (header.count("|") - 1)
    lines.append("## Absolute error% per config\n")
    lines.append(header)
    lines.append(sep)
    order = [c for c in ("full", "no-clamp", "no-guard", "no-pin", "no-reset") if c in profiles]
    for ph in PHASES + ["OVERALL"]:
        if ph == "OVERALL":
            b = _overall(baseline)
            row = [f"| **{ph}** | {_fmt(b)} |"]
            for c in order:
                row.append(f" {_fmt(_overall(profiles[c]))} |")
        else:
            row = [f"| {ph} | {_fmt(baseline.get(ph, float('nan')))} |"]
            for c in order:
                row.append(f" {_fmt(profiles[c].get(ph, float('nan')))} |")
        lines.append("".join(row))

    # 2) Contribution table: Δ vs full for each removed fix.
    abl = [c for c in order if c != "full"]
    if full and abl:
        lines.append("\n## Contribution — Δ vs full (cost of removing each fix)\n")
        h2 = "| Phase | " + " | ".join(abl) + " |"
        lines.append(h2)
        lines.append("|" + "---|" * (h2.count("|") - 1))
        for ph in PHASES + ["OVERALL"]:
            if ph == "OVERALL":
                fv = _overall(full)
                cells = [f"| **{ph}** |"]
                for c in abl:
                    d = _overall(profiles[c]) - fv
                    cells.append(f" {d:+5.1f} |")
            else:
                fv = full.get(ph, float("nan"))
                cells = [f"| {ph} |"]
                for c in abl:
                    cv = profiles[c].get(ph, float("nan"))
                    d = cv - fv
                    cells.append(f" {'  —  ' if d != d else f'{d:+5.1f}'} |")
            lines.append("".join(cells))
        lines.append("\n*Read C_spike: the largest positive Δ is the fix that "
                     "contributes most to surviving the spike.*")

    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(2)
    sys.exit(main(sys.argv[1]))
