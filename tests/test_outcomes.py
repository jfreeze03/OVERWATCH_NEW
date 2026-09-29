"""Next-Fifty #46 (a)-(b): measured outcomes of completed work (app/logic/outcomes.py, pure).

A DONE item is measured on its entity's own daily mart signal since the day it was marked done: held,
re-broke, not fixed, too early -- and a stalled loader never reads as a fix. Re-broke and Not fixed lift
Optimize's Track-all cooldown; nothing else does.
"""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import pytest

from app import config
from app.logic import fix_queue, outcomes
from app.logic.outcomes import (
    action_held,
    done_entities,
    done_family_entities,
    family_outcomes,
    held_basis,
    held_columns,
    rebroke_families,
)

TODAY = date(2026, 9, 29)
DONE = date(2026, 9, 1)


def _frame(kind: str, key: str, series: dict, loaded: date | None = TODAY - timedelta(days=1)) -> pd.DataFrame:
    rows = [{"ENTITY_TYPE": kind, "ENTITY_KEY_U": key, "DAY": day, "CREDITS": v.get("c"), "P95_SEC": v.get("p"),
             "RUNS": v.get("r"), "FAILS": v.get("f"), "LOADED_THROUGH": loaded} for day, v in series.items()]
    return pd.DataFrame(rows)


def _wh_series(after: float = 50.0) -> dict:
    ser = {DONE - timedelta(days=i): {"c": 100.0 + (i % 7) * 5} for i in range(1, 29)}
    ser.update({DONE + timedelta(days=i): {"c": after} for i in range(1, 28)})
    return ser


# ------------------------------------------------------------- (1) warehouse credits ----

def test_warehouse_credits_held_rebroke_not_fixed_too_early():
    held = action_held("WAREHOUSE", "WH", DONE, _frame("WAREHOUSE", "WH", _wh_series()), TODAY)
    assert held["state"] == outcomes.HELD and held["label"] == "Held 27 days"
    assert held["signal"] == outcomes.SIGNAL_CREDITS and held["after"] == pytest.approx(50.0)
    back = _wh_series()
    back.update({DONE + timedelta(days=i): {"c": 120.0} for i in range(15, 28)})
    rb = action_held("WAREHOUSE", "WH", DONE, _frame("WAREHOUSE", "WH", back), TODAY)
    # D7: house date style ("Re-broke Sep 21"), dated when the trailing week climbed back past 90%
    assert rb["state"] == outcomes.REBROKE and rb["label"] == "Re-broke Sep 21"
    assert rb["since"] == date(2026, 9, 21)
    nf = action_held("WAREHOUSE", "WH", DONE, _frame("WAREHOUSE", "WH", _wh_series(after=98.0)), TODAY)
    assert nf["state"] == outcomes.NOT_FIXED and nf["label"] == "Not fixed"
    early = action_held("WAREHOUSE", "WH", TODAY - timedelta(days=4), _frame("WAREHOUSE", "WH", _wh_series()), TODAY)
    assert early["state"] == outcomes.TOO_EARLY and early["label"] == "Too early (3 of 7 days)"


def test_the_completion_day_itself_is_skipped_and_after_days_count_to_yesterday():
    ser = _wh_series()
    ser[DONE] = {"c": 10_000.0}          # the partial done day must never enter either window
    r = action_held("WAREHOUSE", "WH", DONE, _frame("WAREHOUSE", "WH", ser), TODAY)
    assert r["label"] == "Held 27 days" and r["before"] == pytest.approx(115.0)


# ------------------------------------------------------------------- (2) stalled loader ----

def test_a_stalled_loader_never_reads_as_a_fix():
    stalled = {k: v for k, v in _wh_series().items() if k <= DONE + timedelta(days=3)}
    r = action_held("WAREHOUSE", "WH", DONE, _frame("WAREHOUSE", "WH", stalled, loaded=DONE + timedelta(days=3)),
                    TODAY)
    assert r["state"] == outcomes.TOO_EARLY and r["after_days"] == 3
    # the same frame WITHOUT the loaded bound would zero-fill 24 missing days and read as a fix
    naive = action_held("WAREHOUSE", "WH", DONE, _frame("WAREHOUSE", "WH", stalled, loaded=None), TODAY)
    assert naive["state"] == outcomes.HELD


