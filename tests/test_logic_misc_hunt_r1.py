"""Locks for the R1 logic-misc bug hunt (app/logic modules outside the cost/ops/DS clusters).

Each block names the finding it locks. The behaviour locks failed on the pre-fix code; the guard
locks beside them (what a fix must NOT change) pass on both."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta

import pandas as pd
import sqlglot

from app.data import change_impact_sql
from app.logic import wh_change
from app.logic.forecast import month_end_projection
from tests._source import read

# ---- R1-065 / R1-121: the warehouse change tiles count the whole window, PENDING only ----------


def test_warehouse_change_registry_carries_untruncated_window_totals():
    sql = change_impact_sql.warehouse_change_registry(90, "ALFA")
    assert "COUNT(*) OVER () AS TOTAL_CHANGES" in sql
    assert "COUNT_IF(w.VERDICT = 'REGRESSED') OVER () AS TOTAL_REGRESSED" in sql
    assert "COUNT_IF(w.VERDICT = 'IMPROVED') OVER () AS TOTAL_IMPROVED" in sql
    assert "COUNT_IF(w.VERDICT = 'PENDING') OVER () AS TOTAL_PENDING" in sql
    assert "LIMIT 200" in sql            # the table is still the newest 200
    sqlglot.parse(sql, dialect="snowflake")


def test_registry_kpis_reads_the_window_totals_not_the_capped_frame():
    # 260 changes in the window, the newest 200 returned: 30 regressed among them, 90 overall
    df = pd.DataFrame({"VERDICT": ["REGRESSED"] * 30 + ["IMPROVED"] * 170,
                       "TOTAL_CHANGES": [260] * 200, "TOTAL_REGRESSED": [90] * 200,
                       "TOTAL_IMPROVED": [165] * 200, "TOTAL_PENDING": [5] * 200})
    assert wh_change.registry_kpis(df) == {
        "changes": 260, "regressed": 90, "improved": 165, "pending": 5}


def test_registry_kpis_never_counts_no_baseline_as_pending():
    df = pd.DataFrame({"VERDICT": ["NO_BASELINE", "NEUTRAL"]})
    assert wh_change.registry_kpis(df)["pending"] == 0


def test_wh_change_block_says_when_the_table_is_the_newest_slice():
    block = read("app/ui/pages/operations.py").split("def _wh_change_block", 1)[1].split("\ndef ", 1)[0]
    assert "if k[\"changes\"] > len(df):" in block
    assert "Table below shows the latest {len(df)} of {k['changes']:,} tracked" in block
    assert "f\"{k['changes']:,}\"" in block


# ---- R1-078: month-end gap fill also covers the lagged first days of a month -------------------


def _flat_days(start: date, end: date, usd: float = 100.0) -> pd.DataFrame:
    """$usd on every day from start to end, both inclusive."""
    return pd.DataFrame([{"DAY": start + timedelta(days=i), "USD": usd}
                         for i in range((end - start).days + 1)])


def test_month_end_fills_the_lagged_first_days_of_the_month():
    hist = _flat_days(date(2026, 6, 1), date(2026, 9, 30))       # Oct 1 (and Oct 2) not loaded yet
    for engine in ("linear", "seasonal"):
        # Oct 2, Oct 1 lagging: was 3000 (the lagged day counted nowhere); 31 x $100 = 3100
        assert month_end_projection(hist, date(2026, 10, 2), engine=engine).projected_usd == 3100.0
        # Oct 3, Oct 1 and Oct 2 lagging: was 2900
        assert month_end_projection(hist, date(2026, 10, 3), engine=engine).projected_usd == 3100.0


def test_month_start_density_guard_still_blocks_a_sparse_account():
    # every 3rd day only (5 of the trailing 14 present): the lagged Oct 1 is NOT filled
    sparse = pd.DataFrame([{"DAY": date(2026, 9, 30) - timedelta(days=i), "USD": 100.0}
                           for i in range(0, 120, 3)])
    p = month_end_projection(sparse, date(2026, 10, 2), engine="linear").projected_usd
    assert p == 3000.0      # 0 complete MTD + $100 x 30 (today + 29 remaining); no fabricated Oct 1


# ---- R1-079: no COLUMN_HELP entry pairs two "$" into a LaTeX span in a header tooltip ----------


def test_every_column_help_entry_has_at_most_one_dollar():
    from app.logic.metric_registry import COLUMN_HELP
    # Header help renders markdown, where two "$" pair into inline math. Covers every entry, not
    # only the keys an earlier lock enumerated (SPEND_USD / USD carried two and slipped through).
    assert {k: v for k, v in COLUMN_HELP.items() if v.count("$") > 1} == {}


def test_usd_column_help_names_the_settings_rates_not_literals():
    from app.logic.metric_registry import COLUMN_HELP
    for key in ("SPEND_USD", "USD"):
        text = COLUMN_HELP[key]
        assert "CREDIT_PRICE_USD" in text and "AI_CREDIT_PRICE_USD" in text, key
        assert "3.68" not in text and "2.20" not in text, key


# ---- R1-093: AI-user tenure and the projection divisor count Central calendar days -------------


def _evening_starter(first_usage: object) -> pd.DataFrame:
    return pd.DataFrame({"USER_NAME": ["eve"], "FIRST_USAGE": [first_usage], "TOTAL_CREDITS": [40.0],
                         "AVG_DAILY_CREDITS": [10.0], "CREDITS_PER_REQUEST": [1.0], "TOTAL_REQUESTS": [40]})


def test_evening_first_usage_counts_its_central_day(monkeypatch):
    from app.logic import cortex
    monkeypatch.setattr(cortex, "account_today", lambda: date(2026, 9, 30))
    # Sep 27 20:30 Central is Sep 28 in UTC; Sep 27..30 is 4 Central calendar days (was 3)
    for first in (pd.Timestamp("2026-09-27 20:30", tz="America/Chicago"),
                  pd.Timestamp("2026-09-28 01:30", tz="UTC"),          # the same instant, UTC offset
                  pd.Timestamp("2026-09-27 20:30")):                   # naive = Central wall time
        rollup = _evening_starter(first)
        assert cortex.effective_window_days(rollup, 30) == 4, first
        enriched = cortex.enrich_user_rollup(rollup, 2.20, 30)
        assert float(enriched["OBSERVABLE_DAYS"].iloc[0]) == 4, first
        assert float(enriched["TENURE_DAYS"].iloc[0]) == 4, first
        assert float(enriched["PROJECTED_30D_CREDITS"].iloc[0]) == 300.0, first   # was 400


def test_unparseable_first_usage_keeps_the_asked_window(monkeypatch):
    from app.logic import cortex
    monkeypatch.setattr(cortex, "account_today", lambda: date(2026, 9, 30))
    rollup = _evening_starter(None)
    assert cortex.effective_window_days(rollup, 30) == 30
    assert float(cortex.enrich_user_rollup(rollup, 2.20, 30)["OBSERVABLE_DAYS"].iloc[0]) == 30


# ---- R1-102: the alert-explain prompt's instructions never license an answer's figures ---------


def _alert_prompt(detail: str = "") -> str:
    from app.logic.ai_prompts import alert_evidence_prompt
    rows = pd.DataFrame({"DAY": ["2026-09-28"], "SERVICE_TYPE": ["AI_SERVICES"], "CREDITS_BILLED": [12.5]})
    return alert_evidence_prompt("cortex", "AI spend spike", detail, rows, "this week vs prior 7 days")


def test_alert_prompt_instruction_numbers_do_not_ground_figures():
    from app.logic.ai_grounding import check_grounding, evidence_section
    prompt = _alert_prompt()
    assert "EVIDENCE ROWS:" in prompt
    # '(1) ... 1-2 ... (3) ... Max 150 words' and 'prior 7 days' sit ahead of the marker now
    answer = "AI spend jumped 150% to $150; 2% of the account, about $3 per day; costs $7 more, up 7%."
    check = check_grounding(answer, evidence_section(prompt))
    assert set(check.ungrounded) == {"$150", "150%", "2%", "$3", "$7", "7%"}


def test_alert_prompt_title_detail_and_rows_still_ground_their_own_figures():
    from app.logic.ai_grounding import check_grounding, evidence_section
    prompt = _alert_prompt(detail="AI spend up 85% ($412) vs the prior week")
    section = evidence_section(prompt)
    assert "ALERT: AI spend spike" in section and "DETAIL: AI spend up 85%" in section
    assert "CREDITS_BILLED=12.5" in section
    assert check_grounding("AI spend rose 85% to $412.", section).ok
    # instructions still lead the prompt (AIP-2 ordering), ahead of the evidence
    assert prompt.index("Never invent") < prompt.index("EVIDENCE ROWS:") < prompt.index("- DAY=")


# ---- R1-106: the idle-warehouse prompt names a calendar preset's own dates ----------------------


def test_idle_prompt_names_last_month_not_last_n_days(monkeypatch):
    from app.logic import ai_prompts, date_windows
    monkeypatch.setattr(date_windows, "account_today", lambda: date(2026, 9, 30))
    advisor = pd.DataFrame({"WAREHOUSE_NAME": ["WH_A"], "IDLE_USD": [12.0]})
    last_month = ai_prompts.idle_warehouse_prompt(advisor, "ALL", 31,
                                                  bounds=(date(2026, 8, 1), date(2026, 9, 1)))
    assert "last month (Aug 1 - Aug 31, 2026)" in last_month
    assert "last 31 days" not in last_month              # Aug 31-Sep 30 on Sep 30: the wrong dates
    current = ai_prompts.idle_warehouse_prompt(advisor, "ALL", 30,
                                               bounds=(date(2026, 9, 1), date(2026, 10, 1)))
    assert "the current month (Sep 1 - Sep 30, 2026)" in current
    # a trailing window keeps its wording; the dates sit ahead of the evidence marker
    trailing = ai_prompts.idle_warehouse_prompt(advisor, "ALL", 30)
    assert "Idle warehouse analysis for ALL, last 30 days." in trailing
    assert last_month.index("Aug 31, 2026") < last_month.index("EVIDENCE ROWS:")


def test_optimize_passes_the_bounds_to_the_idle_prompt():
    assert "idle_warehouse_prompt(advisor, company, idle_days, bounds=bounds)" in read(
        "app/ui/pages/cost_parts/optimize.py")


def _idle_ai_panel_key(today: date, preset: str) -> str:
    """The idle AI panel's session key as optimize.py builds it, for ``preset`` viewed on ``today``."""
    import functools

    from app.logic import date_windows as dw
    src = read("app/ui/pages/cost_parts/optimize.py")
    call = src.split('subject="evaluate idle warehouse spend"', 1)[0].rsplit("ai_evaluation_panel(", 1)[1]
    m = re.search(r'\bkey=(f"[^"\n]*")', call)
    assert m is not None
    bounds = dw.window_bounds(preset, today)
    names = {"__builtins__": {}, "company": "ALL", "days": dw.resolve_window_days(preset, today),
             "_lm": "_lm" if bounds is not None else "", "bounds": bounds,
             "window_label": functools.partial(dw.window_label, today=today)}
    return str(eval(m.group(1), names))


