"""
services/forecasting/train.py
──────────────────────────────
Offline training pipeline for the SmartLoad forecasting model.

Usage:
    python services/forecasting/train.py --data-dir datasets/alibaba/alibaba_industrial_data.csv
    python services/forecasting/train.py --data-dir datasets/alibaba/alibaba_industrial_data.csv --model prophet

Default model: arima (better for autocorrelated request-rate data).
Prophet is available as --model prophet.

Outputs:
    services/forecasting/models/arima_model.pkl  (or prophet_model.pkl)
    services/forecasting/models/training_log.json   (appended each run)

Temporal split: 70% train / 15% val / 15% test (no shuffling).
ARIMA:   AIC-based order selection on train, walk-forward evaluation on test.
Prophet: validation-MAPE grid search, then single-shot test evaluation.
"""

from __future__ import annotations

import argparse
import json
import pickle
import sys
import warnings
from datetime import datetime, timezone
from itertools import product
from pathlib import Path

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")
import logging
logging.getLogger("prophet").setLevel(logging.WARNING)
logging.getLogger("cmdstanpy").setLevel(logging.WARNING)
logging.getLogger("statsmodels").setLevel(logging.WARNING)

_HERE = Path(__file__).parent
sys.path.insert(0, str(_HERE))
from preprocess import load_alibaba, load_alibaba_with_exog

ARIMA_MODEL_OUT   = _HERE / "models" / "arima_model.pkl"
ARIMAX_MODEL_OUT  = _HERE / "models" / "arimax_model.pkl"
PROPHET_MODEL_OUT = _HERE / "models" / "prophet_model.pkl"
TRAIN_LOG         = _HERE / "models" / "training_log.json"
HORIZON = 5  # minutes ahead

# ── metrics ───────────────────────────────────────────────────────────────────

def mape(actual: np.ndarray, predicted: np.ndarray) -> float:
    mask = actual > 0
    if mask.sum() == 0:
        return float("inf")
    return float(np.mean(np.abs((actual[mask] - predicted[mask]) / actual[mask])) * 100)

def mae(actual: np.ndarray, predicted: np.ndarray) -> float:
    return float(np.mean(np.abs(actual - predicted)))

def _persistence_mape(y_train_last: float, y_test: np.ndarray) -> float:
    pred = np.full_like(y_test, fill_value=y_train_last)
    return mape(y_test, pred)

def _moving_avg_mape(y_seed: np.ndarray, y_test: np.ndarray, window: int = 12) -> float:
    seed = y_seed[-window:] if len(y_seed) >= window else y_seed
    pred = np.full_like(y_test, fill_value=seed.mean())
    return mape(y_test, pred)

# ── ARIMA ─────────────────────────────────────────────────────────────────────

def _arima_aic_search(y_train: np.ndarray) -> tuple[int, int, int]:
    """Select ARIMA(p,d,q) order by AIC on training data."""
    from statsmodels.tsa.arima.model import ARIMA

    # Check stationarity to decide d
    from statsmodels.tsa.stattools import adfuller
    adf_p = adfuller(y_train, autolag="AIC")[1]
    d_values = [0] if adf_p < 0.05 else [1]

    best_aic = float("inf")
    best_order = (1, 0, 0)

    candidates = list(product([1, 2, 3], d_values, [0, 1, 2]))
    print(f"[train] ARIMA AIC search: {len(candidates)} orders …")

    for p, d, q in candidates:
        try:
            res = ARIMA(y_train, order=(p, d, q)).fit()
            if res.aic < best_aic:
                best_aic = res.aic
                best_order = (p, d, q)
            print(f"  ARIMA({p},{d},{q})  AIC={res.aic:.2f}")
        except Exception:
            pass

    print(f"\n[train] best order: ARIMA{best_order}  AIC={best_aic:.2f}")
    return best_order


