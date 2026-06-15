#!/usr/bin/env python3
"""Compare baseline vs smartload for an adaptive-advantage batch.

Reads each side's locust CSVs and prints overall + per-phase error rate and
tail latency, so the SmartLoad advantage (reroute around a 503-shedding backend,
scale out under the spike, reroute around a slow backend) is visible per phase.
"""
import csv, glob, os, sys


def _agg(side_dir):
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
    print(f"\n==== adaptive-advantage: baseline vs smartload  ({len(runs)} run(s)) ====")
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

    # per-phase mean (where the advantage lives)
    order = ["A_ramp", "A_hold", "B_degrade", "C_spike", "D_slow", "E_tail"]
    print(f"\n{'phase':12} | {'BASE err%':>9} {'SL err%':>8} | {'BASE p95':>9} {'SL p95':>7} | "
          f"{'BASE p99':>9} {'SL p99':>7} | {'BASE max':>9} {'SL max':>8}")
    print("-" * 96)
    phs = [p for p in order if p in per_phase["baseline"] or p in per_phase["smartload"]]
    for ph in phs:
        b = _mean(per_phase["baseline"].get(ph, []))
        s = _mean(per_phase["smartload"].get(ph, []))
        print(f"{ph:12} | {b['err']:>8.2f}% {s['err']:>7.2f}% | {b['p95']:>9.0f} {s['p95']:>7.0f} | "
              f"{b['p99']:>9.0f} {s['p99']:>7.0f} | {b['mx']:>9.0f} {s['mx']:>8.0f}")

    # headline
    bo = _mean(agg_over["baseline"]); so = _mean(agg_over["smartload"])
    print(f"\nOVERALL  baseline: err={bo['err']:.2f}%  p95={bo['p95']:.0f}  p99={bo['p99']:.0f}  max={bo['mx']:.0f}")
    print(f"         smartload: err={so['err']:.2f}%  p95={so['p95']:.0f}  p99={so['p99']:.0f}  max={so['mx']:.0f}")
    if bo['err'] and so['err'] is not None:
        print(f"         -> SmartLoad error rate {so['err']:.2f}% vs baseline {bo['err']:.2f}%")


def _n(v):
    try:
        return f"{float(v):.0f}"
    except Exception:
        return str(v)


def _mean(recs):
    if not recs:
        return dict(err=0.0, p95=0.0, p99=0.0, mx=0.0)
    def f(k):
        vals = []
        for r in recs:
            try:
                vals.append(float(r[k]))
            except Exception:
                pass
        return sum(vals) / len(vals) if vals else 0.0
    return dict(err=f("err"), p95=f("p95"), p99=f("p99"), mx=f("mx"))


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else ".")
