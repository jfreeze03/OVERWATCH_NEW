"""Decision Studio Wave-2 SLO trust fixes: #9 P95-basis disclosure, #10 latency
burn -> n/a, #11 staleness gate on AS_OF.

v4.607: the board's slo_summary KPI helper (stale counted apart, has_burn for latency
objectives) had no caller once Option C (v4.597) retired the board, and was removed with
its locks. #11 stays locked on the slo_cockpit SQL (the Entity 360 watchlist badge reads
it) and the trio on the built-in objectives panel that replaced the board.
"""

from pathlib import Path

from app.data import workbench_sql

_SRC = (Path(__file__).resolve().parents[2] / "app" / "ui" / "decision_studio.py").read_text(encoding="utf-8")


# --- #11 staleness ---------------------------------------------------------

def test_slo_cockpit_gates_status_on_staleness():
    sql = workbench_sql.slo_cockpit()
    assert "'STALE'" in sql
    assert "m.AS_OF < DATEADD('day', -2, CURRENT_DATE())" in sql
    # STALE ranks as a warning tier (after BREACH, before/at NO_DATA), not silently MET
    assert "WHEN 'STALE' THEN 1" in sql


# --- UI wiring (#9 caption, #10 n/a, #11 stale surfaced) -------------------

def test_slo_board_surfaces_the_trio():
    # v4.597 (Option C): the custom SLO board was retired (slo_cockpit above stays for the Entity 360
    # watchlist badge, via workbench.watchlist_threshold_status). Its trust trio carries over to the
    # read-only built-in objectives on Operations ▸ Pipeline SLA ▸ Tonight, which replaced it:
    assert "def _slos" not in _SRC
    ops = (Path(__file__).resolve().parents[2] / "app" / "ui" / "pages" / "operations.py").read_text(
        encoding="utf-8")
    panel = ops.split("def _builtin_objectives_panel", 1)[1].split("\ndef ", 1)[0]
    assert 'cad["stale"]' in panel                 # #11 stale (silently stopped) tasks surfaced
    assert '"value": "—"' in panel and "0/0" not in panel   # #10 no evidence reads "—", never 0/0
    assert "tonight still running" in panel        # an in-flight night is not judged as a miss
    assert "Read-only objectives derived from the ETL clock" in panel   # #9 basis disclosure
