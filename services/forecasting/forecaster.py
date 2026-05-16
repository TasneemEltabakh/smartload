"""
services/forecasting/forecaster.py
────────────────────────────────────
Runtime forecasting wrapper. Discovers and loads the best available model
artifact, with a strict priority chain:

  1. arimax_model.pkl  — ARIMAX with exogenous features (best MAPE)
  2. arima_model.pkl   — pure ARIMA, no exog (safe fallback)
  3. moving average    — always succeeds (last resort)

ARIMAX at runtime uses the last known values of request_latency_ms and
error_rate from TimescaleDB, z-score normalised using training statistics
stored in the pkl. These serve as proxies for the Alibaba training features
(avg_cpu_util, system_load). If the exog query fails, the service falls
through to the plain ARIMA model automatically.
"""

from __future__ import annotations

import logging
import pickle
import warnings
from pathlib import Path

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")
logging.getLogger("statsmodels").setLevel(logging.WARNING)
logging.getLogger("prophet").setLevel(logging.WARNING)
logging.getLogger("cmdstanpy").setLevel(logging.WARNING)

from shared.contracts import ForecastResult

_ARIMAX_PATH  = Path(__file__).parent / "models" / "arimax_model.pkl"
_ARIMA_PATH   = Path(__file__).parent / "models" / "arima_model.pkl"
_PROPHET_PATH = Path(__file__).parent / "models" / "prophet_model.pkl"

HORIZON   = 5
MA_WINDOW = 12


