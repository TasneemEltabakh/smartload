"""
experiments/anomaly-engine-bench/run.py
────────────────────────────────────────
Comparison benchmark: threshold vs isolation_forest anomaly engines.

Sweeps a synthetic grid of `BackendFeatures` across latency and
error-rate ranges, scores each point with both engines, and emits:

  - results/<UTC-timestamp>/grid.csv
        One row per (latency_ms, error_rate, std_ms) cell with both
        engines' status + score, plus an agreement column.

  - results/<UTC-timestamp>/SUMMARY.md
        Human-readable summary: agreement rate, disagreement matrix,
        per-engine verdict counts, decision boundary observations.

  - results/<UTC-timestamp>/heatmap.png (optional, requires matplotlib)
        Two side-by-side heatmaps over the latency × error_rate plane,
        colour = verdict (green / yellow / red). Skipped silently if
        matplotlib isn't installed.

This isn't a CI gate — it's an investigation tool. The point is to
surface where the trained model's decision surface agrees or disagrees
with the simple threshold rule, so the domain-adaptation caveat in
engines/isolation_forest/README.md has empirical numbers to back it.

Usage:
    python experiments/anomaly-engine-bench/run.py
    python experiments/anomaly-engine-bench/run.py --latency-max 800 --steps 32

The script imports both engines directly — no docker stack needed.
The isolation_forest engine loads the real shipped .pkl, so the
results reflect the model that production would actually use.
"""

from __future__ import annotations

import argparse
import csv
import sys
from datetime import datetime, timezone
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
_SVC = _REPO / "services" / "anomaly-detector"

# The engine modules use sys.path tricks to find engine_base; mirror them.
for p in (str(_SVC), str(_SVC / "engines" / "isolation_forest"), str(_SVC / "engines" / "threshold")):
    if p not in sys.path:
        sys.path.insert(0, p)


def _build_features_cls():
    from engine_base import BackendFeatures  # noqa: PLC0415
    return BackendFeatures


def _load_engines():
    """Instantiate both engines. Returns (threshold, isolation_forest).

    Either may raise if its dependencies aren't installed. We propagate
    the exception with a clearer message so the user knows what's
    missing."""
    try:
        from engines.threshold.engine import ThresholdEngine  # noqa: PLC0415
        threshold = ThresholdEngine(
            latency_multiplier=3.0,
            error_rate_threshold=0.05,
            min_sample_count=10,
        )
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"could not load threshold engine: {exc}") from exc

    try:
        from engines.isolation_forest.engine import IsolationForestEngine  # noqa: PLC0415
        iso = IsolationForestEngine()
    except FileNotFoundError as exc:
        raise RuntimeError(
            "could not find services/anomaly-detector/models/isolation_forest.pkl — "
            "ensure the trained artifact is checked in or pass --model-path"
        ) from exc
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"could not load isolation_forest engine: {exc}") from exc

    return threshold, iso


def _sweep(latency_max: int, error_max: float, steps: int):
    """Yield (latency_ms, error_rate, std_ms) tuples spanning the grid.

    Choices behind the defaults:
      - latency_ms in [10, latency_max] — 10 ms is around an idle test-
        backend baseline; latency_max=600 is well into "obviously slow"
        territory (~3× the 200 ms SLO).
      - error_rate in [0, error_max] — the SLO threshold is 0.05; we
        sweep up to 0.20 so we cover the "clearly broken" half too.
      - std_ms scales with latency_ms (high latency tends to bring
        high variance); this matches what the metrics window looks
        like in practice better than a fixed std value would."""
    for i in range(steps):
        latency = 10 + (latency_max - 10) * i / max(1, steps - 1)
        std = latency * 0.25  # observed-empirical scaling
        for j in range(steps):
            error_rate = error_max * j / max(1, steps - 1)
            yield latency, error_rate, std


def _score_one(engine, features):
    """Wrap a single score() call so a per-engine exception doesn't
    abort the whole sweep — we record it as status=ERROR and move on."""
    try:
        s = engine.score(features)
        return s.status, round(s.score, 4), None
    except Exception as exc:  # noqa: BLE001
        return "ERROR", 0.0, str(exc)


def main(latency_max: int, error_max: float, steps: int) -> None:
    BackendFeatures = _build_features_cls()
    threshold, iso = _load_engines()

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_dir = Path(__file__).parent / "results" / ts
    out_dir.mkdir(parents=True, exist_ok=True)

    grid_path = out_dir / "grid.csv"
    rows: list[dict] = []

    for latency, error_rate, std in _sweep(latency_max, error_max, steps):
        features = BackendFeatures(
            backend_id="bench-cell",
            latency_ms=latency,
            latency_rolling_mean_ms=latency,
            error_rate=error_rate,
            sample_count=100,  # always above the gate
            latency_rolling_std_ms=std,
        )
        thr_status, thr_score, thr_err = _score_one(threshold, features)
        iso_status, iso_score, iso_err = _score_one(iso, features)
        rows.append({
            "latency_ms":          round(latency, 1),
            "error_rate":          round(error_rate, 4),
            "std_ms":              round(std, 1),
            "threshold_status":    thr_status,
            "threshold_score":     thr_score,
            "isolation_status":    iso_status,
            "isolation_score":     iso_score,
            "agree":               thr_status == iso_status,
            "threshold_err":       thr_err or "",
            "isolation_err":       iso_err or "",
        })

    fieldnames = list(rows[0].keys())
    with grid_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    _write_summary(out_dir, rows, latency_max, error_max, steps)
    _try_heatmap(out_dir, rows, latency_max, error_max, steps)

    print(f"[anomaly-engine-bench] wrote {len(rows)} cells -> {out_dir}")


