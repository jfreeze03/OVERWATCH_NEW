"""Next-Fifty #33: per-warehouse statement-timeout posture (Operations ▸ Warehouses ▸ Sizing & efficiency),
plus the two timeout follow-ups that ship with it: F1 the alert drawer's "Statement timeout 1h" lever is
tighten-only, F2 is locked in history_locks/test_wave2_riders.py.

Pure, builder and source locks: they run on the floor-compat leg too (the shaped AppTests in
test_prc_c1_shaped.py skip there)."""

from __future__ import annotations

import math
import re

import pandas as pd
import pytest

from app import config
from app.data import canary, ops_sql
from app.logic import remediation, stmt_timeout
from app.logic.stmt_timeout import (
    CAP_LADDER_S,
    POSTURE_COLUMNS,
    STATUS_CAPPED,
    STATUS_NOT_VISIBLE,
    STATUS_UNCAPPED,
    STATUS_UNREAD,
    derive_account_timeout,
    effective_timeout_s,
    fix_script,
    is_uncapped,
    parse_timeout_row,
    posture_summary,
    suggested_cap_s,
    tail_window_days,
    tighten_timeout_plan,
    timeout_posture,
    warehouse_universe,
)
from tests._source import page_source, read


def _body(src: str, name: str) -> str:
    return src.split(f"def {name}(", 1)[1].split("\ndef ", 1)[0]


# ---------------------------------------------------------------------------
# semantics
# ---------------------------------------------------------------------------

def test_effective_cap_is_lowest_nonzero():
    assert effective_timeout_s(172800, "", None) == (172800, "Snowflake default")
    assert effective_timeout_s(0, "WAREHOUSE", None) == (604800, "Warehouse")        # 0 = the 7-day max
    assert effective_timeout_s(7200, "WAREHOUSE", 3600) == (3600, "Account")          # the lower one wins
    assert effective_timeout_s(0, "WAREHOUSE", 3600) == (3600, "Account")             # 0 is not "lowest"
    assert effective_timeout_s(300, "WAREHOUSE", 172800) == (300, "Warehouse")
    assert effective_timeout_s(3600, "ACCOUNT", 3600) == (3600, "Account")            # inherited
    assert effective_timeout_s(900000, "WAREHOUSE", None) == (604800, "Warehouse")    # clamped
    assert effective_timeout_s(None, "", 3600) == (None, "")                          # unread stays unread
    assert is_uncapped(172800) and is_uncapped(604800)
    assert not is_uncapped(3600) and not is_uncapped(172799) and not is_uncapped(None)


def test_parse_show_parameters_row():
    up = pd.DataFrame([{"KEY": "STATEMENT_TIMEOUT_IN_SECONDS", "VALUE": "300", "LEVEL": "WAREHOUSE"}])
    low = pd.DataFrame([{"key": "STATEMENT_TIMEOUT_IN_SECONDS", "value": "172800", "level": ""}])
    assert parse_timeout_row(up) == (300.0, "WAREHOUSE")
    assert parse_timeout_row(low) == (172800.0, "")
    assert parse_timeout_row(pd.DataFrame([{"value": "7200", "level": None}])) == (7200.0, "")
    assert parse_timeout_row(pd.DataFrame([{"value": "7200"}])) == (7200.0, "")       # no level column
    assert parse_timeout_row(pd.DataFrame()) == (None, "")
    assert parse_timeout_row(None) == (None, "")
    assert parse_timeout_row(pd.DataFrame([{"key": "X", "level": "ACCOUNT"}])) == (None, "")
    assert parse_timeout_row(pd.DataFrame([{"value": "abc", "level": "ACCOUNT"}])) == (None, "")


def test_derive_account_timeout():
    assert derive_account_timeout([(300.0, "WAREHOUSE"), (3600.0, "ACCOUNT")]) == 3600.0
    assert derive_account_timeout([(300.0, "WAREHOUSE"), (172800.0, "")]) == 172800.0
    assert derive_account_timeout([(300.0, "WAREHOUSE"), (None, "")]) is None     # an unread row proves nothing
    assert derive_account_timeout([(300.0, "WAREHOUSE")]) is None
    assert derive_account_timeout([]) is None


