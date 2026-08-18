"""rec#25: outbound data-exposure governance — share inventory + classification.

Locks the pure classifier (logic/exposure.py) against realistic SHOW SHARES
shapes and confirms the Security "Exposure" tab is wired.
"""

from pathlib import Path

import pandas as pd

from app.data import security_sql
from app.logic.exposure import (
    EXPOSURE_COLUMNS,
    classify_share_exposure,
    parse_consumers,
    summarize_exposure,
)


def _shares(rows):
    """Build a SHOW SHARES-shaped frame (columns as Snowflake returns them)."""
    return pd.DataFrame(rows, columns=[
        "created_on", "kind", "name", "database_name", "to",
        "owner", "comment", "listing_global_name",
    ])


# --- parse_consumers -------------------------------------------------------

def test_parse_consumers_handles_separators_and_noise():
    assert parse_consumers("ORG.ACCT1, ORG.ACCT2") == ("ORG.ACCT1", "ORG.ACCT2")
    assert parse_consumers("ORG.ACCT1 ORG.ACCT2") == ("ORG.ACCT1", "ORG.ACCT2")
    assert parse_consumers('["ORG.ACCT1","ORG.ACCT2"]') == ("ORG.ACCT1", "ORG.ACCT2")
    assert parse_consumers("") == ()
    assert parse_consumers(None) == ()
    assert parse_consumers("[]") == ()
    # de-dups case-insensitively but keeps first spelling
    assert parse_consumers("ORG.ACCT1, org.acct1") == ("ORG.ACCT1",)


# --- classify_share_exposure ----------------------------------------------

def test_empty_or_none_returns_empty_schema():
    for frame in (None, pd.DataFrame()):
        out = classify_share_exposure(frame)
        assert out.empty
        assert list(out.columns) == list(EXPOSURE_COLUMNS)


def test_exposure_labels_by_reach():
    frame = _shares([
        ("2024-01-01", "OUTBOUND", "S_LISTING", "DB1", "", "R", "", "GLOBAL.LISTING"),
        ("2024-01-01", "OUTBOUND", "S_BROAD", "DB2", "A, B, C", "R", "", ""),
        ("2024-01-01", "OUTBOUND", "S_DIRECT", "DB3", "A", "R", "", ""),
        ("2024-01-01", "OUTBOUND", "S_NONE", "DB4", "", "R", "", ""),
        ("2024-01-01", "INBOUND", "S_IN", "DB5", "", "PROVIDER_ACCT", "", ""),
    ])
    out = classify_share_exposure(frame).set_index("SHARE_NAME")
    assert out.loc["S_LISTING", "EXPOSURE"] == "LISTING"
    assert out.loc["S_BROAD", "EXPOSURE"] == "BROAD"
    assert out.loc["S_DIRECT", "EXPOSURE"] == "DIRECT"
    assert out.loc["S_NONE", "EXPOSURE"] == "NO CONSUMERS"
    assert out.loc["S_IN", "EXPOSURE"] == "INBOUND"
    assert int(out.loc["S_BROAD", "CONSUMER_COUNT"]) == 3
    assert int(out.loc["S_NONE", "CONSUMER_COUNT"]) == 0


def test_missing_to_column_is_unknown_not_zero():
    # A frame without the `to` column must not read as "no consumers".
    frame = pd.DataFrame(
        [("OUTBOUND", "S1", "DB1", "R", "")],
        columns=["kind", "name", "database_name", "owner", "listing_global_name"],
    )
    out = classify_share_exposure(frame)
    assert pd.isna(out.loc[0, "CONSUMER_COUNT"])
    # outbound + unknown reach → DIRECT (a surface exists), never "NO CONSUMERS"
    assert out.loc[0, "EXPOSURE"] == "DIRECT"


def test_column_case_insensitivity():
    frame = _shares([("2024-01-01", "OUTBOUND", "S1", "DB1", "A, B, C", "R", "", "")])
    frame.columns = [c.upper() for c in frame.columns]
    out = classify_share_exposure(frame)
    assert out.loc[0, "EXPOSURE"] == "BROAD"
    assert out.loc[0, "SHARE_NAME"] == "S1"


def test_sort_puts_most_exposing_first_inbound_last():
    frame = _shares([
        ("2024-01-01", "INBOUND", "S_IN", "DB5", "", "P", "", ""),
        ("2024-01-01", "OUTBOUND", "S_NONE", "DB4", "", "R", "", ""),
        ("2024-01-01", "OUTBOUND", "S_LISTING", "DB1", "", "R", "", "G.L"),
    ])
    out = classify_share_exposure(frame)
    assert list(out["SHARE_NAME"]) == ["S_LISTING", "S_NONE", "S_IN"]


def test_broad_threshold_is_tunable():
    frame = _shares([("2024-01-01", "OUTBOUND", "S", "DB", "A, B", "R", "", "")])
    assert classify_share_exposure(frame, broad_threshold=2).loc[0, "EXPOSURE"] == "BROAD"
    assert classify_share_exposure(frame, broad_threshold=3).loc[0, "EXPOSURE"] == "DIRECT"


# --- summarize_exposure ----------------------------------------------------

def test_summarize_counts_distinct_consumers_across_outbound():
    frame = _shares([
        ("2024-01-01", "OUTBOUND", "S1", "DB1", "A, B", "R", "", ""),
        ("2024-01-01", "OUTBOUND", "S2", "DB2", "B, C", "R", "", ""),   # B shared twice
        ("2024-01-01", "OUTBOUND", "S3", "DB3", "", "R", "", "G.L"),
        ("2024-01-01", "INBOUND", "S_IN", "DB4", "", "P", "", ""),
    ])
    out = classify_share_exposure(frame)
    stats = summarize_exposure(out)
    assert stats["outbound"] == 3
    assert stats["inbound"] == 1
    assert stats["listings"] == 1
    assert stats["consumer_accounts"] == 3   # A, B, C distinct


def test_summarize_empty():
    assert summarize_exposure(pd.DataFrame())["outbound"] == 0
    assert summarize_exposure(None)["consumer_accounts"] == 0


# --- SQL builder + wiring --------------------------------------------------

def test_show_shares_builder():
    assert security_sql.show_shares_sql() == "SHOW SHARES LIMIT 500"


def test_exposure_tab_wired_on_security_page():
    src = (Path(__file__).resolve().parents[2] / "app" / "ui" / "pages"
           / "security.py").read_text(encoding="utf-8")
    assert "def _exposure_tab(" in src
    assert '"Exposure"' in src
    assert "classify_share_exposure(" in src
    assert "show_shares_sql()" in src
