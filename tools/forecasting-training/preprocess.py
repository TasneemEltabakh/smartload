"""
services/forecasting/preprocess.py
────────────────────────────────────
Load Alibaba microservice trace data from a directory of CSVs and produce a
clean Prophet-ready DataFrame with columns (ds, y) at 1-minute resolution.

Supports two common Alibaba dataset layouts:
  A) Pre-aggregated: one row per time bucket, single numeric value column.
  B) Call-trace / event log: one row per request, timestamp + optional count.
     Aggregated here by counting rows (or summing a count column) per minute.

Usage:
    from preprocess import load_alibaba
    df = load_alibaba("datasets/alibaba/")   # returns pd.DataFrame (ds, y)
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

# Candidate column names for timestamps (tried in order, first match wins)
_TS_CANDIDATES = [
    "timestamp", "time", "start_time", "starttime", "time_stamp",
    "ts", "datetime", "date_time", "bucket", "ds",
    "telemetry_timestamp",
]

# Common resampling frequencies (in minutes) — snap the detected interval to
# the nearest entry so Prophet always sees a clean, regular grid.
_COMMON_FREQ_MINUTES = [1, 5, 10, 15, 30, 60]


def _detect_freq(df: pd.DataFrame, ts_col: str) -> str:
    """Return a pandas offset string for the natural sampling interval."""
    deltas = df[ts_col].diff().dropna()
    median_min = deltas.median().total_seconds() / 60
    # Snap to nearest common frequency
    best = min(_COMMON_FREQ_MINUTES, key=lambda f: abs(f - median_min))
    return f"{best}min"

# Candidate column names for the value to forecast (request_rate proxy)
_VAL_CANDIDATES = [
    "request_count", "request_rate", "rps", "count", "call_count",
    "num_calls", "requests", "rt_count", "y",
]


def _find_col(cols: list[str], candidates: list[str]) -> str | None:
    lower = [c.lower() for c in cols]
    for cand in candidates:
        if cand in lower:
            return cols[lower.index(cand)]
    return None


def load_alibaba(
    data_dir: str | Path,
    *,
    verbose: bool = True,
    clip_outliers: bool = True,
    outlier_sigma: float = 2.0,
) -> tuple[pd.DataFrame, str]:
    """
    Load all CSVs in `data_dir`, auto-detect timestamp + value columns,
    resample to the natural sampling interval, and return:
      (df, freq) where df has columns (ds, y) and freq is a pandas offset
      string like '5min' that callers can pass to make_future_dataframe.

    clip_outliers: clip y values beyond outlier_sigma standard deviations
                   from the mean (reduces spike impact on model fitting).

    Raises ValueError if no usable files are found.
    """
    data_dir = Path(data_dir)
    if data_dir.is_file():
        csv_files = [data_dir]
    else:
        csv_files = sorted(data_dir.glob("*.csv"))
    if not csv_files:
        raise ValueError(f"No CSV files found in {data_dir}")

    frames: list[pd.DataFrame] = []
    for csv_path in csv_files:
        df_raw = pd.read_csv(csv_path, low_memory=False)
        if verbose:
            print(f"[preprocess] {csv_path.name}: {len(df_raw):,} rows, "
                  f"columns = {list(df_raw.columns)}")

        ts_col = _find_col(list(df_raw.columns), _TS_CANDIDATES)
        val_col = _find_col(list(df_raw.columns), _VAL_CANDIDATES)

        if ts_col is None:
            print(f"[preprocess] WARNING: cannot detect timestamp column in "
                  f"{csv_path.name}. Available: {list(df_raw.columns)}")
            print("  Set TS_COL env var or rename the column to 'timestamp'.")
            continue

        df_raw[ts_col] = _parse_timestamps(df_raw[ts_col])
        df_raw = df_raw.dropna(subset=[ts_col])
        df_raw = df_raw.sort_values(ts_col)

        freq = _detect_freq(df_raw, ts_col)
        if verbose:
            print(f"[preprocess] using ts_col='{ts_col}', "
                  f"val_col='{val_col or '(count rows)'}', "
                  f"detected_freq='{freq}'")

        df_raw = df_raw.set_index(ts_col).sort_index()

        if val_col:
            # Pre-aggregated rate/count column: average within each bucket,
            # drop empty buckets (never zero-pad — sparse gaps corrupt training).
            series = pd.to_numeric(df_raw[val_col], errors="coerce")
            resampled = series.resample(freq).mean().dropna()
        else:
            # Event log: count rows per bucket as a proxy for request_rate
            resampled = df_raw.resample(freq).size().astype(float)
            resampled = resampled.rename("request_rate")
            resampled = resampled[resampled > 0]

        resampled = resampled.clip(lower=0)

        chunk = pd.DataFrame({"ds": resampled.index, "y": resampled.values})
        # Ensure timezone-naive UTC (Prophet requires naive datetimes)
        chunk["ds"] = chunk["ds"].dt.tz_localize(None)
        frames.append(chunk)

    if not frames:
        raise ValueError("No usable data could be extracted. "
                         "Check column names and file format.")

    df = pd.concat(frames, ignore_index=True)
    df = df.sort_values("ds").drop_duplicates(subset=["ds"]).reset_index(drop=True)

    if clip_outliers:
        mu, sigma = df["y"].mean(), df["y"].std()
        lo, hi = mu - outlier_sigma * sigma, mu + outlier_sigma * sigma
        clipped = df["y"].clip(lower=lo, upper=hi)
        n_clipped = (clipped != df["y"]).sum()
        df["y"] = clipped
        if verbose and n_clipped > 0:
            print(f"[preprocess] clipped {n_clipped} outliers "
                  f"(±{outlier_sigma}σ → [{lo:.2f}, {hi:.2f}])")

    if verbose:
        print(f"[preprocess] total samples after {freq} resample: {len(df):,}")
        print(f"[preprocess] time range: {df['ds'].min()} → {df['ds'].max()}")
        print(f"[preprocess] y stats: min={df['y'].min():.2f}, "
              f"mean={df['y'].mean():.2f}, max={df['y'].max():.2f}")

    return df, freq


def _parse_timestamps(series: pd.Series) -> pd.Series:
    """
    Try to parse a timestamp column that could be:
      - ISO strings ("2021-01-01 00:00:00")
      - Unix seconds (1609459200)
      - Unix milliseconds (1609459200000)
    """
    if pd.api.types.is_numeric_dtype(series):
        sample = series.dropna().iloc[0] if not series.dropna().empty else 0
        # Unix ms if value > year 3000 in seconds (> ~32 billion)
        if sample > 32_503_680_000:
            return pd.to_datetime(series, unit="ms", errors="coerce", utc=True)
        return pd.to_datetime(series, unit="s", errors="coerce", utc=True)
    return pd.to_datetime(series, infer_datetime_format=True, errors="coerce", utc=True)


# Candidate exogenous column names (all numeric columns except the target)
_EXOG_CANDIDATES = ["avg_cpu_util", "cpu_util", "cpu", "system_load", "load",
                    "mem_util", "memory_util", "error_rate", "request_latency_ms"]


def load_alibaba_with_exog(
    data_dir: str | Path,
    *,
    verbose: bool = True,
    clip_outliers: bool = True,
    outlier_sigma: float = 2.0,
) -> tuple[pd.DataFrame, pd.DataFrame, str]:
    """
    Like load_alibaba() but also returns exogenous features aligned to the
    same time index.

    Returns (df_y, df_exog, freq):
      df_y    — (ds, y) target DataFrame
      df_exog — (ds, col1, col2, ...) exogenous features, same rows as df_y
      freq    — pandas offset string, e.g. '5min'

    Exogenous columns are z-score normalised so ARIMAX coefficients are
    scale-independent. Raw stats are stored separately for runtime use.
    """
    data_dir = Path(data_dir)
    csv_files = [data_dir] if data_dir.is_file() else sorted(data_dir.glob("*.csv"))
    if not csv_files:
        raise ValueError(f"No CSV files found in {data_dir}")

    y_frames:    list[pd.DataFrame] = []
    exog_frames: list[pd.DataFrame] = []

    for csv_path in csv_files:
        df_raw = pd.read_csv(csv_path, low_memory=False)
        ts_col  = _find_col(list(df_raw.columns), _TS_CANDIDATES)
        val_col = _find_col(list(df_raw.columns), _VAL_CANDIDATES)

        if ts_col is None:
            continue

        df_raw[ts_col] = _parse_timestamps(df_raw[ts_col])
        df_raw = df_raw.dropna(subset=[ts_col]).sort_values(ts_col)
        freq = _detect_freq(df_raw, ts_col)
        df_raw = df_raw.set_index(ts_col).sort_index()

        # Target
        series = pd.to_numeric(df_raw[val_col], errors="coerce") if val_col \
            else df_raw.resample(freq).size().astype(float)
        resampled_y = series.resample(freq).mean().dropna().clip(lower=0)
        chunk_y = pd.DataFrame({"ds": resampled_y.index, "y": resampled_y.values})
        chunk_y["ds"] = chunk_y["ds"].dt.tz_localize(None)
        y_frames.append(chunk_y)

        # Exogenous: all numeric columns that match candidates (excluding target)
        exog_cols = [c for c in df_raw.columns
                     if c != val_col
                     and c.lower() in [x.lower() for x in _EXOG_CANDIDATES]
                     and pd.api.types.is_numeric_dtype(df_raw[c])]
        if exog_cols:
            exog_chunk = pd.DataFrame({"ds": resampled_y.index})
            for col in exog_cols:
                s = pd.to_numeric(df_raw[col], errors="coerce")
                resampled_col = s.resample(freq).mean()
                exog_chunk[col] = resampled_col.reindex(resampled_y.index).values
            exog_chunk["ds"] = exog_chunk["ds"].dt.tz_localize(None)
            exog_frames.append(exog_chunk)

    if not y_frames:
        raise ValueError("No usable data could be extracted.")

    df_y = pd.concat(y_frames, ignore_index=True)
    df_y = df_y.sort_values("ds").drop_duplicates("ds").reset_index(drop=True)

    if clip_outliers:
        mu, sigma = df_y["y"].mean(), df_y["y"].std()
        lo, hi = mu - outlier_sigma * sigma, mu + outlier_sigma * sigma
        n_clipped = (df_y["y"].clip(lo, hi) != df_y["y"]).sum()
        df_y["y"] = df_y["y"].clip(lo, hi)
        if verbose and n_clipped > 0:
            print(f"[preprocess] clipped {n_clipped} outliers (±{outlier_sigma}σ)")

    if exog_frames:
        df_exog = pd.concat(exog_frames, ignore_index=True)
        df_exog = df_exog.sort_values("ds").drop_duplicates("ds").reset_index(drop=True)
        # Align exog to y index (inner join on ds)
        df_merged = df_y.merge(df_exog, on="ds", how="inner")
        df_y    = df_merged[["ds", "y"]].copy()
        exog_feature_cols = [c for c in df_merged.columns if c not in ("ds", "y")]
        df_exog = df_merged[["ds"] + exog_feature_cols].copy()
        # Z-score normalise
        for col in exog_feature_cols:
            mu_e  = df_exog[col].mean()
            std_e = df_exog[col].std()
            df_exog[col] = (df_exog[col] - mu_e) / (std_e if std_e > 0 else 1.0)
        if verbose:
            print(f"[preprocess] exog features: {exog_feature_cols} "
                  f"(z-score normalised, {len(df_exog):,} rows)")
    else:
        df_exog = df_y[["ds"]].copy()
        if verbose:
            print("[preprocess] WARNING: no exogenous features found in dataset")

    if verbose:
        print(f"[preprocess] total samples: {len(df_y):,}  freq: {freq}")

    return df_y, df_exog, freq


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Preview Alibaba data preprocessing")
    parser.add_argument("--data-dir", default="datasets/alibaba/",
                        help="Directory containing Alibaba CSV files")
    args = parser.parse_args()

    try:
        df, freq = load_alibaba(args.data_dir, verbose=True)
        print(f"\n── First 5 rows (freq={freq}) ──")
        print(df.head())
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