def test_suggested_cap_ladder():
    assert suggested_cap_s(4.2, 5000) == 300                  # the floor
    assert suggested_cap_s(250, 5000) == 900
    assert suggested_cap_s(700, 5000) == 3600
    assert suggested_cap_s(300, 5000) == 900                  # exactly 3x lands ON a rung
    assert suggested_cap_s(40000, 5000) is None              # x3 above the 24h top step
    assert suggested_cap_s(250, 99) is None                  # too few runs for a p99
    assert suggested_cap_s(float("nan"), 5000) is None
    assert suggested_cap_s(None, 5000) is None and suggested_cap_s(250, None) is None
    for p99 in (0.5, 99.0, 101.0, 599.0, 4800.0, 28799.0):
        cap = suggested_cap_s(p99, 1000)
        assert cap in CAP_LADDER_S and cap >= p99 * 3


def test_tail_window_days():
    assert [tail_window_days(d) for d in (1, 7, 30, 45, 90, 365, "x", None)] == [30, 30, 30, 45, 90, 90, 30, 30]


# ---------------------------------------------------------------------------
# universe + posture frame + script
# ---------------------------------------------------------------------------

def _tail(*rows) -> pd.DataFrame:
    out = []
    for name, runs, p99, mx, timed_out, over in rows:
        r = {"WAREHOUSE_NAME": name, "COMPANY": "ALFA", "COMPLETED_RUNS": runs, "P99_ELAPSED_SEC": p99,
             "MAX_ELAPSED_SEC": mx, "TIMEOUT_CANCELLED_RUNS": timed_out, "TIMEOUT_CANCELLED_TOTAL": 7}
        r.update({f"RUNS_OVER_{s}": over.get(s, 0) for s in CAP_LADDER_S})
        out.append(r)
    return pd.DataFrame(out)


def test_warehouse_universe():
    show = pd.DataFrame({"name": ["WH_A", "WH_B", "WH_IDLE"]})
    tail = _tail(("WH_A", 5000, 250.0, 4000.0, 0, {}), ("WH_GONE", 10, 1.0, 2.0, 0, {}))
    assert warehouse_universe(show, tail, "ALL") == (["WH_A", "WH_B", "WH_IDLE"], ["WH_GONE"])
    # a company scope reads only the (UDF-scoped) tail warehouses SHOW also lists
    assert warehouse_universe(show, tail, "ALFA") == (["WH_A"], ["WH_GONE"])
    # SHOW unusable: fall back to the tail names, and claim nothing about visibility
    assert warehouse_universe(None, tail, "ALL") == (["WH_A", "WH_GONE"], [])
    assert warehouse_universe(pd.DataFrame(), None, "ALL") == ([], [])


def test_warehouse_universe_caps_the_reads_longest_first():
    names = [f"WH_{i:03d}" for i in range(stmt_timeout.MAX_WAREHOUSES_READ + 20)]
    show = pd.DataFrame({"name": names})
    tail = _tail(("WH_119", 500, 10.0, 99999.0, 0, {}))       # the last by name, but the longest run
    read, hidden = warehouse_universe(show, tail, "ALL")
    assert len(read) == stmt_timeout.MAX_WAREHOUSES_READ and read == sorted(read)
    assert "WH_119" in read and hidden == []


def _posture() -> pd.DataFrame:
    tail = _tail(("WH_A", 5000, 250.0, 4000.0, 2, {900: 12, 300: 90}),
                 ("WH_B", 800, 40.0, 7000.0, 1, {}),
                 ("WH_LOW", 20, 5.0, 60.0, 0, {}),
                 ("WH_GONE", 10, 1.0, 2.0, 0, {}))
    params = {"WH_A": (172800.0, ""), "WH_B": (300.0, "WAREHOUSE"), "WH_LOW": (0.0, "WAREHOUSE"),
              "WH_ERR": (None, ""), "WH_IDLE": (172800.0, "")}
    return timeout_posture(["WH_A", "WH_B", "WH_ERR", "WH_IDLE", "WH_LOW"], params, 172800.0, tail,
                           not_visible=["WH_GONE"])


