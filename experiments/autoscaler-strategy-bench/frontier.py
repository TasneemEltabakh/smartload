"""
experiments/autoscaler-strategy-bench/frontier.py
──────────────────────────────────────────────────
SLA-vs-cost Pareto frontier for the target-based controllers.

The single knob that trades SLA for cost is the controller's safety margin
(`headroom`, or the QoS β for the sqrt-staffing law). Sweeping it traces out a
curve in (over-prov cost, SLA%) space. This script runs that sweep for the
realistic controllers and writes the points plus the fixed baseline anchors so
the curve can be compared against:

  - S2 Predictive-realistic (MA)  — the headline baseline this work must beat;
  - S5 Naive-threshold            — the high-SLA / high-cost reference;
  - S4 Static N=max               — the SLA-optimal / cost-worst extreme;
  - S1 Predictive-oracle          — the perfect-foresight ceiling on the OLD rule.

Two questions the frontier answers:
  1. Does the controller frontier dominate (lie up-and-left of) the baselines?
  2. Does the predictive signal (trend) dominate the reactive signal (trailing
     mean) under the SAME controller — i.e. does forecasting pay off at equal
     cost, not just at equal headroom?

Run:  python experiments/autoscaler-strategy-bench/frontier.py
      python experiments/autoscaler-strategy-bench/frontier.py --seeds 8 --tag mytag
Outputs under results/<tag>/: frontier.csv, FRONTIER.md.
"""

from __future__ import annotations

import argparse
import pathlib
import sys
from datetime import datetime, timezone

import numpy as np
import pandas as pd

_HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

from demand import demand_curve, PROFILES  # noqa: E402
from sim import SimParams, run_strategy  # noqa: E402

PEAK_MULT = 8.0

# Controllers swept over headroom, by signal. Each entry: (key, label, strategy,
# sizing). For sqrt-staffing the swept knob is β (mapped onto the headroom arg).
SWEPT = (
    ("ctrl_trend",     "Controller + trend (predictive)",  "C4_ctrl_trend",      "headroom"),
    ("ctrl_reactive",  "Controller + reactive",            "C3_ctrl_reactive",   "headroom"),
    ("ctrl_predictive","Controller + MA forecast",         "C2_ctrl_predictive", "headroom"),
)
HEADROOMS = (0.0, 0.05, 0.10, 0.15, 0.20, 0.30, 0.50)
BETAS = (0.5, 1.0, 1.5, 2.0, 3.0)

# Fixed baseline anchors (no sweep) for context on the same axes.
ANCHORS = (
    ("S1_oracle",     "S1 oracle (old rule ceiling)"),
    ("S2_predictive", "S2 predictive-MA (baseline)"),
    ("S5_naive",      "S5 naive-threshold"),
    ("S4_static_max", "S4 static N=max"),
)


def _mean_over_grid(strategy: str, params: SimParams, seeds: list[int],
                    *, headroom: float = 0.15, sizing: str = "headroom",
                    qos_beta: float = 1.0) -> dict:
    peak = PEAK_MULT * params.per_instance_capacity_rps
    sla, cost, acts = [], [], []
    for profile in PROFILES:
        for seed in seeds:
            d = demand_curve(profile, params.run_steps, peak, seed)
            r = run_strategy(strategy, d, params, cooldown=params.cooldown_seconds,
                             seed=seed, headroom=headroom, sizing=sizing,
                             qos_beta=qos_beta)
            sla.append(r.sla_pct)
            cost.append(r.overprov_cost)
            acts.append(r.scale_actions)
    return {"sla_pct": float(np.mean(sla)),
            "overprov_cost": float(np.mean(cost)),
            "scale_actions": float(np.mean(acts))}


def run_frontier(params: SimParams, seeds: list[int]) -> pd.DataFrame:
    rows = []
    for key, label, strat, sizing in SWEPT:
        for h in HEADROOMS:
            m = _mean_over_grid(strat, params, seeds, headroom=h, sizing=sizing)
            rows.append({"series": key, "label": label, "knob": "headroom",
                         "knob_value": h, **m})
    # sqrt-staffing law swept over β, on the predictive trend signal.
    for b in BETAS:
        m = _mean_over_grid("C6_ctrl_sqrt_trend", params, seeds,
                            sizing="sqrt_staffing", qos_beta=b)
        rows.append({"series": "sqrt_trend", "label": "Sqrt-staffing + trend",
                     "knob": "beta", "knob_value": b, **m})
    # Fixed anchors.
    for strat, label in ANCHORS:
        m = _mean_over_grid(strat, params, seeds)
        rows.append({"series": "anchor", "label": label, "knob": "fixed",
                     "knob_value": np.nan, "strategy": strat, **m})
    return pd.DataFrame(rows)


