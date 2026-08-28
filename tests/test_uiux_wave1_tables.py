"""UI/UX master list — Wave 1 table-correctness batch (W1c).

Locks: F28 byte-column headers stop contradicting their self-humanizing cells ·
F25 metric-registry COLUMN_HELP reaches caller-configured columns too.
"""

from __future__ import annotations

from pathlib import Path

from app.logic.metric_registry import COLUMN_HELP
from app.ui.components import _prettify_header

_SRC = (Path(__file__).resolve().parents[1] / "app" / "ui" / "components.py").read_text(
    encoding="utf-8")


# ---- F28: byte headers -------------------------------------------------------

def test_byte_magnitude_headers_drop_the_fixed_unit():
    # cells render "512 MB" / "1.2 GB" per row, so the header must not pin "(GB)"
    assert _prettify_header("SPILL_REMOTE_GB") == "Spill Remote"
    assert _prettify_header("BYTES_SCANNED_TB") == "Bytes Scanned"


def test_fixed_unit_headers_keep_their_suffix():
    assert _prettify_header("QUEUE_PCT") == "Queue (%)"          # % is a fixed unit
    # a byte RATE (…_PER_…) is never humanized as a magnitude, so its header
    # keeps the unit the cells actually carry
    assert "(TiB)" in _prettify_header("COST_USD_PER_TIB")


def test_prettify_still_leaves_human_headers_alone():
    assert _prettify_header("Ack by") == "Ack by"


# ---- F25: COLUMN_HELP on caller-configured columns ---------------------------

def test_column_help_merges_into_caller_config():
    # the loop must MERGE help into a caller-configured column, not `continue` past it
    idx = _SRC.index("if _col in caller_cfg:")
    block = _SRC[idx:idx + 700]
    assert "COLUMN_HELP.get(str(_col).upper())" in block
    assert '_cc["help"] = _help' in block
    assert 'not _cc.get("help")' in block      # a help the caller set always wins
    # the dollar-lens columns this exists for are in the registry
    assert "BILLED_USD" in COLUMN_HELP