def test_timeout_posture_frame():
    p = _posture()
    assert list(p.columns) == POSTURE_COLUMNS
    assert list(p["STATUS"]) == [STATUS_UNCAPPED] * 3 + [STATUS_CAPPED, STATUS_UNREAD, STATUS_NOT_VISIBLE]
    by = p.set_index("WAREHOUSE_NAME")
    assert list(p["WAREHOUSE_NAME"][:3]) == ["WH_A", "WH_LOW", "WH_IDLE"]   # longest run first, NaN last
    assert by.loc["WH_A", "SUGGESTED_TIMEOUT_SEC"] == 900
    assert by.loc["WH_A", "WOULD_CANCEL_RUNS"] == 12                        # == RUNS_OVER_<suggested>
    assert by.loc["WH_A", "CAP_SOURCE"] == "Snowflake default"
    assert by.loc["WH_LOW", "EFFECTIVE_TIMEOUT_SEC"] == 172800             # 0 -> 7d, the account 2d wins
    assert by.loc["WH_LOW", "CAP_SOURCE"] == "Account"
    assert math.isnan(by.loc["WH_LOW", "SUGGESTED_TIMEOUT_SEC"])            # 20 runs: no p99 suggestion
    # a capped warehouse never gets a suggestion; an idle / unread one keeps NaN tails (never 0)
    assert math.isnan(by.loc["WH_B", "SUGGESTED_TIMEOUT_SEC"]) and by.loc["WH_B", "EFFECTIVE_TIMEOUT_SEC"] == 300
    for col in ("COMPLETED_RUNS", "P99_ELAPSED_SEC", "MAX_ELAPSED_SEC", "TIMEOUT_CANCELLED_RUNS"):
        assert math.isnan(by.loc["WH_IDLE", col]) and math.isnan(by.loc["WH_ERR", col])
    assert math.isnan(by.loc["WH_ERR", "EFFECTIVE_TIMEOUT_SEC"]) and by.loc["WH_ERR", "CAP_SOURCE"] is None
    assert by.loc["WH_GONE", "COMPLETED_RUNS"] == 10                       # not visible, but its tail shows
    assert posture_summary(p) == {"read": 4, "uncapped": 3, "capped": 1, "unread": 1, "not_visible": 1}
    assert posture_summary(pd.DataFrame())["read"] == 0


def test_fix_script_review_only():
    p = _posture()
    script = fix_script(p, 45)
    lines = script.splitlines()
    assert lines[0].startswith("-- Review only: OVERWATCH never runs these.") and "last 45 days" in lines[0]
    alters = [ln for ln in lines if not ln.startswith("--")]
    assert alters == [remediation.statement_timeout_fix("WH_A", 900)]      # ONE live statement
    i = lines.index(alters[0])
    assert lines[i - 1].startswith("-- WH_A: effective cap 48h (Snowflake default); p99 4m 10s")
    assert "would have cancelled 12 of them" in lines[i - 1]
    assert lines[i + 1] == "-- undo: ALTER WAREHOUSE WH_A UNSET STATEMENT_TIMEOUT_IN_SECONDS;"
    assert any(ln.startswith("-- WH_LOW: uncapped; only 20 completed statements") and
               ln.endswith("choose a cap by hand.") for ln in lines)
    assert any(ln.startswith("-- WH_IDLE: uncapped; no completed statements") for ln in lines)
    assert "WH_B" not in script                                           # capped: never an ALTER
    # nothing uncapped -> no script; a capped-only frame never writes one
    assert fix_script(p[p["STATUS"] != STATUS_UNCAPPED], 30) == ""
    assert fix_script(pd.DataFrame(columns=POSTURE_COLUMNS), 30) == ""


