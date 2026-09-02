"""Robust anomaly detection for spend/usage series.

Median/MAD z-scores (not mean/std) so a single spike day cannot hide itself by
inflating the baseline. Pure functions over pandas frames; no Streamlit.
"""

from __future__ import annotations

from datetime import timedelta

import pandas as pd

# Standard-normal consistency constants (Iglewicz & Hoaglin modified z-scores).
_MAD_K = 0.6745
_MEANAD_K = 0.7979
# #37: FIXED app-side flag threshold. It equals the server SP_ANOMALY_SWEEP's
# DEFAULT threshold, but the sweep reads the configurable ALERT_CONFIG.THRESHOLD_NUM
# — this constant does NOT — so once the owner tunes that setting the two diverge
# and the server's COST_ANOMALY_SWEEP events (on Alerts) are the authoritative
# escalation. This app-side scorer stays a fixed-default morning-triage twin.
DEFAULT_THRESHOLD = 3.5

# Materiality gates for warehouse daily-spend anomalies. A usually-idle warehouse
# has a near-zero baseline dispersion (~$1-2), so ANY active day scores a huge
# modified-z and fires a false "investigate" on trivial dollars. The robust
# estimator can't rescue a near-zero-variance baseline — gate the flag on real
# money AND a real baseline. Callers opt in (defaults below keep the generic
# scorer unchanged); the three warehouse-USD panels pass these.
ANOMALY_MIN_USD = 50.0
ANOMALY_MIN_ACTIVE_DAYS = 10

# Known-spike calendar (repo review 2026-08-17, adaptive-compute): predictable
# spend spikes — month-end closes, quarter closes, named events — are not
# anomalies, and flagging them every cycle teaches operators to ignore the
# sweep. The calendar is a SETTINGS string (EXPECTED_SPIKE_CALENDAR) so the
# owner edits it on Admin without a migration. Format, semicolon-separated:
#   MONTH_END:<n>            last n + first n days of every month
#   QUARTER_END:<n>          last n + first n days of every quarter
#   YYYY-MM-DD..YYYY-MM-DD:<label>   explicit date range (renewal, load test)
# Suppressed days KEEP their z-score and gain EXPECTED_SPIKE=<label> — shown as
# "expected (month-end)" instead of an anomaly, never silently dropped.
DEFAULT_SPIKE_CALENDAR = "MONTH_END:1;QUARTER_END:2"


def expected_spike_labels(days: pd.Series, calendar: str | None) -> pd.Series:
    """Label each day that falls inside the known-spike calendar ('' = not expected).

    Malformed rules are skipped (a config typo must never break the sweep)."""
    labels = pd.Series("", index=days.index, dtype=str)
    text = str(calendar or "").strip()
    if not text or days.empty:
        return labels
    ts = pd.to_datetime(days, errors="coerce")
    for rule in text.split(";"):
        rule = rule.strip()
        if not rule:
            continue
        try:
            if ".." in rule:
                span, _, label = rule.partition(":")
                start_s, _, end_s = span.partition("..")
                start = pd.Timestamp(start_s.strip())
                end = pd.Timestamp(end_s.strip())
                if pd.isna(start) or pd.isna(end) or end < start:
                    continue
                hit = (ts >= start) & (ts <= end.normalize() + timedelta(days=1) - timedelta(seconds=1))
                name = (label.strip() or "scheduled event")
            else:
                kind, _, n_s = rule.partition(":")
                n = max(1, min(int(n_s or 1), 10))
                kind = kind.strip().upper()
                if kind == "MONTH_END":
                    month_len = ts.dt.days_in_month
                    hit = (ts.dt.day > month_len - n) | (ts.dt.day <= n)
                    name = "month-end"
                elif kind == "QUARTER_END":
                    q_end_month = ts.dt.month.isin((3, 6, 9, 12))
                    q_start_month = ts.dt.month.isin((1, 4, 7, 10))
                    month_len = ts.dt.days_in_month
                    hit = (q_end_month & (ts.dt.day > month_len - n)) | (q_start_month & (ts.dt.day <= n))
                    name = "quarter-end"
                else:
                    continue
        except (ValueError, TypeError):
            continue
        hit = hit.fillna(False)
        labels = labels.mask(hit & (labels == ""), name)
    return labels


