"""Snowsight reconciliation — byte-unit humanization (audit 2026-08-17).

The data-transfer bug (bytes shown as fixed-decimal TB/GB hid sub-unit values and
never matched Snowsight's MB/GB/TB scale) recurred on ~10 byte surfaces. The
systemic fix: _auto_formats humanizes every _GB/_TB/_MB TABLE column, and
humanize_gb covers the KPI values. This locks both."""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from app.logic.formulas import humanize_gb
from app.ui.components import (
    _auto_formats,
    _byte_unit_for_column,
    _callable_display_format,
)

_ROOT = Path(__file__).resolve().parents[1]


def _src(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


def test_humanize_gb_rescues_sub_gb_values():
    assert humanize_gb(0.03) == "30.7 MB"       # was "0.0 GB"
    assert humanize_gb(1.0) == "1.0 GB"
    assert humanize_gb(2048) == "2.0 TB"
    assert humanize_gb(0) == "0 B"
    assert humanize_gb(float("nan")) == "—"


def test_byte_unit_for_column_recognizes_conventions():
    assert _byte_unit_for_column("SPILL_REMOTE_GB") == ("GB", 1024 ** 3)
    assert _byte_unit_for_column("TB_SCANNED") == ("TB", 1024 ** 4)
    assert _byte_unit_for_column("TOTAL_TB_SCANNED") == ("TB", 1024 ** 4)
    assert _byte_unit_for_column("EGRESS_MB") == ("MB", 1024 ** 2)
    assert _byte_unit_for_column("QUEUED_SEC") is None
    assert _byte_unit_for_column("FAIL_PCT") is None


def test_auto_formats_humanizes_byte_columns_but_not_pct_or_hours():
    df = pd.DataFrame({"TB_SCANNED": [0.003], "SPILL_GB": [0.03],
                       "AVG_SCAN_PCT": [80.0], "SPILL_GB_PER_DAY": [5.0]})
    fmts = _auto_formats(df, set())
    # byte columns -> callable humanizer, and it produces the Snowsight scale.
    assert callable(fmts["TB_SCANNED"]) and fmts["TB_SCANNED"](0.003) == "3.1 GB"
    assert callable(fmts["SPILL_GB"]) and fmts["SPILL_GB"](0.03) == "30.7 MB"
    # a pct column stays fixed-decimal.
    assert fmts["AVG_SCAN_PCT"] == "{:,.1f}"
    # a RATE (…_PER_DAY) is NOT humanized as a magnitude (no format applied).
    assert "SPILL_GB_PER_DAY" not in fmts


def test_callable_display_format_dispatches_bytes_vs_duration():
    assert _callable_display_format("TB_SCANNED") == "%.2f TB"
    assert _callable_display_format("SPILL_REMOTE_GB") == "%.2f GB"
    # a duration column still falls to the duration printf.
    assert _callable_display_format("ELAPSED_SEC").endswith(" s")


def test_kpi_byte_sites_use_humanize_gb():
    ops = _src("app/ui/pages/operations.py")
    assert "humanize_gb(row.get(\"SPILL_REMOTE_GB\"))" in ops
    assert "humanize_gb(row.get(\"GB_SCANNED\"))" in ops
    cr = _src("app/ui/pages/control_room.py")
    # the "Remote spill: 0.0 GB" flagged-exception bug is fixed (fires on >0 but rounded to 0.0).
    assert 'f"{remote_spill_gb:,.1f} GB"' not in cr
    assert cr.count("humanize_gb(remote_spill_gb)") == 2   # exception value + KPI tile
    sec = _src("app/ui/pages/security.py")
    assert 'humanize_gb(xdf["GB"].sum())' in sec


def test_per_table_storage_dollars_show_cents():
    spend = _src("app/ui/pages/cost_parts/spend.py")
    assert 'format="$%.2f"' in spend and 'format="$%.0f"' not in spend.split("Active $")[-1][:400]