def test_idle_ai_panel_key_separates_calendar_presets_of_equal_length():
    # R1-106 review: the prompts now name each preset's own dates, but the stored answer was keyed
    # idle_{company}_{days}{_lm}, and {_lm} is the same for all three presets. On Oct 31 Current month
    # (Oct 1-31) and Last month (Sep 1-30) are both 30 days, so one preset's answer rendered under
    # the other's panel; on Feb 1 Current year (Jan 1-Feb 1) and Last month (January) are both 31.
    from app.config import CURRENT_MONTH_WINDOW, CURRENT_YEAR_WINDOW, LAST_MONTH_WINDOW
    from app.logic import date_windows as dw
    presets = (CURRENT_MONTH_WINDOW, LAST_MONTH_WINDOW, CURRENT_YEAR_WINDOW)
    for today, a, b in ((date(2026, 10, 31), CURRENT_MONTH_WINDOW, LAST_MONTH_WINDOW),
                        (date(2026, 12, 31), CURRENT_MONTH_WINDOW, LAST_MONTH_WINDOW),
                        (date(2027, 2, 1), CURRENT_YEAR_WINDOW, LAST_MONTH_WINDOW)):
        assert dw.resolve_window_days(a, today) == dw.resolve_window_days(b, today)   # the collision
        assert _idle_ai_panel_key(today, a) != _idle_ai_panel_key(today, b), today
        # one key per distinct evidence window (January's current month == current year: same dates)
        windows = {dw.window_bounds(p, today) for p in presets}
        assert len({_idle_ai_panel_key(today, p) for p in presets}) == len(windows), today
    # a trailing window keeps a key of its own
    assert _idle_ai_panel_key(date(2026, 10, 31), 30) == "idle_ALL_30_30d"


