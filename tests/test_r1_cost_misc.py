"""Round-1 hunt locks for the Cost cluster's unit-cost, contract, AI-chargeback, app-cost and Ask fixes
(v4.606: R1-021 R1-031 R1-036 R1-061 R1-114 R1-119 R1-158 R1-159 R1-161 R1-162 R1-163 R1-164 R1-165
R1-166 R1-167; R1-115 is locked in test_ask_pricing.py).

Render tests reuse test_probe_absence_split's recording fakes (``_patch`` / ``_ok`` / ``_failed``); the
sites buried too deep in a page for a cheap render (a CALL drill, a batch-fed chargeback tab) are locked
on the AST shape of their if/elif chain instead: a setup gap only when is_setup_absence(<res>.error_kind),
and every other failure 'unavailable' with the error as detail.
"""

from __future__ import annotations

import ast
from datetime import date
from types import SimpleNamespace

import pandas as pd
import pytest
import sqlglot

from tests._source import read
from tests.test_probe_absence_split import _FAILED, _SETUP, _failed, _ok, _one_unavailable, _patch

_UC = "app/ui/pages/cost_parts/unit_costs.py"
_AC = "app/ui/pages/cost_parts/ai_chargeback.py"


# ------------------------------------------------------------------ AST helpers for the deep sites ----

def _func(path: str, name: str) -> ast.FunctionDef:
    tree = ast.parse(read(path))
    return next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == name)


def _states(body: list[ast.stmt]) -> list[tuple[str | None, str | None]]:
    out = []
    for stmt in body:
        call = stmt.value if isinstance(stmt, ast.Expr) else None
        if isinstance(call, ast.Call) and getattr(call.func, "id", "") == "empty_state":
            kind = call.args[0].value if call.args and isinstance(call.args[0], ast.Constant) else None
            detail = next((ast.unparse(k.value) for k in call.keywords if k.arg == "detail"), None)
            out.append((kind, detail))
    return out


def _branches(path: str, func: str, var: str) -> list[tuple[str, str | None, str | None]]:
    """(test, empty_state kind, detail) for every branch of every if/elif whose test reads ``var``;
    a plain ``else`` is reported as 'else:<the last elif test>'."""
    out = []
    for node in ast.walk(_func(path, func)):
        if not isinstance(node, ast.If) or not any(
                isinstance(n, ast.Name) and n.id == var for n in ast.walk(node.test)):
            continue
        test = ast.unparse(node.test)
        out += [(test, k, d) for k, d in _states(node.body)]
        if node.orelse and not (len(node.orelse) == 1 and isinstance(node.orelse[0], ast.If)):
            out += [(f"else:{test}", k, d) for k, d in _states(node.orelse)]
    return out


def _assert_split(path: str, func: str, var: str, empty_kind: str | None = "no_data_yet") -> None:
    br = _branches(path, func, var)
    assert any(k == "unavailable" and d == f"{var}.error" for _, k, d in br), (var, br)
    assert any(k == "needs_setup" and not t.startswith("else:") and f"is_setup_absence({var}.error_kind)" in t
               for t, k, _ in br), (var, br)
    if empty_kind:
        assert any(k == empty_kind for _, k, _ in br), (var, br)


# --------------------------------------------------- R1-159 / R1-021: the contract tab, rendered ----

_START, _END = date(2025, 10, 1), date(2026, 9, 1)


class _Col:
    def slider(self, _label, _lo, _hi, value, **_k):
        return value

    def number_input(self, _label, _lo, _hi, value, **_k):
        return value