# --------------------------------------------------------------------- (3) task failures ----

def _task_series(fail_every: int = 3) -> dict:
    ser = {DONE - timedelta(days=i): {"r": 24.0, "f": 2.0 if i % fail_every == 0 else 0.0, "p": 60.0}
           for i in range(1, 29)}
    ser.update({DONE + timedelta(days=i): {"r": 24.0, "f": 0.0, "p": 60.0} for i in range(1, 28)})
    return ser


def test_task_failure_arm_held_then_rebroke_on_the_first_failing_day():
    ser = _task_series()
    r = action_held("TASK", "DB.S.T", DONE, _frame("TASK", "DB.S.T", ser), TODAY)
    assert r["state"] == outcomes.HELD and r["signal"] == outcomes.SIGNAL_FAILURES
    assert held_basis(r, 3.68) == "18 failed of 672 runs in the 28 days before; 0 since"
    ser[DONE + timedelta(days=10)] = {"r": 24.0, "f": 1.0, "p": 60.0}
    rb = action_held("TASK", "db.s.t", DONE, _frame("TASK", "DB.S.T", ser), TODAY)   # case-insensitive key
    assert rb["state"] == outcomes.REBROKE and rb["label"] == "Re-broke Sep 11"


# -------------------------------------------------------------------- (4) task runtime ----

def test_task_runtime_arm_uses_a_median_over_run_days_only():
    ser = {DONE - timedelta(days=i): {"r": 24.0, "f": 0.0, "p": 600.0} for i in range(1, 29) if i % 2}
    ser.update({DONE + timedelta(days=i): {"r": 24.0, "f": 0.0, "p": 200.0} for i in range(1, 28) if i % 2})
    r = action_held("TASK", "DB.S.T", DONE, _frame("TASK", "DB.S.T", ser), TODAY)
    # no zero-fill: 14 before run-days at 600 s -> a 600 s baseline, 14 after run-days
    assert r["state"] == outcomes.HELD and r["signal"] == outcomes.SIGNAL_P95
    assert r["before"] == pytest.approx(600.0) and r["after_days"] == 14
    assert held_basis(r, 3.68) == "P95 10m before → 3m 20s after, 14 days measured"


# ------------------------------------------------------------ (5) query-family arm switch ----

def test_query_family_switches_between_failures_and_credits_at_the_arm_pct():
    def fam(fail_per_day: float) -> pd.DataFrame:
        ser = {DONE - timedelta(days=i): {"r": 100.0, "f": fail_per_day, "c": 10.0} for i in range(1, 29)}
        ser.update({DONE + timedelta(days=i): {"r": 100.0, "f": 0.0, "c": 4.0} for i in range(1, 28)})
        return _frame("QUERY_FINGERPRINT", "ABC", ser)
    assert action_held("QUERY_FINGERPRINT", "abc", DONE, fam(2.0), TODAY)["signal"] == outcomes.SIGNAL_FAILURES
    below = action_held("QUERY_FINGERPRINT", "abc", DONE, fam(1.9), TODAY)
    assert below["signal"] == outcomes.SIGNAL_CREDITS and below["state"] == outcomes.HELD
    # warehouses never take the failure arm (no failure signal on the credits fact)
    assert outcomes.FAIL_ARM_PCT == 2.0


# ------------------------------------------------------- (6) not measurable / labels ----