def test_a_failed_tail_read_is_never_reported_as_no_statements():
    # the runtime read failed: every tail value is NaN, and the hand line must say so
    params = {"WH_A": (172800.0, "")}
    p = timeout_posture(["WH_A"], params, 172800.0, None)
    assert math.isnan(p.loc[0, "COMPLETED_RUNS"]) and math.isnan(p.loc[0, "SUGGESTED_TIMEOUT_SEC"])
    failed = fix_script(p, 30, tail_ok=False)
    assert "the runtime tail could not be read" in failed and "no completed statements" not in failed
    assert "no completed statements in the last 30 days" in fix_script(p, 30)   # a read that returned none


def test_fix_script_undo_restores_a_warehouse_level_value_and_hand_writes_quoted_names():
    tail = _tail(("WH_X", 5000, 250.0, 4000.0, 0, {900: 3}), ("wh lower", 5000, 250.0, 4000.0, 0, {900: 3}),
                 ("EVIL\nDROP TABLE T;", 5000, 250.0, 4000.0, 0, {}))
    params = {"WH_X": (0.0, "WAREHOUSE"), "wh lower": (172800.0, ""), "EVIL\nDROP TABLE T;": (172800.0, "")}
    p = timeout_posture(list(params), params, 172800.0, tail)
    script = fix_script(p, 30)
    assert "ALTER WAREHOUSE WH_X SET STATEMENT_TIMEOUT_IN_SECONDS = 900;" in script
    assert "-- undo: ALTER WAREHOUSE WH_X SET STATEMENT_TIMEOUT_IN_SECONDS = 0;" in script
    assert "-- wh lower: uncapped; its name needs a quoted identifier" in script
    assert "WH LOWER" not in script                                    # never ALTERed under the wrong name
    # a name carrying a newline cannot break out of its comment line into a statement
    assert [ln for ln in script.splitlines() if not ln.startswith("--")] == [
        "ALTER WAREHOUSE WH_X SET STATEMENT_TIMEOUT_IN_SECONDS = 900;"]


# ---------------------------------------------------------------------------
# F1: the alert drawer's "Statement timeout 1h" lever is tighten-only
# ---------------------------------------------------------------------------

def test_tighten_timeout_plan_never_loosens():
    tight = tighten_timeout_plan("WH_ADMIN", 300.0, "WAREHOUSE")
    assert tight["stmt"] == "" and tight["level"] == "info" and "already capped at 5m" in tight["message"]
    assert tighten_timeout_plan("WH_A", 3600.0, "WAREHOUSE")["stmt"] == ""        # equal: keep, no-op
    unread = tighten_timeout_plan("WH_A", None)
    assert unread["stmt"] == "" and unread["level"] == "warning"
    default = tighten_timeout_plan("WH_A", 172800.0, "")
    assert default["stmt"] == "ALTER WAREHOUSE WH_A SET STATEMENT_TIMEOUT_IN_SECONDS = 3600;"
    assert default["undo"] == "ALTER WAREHOUSE WH_A UNSET STATEMENT_TIMEOUT_IN_SECONDS;"
    assert default["level"] == "none" and default["message"] == ""
    own = tighten_timeout_plan("WH_A", 7200.0, "WAREHOUSE")
    assert own["undo"] == "ALTER WAREHOUSE WH_A SET STATEMENT_TIMEOUT_IN_SECONDS = 7200;"
    assert tighten_timeout_plan("WH_A", 0.0, "WAREHOUSE")["stmt"].endswith("= 3600;")   # 0 = 7 days