def _arima_walkforward(y_train_full: np.ndarray, y_test: np.ndarray,
                       order: tuple) -> tuple[float, float]:
    """
    Walk-forward evaluation: fit ARIMA on training history, predict 1 step,
    append actual, repeat. Matches runtime forecaster behaviour exactly.
    """
    from statsmodels.tsa.arima.model import ARIMA

    history = list(y_train_full)
    predictions: list[float] = []

    print(f"[train] walk-forward evaluation on {len(y_test)} test steps …")
    for i, actual in enumerate(y_test):
        try:
            model = ARIMA(history, order=order)
            res = model.fit()
            pred = float(res.forecast(steps=1)[0])
            pred = max(pred, 0.0)
        except Exception:
            pred = float(np.mean(history[-12:]))  # fallback: moving avg
        predictions.append(pred)
        history.append(actual)
        if (i + 1) % 200 == 0:
            print(f"  … {i+1}/{len(y_test)} steps done")

    predictions_arr = np.array(predictions)
    return mape(y_test, predictions_arr), mae(y_test, predictions_arr)


def train_arima(data_dir: str) -> None:
    run_ts = datetime.now(timezone.utc).isoformat()

    df, freq = load_alibaba(data_dir, verbose=True, clip_outliers=True)
    y = df["y"].values
    n = len(y)
    n_train = int(n * 0.70)
    n_val   = int(n * 0.85)

    y_train = y[:n_train]
    y_val   = y[n_train:n_val]
    y_test  = y[n_val:]

    print(f"\n[train] split — train: {n_train:,}  val: {len(y_val):,}  "
          f"test: {len(y_test):,}  freq: {freq}")

    # AIC order selection on training data
    best_order = _arima_aic_search(y_train)

    # Walk-forward on test set (train+val as history)
    y_train_full = y[:n_val]
    test_mape, test_mae = _arima_walkforward(y_train_full, y_test, best_order)

    persist_mape = _persistence_mape(y_train_full[-1], y_test)
    ma_mape      = _moving_avg_mape(y_val, y_test)
    mean_y       = y_test.mean()
    norm_mae     = test_mae / mean_y if mean_y > 0 else float("inf")
    rel_imp      = (persist_mape - test_mape) / persist_mape * 100

    print("\n" + "─" * 50)
    print("EVALUATION ON TEST SET  (ARIMA — walk-forward)")
    print("─" * 50)
    print(f"  Test MAPE (ARIMA)      : {test_mape:.2f}%")
    print(f"  Test MAE  (normalised) : {norm_mae:.4f}")
    print(f"  Persistence baseline   : {persist_mape:.2f}%")
    print(f"  Moving-avg baseline    : {ma_mape:.2f}%")
    print(f"  Relative improvement   : {rel_imp:.1f}% vs persistence")
    print(f"  Best ARIMA order       : {best_order}")
    print(f"  Sampling frequency     : {freq}")

    passed = test_mape < 20.0
    print(f"\n  MAPE < 20% gate        : {'PASS ✓' if passed else 'FAIL ✗  (target < 20%)'}")
    print("─" * 50)

    # Save final ARIMA fitted on ALL data
    from statsmodels.tsa.arima.model import ARIMA
    final_result = ARIMA(y, order=best_order).fit()
    ARIMA_MODEL_OUT.parent.mkdir(parents=True, exist_ok=True)
    with open(ARIMA_MODEL_OUT, "wb") as f:
        pickle.dump({"result": final_result, "order": best_order, "freq": freq}, f)
    print(f"\n[train] model saved → {ARIMA_MODEL_OUT}")

    _append_run_log({
        "run_at": run_ts, "model": "arima", "data_dir": str(data_dir),
        "n_total": n, "n_train": n_train, "n_val": len(y_val), "n_test": len(y_test),
        "sampling_freq": freq, "arima_order": list(best_order),
        "test_mape": round(test_mape, 4), "test_mae_normalised": round(norm_mae, 4),
        "persistence_mape": round(persist_mape, 4), "moving_avg_mape": round(ma_mape, 4),
        "rel_improvement_pct": round(rel_imp, 2), "passed": passed,
    })

    if not passed:
        print("\n[train] MAPE target not met. Try: more data, SARIMA seasonal terms, "
              "or feature engineering.")
        sys.exit(1)


# ── Prophet ───────────────────────────────────────────────────────────────────

_PROPHET_GRID = {
    "changepoint_prior_scale": [0.05, 0.1, 0.5, 1.0, 2.0],
    "seasonality_prior_scale": [1.0, 5.0, 10.0, 20.0],
    "seasonality_mode":        ["additive", "multiplicative"],
}


