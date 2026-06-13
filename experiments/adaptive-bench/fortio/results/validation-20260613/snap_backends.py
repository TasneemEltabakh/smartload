#!/usr/bin/env python3
"""Snapshot per-backend pool counters via docker exec. Read-only diagnostic for
the closed-loop validation runs. Usage: snap_backends.py [label] -> prints +
appends one JSON line per call to snaps.jsonl in this directory."""
import json, subprocess, sys, pathlib

HERE = pathlib.Path(__file__).parent
N = 5


def stats(i):
    out = subprocess.run(
        ["docker", "exec", f"smartload-test-backend-{i}", "sh", "-c",
         "wget -q -O - http://localhost:8080/_admin/stats"],
        capture_output=True, text=True,
    ).stdout.strip()
    return json.loads(out)


def main():
    label = sys.argv[1] if len(sys.argv) > 1 else ""
    rows = {i: stats(i) for i in range(1, N + 1)}
    snap = {"label": label, "backends": {
        i: {"accepted": rows[i]["accepted"], "shed": rows[i]["shed"]} for i in rows}}
    print(json.dumps(snap))
    return snap


if __name__ == "__main__":
    main()
