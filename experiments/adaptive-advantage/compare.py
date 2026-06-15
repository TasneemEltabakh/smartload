#!/usr/bin/env python3
"""Compare baseline vs smartload for an adaptive-advantage batch.

Reads each side's locust CSVs and prints overall + per-phase error rate and
tail latency, so the SmartLoad advantage (reroute around a 503-shedding backend,
scale out under the spike, reroute around a slow backend) is visible per phase.

Across all run-NN dirs under a batch, each per-phase and overall error rate is
reported as mean +/- 95% confidence interval, with the sample stdev, so a single
"good" run can no longer be mistaken for a result. A significance flag marks each
phase where SmartLoad's error-rate CI is disjoint from baseline's at this N.

The CI uses the small-sample t approximation: half-width = t* * s / sqrt(n), with
t* read from a two-sided 95% Student-t table (df = n-1); for n >= 31 the normal
z = 1.96 is used (t and z agree to ~4% by then). With a single run the stdev and
CI are undefined and render as "n/a" rather than crashing.

Standard library only (csv, statistics, math) so it runs in the same minimal env
as the services; do NOT add numpy/scipy.
"""
import csv, glob, math, os, statistics, sys

# Two-sided 95% Student-t critical values, indexed by degrees of freedom (n-1).
# For df >= 31 we fall back to the normal approximation z = 1.96.
_T95 = {
    1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571,
    6: 2.447, 7: 2.365, 8: 2.306, 9: 2.262, 10: 2.228,
    11: 2.201, 12: 2.179, 13: 2.160, 14: 2.145, 15: 2.131,
    16: 2.120, 17: 2.110, 18: 2.101, 19: 2.093, 20: 2.086,
    21: 2.080, 22: 2.074, 23: 2.069, 24: 2.064, 25: 2.060,
    26: 2.056, 27: 2.052, 28: 2.048, 29: 2.045, 30: 2.042,
}


def _t95(df):
    """Two-sided 95% t critical value for the given degrees of freedom."""
    return _T95.get(df, 1.96)


def _agg(side_dir):
    """Parse one side's locust *_stats.csv into (overall, {phase: rec}).

    The per-phase request name follows the contract "GET-/-<phase>"; the
    "Aggregated" row is the overall. Returns (None, {}) when no stats CSV.
    """
    f = [x for x in glob.glob(os.path.join(side_dir, "*_stats.csv")) if "history" not in x]
    if not f:
        return None, {}
    overall, per = None, {}
    with open(f[0]) as fh:
        for r in csv.DictReader(fh):
            nm = r.get("Name", "").strip()
            if nm.startswith("phase=") or nm == "":
                continue
            req = int(float(r.get("Request Count", 0) or 0))
            fail = int(float(r.get("Failure Count", 0) or 0))
            rec = dict(req=req, fail=fail,
                       err=100 * fail / req if req else 0.0,
                       p50=r.get("50%", "?"), p95=r.get("95%", "?"),
                       p99=r.get("99%", "?"), mx=r.get("Max Response Time", "?"))
            if nm == "Aggregated":
                overall = rec
            else:
                # name is "GET-/-<phase>"
                ph = nm.split("-")[-1]
                per[ph] = rec
    return overall, per