def _contract(monkeypatch, today: date):
    from app.ui.pages.cost_parts import contract
    rmf: list[tuple[str, str, str]] = []
    rmf_results = {
        "contract_consumed": _ok(pd.DataFrame({"CREDITS_BILLED_TO_DATE": [12_000.0],
                                               "FACT_FIRST_DAY": [_START.isoformat()]})),
        # the efficiency mart is loader-lagged: 27 of the 30 asked days are present
        "steer_idle": _ok(pd.DataFrame({"WAREHOUSE_NAME": ["WH_A"], "IDLE_CREDITS": [27.0],
                                        "COVERED_DAYS": [27]})),
        "steer_pats": _failed("other"),
    }

    def fake_rmf(mart, live, *, key, **_kw):
        rmf.append((key, mart, live))
        return rmf_results[key]

    advisor_days: list[int] = []

    def fake_idle_advisor(_df, _rate, days):
        advisor_days.append(days)
        return pd.DataFrame({"ACTIONABLE": pd.Series([], dtype=bool)})

    burn = _ok(pd.DataFrame({"CREDITS_BILLED": [40.0] * 30}))   # no DAY column: every row is whole-day
    notes: list[tuple[object, str]] = []
    fake, seen = _patch(
        monkeypatch, contract, {"steer_wh_settings": _ok(pd.DataFrame())},
        run_mart_first=fake_rmf, guard=lambda r, *_a, **_k: r.usable(),
        result_caption=lambda r, note="", **_k: notes.append((r, note)),
        daily_spend_wide=lambda _page: burn, account_today=lambda: today,
        idle_advisor=fake_idle_advisor, with_auto_suspend_settings=lambda df, _whs: df,
        panel_help=lambda *_a, **_k: None,
        _year_projection_strip=lambda _s: None, _rate_card_reconciliation=lambda _s: None,
        _org_truth_panel=lambda: False, _org_accounts_spend=lambda: None)
    fake.columns = lambda n: [_Col() for _ in range(n)]
    fake.column_config = SimpleNamespace(NumberColumn=lambda *_a, **_k: None)
    fake.success = lambda text, *_a, **_k: fake.calls.append(("success", str(text)))
    contract._contract_tab({"CONTRACT_CREDITS": 10_000, "CONTRACT_START_DATE": _START.isoformat(),
                            "CONTRACT_END_DATE": _END.isoformat(), "CREDIT_PRICE_USD": 3.0})
    # the source note under the consumed figure (the result_caption fed the contract_consumed read)
    seen["consumed_notes"] = [n for r, n in notes if r is rmf_results["contract_consumed"]]
    return fake, seen, rmf, advisor_days


def test_contract_consumed_reads_are_bounded_to_the_term(monkeypatch):
    _fake, _seen, rmf, _days = _contract(monkeypatch, date(2026, 3, 1))
    ((_key, mart, live),) = [r for r in rmf if r[0] == "contract_consumed"]
    assert "IFF(DAY >= '2025-10-01' AND DAY < '2026-09-01', CREDITS_BILLED, 0)" in mart
    assert "USAGE_DATE >= DATE '2025-10-01' AND USAGE_DATE < DATE '2026-09-01'" in live


@pytest.mark.parametrize("today", [date(2026, 3, 1), date(2026, 8, 31)])   # mid-term, last in-term day
def test_running_term_keeps_pace_and_steering(monkeypatch, today):
    fake, seen, rmf, advisor_days = _contract(monkeypatch, today)
    labels = [k["label"] for k in seen["kpis"][0]]
    assert "Pace" in labels and "Projected term total" in labels
    assert {"steer_idle", "steer_pats"} <= {k for k, _m, _l in rmf}
    # R1-021: the idle lever divides by the 27 days the mart COVERS, not the fixed 30-day ask
    assert advisor_days == [27]
    assert "Exhaustion here is CREDITS-based" in fake.text("caption")
    assert seen["consumed_notes"] == ["Billed credits (cloud-services adjustment applied) since contract start."]