# ---- R1-105: Case File previews render NULL cells as "—", never 'nan' / 'None' / 'NaT' ---------


def test_case_file_preview_renders_nulls_as_a_dash():
    import numpy as np

    from app.logic import case_file as cf
    # a locked-out user's failed-login burst: no success after it, so three NULL columns
    item = cf.new_case_item(
        section="Security", title="Failed-login bursts",
        preview_columns=["USER_NAME", "FAILURES", "FIRST_SUCCESS_AFTER", "BREAKTHROUGH_MIN", "LAST_ERROR"],
        preview_rows=[["SVC_A", 12, pd.NaT, np.nan, None], ["SVC_B", pd.NA, "2026-09-30", 4.5, "locked"]])
    assert item["preview"]["rows"] == [["SVC_A", "12", "—", "—", "—"],
                                       ["SVC_B", "—", "2026-09-30", "4.5", "locked"]]
    md = cf.assemble_markdown([item], generated="g")
    assert "| SVC_A | 12 | — | — | — |" in md
    for token in ("nan", "NaT", "None", "<NA>"):
        assert token not in md, token


def test_add_to_case_sink_turns_every_null_kind_into_none():
    # the exact expression add_to_case_button uses, on a frame shaped like the failed-login read
    head = pd.DataFrame({"USER_NAME": ["SVC_A"], "FAILURES": pd.array([None], dtype="Int64"),
                         "FIRST_SUCCESS_AFTER": [pd.NaT], "BREAKTHROUGH_MIN": [float("nan")],
                         "LAST_ERROR": [None]})
    rows = head.astype(object).where(head.notna(), None).to_numpy().tolist()
    assert rows == [["SVC_A", None, None, None, None]]