class ProphetForecaster:
    """Unified forecaster. Name kept for import compatibility with app.py."""

    def __init__(self) -> None:
        self._arimax_result  = None
        self._arimax_order:  tuple | None = None
        self._arimax_cols:   list[str] = []
        self._arimax_stats:  dict = {}

        self._arima_result   = None
        self._arima_order:   tuple | None = None

        self._prophet_model  = None
        self._freq:   str = "5min"
        self._active: str = "none"   # which model is loaded

    # ── load ──────────────────────────────────────────────────────────────────

    def load_pretrained(self) -> bool:
        if _ARIMAX_PATH.exists() and self._load_arimax():
            return True
        if _ARIMA_PATH.exists() and self._load_arima():
            return True
        if _PROPHET_PATH.exists() and self._load_prophet():
            return True
        print("[forecaster] no model artifact found — using moving average", flush=True)
        return False

    def _load_arimax(self) -> bool:
        try:
            with open(_ARIMAX_PATH, "rb") as f:
                b = pickle.load(f)
            self._arimax_result = b["result"]
            self._arimax_order  = tuple(b["order"])
            self._arimax_cols   = b["exog_cols"]
            self._arimax_stats  = b["exog_stats"]
            self._freq          = b.get("freq", "5min")
            self._active        = "arimax"
            print(f"[forecaster] ARIMAX{self._arimax_order} loaded  "
                  f"exog={self._arimax_cols}  freq={self._freq}", flush=True)
            # Also load ARIMA as silent backup
            if _ARIMA_PATH.exists():
                self._load_arima(silent=True)
            return True
        except Exception as exc:
            print(f"[forecaster] ARIMAX load failed: {exc}", flush=True)
            return False

    def _load_arima(self, silent: bool = False) -> bool:
        try:
            with open(_ARIMA_PATH, "rb") as f:
                b = pickle.load(f)
            self._arima_result = b["result"]
            self._arima_order  = tuple(b["order"])
            if not silent:
                self._freq   = b.get("freq", "5min")
                self._active = "arima"
                print(f"[forecaster] ARIMA{self._arima_order} loaded  "
                      f"freq={self._freq}", flush=True)
            return True
        except Exception as exc:
            if not silent:
                print(f"[forecaster] ARIMA load failed: {exc}", flush=True)
            return False

    def _load_prophet(self) -> bool:
        try:
            from prophet import Prophet  # noqa: F401
            with open(_PROPHET_PATH, "rb") as f:
                b = pickle.load(f)
            self._prophet_model = b["model"]
            self._freq          = b.get("freq", "5min")
            self._active        = "prophet"
            print(f"[forecaster] Prophet loaded  freq={self._freq}", flush=True)
            return True
        except Exception as exc:
            print(f"[forecaster] Prophet load failed: {exc}", flush=True)
            return False

    # ── ARIMAX predict ────────────────────────────────────────────────────────

    def _arimax_predict(self, df: pd.DataFrame,
                        df_exog: pd.DataFrame | None) -> ForecastResult | None:
        if self._arimax_result is None:
            return None
        try:
            y_recent    = self._resample_to_freq(df)["y"].values
            exog_recent = self._build_exog_array(df_exog)
            if exog_recent is None:
                return None   # exog unavailable → fall through to ARIMA

            updated = self._arimax_result.append(y_recent, exog=exog_recent, refit=False)
            # Forecast next step using LAST known exog as lagged predictor
            x_next = exog_recent[-1:] if exog_recent.ndim == 2 else exog_recent[-1].reshape(1, -1)
            fc     = updated.get_forecast(steps=1, exog=x_next)
            pred   = max(float(fc.predicted_mean.iloc[0]), 0.0)
            ci     = fc.conf_int(alpha=0.05)
            lower  = max(float(ci.iloc[0, 0]), 0.0)
            upper  = max(float(ci.iloc[0, 1]), pred)
            return ForecastResult(
                horizon_minutes=HORIZON,
                predicted_rps=pred,
                confidence_lower=lower,
                confidence_upper=upper,
                model_id=f"arimax{self._arimax_order}",
            )
        except Exception as exc:
            print(f"[forecaster] ARIMAX predict failed: {exc} — trying ARIMA", flush=True)
            return None

    def _build_exog_array(self, df_exog: pd.DataFrame | None) -> np.ndarray | None:
        """Normalise runtime exog using training stats. Returns None if unavailable."""
        if df_exog is None or df_exog.empty:
            return None
        cols = self._arimax_cols
        # Map available runtime columns to expected training columns by position
        available = [c for c in df_exog.columns if c != "ds"]
        if not available:
            return None
        rows: list[np.ndarray] = []
        for _, row in df_exog.tail(max(len(df_exog), 1)).iterrows():
            vec = []
            for i, col in enumerate(cols):
                proxy = available[i] if i < len(available) else available[-1]
                raw   = float(row[proxy]) if proxy in row.index else 0.0
                stats = self._arimax_stats.get(col, {"mean": 0.0, "std": 1.0})
                z     = (raw - stats["mean"]) / (stats["std"] if stats["std"] > 0 else 1.0)
                vec.append(z)
            rows.append(np.array(vec))
        return np.vstack(rows)

    # ── ARIMA predict ─────────────────────────────────────────────────────────

    def _arima_predict(self, df: pd.DataFrame) -> ForecastResult | None:
        if self._arima_result is None:
            return None
        try:
            y_recent = self._resample_to_freq(df)["y"].values
            updated  = self._arima_result.append(y_recent, refit=False)
            fc       = updated.get_forecast(steps=1)
            pred     = max(float(fc.predicted_mean.iloc[0]), 0.0)
            ci       = fc.conf_int(alpha=0.05)
            lower    = max(float(ci.iloc[0, 0]), 0.0)
            upper    = max(float(ci.iloc[0, 1]), pred)
            return ForecastResult(
                horizon_minutes=HORIZON,
                predicted_rps=pred,
                confidence_lower=lower,
                confidence_upper=upper,
                model_id=f"arima{self._arima_order}",
            )
        except Exception as exc:
            print(f"[forecaster] ARIMA predict failed: {exc}", flush=True)
            return None

    # ── Prophet predict ───────────────────────────────────────────────────────

    def _prophet_predict(self) -> ForecastResult | None:
        if self._prophet_model is None:
            return None
        try:
            future = self._prophet_model.make_future_dataframe(
                periods=1, freq=self._freq, include_history=False
            )
            fc   = self._prophet_model.predict(future)
            pred = max(float(fc["yhat"].iloc[-1]), 0.0)
            lo   = max(float(fc["yhat_lower"].iloc[-1]), 0.0)
            hi   = max(float(fc["yhat_upper"].iloc[-1]), pred)
            return ForecastResult(
                horizon_minutes=HORIZON, predicted_rps=pred,
                confidence_lower=lo, confidence_upper=hi,
                model_id="prophet_static",
            )
        except Exception as exc:
            print(f"[forecaster] Prophet predict failed: {exc}", flush=True)
            return None

    # ── moving average ────────────────────────────────────────────────────────

    def moving_average_predict(self, df: pd.DataFrame) -> ForecastResult:
        if df.empty:
            return ForecastResult(horizon_minutes=HORIZON, predicted_rps=0.0,
                                  confidence_lower=0.0, confidence_upper=0.0,
                                  model_id="moving_average")
        window    = df["y"].tail(MA_WINDOW)
        predicted = float(window.mean())
        std       = float(window.std()) if len(window) > 1 else 0.0
        return ForecastResult(
            horizon_minutes=HORIZON,
            predicted_rps=max(predicted, 0.0),
            confidence_lower=max(predicted - std, 0.0),
            confidence_upper=max(predicted + std, 0.0),
            model_id="moving_average",
        )

    # ── helpers ───────────────────────────────────────────────────────────────

    def _resample_to_freq(self, df: pd.DataFrame) -> pd.DataFrame:
        if self._freq == "1min":
            return df
        resampled = df.set_index("ds")["y"].resample(self._freq).mean().dropna()
        return pd.DataFrame({"ds": resampled.index, "y": resampled.values}).reset_index(drop=True)

    # ── public predict ────────────────────────────────────────────────────────

    def predict(self, df: pd.DataFrame,
                df_exog: pd.DataFrame | None = None) -> ForecastResult:
        """
        Fallback chain: ARIMAX → ARIMA → Prophet → moving average.
        df_exog is optional; if absent ARIMAX falls through to ARIMA silently.
        """
        if self._active == "arimax":
            result = self._arimax_predict(df, df_exog)
            if result is not None:
                return result
            # ARIMAX failed → try ARIMA backup (loaded silently at startup)
            result = self._arima_predict(df)
            if result is not None:
                return result

        elif self._active == "arima":
            result = self._arima_predict(df)
            if result is not None:
                return result

        elif self._active == "prophet":
            result = self._prophet_predict()
            if result is not None:
                return result

        return self.moving_average_predict(df)