def train_prophet(data_dir: str) -> None:
    from prophet import Prophet

    run_ts = datetime.now(timezone.utc).isoformat()
    df, freq = load_alibaba(data_dir, verbose=True, clip_outliers=True)
    n = len(df)
    n_train = int(n * 0.70)
    n_val   = int(n * 0.85)

    df_train = df.iloc[:n_train].copy()
    df_val   = df.iloc[n_train:n_val].copy()
    df_test  = df.iloc[n_val:].copy()

    print(f"\n[train] split — train: {len(df_train):,}  val: {len(df_val):,}  "
          f"test: {len(df_test):,}  freq: {freq}")

    keys = list(_PROPHET_GRID.keys())
    combos = list(product(*[_PROPHET_GRID[k] for k in keys]))
    print(f"[train] Prophet grid search: {len(combos)} combinations …")

    best_val_mape = float("inf")
    best_params: dict = {}

    for i, combo in enumerate(combos, 1):
        params = dict(zip(keys, combo))
        try:
            m = Prophet(daily_seasonality=True, weekly_seasonality=True,
                        yearly_seasonality=False, **params)
            m.fit(df_train)
            fc = m.predict(df_val[["ds"]])
            pred = np.maximum(fc["yhat"].values, 0)
            val_mape = mape(df_val["y"].values, pred)
        except Exception as exc:
            print(f"  combo {i} FAILED: {exc}")
            continue
        marker = ""
        if val_mape < best_val_mape:
            best_val_mape = val_mape
            best_params = params
            marker = " ◀ best"
        print(f"  [{i:3d}/{len(combos)}] val_MAPE={val_mape:6.2f}%  {params}{marker}")

    print(f"\n[train] best val MAPE: {best_val_mape:.2f}%  params: {best_params}")

    df_trainval = pd.concat([df_train, df_val], ignore_index=True)
    print(f"\n[train] retraining on train+val ({len(df_trainval):,} rows) …")
    final_model = Prophet(daily_seasonality=True, weekly_seasonality=True,
                          yearly_seasonality=False, **best_params)
    final_model.fit(df_trainval)

    fc = final_model.predict(df_test[["ds"]])
    predicted = np.maximum(fc["yhat"].values, 0)
    test_mape_val = mape(df_test["y"].values, predicted)
    test_mae_val  = mae(df_test["y"].values, predicted)
    persist_mape  = _persistence_mape(df_trainval["y"].iloc[-1], df_test["y"].values)
    ma_mape       = _moving_avg_mape(df_val["y"].values, df_test["y"].values)
    mean_y        = df_test["y"].mean()
    norm_mae      = test_mae_val / mean_y if mean_y > 0 else float("inf")
    rel_imp       = (persist_mape - test_mape_val) / persist_mape * 100

    print("\n" + "─" * 50)
    print("EVALUATION ON TEST SET  (Prophet — single-shot)")
    print("─" * 50)
    print(f"  Test MAPE (Prophet)    : {test_mape_val:.2f}%")
    print(f"  Test MAE  (normalised) : {norm_mae:.4f}")
    print(f"  Persistence baseline   : {persist_mape:.2f}%")
    print(f"  Moving-avg baseline    : {ma_mape:.2f}%")
    print(f"  Relative improvement   : {rel_imp:.1f}% vs persistence")
    print(f"  Best hyperparameters   : {best_params}")
    print(f"  Sampling frequency     : {freq}")

    passed = test_mape_val < 20.0
    print(f"\n  MAPE < 20% gate        : {'PASS ✓' if passed else 'FAIL ✗  (target < 20%)'}")
    print("─" * 50)

    PROPHET_MODEL_OUT.parent.mkdir(parents=True, exist_ok=True)
    with open(PROPHET_MODEL_OUT, "wb") as f:
        pickle.dump({"model": final_model, "params": best_params, "freq": freq}, f)
    print(f"\n[train] model saved → {PROPHET_MODEL_OUT}")

    _append_run_log({
        "run_at": run_ts, "model": "prophet", "data_dir": str(data_dir),
        "n_total": n, "n_train": len(df_train), "n_val": len(df_val), "n_test": len(df_test),
        "sampling_freq": freq, "best_params": best_params,
        "test_mape": round(test_mape_val, 4), "test_mae_normalised": round(norm_mae, 4),
        "persistence_mape": round(persist_mape, 4), "moving_avg_mape": round(ma_mape, 4),
        "rel_improvement_pct": round(rel_imp, 2), "passed": passed,
    })

    if not passed:
        sys.exit(1)


