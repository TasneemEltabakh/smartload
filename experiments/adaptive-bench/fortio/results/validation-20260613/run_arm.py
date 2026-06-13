#!/usr/bin/env python3
"""Run one routing-policy arm of the closed-loop validation: snapshot the pool,
drive a fixed-QPS Fortio load through the LB, snapshot again, and record the
LB-observed latency + the per-backend request distribution.

Usage: run_arm.py <label> <qps> <duration> [connections]
Writes <label>.json artifact in this directory and prints a one-line summary.
Read-only w.r.t. the stack (only drives load + reads /_admin/stats)."""
import json, subprocess, sys, pathlib

HERE = pathlib.Path(__file__).parent
PROBE_DIR = HERE.parent.parent          # experiments/adaptive-bench/fortio
sys.path.insert(0, str(PROBE_DIR))
import fortio_probe as fp  # noqa: E402

N = 5


def stats(i):
    out = subprocess.run(
        ["docker", "exec", f"smartload-test-backend-{i}", "sh", "-c",
         "wget -q -O - http://localhost:8080/_admin/stats"],
        capture_output=True, text=True,
    ).stdout.strip()
    d = json.loads(out)
    return {"accepted": d["accepted"], "shed": d["shed"]}


def snap():
    return {i: stats(i) for i in range(1, N + 1)}


def main():
    label, qps, dur = sys.argv[1], int(sys.argv[2]), sys.argv[3]
    conns = int(sys.argv[4]) if len(sys.argv) > 4 else 200

    before = snap()
    cmd = [sys.executable, str(PROBE_DIR / "fortio_probe.py"),
           "--qps", str(qps), "--duration", dur, "--connections", str(conns),
           "--out", str(HERE / f"raw-{label}")]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    after = snap()

    raw_path = HERE / f"raw-{label}" / f"fortio_qps{qps}.json"
    summ = None
    if raw_path.exists():
        report = json.loads(raw_path.read_text())
        summ = fp.summarise(qps, report)

    dist = {}
    total = 0
    for i in range(1, N + 1):
        da = after[i]["accepted"] - before[i]["accepted"]
        ds = after[i]["shed"] - before[i]["shed"]
        dist[i] = {"accepted": da, "shed": ds}
        total += da + ds
    for i in dist:
        d = dist[i]
        d["share_pct"] = round(100 * (d["accepted"] + d["shed"]) / total, 1) if total else 0.0

    result = {"label": label, "qps_offered": qps, "duration": dur,
              "lb": summ, "distribution": dist, "pool_total_requests": total,
              "fortio_stderr_tail": (proc.stderr or "").strip().splitlines()[-2:]}
    (HERE / f"{label}.json").write_text(json.dumps(result, indent=2))

    # one-line summary
    if summ:
        print(f"{label:<14} offered={qps} achieved={summ['actual_qps']:.0f} "
              f"p50={summ['p50_ms']:.0f} p99={summ['p99_ms']:.0f} "
              f"p99.9={summ['p999_ms']:.0f} 2xx={summ['ok_pct']:.0f}% "
              f"503={summ['shed_pct']:.0f}% errs={summ['errors']}")
    else:
        print(f"{label:<14} offered={qps} <no fortio report> "
              f"stderr={result['fortio_stderr_tail']}")
    shares = " ".join(f"b{i}={dist[i]['share_pct']:.0f}%" for i in dist)
    print(f"{'':14} distribution: {shares}")
    return result


if __name__ == "__main__":
    main()