def test_unmeasurable_types_rows_and_reads():
    assert action_held("USER", "X", DONE, None, TODAY)["label"] == "Not measurable"
    assert action_held("WAREHOUSE", "", DONE, None, TODAY)["state"] == outcomes.NOT_MEASURABLE
    assert action_held("WAREHOUSE", "WH", None, _frame("WAREHOUSE", "WH", _wh_series()), TODAY)["state"] == \
        outcomes.NOT_MEASURABLE
    assert action_held("WAREHOUSE", "OTHER", DONE, _frame("WAREHOUSE", "WH", _wh_series()), TODAY)["state"] == \
        outcomes.NOT_MEASURABLE
    zero = {d: {"c": 0.0} for d in _wh_series()}
    assert action_held("WAREHOUSE", "WH", DONE, _frame("WAREHOUSE", "WH", zero), TODAY)["state"] == \
        outcomes.NOT_MEASURABLE


def _actions() -> pd.DataFrame:
    return pd.DataFrame([
        {"ACTION_ID": "a1", "STATUS": "DONE", "SOURCE_ENTITY_TYPE": "WAREHOUSE", "SOURCE_ENTITY_KEY": "wh",
         "COMPLETED_AT": pd.Timestamp(DONE), "UPDATED_AT": pd.Timestamp(DONE)},
        {"ACTION_ID": "a2", "STATUS": "OPEN", "SOURCE_ENTITY_TYPE": "WAREHOUSE", "SOURCE_ENTITY_KEY": "wh",
         "COMPLETED_AT": None, "UPDATED_AT": pd.Timestamp(DONE)},
        {"ACTION_ID": "a3", "STATUS": "DONE", "SOURCE_ENTITY_TYPE": "USER", "SOURCE_ENTITY_KEY": "JDOE",
         "COMPLETED_AT": pd.Timestamp(DONE), "UPDATED_AT": None},
        {"ACTION_ID": "a4", "STATUS": "DONE", "SOURCE_ENTITY_TYPE": "TASK", "SOURCE_ENTITY_KEY": "DB.S.T",
         "COMPLETED_AT": None, "UPDATED_AT": pd.Timestamp(DONE)},   # COALESCE(COMPLETED_AT, UPDATED_AT)
    ])


def test_held_columns_label_every_row_honestly():
    daily = _frame("WAREHOUSE", "WH", _wh_series())
    held, basis = held_columns(_actions(), daily, TODAY, read_ok=True, evaluated={("WAREHOUSE", "WH")}, rate=3.68)
    assert held.tolist() == ["Held 27 days", None, "Not measurable", "Not checked"]
    assert basis.iloc[0].startswith("$") and basis.iloc[1] is None
    failed, _ = held_columns(_actions(), None, TODAY, read_ok=False, evaluated={("WAREHOUSE", "WH")}, rate=3.68)
    assert failed.tolist() == ["Unavailable", None, "Not measurable", "Unavailable"]
    # a DONE row older than the lookback is never measured against a window the read did not cover
    old = _actions().assign(COMPLETED_AT=pd.Timestamp(TODAY - timedelta(days=120)))
    stale, _ = held_columns(old.head(1), daily, TODAY, read_ok=True, evaluated={("WAREHOUSE", "WH")}, rate=3.68)
    assert stale.tolist() == ["Not checked"]
    assert held_columns(None, None, TODAY, read_ok=True, evaluated=(), rate=3.68)[0].empty


# ------------------------------------------------------------- (7) done_entities ----

