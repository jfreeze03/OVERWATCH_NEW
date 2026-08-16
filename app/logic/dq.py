"""rec#26: data-quality row-volume anomaly scoring (robust-z on rows-added).

Pure module (no Streamlit, no clock, no network). Takes the daily rows-added
series from data/dq_sql.product_row_volume and scores each registered table's
most recent LOAD against its own prior loads with a robust z-score (median / MAD)
— outlier-resistant, so one earlier spike can't mask today's anomaly, and it
catches both bloated and thin loads without a fixed percentage cliff.

Scope is deliberately "was the volume anomalous *when the table loaded*", not "did
the table load": comparing only the days a table actually added rows (rows-added
> 0) sidesteps the day-of-week trap entirely — a business-day table is never
falsely flagged because the newest data across the account happens to land on a
weekend. The complementary "a load was expected and didn't run" question is
already answered by the freshness-SLA and Volume-drops panels next to this one.
Null-rate-spike and schema-drift monitors (which need a stored baseline) and the
DQ_BREACH alert are the deferred owner-migration halves.
"""

from __future__ import annotations

import pandas as pd

ANOMALY_COLUMNS = (
    "FQN",
    "DATA_PRODUCT",
    "OWNER_NAME",
    "CRITICALITY",
    "LATEST_DAY",
    "DAYS_STALE",
    "LATEST_ROWS",
    "BASELINE_MEDIAN",
    "ROBUST_Z",
    "DIRECTION",
    "FLAGGED",
)

_MAD_SCALE = 1.4826  # MAD -> std-dev-equivalent for a normal distribution
_Z_DISPLAY_CAP = 99.9  # keep an extreme z (near-zero scale, big change) renderable
# Floor the dispersion at a fraction of the median so a majority-modal baseline
# (MAD collapses to 0 whenever >50% of loads share the modal value) can't blow a
# within-range jitter up to a max-severity z. With z_threshold 3.5 this makes the
# effective flag threshold on a rock-steady table ~50% deviation, not 1 row.
_SCALE_FLOOR_FRAC = 0.15
_STALE_DAYS = 3  # a scored table whose newest load is this many days behind the
#                  freshest registered load is marked stale (assessed on old data)

_EMPTY = pd.DataFrame({name: pd.Series(dtype="object") for name in ANOMALY_COLUMNS})


def _meta(group: pd.DataFrame, col: str) -> object:
    """First non-null catalog attribute for a table (constant within the group)."""
    if col not in group.columns:
        return ""
    non_null = group[col].dropna()
    return non_null.iloc[0] if len(non_null) else ""


def row_volume_anomalies(
    frame: pd.DataFrame | None,
    min_baseline_loads: int = 10,
    z_threshold: float = 3.5,
    min_median_rows: float = 100.0,
) -> pd.DataFrame:
    """Score each registered table's most recent load of rows-added by robust z.

    Input columns (from product_row_volume): FQN, DAY, ROWS_ADDED, DATA_PRODUCT,
    OWNER_NAME, CRITICALITY. Only days that actually added rows (rows-added > 0)
    count as loads. A table is scored only when it has at least
    ``min_baseline_loads`` prior loads and a baseline median >= ``min_median_rows``
    — otherwise there is no basis and it is omitted, never reported healthy.
    Returns one row per scored table with LATEST_DAY / LATEST_ROWS /
    BASELINE_MEDIAN / ROBUST_Z (capped for display) / DIRECTION (SPIKE / DROP /
    NORMAL) / FLAGGED, sorted by |z| descending.
    """
    if frame is None or getattr(frame, "empty", True):
        return _EMPTY.copy()

    df = frame.copy()
    df["DAY"] = pd.to_datetime(df["DAY"], errors="coerce")
    df["ROWS_ADDED"] = pd.to_numeric(df["ROWS_ADDED"], errors="coerce").fillna(0.0)
    df = df.dropna(subset=["DAY"])
    if df.empty:
        return _EMPTY.copy()

    freshest = df.loc[df["ROWS_ADDED"] > 0, "DAY"].max()   # newest load anywhere — staleness ref
    rows: list[dict] = []
    for fqn, group in df.groupby("FQN"):
        daily = group.groupby("DAY")["ROWS_ADDED"].sum().sort_index()
        loads = daily[daily > 0]
        if len(loads) < min_baseline_loads + 1:   # need a baseline plus the latest load
            continue
        latest_day = loads.index[-1]
        latest = float(loads.iloc[-1])
        baseline = loads.iloc[:-1]
        median = float(baseline.median())
        if median < min_median_rows:
            continue  # not a material mover — no basis to judge

        # Robust z with a dispersion FLOOR: a majority-modal baseline drives MAD to
        # 0, which would otherwise flag any within-range jitter at max severity.
        mad = float((baseline - median).abs().median()) * _MAD_SCALE
        scale = max(mad, _SCALE_FLOOR_FRAC * median)
        z = (latest - median) / scale
        z = max(-_Z_DISPLAY_CAP, min(_Z_DISPLAY_CAP, z))

        if z >= z_threshold:
            direction = "SPIKE"
        elif z <= -z_threshold:
            direction = "DROP"
        else:
            direction = "NORMAL"

        rows.append({
            "FQN": fqn,
            "DATA_PRODUCT": _meta(group, "DATA_PRODUCT"),
            "OWNER_NAME": _meta(group, "OWNER_NAME"),
            "CRITICALITY": _meta(group, "CRITICALITY"),
            "LATEST_DAY": latest_day.date().isoformat(),
            "DAYS_STALE": int((freshest - latest_day).days),
            "LATEST_ROWS": round(latest),
            "BASELINE_MEDIAN": round(median),
            "ROBUST_Z": round(z, 1),
            "DIRECTION": direction,
            "FLAGGED": direction != "NORMAL",
        })

    if not rows:
        return _EMPTY.copy()
    out = pd.DataFrame(rows, columns=list(ANOMALY_COLUMNS))
    out["_ABS_Z"] = out["ROBUST_Z"].abs()
    out = out.sort_values(["_ABS_Z", "FQN"], ascending=[False, True]).drop(columns="_ABS_Z")
    return out.reset_index(drop=True)


def summarize_row_volume(frame: pd.DataFrame | None) -> dict[str, int]:
    """KPI counts over a scored anomaly frame (from row_volume_anomalies).

    ``stale`` counts tables whose newest load is >= _STALE_DAYS behind the freshest
    registered load — scored on old data, so the freshness panel is the live view.
    """
    empty = {"monitored": 0, "flagged": 0, "spikes": 0, "drops": 0, "products": 0, "stale": 0}
    if frame is None or getattr(frame, "empty", True):
        return empty
    return {
        "monitored": len(frame),
        "flagged": int(frame["FLAGGED"].sum()),
        "spikes": int((frame["DIRECTION"] == "SPIKE").sum()),
        "drops": int((frame["DIRECTION"] == "DROP").sum()),
        "products": int(frame.loc[frame["FLAGGED"], "DATA_PRODUCT"].nunique()),
        "stale": int((frame["DAYS_STALE"] >= _STALE_DAYS).sum()),
    }