@pytest.mark.parametrize("today", [_END, date(2026, 9, 15)])      # the (exclusive) end day, and after
def test_ended_term_shows_the_final_figure_and_withholds_pace(monkeypatch, today):
    fake, seen, rmf, advisor_days = _contract(monkeypatch, today)
    ((final, result),) = seen["kpis"]
    assert final["label"] == "Final term consumption" and final["value"] == "12,000 cr"
    assert result["value"] == "+2,000 cr over"
    assert not any(k.startswith("steer_") for k, _m, _l in rmf) and advisor_days == []
    ((state, msg),) = [(k, m) for k, m in seen["empty"] if "is over" in m]
    assert state == "needs_setup" and "CONTRACT_END_DATE" in msg
    plan = seen["tables"][-1]
    assert "RECOMMENDED_COMMIT_USD" in plan.columns and "CURRENT_CONTRACT_EXHAUSTED" not in plan.columns
    assert "no remaining balance to exhaust" in fake.text("caption")
    # R1-159 review: the source note names the term bound, not "since contract start" (credits to date)
    (note,) = seen["consumed_notes"]
    assert "since contract start" not in note
    assert "up to the term end 2026-09-01 (end day excluded)" in note


def test_contract_consumed_builders_keep_the_retention_floor_unfiltered():
    from app.data import cost_sql, mart_sql
    mart = mart_sql.fact_contract_consumed("2025-10-01", "2026-09-01")
    live = cost_sql.contract_consumed_credits("2025-10-01", "2026-09-01")
    assert "MIN(DAY) AS FACT_FIRST_DAY" in mart and "WHERE" not in mart
    assert "MIN(USAGE_DATE) AS SOURCE_FIRST_DAY" in live and "WHERE USAGE_DATE" not in live
    for sql in (mart, live):
        sqlglot.parse_one(sql, dialect="snowflake")
    # an unset end keeps the old start-only sum (callers without a term end are unchanged)
    assert "DAY <" not in mart_sql.fact_contract_consumed("2025-10-01")
    assert "USAGE_DATE <" not in cost_sql.contract_consumed_credits("2025-10-01")
    with pytest.raises(ValueError):
        mart_sql.fact_contract_consumed("2025-10-01", "09/01/2026")
    with pytest.raises(ValueError):
        cost_sql.contract_consumed_credits("2025-10-01", "2026-9-1")


def test_contract_pace_past_the_end_is_a_closed_clock():
    from app.logic.forecast import contract_pace
    p = contract_pace(12_000, 10_000, _START, _END, date(2026, 9, 15), trailing_daily_credits=40.0)
    assert p["ok"] and p["days_remaining"] == 0 and p["time_share"] == 100.0
    assert p["projected_term_credits"] == 12_000.0      # nothing projected past the term


# ------------------------------------------------------------ R1-161: the year strip's failed read ----

@pytest.mark.parametrize("kind", (*_FAILED, "ok_empty"))
def test_year_strip_failed_or_empty_read_is_rendered_not_dropped(monkeypatch, kind):
    from app.ui import components
    from app.ui.pages.cost_parts import contract
    cy = _ok(pd.DataFrame()) if kind == "ok_empty" else _failed(kind)
    _fake, _seen = _patch(monkeypatch, contract, {"cy_projection": cy}, guard=components.guard)
    shown: list[tuple[str, str]] = []
    monkeypatch.setattr(components, "empty_state", lambda k, m, *_a, **_kw: shown.append((k, m)))
    contract._year_projection_strip({})
    ((state, msg),) = shown
    if kind == "ok_empty":
        assert state == "no_data_yet" and "No billed metering rows" in msg
    else:
        assert state == "unavailable" and f"boom ({kind})" in msg


# ------------------------------------------- R1-165: rate-card reconciliation + org accounts spend ----

def _rate_card(monkeypatch, org_m, model_m):
    from app.ui.pages.cost_parts import contract
    fake, seen = _patch(monkeypatch, contract,
                        {"org_month_this": org_m, "fact_daily_compute_70": model_m,
                         "org_rate_sheet": _failed("absent")},
                        daily_spend_wide=lambda _page: _failed("other"))
    contract._rate_card_reconciliation({})
    return fake, seen


@pytest.mark.parametrize("kind", _SETUP)
def test_rate_card_absent_org_view_is_setup(monkeypatch, kind):
    _fake, seen = _rate_card(monkeypatch, _failed(kind), _ok(pd.DataFrame({"X": [1]})))
    assert [k for k, _m in seen["empty"]] == ["needs_setup"]


