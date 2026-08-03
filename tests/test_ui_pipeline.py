"""Locks for the V023 UI pipeline: severity triage order, timestamp-column
detection, Styler row cap + printf fallback, heatmap cap."""

from __future__ import annotations

import pandas as pd
import pytest

st = pytest.importorskip("streamlit")

from app.ui.charts import HEATMAP_MAX_ROWS  # noqa: E402
from app.ui.components import (  # noqa: E402
    _PRINTF_EQUIV,
    SEVERITY_RANK,
    STYLER_MAX_ROWS,
    _auto_formats,
    _duration_display_format,
    severity_sort,
    timestampish_columns,
)


def test_severity_sort_triage_order():
    df = pd.DataFrame({
        "SEVERITY": ["INFO", "CRITICAL", "HIGH", "CRITICAL"],
        "RAISED_AT": pd.to_datetime(["2026-07-07 12:00", "2026-07-07 09:00",
                                     "2026-07-07 11:00", "2026-07-07 10:00"]),
    })
    out = severity_sort(df)
    assert list(out["SEVERITY"]) == ["CRITICAL", "CRITICAL", "HIGH", "INFO"]
    # newest CRITICAL first within the tier
    assert out.iloc[0]["RAISED_AT"].hour == 10


def test_severity_sort_noop_without_column():
    df = pd.DataFrame({"X": [1, 2]})
    assert severity_sort(df) is df


def test_severity_rank_covers_the_catalog():
    assert set(SEVERITY_RANK) == {"CRITICAL", "HIGH", "MEDIUM", "LOW", "INFO"}


def test_timestampish_detection():
    cols = ["RAISED_AT", "ACK_AT", "START_TIME", "HOUR_TS", "LAST_DML",
            "LAST_READ", "NEWEST", "AT", "USD", "STATUS", "RATE_LIMIT"]
    hits = set(timestampish_columns(cols))
    assert {"RAISED_AT", "ACK_AT", "START_TIME", "HOUR_TS", "LAST_DML",
            "LAST_READ", "NEWEST", "AT"} == hits


def test_printf_equivalents_cover_auto_formats():
    df = pd.DataFrame({"SPEND_USD": [1.0], "CREDITS_TOTAL": [1.0],
                       "IDLE_PCT": [1.0], "QUERY_COUNT": [1], "LATENCY_MS": [1200.0]})
    fmts = _auto_formats(df, set())
    # rec26: duration columns produce a Styler CALLABLE (humanized H/M/S display);
    # the large-frame path skips callables (renders raw-numeric). Every STRING format,
    # though, must still have a printf equivalent for that Arrow-native path.
    string_fmts = {f for f in fmts.values() if isinstance(f, str)}
    assert string_fmts <= set(_PRINTF_EQUIV), fmts
    assert fmts["SPEND_USD"] == "${:,.2f}"
    assert callable(fmts["LATENCY_MS"])


def test_large_frame_duration_columns_carry_their_unit():
    # rec26 regression guard: on the >400-row Arrow path a duration column can't be
    # humanized (no printf for "1h 30m"), so its value must carry the unit instead —
    # otherwise the header (which drops the token) leaves a unitless bare number.
    assert _duration_display_format("ELAPSED_MS") == "%.0f ms"
    assert _duration_display_format("QUEUED_SEC") == "%.1f s"
    assert _duration_display_format("AVG_SEC") == "%.1f s"


def test_pipeline_constants_sane():
    assert 200 <= STYLER_MAX_ROWS <= 5000        # floor lowered with r13 #19 (Styler is per-cell Python)
    assert 10 <= HEATMAP_MAX_ROWS <= 40