def test_done_entities_newest_first_deduped_lookback_and_cap():
    rows = [{"STATUS": "DONE", "SOURCE_ENTITY_TYPE": "WAREHOUSE", "SOURCE_ENTITY_KEY": f"WH{i}",
             "COMPLETED_AT": pd.Timestamp(TODAY - timedelta(days=i))} for i in range(1, 60)]
    rows += [{"STATUS": "DONE", "SOURCE_ENTITY_TYPE": "warehouse", "SOURCE_ENTITY_KEY": "wh3",
              "COMPLETED_AT": pd.Timestamp(TODAY - timedelta(days=30))},           # older repeat of WH3
             {"STATUS": "DONE", "SOURCE_ENTITY_TYPE": "WAREHOUSE", "SOURCE_ENTITY_KEY": "OLD",
              "COMPLETED_AT": pd.Timestamp(TODAY - timedelta(days=91))},           # outside the lookback
             {"STATUS": "DROPPED", "SOURCE_ENTITY_TYPE": "WAREHOUSE", "SOURCE_ENTITY_KEY": "DROP",
              "COMPLETED_AT": pd.Timestamp(TODAY)},
             {"STATUS": "DONE", "SOURCE_ENTITY_TYPE": "ALERT", "SOURCE_ENTITY_KEY": "E1",
              "COMPLETED_AT": pd.Timestamp(TODAY)}]
    ents = done_entities(pd.DataFrame(rows), TODAY)
    assert len(ents) == outcomes.MAX_ENTITIES == 40
    assert ents[0] == ("WAREHOUSE", "WH1", TODAY - timedelta(days=1 + outcomes.BASELINE_DAYS))
    wh3 = next(e for e in ents if e[1] == "WH3")
    assert wh3[2] == TODAY - timedelta(days=30 + outcomes.BASELINE_DAYS)      # the EARLIEST done wins
    keys = {k for _, k, _ in ents}
    assert not keys & {"OLD", "DROP", "E1", "WH41"}
    assert done_entities(None, TODAY) == [] and done_entities(pd.DataFrame(), TODAY) == []


# ------------------------------------------------------------ (8) constants and caps ----

def test_row_cap_and_cooldown_pins():
    assert outcomes.LOOKBACK_DAYS == fix_queue.TRACK_COOLDOWN_DAYS
    assert outcomes.MAX_ENTITIES * (outcomes.BASELINE_DAYS + outcomes.LOOKBACK_DAYS + 1) <= config.DEFAULT_MAX_ROWS
    assert {outcomes.REBROKE, outcomes.NOT_FIXED} == outcomes.OVERRIDES_COOLDOWN
    from app.data import workbench_sql
    assert workbench_sql.SIGNAL_MAX_ENTITIES == outcomes.MAX_ENTITIES
    assert set(workbench_sql.SIGNAL_ENTITY_TYPES) == set(outcomes.HELD_TYPES)


# ---------------------------------------------------- Optimize's Done-family helpers ----

def _tracked(rows: list[tuple]) -> pd.DataFrame:
    return pd.DataFrame([{"ENTITY_KEY_U": k, "ACTION_STATUS": s, "OPEN_N": o, "DROPPED_N": d, "DONE_N": n,
                          "LAST_DECIDED": last} for k, s, o, d, n, last in rows])


def test_only_pure_done_families_inside_the_lookback_are_measured():
    t = _tracked([("FP1", "DONE", 0, 0, 1, pd.Timestamp(DONE)),
                  ("FP2", "DONE", 0, 1, 1, pd.Timestamp(DONE)),        # dismissed too: never measured
                  ("FP3", "DONE", 1, 0, 1, pd.Timestamp(DONE)),        # an open item: not Done
                  ("FP4", "DONE", 0, 0, 1, pd.NaT),                    # no decided time
                  ("FP5", "DONE", 0, 0, 1, pd.Timestamp(TODAY - timedelta(days=91))),
                  ("FP6", "DONE", 0, 0, 1, pd.Timestamp(DONE))])
    ents = done_family_entities(t, ["fp6", "FP1", "FP2", "FP3", "FP4", "FP5", "FP1"], TODAY)
    assert ents == [("QUERY_FINGERPRINT", "FP6", DONE - timedelta(days=28)),
                    ("QUERY_FINGERPRINT", "FP1", DONE - timedelta(days=28))]          # keys order, deduped
    assert len(done_family_entities(t, ["FP1", "FP6"], TODAY, limit=1)) == 1
    assert done_family_entities(None, ["FP1"], TODAY) == []