@pytest.mark.parametrize("kind", _FAILED)
def test_rate_card_failed_reads_are_unavailable(monkeypatch, kind):
    _fake, seen = _rate_card(monkeypatch, _failed(kind), _ok(pd.DataFrame({"X": [1]})))
    assert "USAGE_IN_CURRENCY_DAILY) could not be read" in _one_unavailable(seen, kind)
    _fake, seen = _rate_card(monkeypatch, _ok(pd.DataFrame({"X": [1]})), _failed(kind))
    assert "FACT_METERING_DAILY) could not be read" in _one_unavailable(seen, kind)


def test_rate_card_empty_reads_are_no_data_yet(monkeypatch):
    _fake, seen = _rate_card(monkeypatch, _ok(pd.DataFrame()), _ok(pd.DataFrame({"X": [1]})))
    assert [k for k, _m in seen["empty"]] == ["no_data_yet"]
    _fake, seen = _rate_card(monkeypatch, _ok(pd.DataFrame({"X": [1]})), _ok(pd.DataFrame()))
    assert [k for k, _m in seen["empty"]] == ["no_data_yet"]


@pytest.mark.parametrize("kind", (*_SETUP, *_FAILED))
def test_org_accounts_spend_split(monkeypatch, kind):
    from app.ui.pages.cost_parts import contract
    _fake, seen = _patch(monkeypatch, contract, {"org_spend": _failed(kind)},
                         methodology_note=lambda *_a, **_k: None)
    contract._org_accounts_spend()
    if kind in _SETUP:
        assert [k for k, _m in seen["empty"]] == ["needs_setup"]
    else:
        assert "could not be read" in _one_unavailable(seen, kind)


# ----------------------------------------------- R1-036 / R1-166: the AI Functions drill, rendered ----

def _ai_functions(monkeypatch, fn_res, days: int = 30):
    from app.ui.pages.cost_parts import ai_chargeback as ac
    _fake, seen = _patch(monkeypatch, ac, {f"cortex_fn_{days}": fn_res},
                         run_mart_first=lambda *_a, **_k: _failed("other"))
    ac._cortex_spend_tab(days, 2.2)
    return seen


@pytest.mark.parametrize("kind", _SETUP)
def test_ai_functions_absent_view_is_setup(monkeypatch, kind):
    seen = _ai_functions(monkeypatch, _failed(kind))
    ((state, msg),) = seen["empty"]
    assert state == "needs_setup" and "CORTEX_AI_FUNCTIONS_USAGE_HISTORY" in msg


@pytest.mark.parametrize("kind", _FAILED)
def test_ai_functions_failed_read_is_unavailable(monkeypatch, kind):
    assert "AI Functions usage could not be read" in _one_unavailable(_ai_functions(monkeypatch, _failed(kind)),
                                                                      kind)


@pytest.mark.parametrize(("days", "label", "capped"), [(30, "30d", False), (365, "90d", True)])
def test_ai_functions_empty_names_the_window_it_scanned(monkeypatch, days, label, capped):
    ((state, msg),) = _ai_functions(monkeypatch, _ok(pd.DataFrame()), days)["empty"]
    assert state == "no_data_yet" and f"in {label}" in msg
    assert ("capped at 90d" in msg) is capped


def test_chargeback_share_budget_and_map_reads_split_on_the_kind():
    # R1-166: these three used to leave their header over nothing on a failed read
    for var in ("share_res", "bud", "map_res"):
        _assert_split(_AC, "_chargeback_tab", var, empty_kind=None)
    assert any(k == "no_data_yet" for _t, k, _d in _branches(_AC, "_chargeback_tab", "share_res"))
    assert any(k == "no_data_yet" for _t, k, _d in _branches(_AC, "_chargeback_tab", "map_res"))