def test_add_to_case_hands_raw_cells_with_nulls_as_none():
    comp = read("app/ui/components.py")
    body = comp.split("def add_to_case_button", 1)[1].split("\ndef ", 1)[0]
    assert "head.astype(str)" not in body                 # stringified NULLs before case_file saw them
    assert "preview_rows=head.astype(object).where(head.notna(), None).to_numpy().tolist()" in body


# ---- R1-112: no idle evidence is not 0% idle in the adaptive-compute candidacy -----------------


def _bursty_80pct_idle():
    from app.logic.adaptive import adaptive_compute_candidacy
    # 1 cr/hr 08-17 with a 15 cr peak at 12-14: bursty (6.7x peak-to-mean), ~52 cr/day
    prof = {**dict.fromkeys(range(8, 18), 1.0), 12: 15.0, 13: 15.0, 14: 15.0}
    hourly = pd.DataFrame({"WAREHOUSE_NAME": ["WH_B"] * len(prof), "HOUR_OF_DAY": list(prof),
                           "AVG_CREDITS": list(prof.values())})
    idle = pd.DataFrame({"WAREHOUSE_NAME": ["WH_B"], "TOTAL_CREDITS": [1000.0], "IDLE_CREDITS": [800.0]})
    other = pd.DataFrame({"WAREHOUSE_NAME": ["WH_OTHER"], "TOTAL_CREDITS": [10.0], "IDLE_CREDITS": [1.0]})
    return adaptive_compute_candidacy, hourly, idle, other


def test_adaptive_with_idle_evidence_routes_heavy_idle_to_auto_suspend():
    score, hourly, idle, _ = _bursty_80pct_idle()
    row = score(hourly, idle).iloc[0]
    assert row["VERDICT"] == "Auto-suspend first" and float(row["IDLE_PCT"]) == 80.0