def test_alert_drawer_timeout_lever_reads_before_it_writes():
    al = read("app/ui/pages/alerts.py")
    assert '"Statement timeout 1h",' in al                    # the 1.52.2 radio identity is unchanged
    assert "remediation.statement_timeout_fix(wh_inline, 3600)" not in al      # the blind SET is gone
    branch = al.split('elif fix_kind.startswith("Statement"):', 1)[1].split("\n                            else:", 1)[0]
    assert "run(ops_sql.warehouse_stmt_timeout_sql(wh_inline)" in branch and "probe=True" in branch
    assert 'tier="metadata"' in branch and "max_rows=0" in branch
    assert "_cl_plan = stmt_timeout.tighten_timeout_plan(wh_inline, _to_cur, _to_lvl)" in branch
    assert branch.index("warehouse_stmt_timeout_sql") < branch.index("tighten_timeout_plan")
    # one shared warning/info render for both guarded levers, so the raw st.info ceiling holds
    assert al.count('st.info(_cl_plan["message"])') == 1
    assert len(re.findall(r"st\.(?:info|success)\(", al)) <= 9
    # the r34 auto-suspend guard and its V157 note wiring are untouched
    assert "_cl_plan = remediation.tighten_suspend_plan(wh_inline, _cl_cur, _cl_known)" in al
    assert 'STATEMENT_TIMEOUT" if fix_kind.startswith("Statement")' in al


# ---------------------------------------------------------------------------
# builders
# ---------------------------------------------------------------------------

def test_show_builders_share_admins_cache_entry_and_quote_the_rest():
    admin = read("app/ui/pages/admin.py")
    assert ("f\"SHOW PARAMETERS LIKE 'STATEMENT_TIMEOUT_IN_SECONDS' IN WAREHOUSE {APP_WAREHOUSE}\"" in admin)
    assert (ops_sql.warehouse_stmt_timeout_sql(config.APP_WAREHOUSE)
            == f"SHOW PARAMETERS LIKE 'STATEMENT_TIMEOUT_IN_SECONDS' IN WAREHOUSE {config.APP_WAREHOUSE}")
    assert ops_sql.warehouse_stmt_timeout_sql("wh x").endswith('IN WAREHOUSE "wh x"')
    assert ops_sql.warehouse_stmt_timeout_sql('A"B').endswith('IN WAREHOUSE "A""B"')
    with pytest.raises(ValueError):
        ops_sql.warehouse_stmt_timeout_sql("  ")
    assert ops_sql.account_stmt_timeout_sql() == "SHOW PARAMETERS LIKE 'STATEMENT_TIMEOUT_IN_SECONDS' IN ACCOUNT"


def test_timeout_tail_shape():
    import sqlglot

    sql = ops_sql.warehouse_timeout_tail(30, "ALL")
    assert ops_sql.warehouse_timeout_tail(400, "ALL").count("DATEADD('day', -90, CURRENT_TIMESTAMP())") == 1
    for s in CAP_LADDER_S:
        assert f"TOTAL_ELAPSED_TIME > {s * 1000}) AS RUNS_OVER_{s}" in sql and f"q.RUNS_OVER_{s}" in sql
    assert "EXECUTION_STATUS <> 'SUCCESS'" in sql and "EXECUTION_STATUS = 'FAIL'" not in sql
    assert "ILIKE '%statement or warehouse timeout%'" in sql
    assert sql.count("COMPANY_FOR_WAREHOUSE") == 1                     # the outer label only, per warehouse
    scoped = ops_sql.warehouse_timeout_tail(30, "ALFA")
    outer = scoped.split("\nFROM q\n", 1)[1]
    assert "COMPANY_FOR_WAREHOUSE(q.WAREHOUSE_NAME) = 'ALFA'" in outer   # filtered on the OUTER query
    assert "q.*" not in sql and "LIMIT" not in sql
    assert "SUM(q.TIMEOUT_CANCELLED_RUNS) OVER () AS TIMEOUT_CANCELLED_TOTAL" in sql
    assert sqlglot.parse_one(sql, read="snowflake").named_selects == [
        "WAREHOUSE_NAME", "COMPANY", "COMPLETED_RUNS", "P99_ELAPSED_SEC", "MAX_ELAPSED_SEC",
        "TIMEOUT_CANCELLED_RUNS", "TIMEOUT_CANCELLED_TOTAL", *[f"RUNS_OVER_{s}" for s in CAP_LADDER_S]]
    # the loader-robustness rule for this file still holds
    src = read("app/data/ops_sql.py")
    assert "EXECUTION_STATUS = 'FAIL'" not in src and src.count("<> 'SUCCESS'") >= 2