@pytest.mark.parametrize(("days", "label"), [(30, "30d"), (365, "90d")])
def test_empty_role_share_names_the_window_the_live_leg_scanned(monkeypatch, days, label):
    # R1-166 review: an ok-but-empty share is the live leg (an empty mart falls through), and the live
    # builder clamps a trailing window to 90d, so a 365d page must not say "no role activity in 365d"
    from contextlib import nullcontext

    from app.ui import components
    from app.ui.pages.cost_parts import ai_chargeback as ac
    dept = _ok(pd.DataFrame({"DEPARTMENT": ["Ops"], "WAREHOUSE_NAME": ["WH_A"], "COMPANY": ["ALFA"],
                             "CREDITS_TOTAL": [10.0]}))
    share = components._mark_served(_ok(pd.DataFrame()), live=True, days=None)   # run_mart_first's stamp
    fake, seen = _patch(
        monkeypatch, ac, {"cb_map": _ok(pd.DataFrame())},
        run_batch_mixed=lambda _specs, **_k: {"dept": dept, "bud": _ok(pd.DataFrame())},
        run_mart_first=lambda *_a, **_k: share, guard=lambda r, *_a, **_k: r.usable(),
        charts=SimpleNamespace(bar_usd=lambda *_a, **_k: None), reconciliation_footer=lambda *_a, **_k: None,
        panel_help=lambda *_a, **_k: None, _statement_export=lambda *_a, **_k: None)
    fake.columns = lambda n: [nullcontext() for _ in range(n)]
    fake.column_config = SimpleNamespace(NumberColumn=lambda *_a, **_k: None)
    fake.selectbox = lambda _label, options, **_k: options[0]
    fake.text_input = lambda _label, value="", **_k: value
    fake.code = lambda *_a, **_k: None
    ac._chargeback_tab("ALL", days, 3.0, False)
    ((state, msg),) = [(k, m) for k, m in seen["empty"] if "role activity" in m]
    assert state == "no_data_yet" and msg == f"No role activity on these warehouses in {label}."


# -------------------------------------- R1-061 / R1-167 / R1-163: serverless tasks, rendered ----

def _serverless(monkeypatch, sls, days: int = 30):
    from app.ui import components
    from app.ui.pages.cost_parts import unit_costs as uc
    daily = pd.DataFrame({"DAY": [], "PIPELINE": [], "USD": []})
    captioned: list = []
    fake, seen = _patch(
        monkeypatch, uc, {f"sls_costs_ALL_{days}__": sls},
        run_mart_first=lambda *_a, **_k: _ok(pd.DataFrame({"X": [1]})),
        # the task-graph panel above renders; the Serverless panel goes through the REAL guard()
        guard=lambda res, *a, **k: components.guard(res, *a, **k) if res is sls else True,
        result_caption=lambda res, *_a, **_k: captioned.append(res),
        graphs=SimpleNamespace(
            enrich_graph_daily=lambda _df, _rate: daily,
            pipeline_summary=lambda _d: pd.DataFrame(columns=["PIPELINE", "USD", "SUCCESS_PCT"])))
    # guard()'s absence states land in seen["empty"] and its truncation line in the fake's captions
    monkeypatch.setattr(components, "empty_state", uc.empty_state)
    monkeypatch.setattr(components, "st", fake)
    fake.column_config = SimpleNamespace(NumberColumn=lambda *_a, **_k: None)
    uc._graphs_tab("ALL", days, 3.0)
    seen["captioned"] = [r for r in captioned if r is sls]
    return fake, seen


@pytest.mark.parametrize("kind", _SETUP)
def test_serverless_absent_view_is_setup(monkeypatch, kind):
    _fake, seen = _serverless(monkeypatch, _failed(kind))
    assert [k for k, _m in seen["empty"]] == ["needs_setup"]


@pytest.mark.parametrize("kind", _FAILED)
def test_serverless_failed_read_is_unavailable_not_inaccessible(monkeypatch, kind):
    fake, seen = _serverless(monkeypatch, _failed(kind))
    assert "SERVERLESS_TASK_HISTORY) could not be read" in _one_unavailable(seen, kind)
    assert "not accessible" not in fake.text("caption")