def suppress_expected_spikes(flagged: pd.DataFrame, calendar: str | None,
                             day_col: str = "DAY") -> pd.DataFrame:
    """Post-filter for flag_anomalies output: an UPWARD anomaly on a calendar day
    becomes EXPECTED_SPIKE=<label> with IS_ANOMALY cleared. Collapses (z<0) are
    NEVER suppressed — a crash during month-end is still a crash. No-op without
    a calendar or the needed columns."""
    out = flagged.copy()
    if out.empty or day_col not in out.columns or "IS_ANOMALY" not in out.columns:
        if "EXPECTED_SPIKE" not in out.columns:
            # "" (not an empty Series) — assigning Series(dtype=str) to a NON-empty
            # frame writes NaN, and NaN != "" reads as "expected" downstream.
            out["EXPECTED_SPIKE"] = ""
        return out
    labels = expected_spike_labels(out[day_col], calendar)
    z = pd.to_numeric(out.get("Z_SCORE", 0), errors="coerce").fillna(0.0)
    suppress = out["IS_ANOMALY"].fillna(False) & (labels != "") & (z > 0)
    out["EXPECTED_SPIKE"] = labels.where(suppress, "")
    out["IS_ANOMALY"] = out["IS_ANOMALY"] & ~suppress
    return out


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
    *,
    min_value: float = 0.0,
    min_active_days: int = 0,
) -> pd.DataFrame:
    """Return a copy with ``Z_SCORE`` and ``IS_ANOMALY`` columns.

    With ``group_col`` (e.g. warehouse), each group gets its own baseline so a
    naturally large warehouse does not mask a small one's spike.

    ``min_value`` and ``min_active_days`` are materiality gates (default off):
    a row is only an anomaly when its own value is at least ``min_value`` AND its
    group has at least ``min_active_days`` non-zero-value rows. This stops a
    usually-idle warehouse from firing a false z+20 flag on a trivial active day
    (see ``ANOMALY_MIN_USD`` / ``ANOMALY_MIN_ACTIVE_DAYS``).
    """
    out = df.copy()
    if out.empty or value_col not in out.columns:
        out["Z_SCORE"] = pd.Series(dtype=float)
        out["IS_ANOMALY"] = pd.Series(dtype=bool)
        return out
    vals = pd.to_numeric(out[value_col], errors="coerce").fillna(0.0)
    if group_col and group_col in out.columns:
        out["Z_SCORE"] = (
            out.groupby(group_col, dropna=False)[value_col]
            .transform(lambda s: robust_zscores(s))
            .fillna(0.0)
        )
        active = (vals > 0).groupby(out[group_col], dropna=False).transform("sum")
        baseline = vals.groupby(out[group_col], dropna=False).transform("median")
    else:
        out["Z_SCORE"] = robust_zscores(out[value_col])
        active = pd.Series(float((vals > 0).sum()), index=out.index)
        baseline = pd.Series(float(vals.median()), index=out.index)
    z = out["Z_SCORE"]
    # An overspend SPIKE (z>0) must be a material DAY — don't flag a $49 spike.
    # A COLLAPSE (z<0) is low BY DEFINITION (that is the collapse), so its
    # materiality rides the entity's BASELINE: a $5k/day warehouse dropping to
    # $10 is a real stalled-pipeline signal (spend.py surfaces it), while a
    # $2/day sandbox dropping to $0 stays noise.
    material = (
        ((z > 0) & (vals >= float(min_value)))
        | ((z < 0) & (baseline >= float(min_value)))
    )
    out["IS_ANOMALY"] = (
        (z.abs() >= float(threshold))
        & material
        & (active >= float(min_active_days))
    )
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


def anomaly_markers(df: pd.DataFrame, day_col: str = "DAY",
                    label_col: str | None = None) -> pd.DataFrame:
    """Collapse flagged-anomaly rows to one spend-trend marker per day (UI15/Ov5).

    Returns a ``DAY`` (YYYY-MM-DD) + ``LABEL`` frame for
    ``charts.spend_trend(..., markers=...)``. With ``label_col`` (e.g.
    WAREHOUSE_NAME) each day's label lists up to three of that day's flagged
    entities; without it, the count. Empty frame when nothing is flagged, so the
    overlay draws no rules."""
    empty = pd.DataFrame(columns=["DAY", "LABEL"])
    if df is None or df.empty or "IS_ANOMALY" not in df.columns or day_col not in df.columns:
        return empty
    hits = df[df["IS_ANOMALY"].astype(bool)].copy()
    if hits.empty:
        return empty
    hits["DAY"] = pd.to_datetime(hits[day_col], errors="coerce").dt.strftime("%Y-%m-%d")
    hits = hits.dropna(subset=["DAY"])
    if hits.empty:
        return empty
    if label_col and label_col in hits.columns:
        out = (hits.groupby("DAY")[label_col]
               .apply(lambda s: ", ".join(list(dict.fromkeys(map(str, s)))[:3]))
               .reset_index().rename(columns={label_col: "LABEL"}))
    else:
        out = hits.groupby("DAY").size().reset_index(name="_n")
        out["LABEL"] = out["_n"].map(lambda n: f"{int(n)} anomal" + ("y" if int(n) == 1 else "ies"))
        out = out[["DAY", "LABEL"]]
    return out.reset_index(drop=True)
