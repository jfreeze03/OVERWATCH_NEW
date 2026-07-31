"""Robust anomaly detection for spend/usage series.

Median/MAD z-scores (not mean/std) so a single spike day cannot hide itself by
inflating the baseline. Pure functions over pandas frames; no Streamlit.
"""

from __future__ import annotations

import pandas as pd

# Standard-normal consistency constants (Iglewicz & Hoaglin modified z-scores).
_MAD_K = 0.6745
_MEANAD_K = 0.7979
DEFAULT_THRESHOLD = 3.5


def complete_days_only(df: pd.DataFrame, day_col: str = "DAY") -> pd.DataFrame:
    """Drop the current, still-growing day before anomaly scoring (bug round 2 B4).

    FACT_WAREHOUSE_DAILY carries a partial row for today that grows through the
    day, so a steady warehouse's part-day spend scores as a low outlier and stamps
    a recurring false SEVERITY=HIGH every morning. The server twin SP_ANOMALY_SWEEP
    already bounds DAY < CURRENT_DATE(); this mirrors it for the app-side scorers.
    Callers keep the full frame for trend charts.
    """
    from app.logic.formulas import account_today
    if df is None or df.empty or day_col not in df.columns:
        return df
    return df[pd.to_datetime(df[day_col], errors="coerce").dt.date < account_today()]


def robust_zscores(values: pd.Series) -> pd.Series:
    """Return modified z-scores; zeros when there is no dispersion or <5 points.

    Median/MAD primary; when MAD collapses (>50% identical points) fall back to
    mean absolute deviation around the median per Iglewicz & Hoaglin — never to
    std, which a single spike inflates enough to hide itself.
    """
    series = pd.to_numeric(values, errors="coerce").astype(float)
    scores = pd.Series(0.0, index=series.index)
    clean = series.dropna()
    if len(clean) < 5:
        return scores
    median = clean.median()
    abs_dev = (clean - median).abs()
    mad = abs_dev.median()
    if mad > 0:
        scores.loc[clean.index] = _MAD_K * (clean - median) / mad
        return scores.fillna(0.0)
    mean_ad = abs_dev.mean()
    if mean_ad > 0:
        scores.loc[clean.index] = _MEANAD_K * (clean - median) / mean_ad
    return scores.fillna(0.0)


def flag_anomalies(
    df: pd.DataFrame,
    value_col: str,
    group_col: str | None = None,
    threshold: float = DEFAULT_THRESHOLD,
) -> pd.DataFrame:
    """Return a copy with ``Z_SCORE`` and ``IS_ANOMALY`` columns.

    With ``group_col`` (e.g. warehouse), each group gets its own baseline so a
    naturally large warehouse does not mask a small one's spike.
    """
    out = df.copy()
    if out.empty or value_col not in out.columns:
        out["Z_SCORE"] = pd.Series(dtype=float)
        out["IS_ANOMALY"] = pd.Series(dtype=bool)
        return out
    if group_col and group_col in out.columns:
        out["Z_SCORE"] = (
            out.groupby(group_col, dropna=False)[value_col]
            .transform(lambda s: robust_zscores(s))
            .fillna(0.0)
        )
    else:
        out["Z_SCORE"] = robust_zscores(out[value_col])
    out["IS_ANOMALY"] = out["Z_SCORE"].abs() >= float(threshold)
    return out


def anomaly_summary(df: pd.DataFrame, label_col: str, value_col: str,
                    day_col: str = "DAY") -> list[dict]:
    """Compact anomaly rows for KPI/alert surfaces, strongest first.

    Each row carries its ``day`` (the value of ``day_col``, or None when the frame
    has no such column) so callers can age one-off spikes out instead of re-firing a
    stale, dateless anomaly every rerun (r6-bug5)."""
    if df.empty or "IS_ANOMALY" not in df.columns:
        return []
    hits = df[df["IS_ANOMALY"]].copy()
    if hits.empty:
        return []
    hits = hits.reindex(hits["Z_SCORE"].abs().sort_values(ascending=False).index)
    _has_day = day_col in hits.columns
    return [
        {
            "label": str(row.get(label_col, "")),
            "value": float(row.get(value_col, 0.0) or 0.0),
            "z": round(float(row.get("Z_SCORE", 0.0) or 0.0), 1),
            "day": row.get(day_col) if _has_day else None,
        }
        for _, row in hits.head(10).iterrows()
    ]