def test_serverless_empty_is_clean_and_the_header_names_the_scanned_window(monkeypatch):
    fake, seen = _serverless(monkeypatch, _ok(pd.DataFrame()), days=365)
    assert [k for k, _m in seen["empty"]] == ["clean"]
    assert "task-day grain, 90d)" in fake.text("markdown")      # the read clamps 365d to the live limit


@pytest.mark.parametrize("truncated", [False, True])
def test_serverless_rows_disclose_a_cut_and_name_the_source(monkeypatch, truncated):
    # R1-061 x R1-062: the task-day rows (newest first) render through guard(), which adds the quiet
    # truncation line when run()'s row cap cut them, and result_caption names the source under the table
    sls = _ok(pd.DataFrame({"DAY": ["2026-09-30"], "TASK_NAME": ["T"], "SERVERLESS_CREDITS": [1.5]}))
    sls.truncated = truncated
    fake, seen = _serverless(monkeypatch, sls)
    assert seen["empty"] == [] and seen["captioned"] == [sls]
    (table,) = [t for t in seen["tables"] if "SERVERLESS_CREDITS" in t.columns]
    assert table["USD"].tolist() == [4.5]
    assert ("Showing the first 1 rows" in fake.text("caption")) is truncated


# ----------------------------------------------- R1-119 / R1-167: the deep unit-cost sites (AST) ----

def test_call_tree_children_read_renders_empty_and_failed_states():
    _assert_split(_UC, "_unit_costs_tab", "kids")


def test_cortex_source_and_etl_coverage_reads_split_on_the_kind():
    _assert_split(_UC, "_unit_costs_tab", "ai_res")
    _assert_split(_UC, "_unit_costs_tab", "cov")
    src = read(_UC)
    # run_batch already re-runs a failed member alone; a FAILED member is final (no third scan)
    assert "if cov is None or not cov.ok:" not in src and "if etl is None or not etl.ok:" not in src


# ------------------------------------------------- R1-163 / R1-164: unit-cost window labels ----

def test_unit_cost_labels_name_the_window_actually_scanned():
    src = read(_UC)
    # ("scanning {uc_days}d" stays: that caption renders only on a TRAILING window, never a preset)
    for raw in ("({uc_days}d)", ", {uc_days}d)", "Total, {uc_days}d", "{uc_days}-day scan window"):
        assert raw not in src, raw
    assert 'f"Proc total ({_uc_wlab})"' in src and 'f"Total, {_uc_wlab}"' in src
    assert "_uc_wlab = window_label(bounds, min(int(uc_days), MAX_LIVE_WINDOW_DAYS))" in src
    # past the live-scan limit the toggle and the caption stop promising "the full page window"
    assert "(the live-scan limit)" in src
    assert "serverless-task panels scan at most the" in src
    assert "if _past_live" in src and "The AI and task-graph pipeline panels below follow the page window" in src


# ---------------------------------------------------- R1-158: the per-call leader over every proc ----

def test_procedure_costs_carry_the_per_call_leader_over_every_group():
    from app.data import insights_sql
    sql = insights_sql.procedure_costs_usd(30, "ALL", "", "", 50)
    head = sql.split("ORDER BY TOTAL_CREDITS", 1)[0] if "ORDER BY TOTAL_CREDITS" in sql else sql
    for col in ("PC_LEADER_NAME", "PC_LEADER_CREDITS"):
        assert f"AS {col}" in head, col
    assert "FIRST_VALUE(calls.PROC_NAME) OVER (" in sql and "/ COUNT(*) DESC" in sql
    assert "QUALIFY" not in sql          # a window column, evaluated before the LIMIT, never a filter
    sqlglot.parse_one(sql, dialect="snowflake")


def test_priciest_proc_kpi_prefers_the_builder_leader():
    src = read(_UC)
    assert '"PC_LEADER_NAME" in p_res.df.columns' in src
    assert 'safe_float(_row0.get("PC_LEADER_CREDITS"))' in src
    # the leaderboard table drops the KPI-only window columns and says it ranks by total
    assert 'drop(columns=["PC_LEADER_NAME", "PC_LEADER_CREDITS"], errors="ignore")' in src
    assert "top 50 by measured spend" in src