# ── ARIMAX ────────────────────────────────────────────────────────────────────

def _arimax_aic_search(y_train: np.ndarray, exog_train: np.ndarray) -> tuple[int, int, int]:
    from statsmodels.tsa.arima.model import ARIMA
    from statsmodels.tsa.stattools import adfuller

    adf_p = adfuller(y_train, autolag="AIC")[1]
    d_values = [0] if adf_p < 0.05 else [1]

    best_aic = float("inf")
    best_order = (1, 0, 0)
    candidates = list(product([1, 2, 3], d_values, [0, 1, 2]))
    print(f"[train] ARIMAX AIC search: {len(candidates)} orders …")

    # Lagged exog: x[t-1] predicts y[t], so train on y[1:] with exog[:-1]
    y_lag  = y_train[1:]
    x_lag  = exog_train[:-1]

    for p, d, q in candidates:
        try:
            res = ARIMA(y_lag, exog=x_lag, order=(p, d, q)).fit()
            if res.aic < best_aic:
                best_aic = res.aic
                best_order = (p, d, q)
            print(f"  ARIMAX({p},{d},{q})  AIC={res.aic:.2f}")
        except Exception:
            pass

    print(f"\n[train] best order: ARIMAX{best_order}  AIC={best_aic:.2f}")
    return best_order


def _arimax_walkforward(y_history: np.ndarray, exog_history: np.ndarray,
                        y_test: np.ndarray, exog_test: np.ndarray,
                        order: tuple) -> tuple[float, float]:
    """
    Walk-forward: at each step use lagged exog (x[t-1]) to predict y[t].
    exog_test[i] is the exog value at the same time as y_test[i].
    So we use exog_history[-1] to predict y_test[0], then exog_test[i-1]
    to predict y_test[i].
    """
    from statsmodels.tsa.arima.model import ARIMA

    hist_y    = list(y_history)
    hist_exog = list(exog_history)
    predictions: list[float] = []

    print(f"[train] ARIMAX walk-forward on {len(y_test)} test steps …")
    for i, (actual_y, actual_exog) in enumerate(zip(y_test, exog_test)):
        # Current exog = last known exog value (lagged predictor for next step)
        x_pred = np.array(hist_exog[-1:])
        try:
            y_arr = np.array(hist_y[1:])
            x_arr = np.array(hist_exog[:-1])
            # Ensure shapes match
            min_len = min(len(y_arr), len(x_arr))
            res = ARIMA(y_arr[-min_len:], exog=x_arr[-min_len:], order=order).fit()
            pred = float(res.forecast(steps=1, exog=x_pred)[0])
            pred = max(pred, 0.0)
        except Exception:
            pred = float(np.mean(hist_y[-12:]))
        predictions.append(pred)
        hist_y.append(actual_y)
        hist_exog.append(actual_exog)
        if (i + 1) % 200 == 0:
            print(f"  … {i+1}/{len(y_test)} steps done")

    arr = np.array(predictions)
    return mape(y_test, arr), mae(y_test, arr)