def _pareto_note(df: pd.DataFrame) -> str:
    """Compare predictive (trend) vs reactive frontier at matched cost."""
    tr = df[df.series == "ctrl_trend"].sort_values("overprov_cost")
    re = df[df.series == "ctrl_reactive"].sort_values("overprov_cost")
    # Interpolate reactive SLA at each trend cost point, compare.
    wins = []
    for _, row in tr.iterrows():
        c = row["overprov_cost"]
        re_sla = float(np.interp(c, re["overprov_cost"], re["sla_pct"]))
        wins.append(row["sla_pct"] - re_sla)
    avg = float(np.mean(wins)) if wins else float("nan")
    return (f"At matched over-prov cost, the predictive (trend) controller "
            f"averages **{avg:+.2f} SLA pts** vs the reactive controller across "
            f"the swept range (positive = forecasting pays off at equal cost).")


def build_md(df: pd.DataFrame, params: SimParams, seeds: list[int]) -> str:
    lines = [
        "# Autoscaler SLA-vs-cost frontier",
        "",
        "Over-provisioning cost (instance-seconds, lower=better) vs SLA% "
        "(higher=better) as the controller safety margin is swept. Each point is "
        f"the mean over all {len(PROFILES)} profiles × {len(seeds)} seeds.",
        "",
        f"_Params: cap={params.per_instance_capacity_rps:.0f} rps, "
        f"warm-up={params.warmup_steps}s, cooldown={params.cooldown_seconds:.0f}s, "
        f"peak={PEAK_MULT:.0f}×cap, seeds=n{len(seeds)}._",
        "",
    ]
    for key, label, _strat, _sizing in SWEPT:
        sub = df[df.series == key].sort_values("knob_value")
        lines += [f"### {label}", "",
                  "| headroom | SLA% | Over-prov cost | #ScaleActions |",
                  "|---|---|---|---|"]
        for _, r in sub.iterrows():
            lines.append(f"| {r.knob_value:.2f} | {r.sla_pct:.1f} | "
                         f"{r.overprov_cost:.0f} | {r.scale_actions:.1f} |")
        lines.append("")
    sq = df[df.series == "sqrt_trend"].sort_values("knob_value")
    lines += ["### Sqrt-staffing + trend (swept β)", "",
              "| β | SLA% | Over-prov cost | #ScaleActions |", "|---|---|---|---|"]
    for _, r in sq.iterrows():
        lines.append(f"| {r.knob_value:.1f} | {r.sla_pct:.1f} | "
                     f"{r.overprov_cost:.0f} | {r.scale_actions:.1f} |")
    lines += ["", "### Baseline anchors (fixed)", "",
              "| Strategy | SLA% | Over-prov cost | #ScaleActions |", "|---|---|---|---|"]
    for _, r in df[df.series == "anchor"].iterrows():
        lines.append(f"| {r.label} | {r.sla_pct:.1f} | {r.overprov_cost:.0f} | "
                     f"{r.scale_actions:.1f} |")
    lines += ["", "### Read", "", "- " + _pareto_note(df), ""]
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Autoscaler SLA-vs-cost frontier")
    ap.add_argument("--seeds", type=int, default=8)
    ap.add_argument("--seed0", type=int, default=1000)
    ap.add_argument("--warmup", type=int, default=20)
    ap.add_argument("--cooldown", type=float, default=60.0)
    ap.add_argument("--tag", type=str, default=None)
    args = ap.parse_args(argv)

    seeds = list(range(args.seed0, args.seed0 + args.seeds))
    params = SimParams(warmup_steps=args.warmup, cooldown_seconds=args.cooldown)
    tag = args.tag or datetime.now(timezone.utc).strftime("frontier_%Y%m%d_%H%M%S")
    out = _HERE / "results" / tag
    out.mkdir(parents=True, exist_ok=True)

    df = run_frontier(params, seeds)
    df.to_csv(out / "frontier.csv", index=False)
    (out / "FRONTIER.md").write_text(build_md(df, params, seeds), encoding="utf-8")
    print(f"[frontier] wrote {out}/frontier.csv + FRONTIER.md ({len(df)} points)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
