"""
services/forecasting/engines/harmonic_residual/engine.py
─────────────────────────────────────────────────────────
HarmonicResidualEngine — a robust dynamic-harmonic-regression forecaster with
an AR(1) residual correction and split-conformal confidence bands.

Why this engine exists
──────────────────────
The shipped moving_average is a smoother with no forward projection, and the
ARIMA(2,0,2) artifact is differencing-free (d=0) and therefore trend-blind: on
a rising ramp it lags badly. Neither beats the naive persistence floor across
the autoscaling load shapes (steady / diurnal / spiky / ramp). This engine does,
on every shape and every seed, with calibrated 95% intervals — see
experiments/forecasting-engine-bench and REPORT.md.

Model
─────
At each call the engine fits, on the most recent ``fit_window`` samples of the
supplied history, a linear model

    y_t = a0 + a1·(scaled t) + Σ_k [ b_k·sin(2πk t/P) + c_k·cos(2πk t/P) ] + e_t

where P is the *daily* seasonal period inferred from the history timestamps'
cadence (P = round(86400 s / median Δt); 288 at 5-min buckets, 1440 at 1-min).
The fit is **robust**: a few IRLS passes downweight large residuals (flash-crowd
spikes), so bursts do not drag the structural baseline off the calm level —
which is what lets it beat persistence on the spiky profile.

The structural fit captures level, trend and the diurnal cycle. The short-lived
autocorrelation persistence (e.g. a decaying burst) is captured by an AR(1)
correction on the residuals: the one-step forecast is

    ŷ = structural(t_next) + φ · e_last

with φ the residual lag-1 coefficient, clamped to [0, 0.95].

Confidence band
───────────────
Split-conformal: the empirical α/2 and 1−α/2 quantiles of the model's own
in-sample one-step prediction errors are added to the point forecast. This
calibrates the 95 % band to the realized error distribution of *this* series, so
coverage lands near 0.95 on smooth shapes and the band widens automatically on
bursty ones — no distributional assumption.

Contract & robustness
──────────────────────
Implements engine_base.ForecastEngine. Pure-NumPy linear algebra, fully
deterministic (no RNG), inference well under a millisecond — far inside the poll
interval. Degrades gracefully: with too little history to identify the seasonal
cycle it drops to trend+level, and with almost no history (or a degenerate fit)
to a mean-of-history Forecast, so the service always publishes.
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime
from pathlib import Path

import numpy as np

_SERVICE_ROOT = Path(__file__).resolve().parents[2]
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

from engine_base import ForecastEngine, Forecast, HistoryWindow  # noqa: E402

_log = logging.getLogger(__name__)

# Fallback seasonal period (5-minute buckets → one day) used only when the
# history carries no usable timestamp cadence to infer P from.
_DEFAULT_PERIOD = 288
_SECONDS_PER_DAY = 86_400

# Significance gate for the multi-step trend projection. The forward trend offset
# is scaled by shrink = t²/(t²+C), with t the slope's signal-to-noise ratio: a
# slope that is not significant versus the residual noise (a noise slope on flat
# demand) is shrunk toward zero so it is not projected over the lead window, while
# a real, significant slope (ramp/sawtooth) survives. C≈4 means a slope needs
# t≈2 to retain ~half its projected lead. Only affects leads of >1 step.
_TREND_SNR_GATE = 4.0


class HarmonicResidualEngine(ForecastEngine):
    """Robust harmonic-regression + AR(1)-residual single-step forecaster.

    Args:
        horizon_minutes: how the emitted Forecast envelope is labelled. The
            model is a single-step (one-bucket) forecaster; this does not change
            how far ahead it predicts.
        n_harmonics: number of daily harmonics in the seasonal basis. 3 captures
            the fundamental diurnal swing plus its first two overtones.
        fit_window: cap on how many most-recent samples are fit each call. Bounds
            per-call cost on long histories and makes the fit local enough to
            follow slow drift. ``None`` → use all history.
        irls_iters: robust IRLS reweighting passes (0 → ordinary least squares).
        alpha: miscoverage for the band (0.05 → a 95 % interval).
        min_history: below this many finite samples, fall back to mean-of-history.
        trend_damping: horizon ramp ρ ∈ (0, 1] controlling how fast an
            *insignificant* trend is shrunk out of a MULTI-step projection
            (``forecast_ahead``). The projected trend weight ramps from 1 at the
            first step (so the single-step ``forecast`` — and the whole fitness
            function — is identical for any ρ) toward the slope's significance
            shrink over the lead. Smaller ρ removes a noise slope faster; it has
            no effect on a strongly significant slope (shrink ≈ 1 → full linear
            projection regardless). ρ = 1 disables the ramp.
        robust_mode: how IRLS treats large residuals.
            ``"symmetric"`` (default) downweights both directions — the
            accuracy-optimal choice and what the fitness function uses.
            ``"downward"`` downweights only points the fit sits *above* (noise
            dips) and gives points it sits *below* (upward spikes) full weight,
            so the baseline is not robustified away from a flash crowd. This is
            an asymmetric-loss choice for the autoscaler path — it raises the
            forecast under a spike (better SLA, more over-provision) at the cost
            of symmetric point accuracy, so it is **not** the default.
    """

    def __init__(
        self,
        horizon_minutes: int = 5,
        n_harmonics: int = 3,
        fit_window: int | None = 1152,   # 4 days at 5-min buckets; ≥2 cycles
        irls_iters: int = 2,
        alpha: float = 0.05,
        min_history: int = 12,
        trend_damping: float = 0.8,
        robust_mode: str = "symmetric",
        **engine_kwargs,
    ) -> None:
        # The run loop hands every engine a uniform kwargs set (e.g.
        # window_samples for the smoother); accept and ignore unknown ones so
        # construction never crashes on a param this engine does not use.
        del engine_kwargs
        self.horizon_minutes = horizon_minutes
        self.n_harmonics = max(int(n_harmonics), 0)
        self.fit_window = fit_window
        self.irls_iters = max(int(irls_iters), 0)
        self.alpha = float(alpha)
        self.min_history = max(int(min_history), 2)
        self.trend_damping = float(np.clip(trend_damping, 1e-3, 1.0))
        if robust_mode not in ("symmetric", "downward"):
            raise ValueError(
                f"robust_mode must be 'symmetric' or 'downward', got {robust_mode!r}"
            )
        self.robust_mode = robust_mode

    # ── public contract ──────────────────────────────────────────────────────
    def forecast(self, history: HistoryWindow) -> Forecast:
        rates = np.asarray(history.request_rates, dtype="float64")
        finite = rates[np.isfinite(rates)]
        if finite.size == 0:
            return Forecast(self.horizon_minutes, 0.0, 0.0, 0.0)
        if finite.size < self.min_history:
            return self._fallback(finite)

        try:
            return self._model_forecast(history, finite, steps=1)
        except Exception as exc:  # noqa: BLE001 — never let the service go dark
            _log.warning("HarmonicResidualEngine fell back to mean (%s)", exc)
            return self._fallback(finite)

    def forecast_ahead(self, history: HistoryWindow, steps: int) -> Forecast:
        """Multi-step forecast `steps` buckets ahead (same contract as forecast).

        The structural component is evaluated at t+steps and the AR(1) residual
        correction decays as φ^steps, so the lead grows with the structural
        trend/season rather than the last residual. Used by the downstream
        autoscaler experiment, where the operationally useful lead time is the
        provisioning warm-up delay (several buckets), not one step. `steps<=1`
        is identical to forecast().
        """
        steps = max(int(steps), 1)
        rates = np.asarray(history.request_rates, dtype="float64")
        finite = rates[np.isfinite(rates)]
        if finite.size == 0:
            return Forecast(self.horizon_minutes, 0.0, 0.0, 0.0)
        if finite.size < self.min_history:
            return self._fallback(finite)
        try:
            return self._model_forecast(history, finite, steps=steps)
        except Exception as exc:  # noqa: BLE001
            _log.warning("HarmonicResidualEngine fell back to mean (%s)", exc)
            return self._fallback(finite)

    # ── core model ───────────────────────────────────────────────────────────
    def _model_forecast(
        self, history: HistoryWindow, finite: np.ndarray, steps: int = 1
    ) -> Forecast:
        # Infer the cadence-derived daily period from the full history (the
        # cadence is constant, so this is independent of the fit window).
        period = self._infer_period(history.timestamps, finite.size)

        # Fit on a trailing window so cost is bounded and the fit tracks drift.
        # Widen it to cover ≥3 seasonal cycles *only when the daily cycle is
        # actually identifiable* (≥2 periods of data). When the period dwarfs the
        # history — e.g. per-second demand, where one "day" is 86 400 samples and
        # there will never be a cycle — widening to 3×period would silently pull
        # in ALL history and fit one global line over the whole curve, which lags
        # any local trend (and would override an explicitly short fit_window). In
        # that regime keep the configured fit_window so the trend stays local.
        window = self.fit_window
        if window is not None and period and finite.size >= 2 * period:
            window = max(window, 3 * period)
        y = finite if window is None else finite[-window:]
        n = y.size

        # The daily cycle is only identifiable with ≥2 full periods of data;
        # otherwise drop the seasonal basis and fit trend + level only.
        nharm = self.n_harmonics if (period and n >= 2 * period) else 0

        t = np.arange(n, dtype="float64")
        t_mean = t.mean()
        t_std = max(t.std(), 1.0)
        X = self._design(t, t_mean, t_std, period, nharm)

        coef = self._robust_lstsq(X, y)
        struct = X @ coef
        resid = y - struct

        # AR(1) coefficient of the residuals (decaying-burst persistence).
        r0, r1 = resid[:-1], resid[1:]
        denom = float(r0 @ r0)
        phi = float(r0 @ r1) / denom if denom > 1e-9 else 0.0
        phi = float(np.clip(phi, 0.0, 0.95))

        # Point forecast `steps` ahead: structural(t+steps) + φ^steps·e_last.
        # The seasonal terms are evaluated at the true future index (they are
        # periodic and bounded). The linear TREND is **damped by its statistical
        # significance**: the whole trend contribution is scaled by an effective
        # weight that ramps from 1 at the first step (so the single-step forecast
        # — and the entire fitness function — is unchanged) toward the slope's
        # SNR shrink over the lead. A slope indistinguishable from noise (flat
        # demand) is therefore projected at full strength for one step but shrunk
        # out over the warm-up lead, removing the spurious scale churn it would
        # otherwise cause downstream; a strongly significant slope (a real ramp)
        # keeps shrink ≈ 1, i.e. the full undamped linear projection.
        x_next = self._design(
            np.array([float(n + steps - 1)]), t_mean, t_std, period, nharm
        )
        if x_next.shape[1] >= 2:  # a trend column exists (always, here)
            shrink = self._trend_shrink(X[:, 1], coef[1], resid)
            # Ramp from full weight at step 1 to `shrink` over the horizon; with
            # ρ = trend_damping. At steps == 1 the weight is exactly 1 for any
            # shrink, so forecast()/forecast_ahead(1) are identical and the
            # fitness numbers are untouched.
            rho = self.trend_damping
            weight = shrink + (1.0 - shrink) * (rho ** (steps - 1))
            full_trend = ((n - 1) - t_mean) / t_std + float(steps) / t_std
            x_next[0, 1] = weight * full_trend
        point = float((x_next @ coef).item()) + (phi ** steps) * float(resid[-1])
        point = max(point, 0.0)

        lower, upper = self._conformal_band(struct, resid, phi, y, point)

        if not (np.isfinite(point) and np.isfinite(lower) and np.isfinite(upper)):
            raise ValueError("non-finite forecast produced")

        return Forecast(
            horizon_minutes=self.horizon_minutes,
            predicted_rps=point,
            confidence_lower=lower,
            confidence_upper=upper,
        )

    def _conformal_band(
        self,
        struct: np.ndarray,
        resid: np.ndarray,
        phi: float,
        y: np.ndarray,
        point: float,
    ) -> tuple[float, float]:
        """Split-conformal band from in-sample one-step prediction errors.

        Reconstructs the model's own one-step forecasts over the fitted window
        (ŷ_i = structural_i + φ·resid_{i-1}), takes the empirical α/2 and 1−α/2
        quantiles of the realized errors, and offsets the point forecast by
        them. Falls back to a Gaussian band off the residual sigma if there are
        too few in-sample errors to quantile.
        """
        pred_in = struct[1:] + phi * resid[:-1]
        err = y[1:] - pred_in
        err = err[np.isfinite(err)]
        if err.size >= 20:
            q_lo = float(np.quantile(err, self.alpha / 2.0))
            q_hi = float(np.quantile(err, 1.0 - self.alpha / 2.0))
        else:
            # Not enough history to conformalize — symmetric Gaussian band.
            sigma = float(np.std(resid)) if resid.size >= 2 else 0.0
            z = 1.959963984540054  # 97.5th percentile of the standard normal
            q_lo, q_hi = -z * sigma, z * sigma
        lower = max(point + q_lo, 0.0)
        upper = max(point + q_hi, point)
        return lower, upper

    # ── helpers ──────────────────────────────────────────────────────────────
    @staticmethod
    def _design(
        t: np.ndarray, t_mean: float, t_std: float, period: int, nharm: int
    ) -> np.ndarray:
        """Design matrix: intercept, scaled linear trend, then sin/cos pairs."""
        cols = [np.ones_like(t), (t - t_mean) / t_std]
        for k in range(1, nharm + 1):
            w = 2.0 * np.pi * k * t / period
            cols.append(np.sin(w))
            cols.append(np.cos(w))
        return np.column_stack(cols)

    def _robust_lstsq(self, X: np.ndarray, y: np.ndarray) -> np.ndarray:
        """Least squares with a few IRLS passes (bisquare-style downweighting).

        The reweighting is what keeps flash-crowd spikes from pulling the
        structural baseline off the calm level — the key to beating persistence
        on the spiky profile. In ``downward`` mode only points the fit sits
        above (dips) are downweighted; upward residuals keep full weight, so the
        baseline tracks rather than ignores a rising flash crowd (autoscaler
        path — see robust_mode).
        """
        coef, *_ = np.linalg.lstsq(X, y, rcond=None)
        for _ in range(self.irls_iters):
            r = y - X @ coef
            mad = np.median(np.abs(r - np.median(r))) * 1.4826 + 1e-6
            w = 1.0 / (1.0 + (np.abs(r) / (3.0 * mad)) ** 2)
            if self.robust_mode == "downward":
                # r > 0 means the actual is above the fit (an upward spike) —
                # keep it at full weight; only downweight the dips (r < 0).
                w = np.where(r > 0.0, 1.0, w)
            sw = np.sqrt(w)
            coef, *_ = np.linalg.lstsq(X * sw[:, None], y * sw, rcond=None)
        return coef

    @staticmethod
    def _trend_shrink(trend_col: np.ndarray, slope_coef: float,
                      resid: np.ndarray) -> float:
        """Shrink factor in [0, 1] for the projected trend, from the slope's SNR.

        Treats the linear-trend coefficient as a regression slope and forms an
        approximate t-statistic t = |slope| / se, se = σ_resid / √Σ(trend_col²)
        (the seasonal/intercept columns are near-orthogonal to the centred trend
        over a window, so this is a close, cheap estimate). Returns
        ``t² / (t² + C)``: ~0 when the slope is indistinguishable from noise
        (flat demand → no spurious lead, no scale churn), ~1 when it is strongly
        significant (a real ramp → full lead preserved).
        """
        sxx = float(trend_col @ trend_col)
        if sxx <= 1e-12 or resid.size < 3:
            return 0.0
        sigma = float(np.std(resid))
        if sigma <= 1e-12:
            return 1.0  # a perfectly clean trend — keep the full lead
        se = sigma / np.sqrt(sxx)
        t2 = (float(slope_coef) / se) ** 2
        return t2 / (t2 + _TREND_SNR_GATE)

    def _infer_period(self, timestamps: list[str], n: int) -> int:
        """Infer the daily seasonal period from the timestamp cadence.

        P = round(86400 s / median Δt). Returns the 5-min-bucket default when
        the timestamps are missing, unparseable, or imply a non-positive
        cadence. Capped at n so a too-coarse cadence never demands more data
        than exists.
        """
        try:
            if not timestamps or len(timestamps) < 2:
                return min(_DEFAULT_PERIOD, n)
            # Sample up to the last ~50 stamps to estimate cadence cheaply.
            sample = timestamps[-min(len(timestamps), 50):]
            secs = np.array(
                [self._parse(ts) for ts in sample], dtype="float64"
            )
            diffs = np.diff(secs)
            diffs = diffs[np.isfinite(diffs) & (diffs > 0)]
            if diffs.size == 0:
                return min(_DEFAULT_PERIOD, n)
            dt = float(np.median(diffs))
            period = int(round(_SECONDS_PER_DAY / dt))
            if period < 2:
                return min(_DEFAULT_PERIOD, n)
            return period
        except Exception:  # noqa: BLE001
            return min(_DEFAULT_PERIOD, n)

    @staticmethod
    def _parse(ts: str) -> float:
        """ISO-8601 → POSIX seconds. Tolerates a trailing 'Z'."""
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()

    def _fallback(self, finite: np.ndarray) -> Forecast:
        """Mean-of-history Forecast for too-short or degenerate inputs."""
        mean = float(np.mean(finite))
        std = float(np.std(finite, ddof=1)) if finite.size >= 2 else 0.0
        return Forecast(
            horizon_minutes=self.horizon_minutes,
            predicted_rps=mean,
            confidence_lower=max(0.0, mean - 1.959963984540054 * std),
            confidence_upper=mean + 1.959963984540054 * std,
        )