def main(batch):
    runs = sorted(glob.glob(os.path.join(batch, "run-*")))
    n = len(runs)
    print(f"\n==== adaptive-advantage: baseline vs smartload  ({n} run(s)) ====")
    if n < 2:
        print("note: single-run input -> stdev / 95% CI are undefined (shown as n/a)")
    # overall, per run
    print(f"\n{'run / side':22} {'reqs':>7} {'err%':>7} {'p50':>5} {'p95':>6} {'p99':>7} {'max':>8}")
    agg_over = {"baseline": [], "smartload": []}
    per_phase = {"baseline": {}, "smartload": {}}
    for run in runs:
        for side in ("baseline", "smartload"):
            o, per = _agg(os.path.join(run, side))
            if not o:
                continue
            agg_over[side].append(o)
            print(f"{os.path.basename(run)+'/'+side:22} {o['req']:>7} {o['err']:>6.2f}% "
                  f"{_n(o['p50']):>5} {_n(o['p95']):>6} {_n(o['p99']):>7} {_n(o['mx']):>8}")
            for ph, rec in per.items():
                per_phase[side].setdefault(ph, []).append(rec)

    # per-phase error% with rigor: mean +/- 95% CI, stdev, and a significance flag
    order = ["A_ramp", "A_hold", "B_degrade", "C_spike", "D_slow", "E_tail"]
    phs = [p for p in order if p in per_phase["baseline"] or p in per_phase["smartload"]]
    # any phase not in the canonical order still gets reported, appended after it
    phs += [p for p in sorted(set(per_phase["baseline"]) | set(per_phase["smartload"]))
            if p not in phs]

    print(f"\n{'phase':12} | {'BASE err% (mean +/-95%CI, sd)':32} | "
          f"{'SL err% (mean +/-95%CI, sd)':32} | {'signif':8}")
    print("-" * 96)
    for ph in phs:
        b = _err_stats(per_phase["baseline"].get(ph, []))
        s = _err_stats(per_phase["smartload"].get(ph, []))
        print(f"{ph:12} | {_cell(b):32} | {_cell(s):32} | {_sig(b, s):8}")

    # per-phase latency: mean tail across runs (values are p95/p99/max in ms)
    print(f"\n{'phase':12} | {'BASE p95':>9} {'SL p95':>7} | {'BASE p99':>9} {'SL p99':>7} | "
          f"{'BASE max':>9} {'SL max':>8}")
    print("-" * 72)
    for ph in phs:
        b = _lat_mean(per_phase["baseline"].get(ph, []))
        s = _lat_mean(per_phase["smartload"].get(ph, []))
        print(f"{ph:12} | {_ln(b['p95']):>9} {_ln(s['p95']):>7} | {_ln(b['p99']):>9} {_ln(s['p99']):>7} | "
              f"{_ln(b['mx']):>9} {_ln(s['mx']):>8}")

    # headline: overall error% with the same rigor + latency means
    bo_err = _err_stats(agg_over["baseline"]); so_err = _err_stats(agg_over["smartload"])
    bo_lat = _lat_mean(agg_over["baseline"]); so_lat = _lat_mean(agg_over["smartload"])
    print(f"\n{'OVERALL':12} | {_cell(bo_err):32} | {_cell(so_err):32} | {_sig(bo_err, so_err):8}")
    print(f"         baseline:  p95={_ln(bo_lat['p95'])}  p99={_ln(bo_lat['p99'])}  max={_ln(bo_lat['mx'])}")
    print(f"         smartload: p95={_ln(so_lat['p95'])}  p99={_ln(so_lat['p99'])}  max={_ln(so_lat['mx'])}")
    if bo_err[0] is not None and so_err[0] is not None:
        verdict = {"** sig": "significant", "~ ns": "not significant at this N",
                   "n/a": "single run, no CI"}[_sig(bo_err, so_err)]
        print(f"         -> SmartLoad error rate {so_err[0]:.2f}% vs baseline "
              f"{bo_err[0]:.2f}%  ({verdict})")


def _n(v):
    try:
        return f"{float(v):.0f}"
    except Exception:
        return str(v)


def _ln(v):
    """Format a latency mean: number rounded, or 'n/a' when absent."""
    return "n/a" if v is None else f"{v:.0f}"


def _floats(recs, key):
    """Coerce rec[key] to floats across runs, skipping unparseable ('?') cells."""
    out = []
    for r in recs:
        try:
            out.append(float(r[key]))
        except Exception:
            pass
    return out


def _stats(vals):
    """Return (mean, stdev, ci_half) for a sample of floats.

    stdev and ci_half are None when n < 2 (dispersion is undefined for one
    point). ci_half is the 95% CI half-width: t* * s / sqrt(n).
    """
    n = len(vals)
    if n == 0:
        return None, None, None
    m = statistics.mean(vals)
    if n < 2:
        return m, None, None
    s = statistics.stdev(vals)
    half = _t95(n - 1) * s / math.sqrt(n)
    return m, s, half


def _err_stats(recs):
    """Stats over the per-run error% for a phase/overall: (mean, stdev, ci_half)."""
    return _stats(_floats(recs, "err"))


def _lat_mean(recs):
    """Mean p95/p99/max across runs; None per metric when no parseable values."""
    def m(key):
        vals = _floats(recs, key)
        return sum(vals) / len(vals) if vals else None
    return dict(p95=m("p95"), p99=m("p99"), mx=m("mx"))


def _cell(st):
    """Render an error-stat triple as 'mean +/-ci  sd' (or n/a parts)."""
    m, sd, ci = st
    if m is None:
        return "n/a"
    if ci is None:
        return f"{m:6.2f}% +/-n/a   sd n/a"
    return f"{m:6.2f}% +/-{ci:5.2f} sd {sd:5.2f}"


def _sig(b, s):
    """Significance flag: SmartLoad CI disjoint from baseline CI -> sig.

    Returns 'n/a' when either side lacks a CI (single run or no data).
    """
    bm, _, bci = b
    sm, _, sci = s
    if bm is None or sm is None or bci is None or sci is None:
        return "n/a"
    blo, bhi = bm - bci, bm + bci
    slo, shi = sm - sci, sm + sci
    disjoint = bhi < slo or shi < blo
    # ASCII-only flags: a Windows cp1252 console cannot encode emoji and would crash.
    return "** sig" if disjoint else "~ ns"


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else ".")
