# Module internals references

Per-module engineering deep-dives for the decision-plane engines reworked in the
v1.0.7bl–bo wave (#169–172). Each document explains one module's internal logic
with data-flow and decision diagrams, the exact algorithm and formulas, a
parameter table, and the benchmark results quoted directly from the harnesses
under `experiments/`. They sit alongside the canonical `SOURCE_OF_TRUTH.html`
(architecture and contracts) and `PROJECT_WALKTHROUGH.md` (narrative code tour),
and carry the level of internal detail those two do not.

| Module | Document | Covers |
|---|---|---|
| RL / routing-policy plane | [`rl-engine.md`](rl-engine.md) | The policy contract and mode composition, the latency-monotone `candidate_mono` router (scoring, capacity estimate, monotonicity proof), how it is trained, and the non-monotone `candidate_maxxer` benchmark foil |
| Forecasting | [`forecasting.md`](forecasting.md) | The `harmonic_residual` engine (structural harmonic fit, robust IRLS, AR(1) residual, conformal band), the scaler-facing mode, and synthetic / real-data / downstream results |
| Anomaly detection | [`anomaly-detector.md`](anomaly-detector.md) | The `TrendExtractor` temporal features and the `trend_rule` / `trend_forest` engines that close the gradual-degradation gap, with per-profile F1 / recall / FP results |
| Autoscaling | [`autoscaler.md`](autoscaler.md) | The target-based controller (two sizing laws, asymmetric cooldowns, deadband) versus the live ±1 rule, and the strategy-bench results. Documents the wired, deployed-default status and the unit-only live-glue caveat honestly |

Each document quotes benchmark numbers verbatim from the report and summary files
and states the experimental setup (seeds, profiles, held-out sets) so a result can
be traced back to its source.