def train_arimax(data_dir: str) -> None:
    run_ts = datetime.now(timezone.utc).isoformat()

    df_y, df_exog, freq = load_alibaba_with_exog(data_dir, verbose=True,
                                                  clip_outliers=True)
    exog_cols = [c for c in df_exog.columns if c != "ds"]
    if not exog_cols:
        print("[train] ERROR: no exogenous features found — cannot train ARIMAX")
        sys.exit(1)

    print(f"[train] ARIMAX using exog features: {exog_cols}")

    y    = df_y["y"].values
    exog = df_exog[exog_cols].values          # shape (n, n_features)
    n    = len(y)
    n_train = int(n * 0.70)
    n_val   = int(n * 0.85)

    y_train    = y[:n_train];      exog_train    = exog[:n_train]
    y_val      = y[n_train:n_val]; exog_val      = exog[n_train:n_val]
    y_test     = y[n_val:];        exog_test     = exog[n_val:]
    y_trainval = y[:n_val];        exog_trainval = exog[:n_val]

    print(f"\n[train] split — train: {n_train:,}  val: {len(y_val):,}  "
          f"test: {len(y_test):,}  exog_cols: {exog_cols}")

    best_order = _arimax_aic_search(y_train, exog_train)

    test_mape_v, test_mae_v = _arimax_walkforward(
        y_trainval, exog_trainval, y_test, exog_test, best_order
    )

    persist_mape = _persistence_mape(y_trainval[-1], y_test)
    ma_mape      = _moving_avg_mape(y_val, y_test)
    mean_y       = y_test.mean()
    norm_mae     = test_mae_v / mean_y if mean_y > 0 else float("inf")
    rel_imp      = (persist_mape - test_mape_v) / persist_mape * 100

    print("\n" + "─" * 50)
    print("EVALUATION ON TEST SET  (ARIMAX — walk-forward)")
    print("─" * 50)
    print(f"  Test MAPE (ARIMAX)     : {test_mape_v:.2f}%")
    print(f"  Test MAE  (normalised) : {norm_mae:.4f}")
    print(f"  Persistence baseline   : {persist_mape:.2f}%")
    print(f"  Moving-avg baseline    : {ma_mape:.2f}%")
    print(f"  Relative improvement   : {rel_imp:.1f}% vs persistence")
    print(f"  Best ARIMAX order      : {best_order}")
    print(f"  Exog features          : {exog_cols}")
    print(f"  Sampling frequency     : {freq}")

    passed = test_mape_v < 20.0
    print(f"\n  MAPE < 20% gate        : {'PASS ✓' if passed else 'FAIL ✗  (target < 20%)'}")
    print("─" * 50)

    # Save ARIMAX — arima_model.pkl is never touched
    from statsmodels.tsa.arima.model import ARIMA
    final_result = ARIMA(
        y[1:], exog=exog[:-1], order=best_order
    ).fit()

    # Store training exog stats for runtime normalisation of proxy metrics
    exog_stats = {
        col: {"mean": float(df_exog[col].mean()), "std": float(df_exog[col].std())}
        for col in exog_cols
    }

    ARIMAX_MODEL_OUT.parent.mkdir(parents=True, exist_ok=True)
    with open(ARIMAX_MODEL_OUT, "wb") as f:
        pickle.dump({
            "result":      final_result,
            "order":       best_order,
            "freq":        freq,
            "exog_cols":   exog_cols,
            "exog_stats":  exog_stats,
        }, f)
    print(f"\n[train] ARIMAX model saved → {ARIMAX_MODEL_OUT}")
    print(f"[train] ARIMA  model untouched → {ARIMA_MODEL_OUT}")

    _append_run_log({
        "run_at": run_ts, "model": "arimax", "data_dir": str(data_dir),
        "n_total": n, "n_train": n_train, "n_val": len(y_val), "n_test": len(y_test),
        "sampling_freq": freq, "arimax_order": list(best_order), "exog_cols": exog_cols,
        "test_mape": round(test_mape_v, 4), "test_mae_normalised": round(norm_mae, 4),
        "persistence_mape": round(persist_mape, 4), "moving_avg_mape": round(ma_mape, 4),
        "rel_improvement_pct": round(rel_imp, 2), "passed": passed,
    })

    if not passed:
        print("\n[train] MAPE target not met. ARIMA model is still available as fallback.")
        sys.exit(1)


# ── run log ───────────────────────────────────────────────────────────────────

def _append_run_log(entry: dict) -> None:
    TRAIN_LOG.parent.mkdir(parents=True, exist_ok=True)
    history: list = []
    if TRAIN_LOG.exists():
        try:
            history = json.loads(TRAIN_LOG.read_text())
        except (json.JSONDecodeError, OSError):
            history = []
    history.append(entry)
    TRAIN_LOG.write_text(json.dumps(history, indent=2))
    print(f"[train] run appended → {TRAIN_LOG}")


# ── entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Train SmartLoad forecasting model")
    parser.add_argument("--data-dir", default="datasets/alibaba/",
                        help="Directory or CSV file containing Alibaba data")
    parser.add_argument("--model", choices=["arima", "arimax", "prophet"],
                        default="arima", help="Model to train (default: arima)")
    args = parser.parse_args()

    if args.model == "arima":
        train_arima(args.data_dir)
    elif args.model == "arimax":
        train_arimax(args.data_dir)
    else:
        train_prophet(args.data_dir)
