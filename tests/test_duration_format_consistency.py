"""Duration display is CONSISTENT and AUTHORITATIVE, everywhere, and stays that way.

Recurring frustration: raw seconds kept appearing in tables (e.g. a query "Elapsed (s) = 6008.4"
or a task "Avg = 145.0s") while KPI cards showed the app's Hr/Min/Sec format. Two root causes, both
now fixed in the ONE shared table machinery (app/ui/components.py) so no per-caller vigilance is needed:

  1. A caller st.column_config.NumberColumn(format="%.1f") on a duration column OVERRODE the humanize
     (Streamlit's column_config printf beats the Styler-formatted cell) -> styled_table now DROPS any
     caller config for a duration column so the humanize can't be overridden.
  2. Tables over STYLER_MAX_ROWS (400) skip the Styler entirely, and no printf renders Hr/Min/Sec ->
     styled_table now PRE-FORMATS duration columns to Hr/Min/Sec strings on that large-frame path.

These guards fail if either mechanism is removed, or if any page re-introduces a raw-seconds override.
"""

from __future__ import annotations

import re
from pathlib import Path

import pandas as pd

from app.ui.components import _auto_formats, _duration_unit_for_column

_ROOT = Path(__file__).resolve().parents[1]


# --- the duration detector recognizes the SQL naming, without false hits -----
def test_duration_unit_detection():
    for col in ("ELAPSED_SEC", "AVG_SEC", "QUEUED_SEC", "P95_ELAPSED_SEC", "MEDIAN_S",
                "EST_WAIT_S", "AVG_TOTAL_S"):
        assert _duration_unit_for_column(col) == "s", col
    assert _duration_unit_for_column("LATENCY_MS") == "ms"
    assert _duration_unit_for_column("ELAPSED_MS") == "ms"
    # false-hit guards: counts / cluster sizing are NOT durations
    for col in ("MIN_CLUSTER_COUNT", "QUERY_COUNT", "RUNS", "FAILED", "PEAK_QUEUED", "TOKENS"):
        assert _duration_unit_for_column(col) is None, col


# --- humanize is AUTHORITATIVE over any caller column_config (root cause #1) --
def test_duration_humanize_outranks_caller_column_config():
    df = pd.DataFrame({"ELAPSED_SEC": [6008.4], "AVG_SEC": [145.0], "QUEUED_SEC": [4.4]})
    # even when the caller configured all three (skip set), _auto_formats attaches the humanize
    fmts = _auto_formats(df, skip={"ELAPSED_SEC", "AVG_SEC", "QUEUED_SEC"})
    for col in ("ELAPSED_SEC", "AVG_SEC", "QUEUED_SEC"):
        assert callable(fmts.get(col)), f"{col} lost its duration humanize under a caller config"
    # 6008s rolls up to hours/minutes, 145s to minutes; neither shows the raw number
    assert fmts["ELAPSED_SEC"](6008.4) == "1h 40m"
    assert fmts["AVG_SEC"](145.0) == "2m 25s"
    assert "145" not in fmts["AVG_SEC"](145.0)
    # sub-minute stays as seconds (correct — a 4.4s task should read "4.4s", not "0m 4s")
    assert fmts["QUEUED_SEC"](4.4) == "4.4s"


# --- the machinery still enforces both mechanisms (source locks) -------------
def test_styled_table_enforces_authoritative_durations():
    comp = (_ROOT / "app" / "ui" / "components.py").read_text(encoding="utf-8")
    # mechanism 1: caller config on a duration column is dropped so nothing can override the humanize
    assert "_dur_cols = [c for c in df.columns if c in fmts and _duration_unit_for_column(c)]" in comp
    assert "if _dur_cols and column_config:" in comp
    # mechanism 2: the >400-row (no-Styler) path pre-formats duration columns to Hr/Min/Sec strings
    assert "data[_dc] = data[_dc].map(lambda v, _u=_du: humanize_duration(v, _u))" in comp


# --- anti-recurrence lint: no page pins a raw-seconds NumberColumn on a duration column ---
_DURATION_KEY_NUMBERCOLUMN = re.compile(
    r'"[A-Z0-9_]+_(?:SEC|MS|S)"\s*:\s*st\.column_config\.NumberColumn')


def test_no_page_overrides_a_duration_column_with_a_raw_number_format():
    offenders = []
    for py in (_ROOT / "app" / "ui").rglob("*.py"):
        if "__pycache__" in py.parts:
            continue
        offenders.extend(f"{py.relative_to(_ROOT)}: {m.group(0)}"
                         for m in _DURATION_KEY_NUMBERCOLUMN.finditer(py.read_text(encoding="utf-8")))
    assert not offenders, (
        "A duration column (_SEC/_MS/_S) was pinned to a raw NumberColumn format, which shows raw "
        "seconds instead of the Hr/Min/Sec humanize. Drop the explicit format — durations humanize "
        "by convention (the shared machinery strips this anyway). Offenders:\n" + "\n".join(offenders))


def test_bar_count_humanizes_a_duration_metric():
    import app.ui.charts as charts
    # the takeaway/share-note humanizes a duration via value_fn (was raw seconds)
    note = charts._share_note("WH_A", 6008.0, 6008.0, dollars=False,
                              value_fn=lambda v: charts._fmt_metric_value(v, "sec"))
    assert "1h 40m" in note and "6008" not in note
    # bar_count exposes a duration `unit` that routes the tooltip AND takeaway through the humanizer
    bar = (_ROOT / "app" / "ui" / "charts.py").read_text(encoding="utf-8")
    bar = bar.split("def bar_count", 1)[1].split("\ndef ", 1)[0]
    assert 'data["ValueText"] = data["Value"].map(lambda v: _fmt_metric_value(v, unit))' in bar
    assert "value_fn=(lambda v: _fmt_metric_value(v, unit)) if _dur else None" in bar
    # the warehouse-contention bar chart (the last deferred raw-seconds case) now passes unit="sec"
    ops = (_ROOT / "app" / "ui" / "pages" / "operations.py").read_text(encoding="utf-8")
    assert 'takeaway=True, unit="sec"' in ops


def test_pages_do_not_bypass_the_table_machinery_with_raw_dataframe():
    # every page renders tabular data through styled_table/selectable_table (which humanize durations),
    # not a bare st.dataframe that would show raw values. (KPI cards use humanize_duration directly.)
    offenders = []
    for py in (_ROOT / "app" / "ui" / "pages").rglob("*.py"):
        if "__pycache__" in py.parts:
            continue
        if re.search(r"\bst\.dataframe\s*\(", py.read_text(encoding="utf-8")):
            offenders.append(str(py.relative_to(_ROOT)))
    assert not offenders, (
        "A page renders a raw st.dataframe, bypassing the shared formatting (durations/bytes/$). "
        "Use styled_table/selectable_table instead. Offenders: " + ", ".join(offenders))
