"""Review R1-170: the alert drawer's 'Tighten auto-suspend' guard reads the setting it guards LIVE.

The r34 A3 guard (remediation.tighten_suspend_plan) refuses a SET AUTO_SUSPEND = 60 that would RAISE a tighter
timer -- but it read the current value from the shared 'jump_wh' SHOW WAREHOUSES entry on the 4 h metadata tier.
A timer a DBA set to 30 s in a worksheet since that entry was cached read as the old 600 s, so the plan generated
SET = 60 (a loosening) and booked an ESTIMATED ledger row promising an adoption the change scan never makes. The
sibling statement-timeout lever moved to the live tier for exactly this reason (review C13).
"""

from __future__ import annotations

import pandas as pd

from app.core.query import CACHE_TTLS
from app.data import recheck_sql
from app.logic import remediation
from tests._source import read


def _tighten_branch() -> str:
    al = read("app/ui/pages/alerts.py")
    return al.split('if fix_kind.startswith("Tighten"):', 1)[1].split('elif fix_kind.startswith("Statement"):', 1)[0]


def test_tighten_guard_reads_one_warehouse_on_the_live_tier() -> None:
    branch = _tighten_branch()
    assert "_cl_sql = recheck_sql.warehouse_settings_sql(wh_inline)" in branch
    show = branch.split("_cl_whs = (run(", 1)[1].split("if _cl_sql else None)", 1)[0]
    assert 'tier="live"' in show and "max_rows=0" in show and "probe=True" in show
    assert CACHE_TTLS["live"] <= 30
    # never the shared 4 h metadata SHOW WAREHOUSES entry again
    assert 'tier="metadata"' not in branch and 'key="jump_wh"' not in branch
    assert "show_warehouses_sql()" not in branch
    # a failed read leaves the setting unknown: only an ok frame is parsed
    assert "_cl_whs.df if _cl_whs is not None and _cl_whs.ok else None" in branch
    assert "_cl_plan = remediation.tighten_suspend_plan(wh_inline, _cl_cur, _cl_known)" in branch


def test_live_value_decides_the_plan_not_a_cached_one() -> None:
    from app.logic.insights import auto_suspend_in_force
    # the DBA tightened WH_X to 30 s: the live row decides -> no ALTER that would raise it to 60
    live = pd.DataFrame({"name": ["WH_X"], "auto_suspend": [30]})
    known, cur = auto_suspend_in_force(live, "WH_X")
    assert (known, cur) == (True, 30.0)
    plan = remediation.tighten_suspend_plan("WH_X", cur, known)
    assert plan["stmt"] == "" and "already at AUTO_SUSPEND=30s" in plan["message"]
    # a looser live timer still tightens
    known, cur = auto_suspend_in_force(pd.DataFrame({"name": ["WH_X"], "auto_suspend": [600]}), "WH_X")
    assert remediation.tighten_suspend_plan("WH_X", cur, known)["stmt"] == "ALTER WAREHOUSE WH_X SET AUTO_SUSPEND = 60;"


def test_like_wildcard_rows_and_failed_reads_are_unknown() -> None:
    from app.logic.insights import auto_suspend_in_force
    # LIKE 'WH_X' also matches 'WHAX': only the exact (case-insensitive) name counts
    near = pd.DataFrame({"NAME": ["WHAX", "wh_x"], "AUTO_SUSPEND": [600, 45]})
    assert auto_suspend_in_force(near, "WH_X") == (True, 45.0)
    assert auto_suspend_in_force(pd.DataFrame({"name": ["WHAX"], "auto_suspend": [600]}), "WH_X") == (False, None)
    assert auto_suspend_in_force(None, "WH_X") == (False, None)                       # the read failed
    assert auto_suspend_in_force(pd.DataFrame(), "WH_X") == (False, None)
    # R1-071: a NULL on a LISTED row is the known never-suspend setting (0), not an unknown value
    assert auto_suspend_in_force(pd.DataFrame({"name": ["WH_X"], "auto_suspend": [None]}), "WH_X") == (True, 0.0)
    assert auto_suspend_in_force(pd.DataFrame({"name": ["WH_X"], "auto_suspend": ["?"]}), "WH_X") == (False, None)
    assert auto_suspend_in_force(pd.DataFrame({"name": ["WH_X"]}), "WH_X") == (False, None)
    # unknown -> the guard generates nothing executable
    assert remediation.tighten_suspend_plan("WH_X", None, False)["stmt"] == ""


def test_warehouse_settings_builder_is_one_validated_warehouse() -> None:
    assert recheck_sql.warehouse_settings_sql("WH_ALFA_BI_PRD") == "SHOW WAREHOUSES LIKE 'WH_ALFA_BI_PRD'"
    assert recheck_sql.warehouse_settings_sql(" wh_x ") == "SHOW WAREHOUSES LIKE 'wh_x'"
    for bad in ("", "WH; DROP TABLE X", "WH'X", None):
        assert recheck_sql.warehouse_settings_sql(bad) is None  # type: ignore[arg-type]