def test_adaptive_without_idle_evidence_shows_no_idle_pct_and_no_discount():
    score, hourly, _, other = _bursty_80pct_idle()
    # the idle read failed (None), or the warehouse is missing from the LIMIT-100 idle frame
    for idle in (None, other):
        row = score(hourly, idle).iloc[0]
        assert pd.isna(row["IDLE_PCT"]), idle                 # "—", never a made-up 0%
        assert "idle n/a" in row["RATIONALE"]
        assert row["VERDICT"] == "Strong candidate"           # no discount, and no idle override
        assert int(row["SCORE"]) == 100


def test_adaptive_panel_surfaces_a_failed_idle_read():
    from tests._source import read as _read
    body = _read("app/ui/pages/operations.py").split("def _adaptive_candidacy_panel", 1)[1].split("\ndef ", 1)[0]
    i = body.index("if not idle.ok:")
    assert 'empty_state("unavailable", "The idle read failed' in body[i:i + 600]
    assert "detail=str(idle.error" in body[i:i + 600]


# ---- R1-120: a warehouse with QAS spend under the floor is not "QAS off" ----------------------


def test_low_qas_spend_with_eligible_workload_is_not_an_enable_candidate():
    from app.logic.serverless_roi import classify_qas_roi
    for spend in (0.01, 3.0, 4.99):
        v = classify_qas_roi(spend, 400)
        assert v.action != "enable" and "off" not in v.verdict, spend
        assert v.action == "keep" and "low spend" in v.verdict, spend
    # no spend at all is still the enable opportunity, and >= the floor is still "Working"
    assert classify_qas_roi(0.0, 400).action == "enable"
    assert classify_qas_roi(5.0, 400).verdict.startswith("Working")


# ---- R1-122: the drill streak counts consecutive calendar months, not passing events ----------
# Every call pins ``now``: since the streak is anchored on the account calendar (R1-122 review), a
# test that leaves it to the real clock would go red as soon as its rows are older than a month.


def _drill(raised: str, ok: bool = True) -> dict:
    return {"RAISED_AT": raised, "NOTIFIED_AT": raised if ok else None, "ACK_AT": raised if ok else None}


def test_drill_streak_breaks_on_a_missing_month():
    from app.logic.drill import drill_report
    # Sep and Jul passed, no August drill at all (task suspended): the streak is 1, not 2
    df = pd.DataFrame([_drill("2026-09-01 09:00"), _drill("2026-07-01 09:00")])
    assert drill_report(df, now=datetime(2026, 9, 20))["streak_months"] == 1


def test_drill_streak_is_one_outcome_per_month():
    from app.logic.drill import drill_report
    # two passing rows in September (a hand insert) count once; Aug passed; Jul failed
    df = pd.DataFrame([_drill("2026-09-15 09:00"), _drill("2026-09-01 09:00"),
                       _drill("2026-08-01 09:00"), _drill("2026-07-01 09:00", ok=False)])
    report = drill_report(df, now=datetime(2026, 9, 20))
    assert report["streak_months"] == 2
    assert report["last"]["delivered"] and report["last"]["acked"]
    # a year boundary is consecutive: Jan after Dec
    df = pd.DataFrame([_drill("2027-01-01 09:00"), _drill("2026-12-01 09:00")])
    assert drill_report(df, now=datetime(2027, 1, 10))["streak_months"] == 2


def test_drill_streak_is_zero_while_the_newest_drill_is_months_old():
    from app.logic.drill import drill_report
    # task suspended since June, viewed in September: Jul/Aug/Sep wrote no row. The walk used to
    # start at June's pass and showed a green 3-month streak; the gap that is still open breaks it.
    df = pd.DataFrame([_drill("2026-06-01 09:00"), _drill("2026-05-01 09:00"), _drill("2026-04-01 09:00")])
    report = drill_report(df, now=datetime(2026, 9, 20))
    assert report["ran"] and report["streak_months"] == 0
    assert report["last"]["delivered"] and report["last"]["acked"]   # the last drill itself did pass
    # one missed month is enough: viewed in July, June's pass is no longer the newest due month
    assert drill_report(df, now=datetime(2026, 7, 20))["streak_months"] == 0
    assert drill_report(df, now=datetime(2026, 6, 20))["streak_months"] == 3


