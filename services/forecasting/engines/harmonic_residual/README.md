# harmonic_residual engine

Robust dynamic-harmonic-regression forecaster with an AR(1) residual correction
and split-conformal confidence bands. A genuinely forward-projecting single-step
forecaster that beats the naive persistence floor on every autoscaling load shape
— steady, diurnal, spiky, and the trend (ramp) case the differencing-free ARIMA
artifact cannot handle. Activated with `FORECAST_ENGINE=harmonic_residual`.

## Status

**Candidate — recommended for promotion behind a config flag.** Clears every
acceptance gate on synthetic *and* real data and converts into a downstream
autoscaler SLA win. No trained artifact and no new dependencies (pure NumPy),
fully deterministic.

| Layer | Where |
|---|---|
| Inference engine | `services/forecasting/engines/harmonic_residual/engine.py` |
| Unit tests | `services/forecasting/engines/harmonic_residual/test_engine.py` |
| Fitness benchmark | `experiments/forecasting-engine-bench/` (synthetic) |
| Real-data benchmark | `experiments/forecasting-engine-bench/real_data.py` |
| Downstream (autoscaler) benchmark | `experiments/forecasting-downstream-bench/` |
| Full write-up (tried, failures, numbers, calibration, downstream) | `experiments/forecasting-engine-bench/REPORT.md` |

## Model

At each call, on the most-recent samples of the supplied history:

1. **Structural fit** — least squares of `level + linear trend + Σ daily sin/cos
   harmonics`. The daily period is **inferred from the timestamp cadence**
   (288 at 5-min buckets, 1440 at 1-min) — so the same engine works at any
   cadence with no hard-coded period, fixing the 5-min/1-min bucketing mismatch.
2. **Robust IRLS** — bisquare-style reweighting downweights flash-crowd spikes so
   they don't drag the structural baseline off the calm level (the key to beating
   persistence on the spiky profile).
3. **AR(1) residual correction** — one-step forecast `structural(t+1) + φ·e_last`,
   `φ` clamped to `[0, 0.95]`; captures decaying-burst persistence.
4. **Split-conformal band** — empirical quantiles of the model's own in-sample
   one-step errors → a 95% band calibrated to the realized error distribution.

`forecast_ahead(history, steps)` projects `steps` buckets ahead (used by the
downstream autoscaler experiment to look the provisioning warm-up delay ahead),
with the trend **damped by its statistical significance** so a noise slope on
flat demand is not projected over the lead (no spurious scale churn) while a real
trend projects fully. The first step keeps full weight, so the single-step
`forecast()` and the fitness-function results are unchanged.

### Scaler-facing mode

The accuracy-optimal default (long window, symmetric robustness, calibrated
central forecast) is right for the forecasting service's own SLOs. An autoscaler
has a different, asymmetric loss (under-provisioning during a spike is the
expensive error) and a high serving cadence, so for that path configure:

```python
HarmonicResidualEngine(fit_window=120, robust_mode="downward")  # per-second demand
    .forecast_ahead(history_with_timestamps, steps=warmup_lead)
```

- `fit_window` short → a **local** trend (the seasonal-widening only fires when a
  daily cycle is actually identifiable, `n ≥ 2·period`, so it never overrides a
  short window at high cadence).
- `robust_mode="downward"` → dips are still robustified but **upward flash crowds
  keep full weight**, so the baseline tracks a spike instead of ignoring it.

Under the autoscaler's target-based controller this matches the hand-tuned Holt
baseline (99.2 % SLA) and approaches the oracle ceiling (99.9 %). Both
`robust_mode` and the short window are **opt-in** — the default is unchanged, so
the fitness function and accuracy SLOs are untouched. See REPORT.md §6.2.

## Headline numbers

- **Synthetic fitness fn (overall MAPE):** harmonic_residual **5.4%** vs naive
  7.5%, ARIMA 8.9%, moving_average 10.5%. Beats naive on MAPE **and** sMAPE on
  every profile incl. ramp; CI-coverage 0.954–0.957 (target [0.93, 0.97]);
  latency 0.7 ms.
- **Real data:** beats naive on Azure Functions 2019 (2.9% vs 3.0%) and
  WorldCup98 (14.6% vs 16.5%); calibrated band where moving_average is badly
  under-covered.
- **Downstream:** predictive scaling driven by this engine beats reactive by
  **+6.3 SLA pp** (closing 34% of the reactive→oracle gap), where the
  moving-average "predictive" path is byte-identical to reactive.

## Activation

```bash
# .env
FORECAST_ENGINE=harmonic_residual
```

Selectable via `engine_base.select_engine("harmonic_residual")`. No artifact to
ship or version.