def test_family_outcomes_and_the_cooldown_override_set():
    def fam(key: str, after: float) -> pd.DataFrame:
        ser = {DONE - timedelta(days=i): {"c": 10.0, "r": 100.0, "f": 0.0} for i in range(1, 29)}
        ser.update({DONE + timedelta(days=i): {"c": after, "r": 100.0, "f": 0.0} for i in range(1, 28)})
        return _frame("QUERY_FINGERPRINT", key, ser)
    daily = pd.concat([fam("HELD", 2.0), fam("NOTFIXED", 9.9)], ignore_index=True)
    t = _tracked([(k, "DONE", 0, 0, 1, pd.Timestamp(DONE)) for k in ("HELD", "NOTFIXED", "NODATA")])
    out = family_outcomes(t, daily, TODAY, ["HELD", "NOTFIXED", "NODATA"])
    assert {k: v["state"] for k, v in out.items()} == {"HELD": outcomes.HELD, "NOTFIXED": outcomes.NOT_FIXED,
                                                       "NODATA": outcomes.NOT_MEASURABLE}
    assert set(rebroke_families(t, daily, TODAY, ["HELD", "NOTFIXED", "NODATA"])) == {"NOTFIXED"}


def test_labels_never_carry_iso_dates_or_dollar_signs():
    ser = _wh_series()
    ser.update({DONE + timedelta(days=i): {"c": 120.0} for i in range(15, 28)})
    lbl = action_held("WAREHOUSE", "WH", DONE, _frame("WAREHOUSE", "WH", ser), TODAY)["label"]
    assert "2026-" not in lbl and "$" not in lbl


# ------------------------------------------- UI wiring (source; runs on the floor leg too) ----

def _wb_body(name: str) -> str:
    from tests._source import read
    return read("app/ui/workbench.py").split(f"def {name}(", 1)[1].split("\ndef ", 1)[0]


def test_with_held_reads_once_only_for_measurable_done_rows():
    body = _wb_body("_with_held")
    ents = body.index("ents = outcomes.done_entities(frame, today, type_col=type_col, key_col=key_col)")
    gate = body.index("if ents:")
    rd = body.index("run(workbench_sql.entity_daily_signals(ents)")
    assert ents < gate < rd and body.count("run(") == 1
    assert 'tier="recent", probe=True' in body and "ACCOUNT_USAGE" not in body
    assert "read_ok = bool(res.ok)" in body and "outcomes.held_columns(frame, daily, today, read_ok=read_ok," in body
    assert "evaluated={(t, k) for t, k, _ in ents}" in body


def test_action_center_measures_completed_work_only_with_include_completed():
    body = _wb_body("render_action_center")
    gate = body.index("if include_closed and extended:")
    held = body.index('frame = _with_held(frame, key="action_held_signals")')
    assert gate < held < body.index("display = frame.reset_index(drop=True)")
    assert "The signals are account-wide, not company-filtered." in body
    ac_list = body.split("def _ac_list(", 1)[1][:1200]
    assert '"Held?"' in ac_list and '"DEFER_UNTIL"' in ac_list
    detail = _wb_body("_render_action_detail")
    assert '*([(f"Held? {_held_lbl}", _held_severity(_held_lbl))] if _held_lbl else [])' in detail
    assert "reopen it with Status: OPEN." in detail and "st.caption(md_dollars(" in detail
    # a tracked DETAIL carries dollar figures; the markdown sink escapes them
    assert 'st.write(md_dollars(str(row.get("DETAIL"))))' in detail


def test_entity_360_computes_held_before_the_work_and_outcomes_header():
    body = _wb_body("render_entity_360")
    rel = body.index("related = run(")
    held = body.index("_rel_held = (kind in outcomes.HELD_TYPES and related.usable()")
    call = body.index("    if _rel_held:\n        _rel_df = (_with_held(related.df.assign(SOURCE_ENTITY_TYPE=kind, "
                      "SOURCE_ENTITY_KEY=key),")
    assert 'related.df["STATUS"].astype(str).str.upper().eq("DONE").any()' in body[held:call]
    header = body.index('st.markdown("**Work and outcomes**")')
    assert rel < held < call < header
    assert 'styled_table(_rel_df.drop(columns=["HELD_BASIS"], errors="ignore"), height=240,' in body
    assert "Newest completed item — Held?" in body