def test_drill_month_is_due_an_hour_after_the_first_of_month_run():
    from app.logic.drill import drill_report
    df = pd.DataFrame([_drill("2026-09-01 09:00"), _drill("2026-08-01 09:00")])
    # before (and within an hour of) the 09:00 CT run on Oct 1, September is still the newest due month
    assert drill_report(df, now=datetime(2026, 10, 1, 8, 30))["streak_months"] == 2
    assert drill_report(df, now=datetime(2026, 10, 1, 9, 59))["streak_months"] == 2
    # from 10:00 CT, October's drill is due and missing
    assert drill_report(df, now=datetime(2026, 10, 1, 10, 0))["streak_months"] == 0
    assert drill_report(df, now=datetime(2026, 10, 15))["streak_months"] == 0
    # an October drill that already ran (a manual EXECUTE TASK before 09:00) is the newest outcome
    early = pd.DataFrame([_drill("2026-10-01 07:00"), *df.to_dict("records")])
    assert drill_report(early, now=datetime(2026, 10, 1, 8, 0))["streak_months"] == 3
    # an aware ``now`` is read on the account clock: 14:30 UTC = 09:30 CDT, 15:30 UTC = 10:30 CDT
    assert drill_report(df, now=datetime(2026, 10, 1, 14, 30, tzinfo=UTC))["streak_months"] == 2
    assert drill_report(df, now=datetime(2026, 10, 1, 15, 30, tzinfo=UTC))["streak_months"] == 0


def test_drill_streak_defaults_to_the_account_clock(monkeypatch):
    from app.logic import drill
    df = pd.DataFrame([_drill("2026-06-01 09:00"), _drill("2026-05-01 09:00")])
    monkeypatch.setattr(drill, "account_now", lambda: datetime(2026, 9, 20, 12, 0))
    assert drill.drill_report(df)["streak_months"] == 0
    monkeypatch.setattr(drill, "account_now", lambda: datetime(2026, 6, 20, 12, 0))
    assert drill.drill_report(df)["streak_months"] == 2


def test_drill_due_hour_matches_the_drill_task_schedule():
    from app.logic import drill
    from app.logic.formulas import ACCOUNT_TIMEZONE
    sql = read("snowflake/alert_drill.sql")
    m = re.search(r"SCHEDULE = 'USING CRON (\d+) (\d+) (\d+) \* \* ([\w/]+)'", sql)
    assert m is not None
    minute, hour, day, tz = m.groups()
    assert (int(minute), int(hour), int(day), tz) == (0, drill.DRILL_HOUR, drill.DRILL_DAY, ACCOUNT_TIMEZONE)


# ---- R1-123: threshold suggestions read UNTAGGED_N and do not overstate their evidence --------


def _rule_events(noise, actioned=(), untagged=None):
    rows = [{"RULE_ID": "COST_SPIKE", "METRIC_VALUE": v, "RESOLUTION_KIND": "NOISE"} for v in noise]
    rows += [{"RULE_ID": "COST_SPIKE", "METRIC_VALUE": v, "RESOLUTION_KIND": "ACTIONED"} for v in actioned]
    frame = pd.DataFrame(rows)
    if untagged is not None:
        frame["UNTAGGED_N"] = untagged
    return frame


def test_suggestion_basis_names_tagged_resolutions_and_flags_untagged_majority():
    from app.logic.tuning import suggestions_by_rule
    out = suggestions_by_rule(_rule_events([10, 11, 12, 11, 10], untagged=300), {"COST_SPIKE": 8.0})
    row = out.iloc[0]
    assert int(row["UNTAGGED_N"]) == 300                          # the fetched denominator is read
    assert row["BASIS"].startswith("All 5 tagged resolutions with a metric value were noise;")
    assert "All 5 resolved events" not in row["BASIS"]
    assert "300 more closed untagged; tag them before trusting this." in row["BASIS"]
    assert float(row["SUGGESTED_THRESHOLD"]) == 12.98              # threshold math unchanged


def test_suggestion_without_an_untagged_majority_carries_no_caveat():
    from app.logic.tuning import suggestions_by_rule
    out = suggestions_by_rule(_rule_events([10, 11, 12, 11, 10], untagged=3), {"COST_SPIKE": 8.0})
    assert int(out.iloc[0]["UNTAGGED_N"]) == 3 and "Caveat" not in out.iloc[0]["BASIS"]