class _KpisRendered(Exception):
    """Raised by the fake kpi_row: the unit-cost KPI row is all the render tests below need to see."""


_NO_LEADER_COLS = object()


def _priciest_per_call_kpi(monkeypatch, leader: object) -> dict:
    """Render _unit_costs_tab up to its KPI row over a top-50-by-TOTAL procedure frame whose in-frame
    per-call leader (DB.S.FREQ_07, 0.35 cr/call) is NOT row 0, carrying the builder's PC_LEADER_* columns
    (``leader`` as the name, 2.0 cr/call) unless ``leader`` is _NO_LEADER_COLS."""
    from app.ui.pages.cost_parts import unit_costs as uc
    procs = pd.DataFrame({"PROC_NAME": [f"DB.S.FREQ_{i:02d}" for i in range(50)],
                          "TOTAL_CREDITS": [100.0 - i for i in range(50)],
                          "CREDITS_PER_CALL": [0.35 if i == 7 else 0.01 for i in range(50)]})
    if leader is not _NO_LEADER_COLS:
        procs["PC_LEADER_NAME"] = [leader] * len(procs)
        procs["PC_LEADER_CREDITS"] = 2.0
    kpis: list[dict] = []

    def kpi_row(items, *_a, **_k):
        kpis.extend(items)
        raise _KpisRendered

    _patch(monkeypatch, uc, {"unit_ai_mart_30": _failed("other")},
           run_batch=lambda _jobs, **_k: {"q": _failed("other"), "p": _ok(procs), "ai": _failed("other")},
           panel_help=lambda *_a, **_k: None, kpi_row=kpi_row)
    with pytest.raises(_KpisRendered):
        uc._unit_costs_tab({"company": "ALL", "days": 30, "database": "", "schema_contains": "",
                            "bounds": None, "warehouse_contains": "", "user_contains": ""}, 3.0, 2.2)
    (kpi,) = [k for k in kpis if k["label"] == "Priciest procedure (per call)"]
    return kpi


def test_priciest_proc_kpi_names_the_leader_ranked_below_the_top_50(monkeypatch):
    # R1-158 review: a $6/call monthly batch proc ranking past 50th by total is named from PC_LEADER_*
    kpi = _priciest_per_call_kpi(monkeypatch, "DB.S.MONTHLY_BATCH")
    assert (kpi["value"], kpi["delta"]) == ("$6.00", "DB.S.MONTHLY_BATCH")
    assert "across every procedure" in kpi["help"]


@pytest.mark.parametrize("leader", [_NO_LEADER_COLS, None, float("nan"), "  "])
def test_priciest_proc_kpi_without_a_builder_leader_re_sorts_the_frame(monkeypatch, leader):
    # no (or a blank) PC_LEADER_NAME: the in-frame per-call leader, never row 0 (the top proc by total)
    kpi = _priciest_per_call_kpi(monkeypatch, leader)
    assert (kpi["value"], kpi["delta"]) == ("$1.05", "DB.S.FREQ_07")
    assert "among the procedures listed below" in kpi["help"]


# ---------------------------------------------- R1-031: app cost totals are not the capped frame ----

@pytest.mark.parametrize("builder", ["app_cost_mart", "app_cost_live"])
def test_app_cost_builders_carry_uncapped_application_totals(builder):
    from app.data import app_cost_sql
    sql = getattr(app_cost_sql, builder)(30, "ALL")
    assert "LIMIT 1000" not in sql
    # spend.py's "Measured $ by application" (the consumer) charts exactly these two pre-cap totals
    assert "AS APP_CREDITS" in sql and "AS APP_QUERIES" in sql
    qualify = sql.split("QUALIFY", 1)[1]
    assert "ROW_NUMBER() OVER (ORDER BY SUM(" in qualify and "<= 1000" in qualify
    assert "OR ROW_NUMBER() OVER (PARTITION BY" in qualify and ") = 1" in qualify
    sqlglot.parse_one(sql, dialect="snowflake")


