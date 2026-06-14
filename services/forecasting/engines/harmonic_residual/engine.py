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
    """

    def __init__(
        self,
        horizon_minutes: int = 5,
        n_harmonics: int = 3,
        fit_window: int | None = 1152,   # 4 days at 5-min buckets; ≥2 cycles
        irls_iters: int = 2,
        alpha: float = 0.05,
        min_history: int = 12,
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
        # Widen it to cover ≥3 seasonal cycles when there is data for them, so
        # the daily basis stays identifiable at any cadence (e.g. 1-min data
        # needs ~3×1440 samples, not the 5-min default of 1152).
        window = self.fit_window
        if window is not None and period:
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
        x_next = self._design(
            np.array([float(n + steps - 1)]), t_mean, t_std, period, nharm
        )
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
        on the spiky profile.
        """
        coef, *_ = np.linalg.lstsq(X, y, rcond=None)
        for _ in range(self.irls_iters):
            r = y - X @ coef
            mad = np.median(np.abs(r - np.median(r))) * 1.4826 + 1e-6
            w = 1.0 / (1.0 + (np.abs(r) / (3.0 * mad)) ** 2)
            sw = np.sqrt(w)
            coef, *_ = np.linalg.lstsq(X * sw[:, None], y * sw, rcond=None)
        return coef

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
