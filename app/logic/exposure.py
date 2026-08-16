"""rec#25: outbound data-exposure governance — classify shares by who can see them.

Pure module (no Streamlit, no clock, no network). Takes the client-side frame from
``SHOW SHARES`` and answers "which of our objects are exposed to whom": outbound
shares are the exposure surface, the ``to`` column lists consumer accounts, and a
share published as a marketplace listing (``listing_global_name`` set) is broad by
construction even when ``to`` is empty. Inbound shares are data we *consume*, kept
only for context so the surface count is honest.

Staleness-by-last-access (ORGANIZATION_USAGE.LISTING_ACCESS_HISTORY) and the
per-object ``SHOW GRANTS TO SHARE`` drill are the natural next slices; this one
establishes the who-can-see-what inventory from a single metadata read.
"""

from __future__ import annotations

import re

import pandas as pd

# Output schema (uppercase, matching the app's table convention).
EXPOSURE_COLUMNS = (
    "SHARE_NAME",
    "KIND",
    "DATABASE",
    "OWNER",
    "CONSUMER_COUNT",
    "CONSUMERS",
    "LISTING",
    "EXPOSURE",
    "CREATED",
)

# EXPOSURE labels, ordered most- to least-exposing for stable sorting.
_EXPOSURE_RANK = {
    "LISTING": 0,      # published to the marketplace/org listing — potentially public
    "BROAD": 1,        # shared directly to many consumer accounts
    "DIRECT": 2,       # shared to at least one consumer account
    "NO CONSUMERS": 3,  # outbound share granted to nobody yet (hygiene, latent)
    "INBOUND": 4,      # data we consume, not expose
}

_EMPTY = pd.DataFrame({name: pd.Series(dtype="object") for name in EXPOSURE_COLUMNS})


def parse_consumers(value: object) -> tuple[str, ...]:
    """Split a ``SHOW SHARES`` ``to`` field into distinct consumer accounts.

    The column is free-form across Snowflake versions (comma- or space-separated,
    sometimes bracketed/quoted), so parse defensively and drop empties. An absent
    or placeholder value yields no consumers.
    """
    if value is None:
        return ()
    text = str(value).strip()
    if not text or text.lower() in {"none", "nan", "null", "[]", "()", "[ ]"}:
        return ()
    text = text.strip("[](){}")
    out: list[str] = []
    seen: set[str] = set()
    for part in re.split(r"[,\s]+", text):
        token = part.strip().strip("\"'")
        if token and token.upper() not in seen:
            seen.add(token.upper())
            out.append(token)
    return tuple(out)


def _column(frame: pd.DataFrame, name: str) -> pd.Series:
    """Fetch a lowercased column, or an all-None series when it is absent."""
    if name in frame.columns:
        return frame[name]
    return pd.Series([None] * len(frame), index=frame.index, dtype="object")


def classify_share_exposure(frame: pd.DataFrame | None, broad_threshold: int = 3) -> pd.DataFrame:
    """Turn a ``SHOW SHARES`` frame into a ranked exposure inventory.

    ``broad_threshold`` is the consumer count at or above which a directly-shared
    outbound share is flagged BROAD. Column names are matched case-insensitively;
    missing columns degrade to blank rather than raising, and an empty/None input
    returns the empty schema so callers can render "nothing exposed" honestly.
    """
    if frame is None or getattr(frame, "empty", True):
        return _EMPTY.copy()

    src = frame.copy()
    src.columns = [str(col).strip().lower() for col in src.columns]

    kind = _column(src, "kind").map(lambda v: str(v or "").strip().upper())
    listing = _column(src, "listing_global_name").map(lambda v: str(v or "").strip())
    consumers = _column(src, "to").map(parse_consumers)
    # CONSUMER_COUNT is only meaningful when the account can see the `to` column;
    # its absence is unknown (NA), never a confident zero.
    has_to = "to" in src.columns
    consumer_count = consumers.map(len) if has_to else pd.Series([pd.NA] * len(src), index=src.index)

    out = pd.DataFrame(
        {
            "SHARE_NAME": _column(src, "name").map(lambda v: str(v or "").strip()),
            "KIND": kind.where(kind != "", "OUTBOUND"),
            "DATABASE": _column(src, "database_name").map(lambda v: str(v or "").strip()),
            "OWNER": _column(src, "owner").map(lambda v: str(v or "").strip()),
            "CONSUMER_COUNT": pd.array(consumer_count, dtype="Int64"),
            "CONSUMERS": consumers.map(lambda tup: ", ".join(tup)),
            "LISTING": listing,
            "CREATED": _column(src, "created_on"),
        }
    )

    def _exposure(row: pd.Series) -> str:
        if row["KIND"] == "INBOUND":
            return "INBOUND"
        if row["LISTING"]:
            return "LISTING"
        count = row["CONSUMER_COUNT"]
        if pd.isna(count):
            # `to` unavailable but the share is outbound: report the surface as
            # DIRECT rather than a false "no consumers".
            return "DIRECT"
        if count >= broad_threshold:
            return "BROAD"
        if count >= 1:
            return "DIRECT"
        return "NO CONSUMERS"

    out["EXPOSURE"] = out.apply(_exposure, axis=1)
    out["_RANK"] = out["EXPOSURE"].map(_EXPOSURE_RANK).fillna(len(_EXPOSURE_RANK))
    out = out.sort_values(
        ["_RANK", "CONSUMER_COUNT", "SHARE_NAME"],
        ascending=[True, False, True],
        na_position="last",
    ).drop(columns="_RANK")
    return out[list(EXPOSURE_COLUMNS)].reset_index(drop=True)


def summarize_exposure(frame: pd.DataFrame | None) -> dict[str, int]:
    """KPI counts over a classified exposure frame (from classify_share_exposure).

    ``consumer_accounts`` counts DISTINCT consumer accounts reached across every
    outbound share, so the same partner shared two databases is counted once.
    """
    empty = {
        "outbound": 0,
        "inbound": 0,
        "listings": 0,
        "broad": 0,
        "consumer_accounts": 0,
    }
    if frame is None or getattr(frame, "empty", True):
        return empty
    outbound = frame[frame["KIND"] != "INBOUND"]
    accounts: set[str] = set()
    for consumers in frame.loc[frame["KIND"] != "INBOUND", "CONSUMERS"]:
        for token in parse_consumers(consumers):
            accounts.add(token.upper())
    return {
        "outbound": len(outbound),
        "inbound": int((frame["KIND"] == "INBOUND").sum()),
        "listings": int((frame["EXPOSURE"] == "LISTING").sum()),
        "broad": int((frame["EXPOSURE"] == "BROAD").sum()),
        "consumer_accounts": len(accounts),
    }