# --------------------------------------------------- R1-162: chargeback sources name the mart ----

def test_chargeback_source_labels_name_the_mart_they_read():
    from app.data import chargeback_sql
    for sql in (chargeback_sql.department_window_credits(30, "ALL"),
                chargeback_sql.department_month_credits("2026-08", "ALL")):
        assert "FACT_WAREHOUSE_DAILY" in sql and "WAREHOUSE_METERING_HISTORY" not in sql
    src = read(_AC)
    assert "WAREHOUSE_METERING_HISTORY x DEPARTMENT_MAP" not in src
    assert "WAREHOUSE_METERING_HISTORY (calendar month)" not in src


# --------------------------------------------- R1-114: Ask never names the 'n/a' bucket a pattern ----

def _cs(shapes: pd.DataFrame):
    from app.logic.ask.registry import _analyze_cs_by_query
    from app.logic.ask.types import AskParams
    return _analyze_cs_by_query(AskParams(30, "ALL"), {"shapes": shapes})


def test_cs_answer_skips_the_unhashed_bucket_for_the_driver():
    res = _cs(pd.DataFrame({
        "QUERY_PARAMETERIZED_HASH": ["n/a", "abc123"],
        "QUERY_TYPE": ["SHOW", "SELECT"],
        "SAMPLE_TEXT": ["SHOW TABLES IN SCHEMA X", "SELECT * FROM T WHERE id = ?"],
        "RUNS": [9000, 400], "CS_CREDITS": [80.0, 20.0]}))
    assert "is a SHOW pattern" not in res.headline
    assert "unhashed mixed statements" in res.headline and "a SELECT pattern" in res.headline
    assert "20% of the top 2 query shapes" in res.headline       # share base still includes the bucket
    assert res.bullets[0].startswith("Mixed statements (no family hash)")
    assert "SHOW TABLES" not in res.bullets[0]
    ev = res.evidence
    assert ev.loc[0, "SAMPLE_TEXT"] == "(mixed statements, no family hash)"
    assert ev.loc[0, "QUERY_TYPE"] == "(mixed)" and ev.loc[1, "QUERY_TYPE"] == "SELECT"


def test_cs_answer_with_only_the_unhashed_bucket_claims_no_pattern():
    res = _cs(pd.DataFrame({"QUERY_PARAMETERIZED_HASH": ["N/A"], "QUERY_TYPE": ["SHOW"],
                            "SAMPLE_TEXT": ["SHOW TABLES"], "RUNS": [9000], "CS_CREDITS": [80.0]}))
    assert res.confidence == "grounded"
    assert "could not be attributed to a query family" in res.headline
    assert "pattern" not in res.headline


def test_cs_answer_without_a_hash_column_is_unchanged():
    res = _cs(pd.DataFrame({"QUERY_TYPE": ["SELECT"], "SAMPLE_TEXT": ["x"], "RUNS": [10],
                            "CS_CREDITS": [5.0]}))
    assert "is a SELECT pattern" in res.headline


# ---------------------------------------------------- R1-115: Ask says why CS credits have no $ ----

def test_ask_page_explains_unpriced_gross_cs_credits():
    src = read("app/ui/pages/ask.py")
    assert "is_gross_cs_column(c)" in src and "not priced here" in src


def test_repeated_patterns_label_names_the_mart_window_not_the_live_cap():
    """v4.606 integration: R1-015 made pattern_cost clamp to MAX_MART_WINDOW_DAYS (365) while R1-163 labelled
    the panel with the 90-day live-scan limit -- the caption and the page note now name the window it reads."""
    src = read(_UC)
    caption = src.split("**Repeated patterns", 1)[1].split("grouped by", 1)[0]
    assert "MAX_MART_WINDOW_DAYS" in caption and "_live_wlab" not in caption
    note = src.split("the repeated-pattern panel up to", 1)
    assert len(note) == 2 and "repeated-pattern, ETL and serverless-task" not in src