def _write_summary(out_dir: Path, rows: list[dict], latency_max: int, error_max: float, steps: int) -> None:
    """Build SUMMARY.md from the grid rows.

    Centralised so the README can describe what's in there without
    chasing the layout across multiple write sites."""
    n = len(rows)
    agreement_rate = sum(1 for r in rows if r["agree"]) / n if n else 0.0

    thr_counts: dict[str, int] = {}
    iso_counts: dict[str, int] = {}
    disagree_pairs: dict[tuple[str, str], int] = {}

    for r in rows:
        thr_counts[r["threshold_status"]] = thr_counts.get(r["threshold_status"], 0) + 1
        iso_counts[r["isolation_status"]] = iso_counts.get(r["isolation_status"], 0) + 1
        if not r["agree"]:
            key = (r["threshold_status"], r["isolation_status"])
            disagree_pairs[key] = disagree_pairs.get(key, 0) + 1

    lines = [
        "# Anomaly Engine Comparison — Results",
        "",
        f"Generated at `{out_dir.name}` — sweep covers latency in [10, {latency_max}] ms × "
        f"error_rate in [0, {error_max}] across {steps} × {steps} = {n} cells.",
        "",
        "## Headline",
        "",
        f"- **Agreement rate**: {agreement_rate:.1%} ({sum(1 for r in rows if r['agree'])} / {n} cells)",
        "",
        "## Per-engine verdict distribution",
        "",
        "| Status | threshold | isolation_forest |",
        "|---|---:|---:|",
    ]
    for status in ("healthy", "degraded", "unhealthy", "ERROR"):
        thr = thr_counts.get(status, 0)
        iso = iso_counts.get(status, 0)
        if thr or iso:
            lines.append(f"| `{status}` | {thr} | {iso} |")
    lines += ["", "## Disagreement pairs (threshold → isolation_forest)", ""]
    if disagree_pairs:
        lines += ["| threshold says | isolation_forest says | cells |", "|---|---|---:|"]
        for (thr_s, iso_s), count in sorted(disagree_pairs.items(), key=lambda kv: -kv[1]):
            lines.append(f"| `{thr_s}` | `{iso_s}` | {count} |")
    else:
        lines.append("_(none — perfect agreement, which is suspicious; review the grid CSV)_")
    lines += [
        "",
        "## How to interpret",
        "",
        "- Where both engines say `unhealthy`, the model is doing what the threshold rule would.",
        "- Where the threshold says `unhealthy` and the model says `healthy`, the model is **under-reacting** to the simple rule — typically a domain-adaptation issue: the production_scaler maps real-millisecond latency into a space where the model's training-distribution outliers don't land.",
        "- Where the threshold says `healthy` but the model says `unhealthy`, the model is finding signal the simple rule misses (latency-std interactions, error-rate compounding) — the value-add case for the model.",
        "- Pure-disagreement rows worth eyeballing in `grid.csv`: pick the boundary cells (just-above-threshold latency × near-zero error_rate, etc.) and check whether the model's verdict tracks what an operator would call.",
        "",
        "Re-run with different sweep ranges via `python run.py --latency-max <N> --error-max <X> --steps <S>` to probe specific regions.",
    ]
    (out_dir / "SUMMARY.md").write_text("\n".join(lines), encoding="utf-8")


def _try_heatmap(out_dir: Path, rows: list[dict], latency_max: int, error_max: float, steps: int) -> None:
    """Optional heatmap, skipped silently if matplotlib isn't around.

    We don't add matplotlib to the bench requirements because the CSV
    + SUMMARY.md already convey the result; the plot is a nice-to-have
    for human readers, not a load-bearing artifact."""
    try:
        import matplotlib  # noqa: PLC0415
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt  # noqa: PLC0415
        import numpy as np  # noqa: PLC0415
    except ImportError:
        return

    status_to_int = {"healthy": 0, "degraded": 1, "unhealthy": 2, "ERROR": -1}
    thr_grid = np.full((steps, steps), -1, dtype=int)
    iso_grid = np.full((steps, steps), -1, dtype=int)
    for k, r in enumerate(rows):
        i, j = divmod(k, steps)
        thr_grid[i, j] = status_to_int.get(r["threshold_status"], -1)
        iso_grid[i, j] = status_to_int.get(r["isolation_status"], -1)

    fig, axes = plt.subplots(1, 2, figsize=(11, 5))
    extent = [0, error_max, 10, latency_max]
    for ax, grid, title in (
        (axes[0], thr_grid, "threshold engine"),
        (axes[1], iso_grid, "isolation_forest engine"),
    ):
        im = ax.imshow(
            grid, origin="lower", aspect="auto", extent=extent,
            cmap="RdYlGn_r", vmin=0, vmax=2,
        )
        ax.set_xlabel("error_rate")
        ax.set_ylabel("latency_ms")
        ax.set_title(title)
        cbar = fig.colorbar(im, ax=ax, ticks=[0, 1, 2])
        cbar.ax.set_yticklabels(["healthy", "degraded", "unhealthy"])
    fig.suptitle("Verdict over (latency, error_rate) — anomaly engines compared")
    fig.tight_layout()
    fig.savefig(out_dir / "heatmap.png", dpi=120)
    plt.close(fig)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Compare threshold vs isolation_forest engines across a feature grid")
    parser.add_argument("--latency-max", type=int, default=600,
                        help="upper bound of the latency_ms sweep (default: 600)")
    parser.add_argument("--error-max", type=float, default=0.20,
                        help="upper bound of the error_rate sweep (default: 0.20)")
    parser.add_argument("--steps", type=int, default=16,
                        help="cells per axis — total cells = steps × steps (default: 16)")
    args = parser.parse_args()
    main(args.latency_max, args.error_max, args.steps)
