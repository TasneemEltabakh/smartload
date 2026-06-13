"""
experiments/_bench_common/
───────────────────────────
Small shared library for the experiment harnesses under ``experiments/``.

Both the baseline-vs-smartload (#148) and adaptive-bench (#156/#157) harnesses
batch N independent runs and report per-metric ``mean ± confidence interval``
(SOT §35.3 / #160). The confidence-interval maths and the tidy multi-run
aggregator live here so the two harnesses agree to the digit.

Consumers add ``experiments/`` to ``sys.path`` and ``from _bench_common import
bench_stats``::

    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # → experiments/
    from _bench_common import bench_stats
"""
