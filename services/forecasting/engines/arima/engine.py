"""
services/forecasting/engines/arima/engine.py
─────────────────────────────────────────────
ArimaEngine — loads a pre-trained ARIMA(p,d,q) model and serves
single-step forecasts on the engine_base.ForecastEngine contract.

Artifact handoff
────────────────
The trained model lives at services/forecasting/models/arima_model.pkl
and is produced by tools/forecasting-training/train.py. The pickled
bundle has the following shape:

    {
        "result":    statsmodels ARIMA result object (.append + .get_forecast),
        "order":     (p, d, q) tuple,
        "freq":      "5min" (default training frequency),
        "exog_cols": list[str] — empty for plain ARIMA,
        "exog_stats": dict      — empty for plain ARIMA,
    }

Plain-ARIMA support only on this engine — exog is documented in the
bundle for compatibility with the ARIMAX variant but ignored here. The
engine_base.HistoryWindow contract doesn't currently expose live exog
to engines, so an ARIMAX path would need an ABC extension (filed as
follow-up — see tools/forecasting-training/README.md acceptance section).

Fallback
────────
If the artifact is missing or fails to unpickle, the engine constructs
fine and `forecast()` returns a mean-of-history Forecast — so the
service stays alive while an operator promotes a new artifact. The
service-level select_engine fallback to `moving_average` covers the
case where this whole engine module fails to import.

Authors
───────
Model architecture, feature engineering, training pipeline by Nada
Nabil (originally in PR #144). Engine handoff to the #138 plugin
layout by Tasneem Muhammed.
"""

from __future__ import annotations

import logging
import pickle
import sys
from pathlib import Path

import numpy as np

_SERVICE_ROOT = Path(__file__).resolve().parents[2]
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

from engine_base import ForecastEngine, Forecast, HistoryWindow  # noqa: E402

# Tame the noisy statsmodels warnings on append/forecast under live data.
logging.getLogger("statsmodels").setLevel(logging.WARNING)

_log = logging.getLogger(__name__)

# Default artifact location relative to the service root — works in both
# the container layout (/app/models/) and dev layout
# (services/forecasting/models/).
_DEFAULT_MODEL_PATH = _SERVICE_ROOT / "models" / "arima_model.pkl"

# How many historical samples to feed back into the model on append. Keeps
# inference O(window) regardless of how long the run loop has been polling.
_MAX_APPEND_SAMPLES = 60


class ArimaEngine(ForecastEngine):
    """Pre-trained ARIMA(p,d,q) engine.

    Loads the artifact at construction. If the load fails, `forecast()`
    falls back to a mean-of-history prediction so the service keeps
    publishing. The run loop's `select_engine` fallback handles the
    case where this entire module fails to import.

    Args:
        horizon_minutes: forecast horizon. The trained model is a
            single-step (1-bucket) forecaster — the horizon here is how
            the Forecast envelope is labelled, not how far ahead the
            model actually predicts.
        model_path: path to the .pkl artifact. Defaults to
            services/forecasting/models/arima_model.pkl.
    """

    def __init__(
        self,
        horizon_minutes: int = 5,
        model_path: str | None = None,
        **engine_kwargs,
    ) -> None:
        # The run loop's `EnginePolicy.engine_kwargs()` passes a uniform set
        # of params (e.g. window_samples) to every engine so policy-derived
        # tuning flows through one call site. ARIMA doesn't use most of
        # them — accept gracefully so unknown kwargs don't crash construction.
        del engine_kwargs
        self.horizon_minutes = horizon_minutes
        path = Path(model_path) if model_path else _DEFAULT_MODEL_PATH
        self._result = None
        self._order: tuple | None = None
        self._loaded = self._load(path)
        if self._loaded:
            _log.info("ArimaEngine: loaded ARIMA%s from %s", self._order, path)
        else:
            _log.warning(
                "ArimaEngine: artifact at %s unavailable; "
                "forecast() will use mean-of-history fallback",
                path,
            )

    def forecast(self, history: HistoryWindow) -> Forecast:
        rates = history.request_rates
        if not rates:
            return Forecast(self.horizon_minutes, 0.0, 0.0, 0.0)

        if not self._loaded or self._result is None:
            return self._fallback_forecast(rates)

        try:
            return self._model_forecast(rates)
        except Exception as exc:  # noqa: BLE001
            _log.warning(
                "ArimaEngine.forecast() raised (%s); falling back to mean", exc
            )
            return self._fallback_forecast(rates)

    @property
    def model_loaded(self) -> bool:
        """True iff the artifact loaded successfully. Exposed for /health."""
        return self._loaded

    # ──────────────────────────────────────────────────────────────────────

    def _load(self, path: Path) -> bool:
        if not path.exists():
            return False
        try:
            with open(path, "rb") as f:
                bundle = pickle.load(f)
        except Exception as exc:  # noqa: BLE001
            _log.error("ArimaEngine: pickle load failed: %s", exc)
            return False

        try:
            self._result = bundle["result"]
            self._order = tuple(bundle["order"])
        except (KeyError, TypeError) as exc:
            _log.error("ArimaEngine: artifact bundle has wrong shape: %s", exc)
            return False
        return True

    def _model_forecast(self, rates: list[float]) -> Forecast:
        # Cap the appended window so a long-running service doesn't pay an
        # O(history) cost per cycle. The model has already seen the full
        # training distribution; recent samples nudge the state.
        recent = [r for r in rates[-_MAX_APPEND_SAMPLES:] if np.isfinite(r)]
        if not recent:
            raise ValueError("no finite samples to append")
        y_recent = np.asarray(recent, dtype=float)
        updated = self._result.append(y_recent, refit=False)
        fc = updated.get_forecast(steps=1)

        pred = max(float(np.asarray(fc.predicted_mean).flat[0]), 0.0)
        ci = np.asarray(fc.conf_int(alpha=0.05))
        lower = max(float(ci.flat[0]), 0.0)
        upper = max(float(ci.flat[1]), pred)

        if not (np.isfinite(pred) and np.isfinite(lower) and np.isfinite(upper)):
            raise ValueError("non-finite forecast produced by ARIMA model")

        return Forecast(
            horizon_minutes=self.horizon_minutes,
            predicted_rps=pred,
            confidence_lower=lower,
            confidence_upper=upper,
        )

    def _fallback_forecast(self, rates: list[float]) -> Forecast:
        """Mean-of-history Forecast used when the artifact is unavailable."""
        rates = [r for r in rates if np.isfinite(r)]
        if not rates:
            return Forecast(self.horizon_minutes, 0.0, 0.0, 0.0)
        mean = sum(rates) / len(rates)
        if len(rates) >= 2:
            var = sum((r - mean) ** 2 for r in rates) / (len(rates) - 1)
            std = var ** 0.5
        else:
            std = 0.0
        return Forecast(
            horizon_minutes=self.horizon_minutes,
            predicted_rps=mean,
            confidence_lower=max(0.0, mean - std),
            confidence_upper=mean + std,
        )