def test_canary_registration():
    reg = dict(canary.CANARIES)
    assert reg["ops.warehouse_timeout_tail"]() == ops_sql.warehouse_timeout_tail(1, "ALFA")
    names = [n for n, _ in canary.CANARIES]
    assert not any("stmt_timeout" in n for n in names)                 # SHOW cannot be EXPLAINed
    assert all("SHOW PARAMETERS" not in fn() for _, fn in canary.CANARIES if _.startswith("ops."))


# ---------------------------------------------------------------------------
# page wiring
# ---------------------------------------------------------------------------

def test_panel_wiring_and_gating():
    ops = page_source("operations")
    sizing = _body(ops, "_wh_sizing_efficiency")
    assert "_stmt_timeout_posture_panel(company, days)" in sizing
    assert sizing.index("result_caption(_hh)") < sizing.index("_stmt_timeout_posture_panel(company, days)") \
        < sizing.index("_adaptive_candidacy_panel(company, days, bounds=bounds)")
    body = _body(ops, "_stmt_timeout_posture_panel")
    assert body.index('key="ops_wh_timeout_load"') < body.index("run(")     # every read behind the toggle
    for banned in ("execute_statement", "confirm_gate", "write_gate_open", "REMEDIATION_LOG", "ACCOUNT_USAGE",
                   "methodology_note", "st.info(", "st.success(", ") or {}", "st.selectbox", "st.subheader"):
        assert banned not in body, banned
    assert "st.code(script" in body and body.count("probe=True") == 2
    assert "stmt_timeout.fix_script(posture, tail_days, tail_ok=tail.ok)" in body   # a failed tail says so
    assert 'key="jump_wh"' in body and 'tier="metadata"' in body          # shares the SHOW WAREHOUSES cache
    assert "run_batch" not in body                                      # plain run() per SHOW (D4)
    assert 'empty_state("clean"' in body and 'summ["read"] and not summ["uncapped"]' in body
    assert 'TIMEOUT_CANCELLED_TOTAL"].iloc[0]' in body                  # the SQL total, never a frame sum
    # the page-wide counts this panel must not move
    assert read("app/ui/pages/operations.py").count("ACCOUNT_USAGE") == 42
    assert "stmt_timeout," in ops.split("from app.logic import (", 1)[1].split(")", 1)[0]


def test_admin_parity_and_untouched():
    admin = read("app/ui/pages/admin.py")
    m = re.search(r"^_SNOWFLAKE_DEFAULT_STMT_TIMEOUT_S = ([\d_]+)$", admin, re.M)
    assert m and int(m.group(1).replace("_", "")) == stmt_timeout.SNOWFLAKE_DEFAULT_STMT_TIMEOUT_S == 172_800
    assert "stmt_timeout." not in admin and "ops_sql.warehouse_stmt_timeout_sql" not in admin  # Admin untouched


def test_duration_and_count_naming():
    from app.ui.components import _COUNT_SUFFIXES, _duration_unit_for_column
    for col in POSTURE_COLUMNS:
        if col.endswith("_SEC"):
            assert _duration_unit_for_column(col) == "s", col
        if col.endswith("RUNS"):
            assert col.endswith(_COUNT_SUFFIXES) and _duration_unit_for_column(col) is None, col
    assert _duration_unit_for_column("TIMEOUT_CANCELLED_RUNS") is None


def test_status_colors():
    from app.ui.status_colors import status_css
    assert status_css("STATUS", STATUS_UNCAPPED) and status_css("STATUS", STATUS_CAPPED)
    assert status_css("STATUS", STATUS_UNCAPPED) != status_css("STATUS", STATUS_CAPPED)
    assert status_css("STATUS", STATUS_NOT_VISIBLE) == ""
