"""Round-2 review fixes in the alert logic (cluster e4): Investigate routing and filters, per-alert evidence,
threshold tuning, the triage feed dedupe, the delivery banner's cache tier, the ETL clock parse and the
in-progress metering day.

Each test is built from the CURRENT raiser procs' title shapes (re-read from the migrations, last definition wins)
where it can be, so a new arm or a reworded title fails here instead of silently mis-routing.
"""

from __future__ import annotations

import re
from datetime import date

import pandas as pd
import pytest

from app.data import alert_evidence_sql, recheck_sql
from app.logic import actions, navigate, tuning
from app.logic.alert_evidence import GENERIC_EVIDENCE_RULES, plan_for_alert
from tests.test_alert_rule_consistency import _config_enabled, _raised_rule_ids, _raiser_bodies

_INSERT = "INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS"


def _arms() -> list[tuple[str, set[str], str]]:
    """(proc, rule ids, statement) for every ALERT_EVENTS INSERT in the latest raiser bodies."""
    out = []
    for proc, body in _raiser_bodies().items():
        for chunk in body.split(_INSERT)[1:]:
            stmt = chunk.split(";\n", 1)[0]
            rids = set(re.findall(r"RULE_ID\s*=\s*'([A-Z_]+)'", stmt))
            rids |= set(re.findall(r"SELECT\s+'([A-Z]+_[A-Z0-9_]+)'", stmt))
            out.append((proc, rids, stmt))
    return out


def _live_rules() -> set[str]:
    return _raised_rule_ids() & _config_enabled()


# ------------------------------------------------------------------------------------ navigate: R2-037 ----

_USER_LED_TITLE = re.compile(r"^\s*(?:LEFT\(\s*)?[A-Za-z_]\w*\.(?:USER_NAME|GRANTEE_NAME)\s*\|\|", re.M)


def test_r2_037_every_user_led_title_gets_no_entity_filter():
    """A dotted user name (first.last.name) reads as DB.SCHEMA. to _DB_RE, so every arm whose TITLE leads with a
    user name must be carved out -- found by scanning the arms, so the next such arm cannot slip through."""
    user_led = {rid for _p, rids, stmt in _arms() if _USER_LED_TITLE.search(stmt) for rid in rids}
    assert {"SEC_CRED_EXPIRY", "SEC_NEW_ADMIN_NETWORK", "SEC_FAILED_LOGINS", "COST_AI_USER_RUNAWAY"} <= user_led
    missing = user_led - navigate._NO_ENTITY_FILTER_RULES
    assert not missing, f"user-led titles that would set a bogus database filter: {sorted(missing)}"


# A user-name column concatenated at ANY position of an arm's TITLE or DETAIL (investigation_target reads both, as
# the drawer passes them): before or after a '||', bare or inside LEFT(..)/COALESCE(..). OWNER_HINT is V160's
# 'User ' || USER_NAME / '<APP> · ' || USER_NAME (asserted below). DEDUPE_KEY lines ('<alias>.RULE_ID || '|'') are
# never shown, so they are skipped.
_USER_COLS = r"(?:USER_NAME|GRANTEE_NAME|OWNER_HINT)"
_USER_CONCAT = re.compile(
    rf"\|\|\s*(?:(?:LEFT|COALESCE)\(\s*)?[A-Za-z_]\w*\.{_USER_COLS}\b"
    rf"|\b[A-Za-z_]\w*\.{_USER_COLS}\b(?:\s*,\s*[^)]*\))?\s*\|\|")
_DEDUPE_LINE = re.compile(r"RULE_ID\s*\|\|\s*'\|'")


def _user_named_rules() -> set[str]:
    return {rid for _p, rids, stmt in _arms()
            if any(_USER_CONCAT.search(line) for line in stmt.splitlines() if not _DEDUPE_LINE.search(line))
            for rid in rids}


def test_r2_037_a_user_name_anywhere_in_the_text_never_sets_a_database_filter():
    """The line-start scan above missed COST_SLEEP_POLLING, whose title ENDS with the poller's owner ('User
    first.last.name'), so its Investigate set a sticky database filter FIRST. Every arm that shows a user name at
    any position must be carved out of the database filter (or out of every entity filter)."""
    flagged = _user_named_rules()
    assert {"SEC_CRED_EXPIRY", "SEC_NEW_ADMIN_NETWORK", "SEC_FAILED_LOGINS", "COST_AI_USER_RUNAWAY",
            "SEC_LOGIN_TAKEOVER", "SEC_ADMIN_GRANT", "COST_SLEEP_POLLING"} <= flagged, sorted(flagged)
    carved = navigate._NO_ENTITY_FILTER_RULES | navigate._NO_DATABASE_FILTER_RULES
    missing = flagged - carved
    assert not missing, f"user names in the alert text that would set a bogus database filter: {sorted(missing)}"
    # OWNER_HINT really is a user name: SP_SCAN_SLEEP_POLLING builds it from USER_NAME (cs_driver.owner_hint twin)
    body = _raiser_bodies()["SP_SCAN_SLEEP_POLLING"]
    assert "'User ' || a.USER_NAME" in body and "ap.USER_TOP_APP || ' · ' || a.USER_NAME" in body


@pytest.mark.parametrize("owner", ["User first.last.name", "ControlM · first.last.name"])
def test_r2_037_sleep_polling_keeps_its_warehouse_and_gets_no_database(owner):
    title = f"WH_ALFA_LOAD sleep polling ~$105/week: {owner}"
    detail = f"Sleep polling on 7 of the 7 complete days ... Owner: {owner}. Next step: ..."
    want = {"warehouse_contains": "WH_ALFA_LOAD"}
    assert navigate.investigation_target("COST_SLEEP_POLLING", title)["filters"] == want
    assert navigate.investigation_target("COST_SLEEP_POLLING", title, detail)["filters"] == want
    assert navigate.investigation_target("COST_SLEEP_POLLING", f"{title} {detail}")["filters"] == want
    assert navigate.investigation_target("COST_SLEEP_POLLING", "WH_X sleep polling ~$1/week: User first.last.name"
                                         )["filters"] == {"warehouse_contains": "WH_X"}
    # the 'No warehouse' series names no warehouse, not even one hidden in a user name
    assert navigate.rule_warehouse("COST_SLEEP_POLLING", "No warehouse sleep polling ~$30/week: User WH_SVC.A.B") == ""
    # the carve-out is rule-scoped: an entity rule reading the same text still extracts the (bogus) database
    assert navigate.investigation_target("PERF_SPILL_GB", title)["filters"].get("database") == "FIRST"


@pytest.mark.parametrize(("rule", "text"), [
    ("SEC_NEW_ADMIN_NETWORK", "first.last.name logged in from new network 10.1.2.3 First seen 2026-09-30"),
    ("SEC_CRED_EXPIRY", "first.last.name programmatic_access_token 'MY_PAT' expires in 5 day(s) Rotate before"),
    ("SEC_FAILED_LOGINS", "first.last.name had 12 failed logins on 2026-09-30 WH_X"),
    ("DQ_RECON_ERROR", "42 reconciliation error(s) across 3 metric(s) Metric(s): POLICY.PREMIUM.AMT, CLM.PAID.X"),
])
def test_r2_037_user_text_sets_no_sticky_filter(rule, text):
    assert navigate.investigation_target(rule, text)["filters"] == {}
    # the carve-out is rule-scoped: an entity rule reading the same text still extracts the (bogus) database
    assert navigate.investigation_target("PERF_SPILL_GB", text)["filters"].get("database") in ("FIRST", "POLICY")


# ------------------------------------------------------------------------- navigate: R2-088 / R2-091 ----

@pytest.mark.parametrize(("rule", "page", "section"), [
    ("DQ_BREACH", "Operations", "Pipeline SLA"),
    ("DQ_RECON_ERROR", "Operations", "Pipeline SLA"),
    ("DQ_SCHEMA_DRIFT", "Security", "Changes"),
    ("WH_CHANGE_REGRESSION", "Operations", "Change impact"),
    ("COST_CONTRACT_BREACH", "Cost Intelligence", "Contract & Forecast"),
    ("COST_ORG_ACCOUNT_CREEP", "Cost Intelligence", "Contract & Forecast"),
    ("PIPE_TASK_FAILURES", "Operations", "Tasks"),
    ("SEC_NEW_EXPOSURE", "Security", "Changes"),
    # no monthly-budget content on Contract & Forecast: the pace / forecast rules keep the COST default
    ("COST_BUDGET_PACE", "Cost Intelligence", "Spend & Attribution"),
    ("COST_FORECAST_BREACH", "Cost Intelligence", "Spend & Attribution"),
])
def test_r2_088_091_investigate_lands_where_the_playbook_points(rule, page, section):
    got = navigate.investigation_target(rule, "")
    assert (got["page"], got["section"]) == (page, section)


def test_r2_088_only_ops_rules_land_on_overview():
    """Overview has no panel for any alert family; only the OPS_* self-watch rules land there on purpose (their
    home, Admin, is off most profiles)."""
    live = _live_rules()
    assert len(live) >= 40, sorted(live)
    stray = sorted(r for r in live if navigate.investigation_target(r, "")["page"] == "Overview"
                   and not r.startswith("OPS_"))
    assert not stray, f"live rules whose Investigate lands on Overview: {stray}"


def test_r2_091_every_family_default_serves_a_live_rule():
    """The BUDGET / TASK defaults matched no rule id, so the routes they promised never happened."""
    live = _live_rules()
    for prefix, _target in navigate._FAMILY_DEFAULTS:
        assert any(r.startswith(prefix) for r in live), f"dead family default {prefix!r}"


# ---------------------------------------------------------------------------------- navigate: R2-092 ----

def test_r2_092_title_shapes_match_the_raisers():
    """The anchored title shapes rule_warehouse reads are the ones the current raisers write."""
    bodies = "\n".join(_raiser_bodies().values())
    for sql in ("f.WAREHOUSE_NAME || ' used ' || ROUND(", "q.WAREHOUSE_NAME || ' queued ' || ROUND(",
                "q.WAREHOUSE_NAME || ' spilled ' || ROUND(", "o.WAREHOUSE_NAME || ' idle waste ~$'",
                "'WAREHOUSE ' || WAREHOUSE_NAME AS SERIES",
                "l.SERIES || IFF(l.SIGNED_Z < 0, ' collapsed to ', ' spiked to ') ||",
                "'Warehouse ' || r.WAREHOUSE_NAME || ' regressed after '",
                "IFF(o.WAREHOUSE_NAME = 'NONE', 'No warehouse', o.WAREHOUSE_NAME) || ' sleep polling ~$'"):
        assert sql in bodies, sql


@pytest.mark.parametrize(("rule", "title", "want"), [
    ("COST_WH_DAILY_CREDITS", "BLCOMPUTE_WH used 80.2 credits on 2026-09-30", "BLCOMPUTE_WH"),
    ("PERF_QUEUED_MINUTES", "CROWDSTRIKE_WH queued 45.0 min in 24h", "CROWDSTRIKE_WH"),
    ("PERF_SPILL_GB", "COMPUTE_WH spilled 12.5 GB remote in 24h", "COMPUTE_WH"),
    ("COST_IDLE_OPPORTUNITY", "BLCOMPUTE_WH idle waste ~$412/mo: AUTO_SUSPEND 600s -> 60s", "BLCOMPUTE_WH"),
    ("COST_SLEEP_POLLING", "DOC_ALWH sleep polling ~$105/week: Task owner", "DOC_ALWH"),
    ("COST_ANOMALY_SWEEP", "WAREHOUSE POSIT_WORKBENCH spiked to 182.4 credits on 2026-09-28 (z=6.3)",
     "POSIT_WORKBENCH"),
    ("COST_CLOUD_SVC_ANOMALY", "CLOUD SVC COMPUTE_WH cloud-services spiked to 3.0 credits on 2026-09-29 (z=4.0)",
     "COMPUTE_WH"),
    ("WH_CHANGE_REGRESSION", "Warehouse COMPUTE_WH regressed after WAREHOUSE_SIZE MEDIUM->LARGE on 2026-09-20",
     "COMPUTE_WH"),
    # the WH_ shape still works, and a title-led name wins over a WH_ token in the detail
    ("COST_WH_DAILY_CREDITS", "WH_ALFA_LOAD used 80 credits on 2026-09-30 see WH_OTHER", "WH_ALFA_LOAD"),
    # a quoted (lower-case / punctuated) name or the NULL series is never guessed at
    ("COST_WH_DAILY_CREDITS", "my_wh used 80 credits on 2026-09-30 WH_DECOY", ""),
    ("PERF_QUEUED_MINUTES", "WH-1 queued 5.0 min in 24h", ""),
    ("COST_CLOUD_SVC_ANOMALY", "CLOUD SVC NONE cloud-services spiked to 9.9 credits on 2026-09-29 (z=6.0)", ""),
    ("COST_SLEEP_POLLING", "No warehouse sleep polling ~$30/week: User X", ""),
    # rules without a title shape keep the WH_* token search
    ("COST_CLOUD_SVC_RATIO", "WH_TRXS_TRANSFORM cloud-services ratio 31.2% (24h)", "WH_TRXS_TRANSFORM"),
])
def test_r2_092_rule_warehouse_reads_the_title_position(rule, title, want):
    assert navigate.rule_warehouse(rule, title) == want


def test_r2_092_a_non_wh_prefixed_warehouse_keeps_every_drawer_affordance():
    title = "BLCOMPUTE_WH used 80.2 credits on 2026-09-30 Per-warehouse daily metering."
    assert navigate.inline_fix_warehouse("COST_WH_DAILY_CREDITS", title) == "BLCOMPUTE_WH"
    sql = recheck_sql.recheck_sql("COST_WH_DAILY_CREDITS", navigate.inline_fix_warehouse(
        "COST_WH_DAILY_CREDITS", title))
    assert sql is not None and "UPPER(WAREHOUSE_NAME) = 'BLCOMPUTE_WH'" in sql
    assert navigate.investigation_target("COST_WH_DAILY_CREDITS", title)["filters"] == {
        "warehouse_contains": "BLCOMPUTE_WH"}
    assert navigate.fix_target("COST_WH_DAILY_CREDITS", title)["filters"] == {"warehouse_contains": "BLCOMPUTE_WH"}
    plan = plan_for_alert("PERF_QUEUED_MINUTES", "CROWDSTRIKE_WH queued 45.0 min in 24h", "", "2026-09-30")
    assert plan is not None and plan.kind == "queueing" and plan.warehouse == "CROWDSTRIKE_WH"
    gen = plan_for_alert("COST_WH_DAILY_CREDITS", title, "", "2026-09-30")
    assert gen is not None and gen.warehouse == "BLCOMPUTE_WH" and gen.day == "2026-09-30"


_SVC_SWEEP = "SERVICE AUTO_CLUSTERING spiked to 140.0 credits on 2026-09-29 (z=8.1)"
_SVC_AI_DETAIL = ("Median 12.0 credits/day over the prior 28d. Robust z-score 8.1 vs threshold 3.5. | AI: The top "
                  "family 'USE WAREHOUSE WH_ALFA_ETL; MERGE INTO ALFA_DB.RAW.CLAIMS ...' ran 4.1h vs 0.3h.")


def test_r2_092_a_service_series_sweep_never_takes_a_warehouse_from_the_ai_detail():
    """A SERVICE-series sweep title matches no WAREHOUSE shape, and rule_warehouse fell back to the first WH_* token
    in TITLE + DETAIL -- the sweep's ' | AI: ' text -- so an AUTO_CLUSTERING spike offered 'Respond -- closed loop
    on WH_ALFA_ETL' (an ALTER WAREHOUSE booked against the event) and scoped Investigate / Generate fix to it."""
    for args in ((_SVC_SWEEP, _SVC_AI_DETAIL), (f"{_SVC_SWEEP} {_SVC_AI_DETAIL}",)):
        assert navigate.rule_warehouse("COST_ANOMALY_SWEEP", *args) == ""
        assert navigate.inline_fix_warehouse("COST_ANOMALY_SWEEP", *args) == ""
        assert navigate.investigation_target("COST_ANOMALY_SWEEP", *args)["filters"] == {}
        assert navigate.fix_target("COST_ANOMALY_SWEEP", *args)["filters"] == {}
    # the WAREHOUSE series keeps its title-led warehouse, whatever the AI text names
    wh_title = "WAREHOUSE COMPUTE_WH spiked to 99.0 credits on 2026-09-28 (z=5.0)"
    assert navigate.inline_fix_warehouse("COST_ANOMALY_SWEEP", wh_title, _SVC_AI_DETAIL) == "COMPUTE_WH"
    assert navigate.investigation_target("COST_ANOMALY_SWEEP", wh_title, _SVC_AI_DETAIL)["filters"] == {
        "warehouse_contains": "COMPUTE_WH"}


def test_r2_092_a_shaped_rule_never_reads_its_warehouse_from_the_detail():
    # an older title shape falls back to a WH_* token in the TITLE only
    assert navigate.rule_warehouse("PERF_SPILL_GB", "Remote spill on WH_ALFA_LOAD", "see WH_DECOY") == "WH_ALFA_LOAD"
    assert navigate.rule_warehouse("PERF_SPILL_GB", "Remote spill high", "warehouse WH_DECOY") == ""
    assert navigate.inline_fix_warehouse("PERF_QUEUED_MINUTES", "Queueing high", "WH_DECOY queued") == ""
    # a rule without a title shape still reads the raiser's DETAIL, but never an appended AI narrative
    assert navigate.rule_warehouse("COST_CLOUD_SVC_RATIO", "cloud-services ratio 31.2%", "on WH_TRXS") == "WH_TRXS"
    for tail in (" | AI: WH_LLM_PICK ran 4h", " | AI hypothesis: WH_LLM_PICK is the cause"):
        assert navigate.rule_warehouse("COST_CLOUD_SVC_RATIO", "cloud-services ratio 31.2%", "x" + tail) == ""
        assert navigate.rule_warehouse("COST_CLOUD_SVC_RATIO", "cloud-services ratio 31.2% x" + tail) == ""
    # nor a database: the AI text's 'ALFA_DB.RAW.CLAIMS' is no Investigate scope
    spill = "COMPUTE_WH spilled 12.5 GB remote in 24h"
    assert navigate.investigation_target("PERF_SPILL_GB", spill, _SVC_AI_DETAIL)["filters"] == {
        "warehouse_contains": "COMPUTE_WH"}
    assert navigate.investigation_target("PERF_SPILL_GB", spill, "MERGE INTO ALFA_DB.RAW.CLAIMS")["filters"] == {
        "warehouse_contains": "COMPUTE_WH", "database": "ALFA_DB"}


# ----------------------------------------------------------------------------- alert evidence: R2-089 ----

_SWEEP_WH = "WAREHOUSE WH_ALFA_BI_PRD spiked to 182.4 credits on 2026-09-28 (z=6.3)"
_SWEEP_DETAIL = ("Median 41.2 credits/day over the prior 28d. Robust z-score 6.3 vs threshold 3.5. "
                 "Investigate: Cost > Spend / Attribution for that day.")


def test_r2_089_warehouse_series_sweep_gets_the_generic_pack_for_its_warehouse_and_day():
    for title in (_SWEEP_WH, _SWEEP_WH.replace("spiked to 182.4", "collapsed to 0.4").replace("z=6.3", "z=-4.2"),
                  "WAREHOUSE COMPUTE_WH spiked to 99.0 credits on 2026-09-28 (z=5.0)"):
        plan = plan_for_alert("COST_ANOMALY_SWEEP", title, _SWEEP_DETAIL, "2026-09-30 06:40:03")
        assert plan is not None and plan.kind == "generic", title
        assert plan.day == "2026-09-28" and plan.warehouse == title.split()[1]
        assert "ELAPSED_H_PRIOR_AVG" in alert_evidence_sql.build(plan)


def test_r2_089_llm_text_in_detail_never_picks_the_series():
    for detail in (_SWEEP_DETAIL + " | AI: The spike is driven by the ingestion service running a large backfill.",
                   _SWEEP_DETAIL + " | AI hypothesis: the search optimization service on WH_X maintained it."):
        plan = plan_for_alert("COST_ANOMALY_SWEEP", _SWEEP_WH, detail, "2026-09-30")
        assert plan is not None and plan.kind == "generic" and plan.service == ""
        assert plan.warehouse == "WH_ALFA_BI_PRD"
    svc = plan_for_alert("COST_ANOMALY_SWEEP", "SERVICE AUTO_CLUSTERING spiked to 140.0 credits on 2026-09-29 "
                                               "(z=8.1)", "x | AI: service RUNNING", "2026-09-30")
    assert svc is not None and svc.kind == "metering_service" and svc.service == "AUTO_CLUSTERING"
    assert svc.day == "2026-09-29"
    # a series only DETAIL names is no series
    assert plan_for_alert("COST_ANOMALY_SWEEP", "something odd", "SERVICE PIPE spiked to 3 credits", "") is None


# --------------------------------------------------------------------- alert evidence: R2-046 / R2-090 ----

@pytest.mark.parametrize(("rule", "title", "detail"), [
    ("COST_CONTRACT_BREACH", "Contract projected to exhaust in 13 day(s) (2027-01-02)", "Burn 812 credits/day"),
    ("COST_CONTRACT_BREACH", "Contract EXHAUSTED: 1200 credits over (crossed 2026-09-20, 11 day(s) ago)", ""),
    ("COST_STORAGE_SURGE", "ALFA_DW grew 412.3 GB in a day", "Growth on 2026-09-28 vs 7d median"),
    ("COST_EGRESS_SPIKE", "Egress 250.4 GB in 24h (threshold 100 GB)", "DATA_TRANSFER_HISTORY"),
    ("COST_BUDGET_PACE", "MTD spend $41000 is 1.32x the budget pace", "Budget $100000/mo"),
    ("COST_FORECAST_BREACH", "Projected month-end $120560 exceeds budget $100000", "MTD $50000"),
    ("COST_ORG_ACCOUNT_CREEP", "TRXS_DR org spend up 250% week-over-week", "Last 7d 3500 vs prior 1000 USD"),
    ("PERF_QUERY_FAIL_PCT", "Query failure rate 14.2% >= 10%", "71 of 500 queries failed in last 24h."),
    ("COST_DEPT_BUDGET_PACE", "Finance over budget pace by 42%", "WH_ALFA_FIN"),
    ("COST_SOMETHING_NEW", "WH_X spend odd", ""),
])
def test_r2_046_090_off_topic_rules_get_no_latency_pack(rule, title, detail):
    assert plan_for_alert(rule, title, detail, "2026-12-20 06:52:00") is None


def test_r2_046_090_every_live_cost_perf_rule_is_classified():
    """A live COST_/PERF_ rule either has a bespoke pack, is on the generic allow-list, or is withheld --
    never the generic latency pack by default."""
    bespoke = {"COST_CLOUD_SVC_RATIO", "COST_CLOUD_SVC_ANOMALY", "COST_AI_CREEP", "COST_SERVERLESS_CREEP",
               "COST_ANOMALY_SWEEP", "PERF_FINGERPRINT_DRIFT", "PERF_QUEUED_MINUTES", "COST_SLEEP_POLLING"}
    assert {"COST_WH_DAILY_CREDITS", "COST_DAILY_CREDITS", "PERF_SPILL_GB"} == GENERIC_EVIDENCE_RULES
    for rule in sorted(r for r in _live_rules() if r.startswith(("COST_", "PERF_"))):
        plan = plan_for_alert(rule, "WH_X used 1 credits on 2026-09-30", "", "2026-09-30")
        if plan is not None and plan.kind == "generic":
            assert rule in GENERIC_EVIDENCE_RULES or rule in bespoke, rule


# ------------------------------------------------------------------- tuning: R2-038 / R2-045 / R2-086 ----

def _ev(values, kinds) -> pd.DataFrame:
    return pd.DataFrame({"METRIC_VALUE": values, "RESOLUTION_KIND": kinds})


def test_r2_086_rules_that_never_read_their_threshold_are_withheld():
    assert frozenset({
        "SEC_ADMIN_GRANT", "OPS_PIPELINE_DEGRADED", "OPS_SCAN_DEGRADED", "OPS_CANARY_FAIL", "PERF_SLO_BREACH",
        "DQ_SCHEMA_DRIFT"}) == tuning.NO_THRESHOLD_RULES
    ev = _ev([31.5, 44.0, 3.0, 2.0, 52.1, 30.2], ["NOISE"] * 6)
    for rule in sorted(tuning.NO_THRESHOLD_RULES):
        got = tuning.suggest_threshold(ev, 0.0, rule_id=rule)
        assert got["ok"] is False and "suggested" not in got, rule
        assert got["basis"] == tuning.NO_THRESHOLD_BASES[rule] and got["noise_n"] == 6
    assert tuning.NO_THRESHOLD_BASIS == "Raised once per grant; this rule has no threshold."


def _proc_reads_threshold(body: str, rid: str) -> bool:
    """A proc that reads the rule's THRESHOLD_NUM into a variable before its INSERT (the z / % sweeps)."""
    return bool(re.search(r"THRESHOLD_NUM\)?,?[^;]*?INTO\s+:\w+\s+FROM\s+DBA_MAINT_DB\.OVERWATCH\.ALERT_CONFIG"
                          rf"\s+WHERE\s+RULE_ID\s*=\s*'{rid}'", body))


def test_r2_038_no_threshold_set_matches_the_raisers():
    """Every arm that never reads THRESHOLD_NUM is withheld, and every withheld rule really ignores it."""
    bodies = _raiser_bodies()
    ignores: dict[str, bool] = {}
    for proc, rids, stmt in _arms():
        for rid in rids:
            reads = "THRESHOLD_NUM" in stmt or _proc_reads_threshold(bodies[proc], rid)
            ignores[rid] = ignores.get(rid, True) and not reads
    ignoring = {r for r, flag in ignores.items() if flag}
    assert {"SEC_ADMIN_GRANT", "OPS_PIPELINE_DEGRADED", "OPS_SCAN_DEGRADED"} <= ignoring
    assert ignoring == set(tuning.NO_THRESHOLD_RULES), (
        f"arms ignoring THRESHOLD_NUM {sorted(ignoring)} != NO_THRESHOLD_RULES {sorted(tuning.NO_THRESHOLD_RULES)}")


def test_r2_045_metric_not_in_threshold_units_is_withheld():
    assert set(tuning.METRIC_NOT_THRESHOLD_UNITS) == {"COST_BUDGET_PACE", "COST_FORECAST_BREACH", "DQ_RECON_ERROR"}
    bodies = "\n".join(_raiser_bodies().values())
    # the facts the classification rests on: dollars / error counts written, a multiple / metric count tested
    for sql in ("               m.MTD_USD,\n", "AND m.MTD_USD > :budget_usd * (m.DAY_OF_MONTH - 1) / m.DAYS_IN_MONTH "
                "* c.THRESHOLD_NUM", "               m.MTD_USD + m.DAILY_RATE_USD * (m.DAYS_IN_MONTH - "
                "m.DAY_OF_MONTH),\n", "> :budget_usd * c.THRESHOLD_NUM", "               r.ERRORS,\n",
                "r.METRICS >= COALESCE(c.THRESHOLD_NUM, 1)"):
        assert sql in bodies, sql
    pace = _ev([21000, 24000, 28000, 30000, 33000, 36000], ["NOISE"] * 6)
    for rule in sorted(tuning.METRIC_NOT_THRESHOLD_UNITS):
        got = tuning.suggest_threshold(pace, 1.10, rule_id=rule)
        assert got == {"ok": False, "basis": tuning.METRIC_NOT_THRESHOLD_UNITS[rule], "noise_n": 6,
                       "actioned_n": 0}
    table = tuning.suggestions_by_rule(pace.assign(RULE_ID="COST_BUDGET_PACE"), {"COST_BUDGET_PACE": 1.10})
    assert pd.isna(table.loc[0, "SUGGESTED_THRESHOLD"])


def test_r2_086_dq_breach_is_tuned_on_the_magnitude():
    assert frozenset({"DQ_BREACH"}) == tuning.ABS_METRIC_RULES
    assert "AND ABS(s.RAW_Z) >= c.THRESHOLD_NUM" in "\n".join(_raiser_bodies().values())
    # noise drops at z -4..-9, actioned spikes at 12..20: on |z| the noise sits BELOW the actioned floor
    ev = _ev([-4.0, -5.0, -6.0, -7.0, -8.0, -9.0, 12.0, 15.0, 20.0], ["NOISE"] * 6 + ["ACTIONED"] * 3)
    got = tuning.suggest_threshold(ev, 3.5, rule_id="DQ_BREACH")
    assert got["ok"] is True and got["suggested"] > 9.0, got
    # all-drop data that is separable on |z| is not "overlap"
    drops = _ev([-4.0, -4.5, -5.0, -5.5, -6.0, -20.0, -25.0, -30.0], ["NOISE"] * 5 + ["ACTIONED"] * 3)
    got = tuning.suggest_threshold(drops, 3.5, rule_id="DQ_BREACH")
    assert got["ok"] is True and 6.0 < got["suggested"] < 20.0, got


# --------------------------------------------------------------------------------- triage: R2-087 ----

def _alert(title: str, rule: str = "COST_ANOMALY_SWEEP") -> dict:
    return {"RULE_ID": rule, "SEVERITY": "HIGH", "TITLE": title, "DETAIL": "", "RAISED_AT": "2026-09-30",
            "STATUS": "OPEN", "EVENT_ID": title[:8]}


def test_r2_087_service_series_sweep_events_stay_in_the_triage_queue():
    assert "'SERVICE ' || SERVICE_TYPE, 'ALL', DAY, SUM(CREDITS_BILLED)" in "\n".join(_raiser_bodies().values())
    svc = "SERVICE AUTO_CLUSTERING spiked to 140 credits on 2026-09-29 (z=8.1)"
    agg = "SERVICE WAREHOUSE_METERING spiked to 900 credits on 2026-09-29 (z=7.4)"
    wh = "WAREHOUSE WH_ALFA_BI_PRD spiked to 182.4 credits on 2026-09-28 (z=6.3)"
    queue = actions.triage_queue(pd.DataFrame([_alert(svc), _alert(agg), _alert(wh)]), None, [])
    assert set(queue["TITLE"]) == {svc, agg}            # only the warehouse twin is dropped
    alone = actions.triage_queue(pd.DataFrame([_alert(svc)]), None, [])
    assert not alone.empty and alone.iloc[0]["SEVERITY"] == "HIGH"


def test_r2_087_control_room_caption_says_only_warehouse_series_sweep_events_are_excluded():
    """The triage caption still said the sweep's COST_ANOMALY_SWEEP events were all excluded (shown on Alerts) while
    the SERVICE-series ones sat in the queue right above it."""
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "app/ui/pages/control_room.py").read_text(encoding="utf-8")
    body = src.split('"Spend anomalies: robust median/MAD z-score per warehouse', 1)[1].split("elif section ==", 1)[0]
    caption = re.sub(r'"\s*\n\s*f?"', "", body)          # join the implicit string concatenation
    assert ("its warehouse-series COST_ANOMALY_SWEEP events (excluded here, shown on Alerts) stay authoritative; "
            "its service-series events have no in-app twin and stay in the queue.") in caption
    assert "its COST_ANOMALY_SWEEP events (excluded here" not in caption
    # ... and that is what the feed does: the SERVICE row stays, the WAREHOUSE twin goes
    kept = actions._dedupe_alert_feed(pd.DataFrame([
        _alert("SERVICE AUTO_CLUSTERING spiked to 140 credits on 2026-09-29 (z=8.1)"),
        _alert("WAREHOUSE WH_ALFA_BI_PRD spiked to 182.4 credits on 2026-09-28 (z=6.3)")]))
    assert list(kept["TITLE"]) == ["SERVICE AUTO_CLUSTERING spiked to 140 credits on 2026-09-29 (z=8.1)"]


# -------------------------------------------------------------------------------- ETL clock: R2-108 ----

@pytest.mark.parametrize("raw", ["7: 30", "07 :30", "+7:30", "007:30", "07:030", "7:3_0", "-0:30",
                                 "\u0667:\u0663\u0660", "\t07:30", "07:30\n"])
def test_r2_108_parse_hhmm_refuses_what_the_proc_refuses(raw):
    from app.logic.insights import _parse_hhmm
    assert _parse_hhmm(raw, (7, 0)) == (7, 0)


def test_r2_108_parse_hhmm_still_reads_valid_clocks():
    from app.logic.insights import _parse_hhmm
    assert _parse_hhmm(" 07:30 ", (7, 0)) == (7, 30)
    assert _parse_hhmm("7:5", (0, 0)) == (7, 5)
    assert _parse_hhmm("23:59", (0, 0)) == (23, 59)
    assert _parse_hhmm("24:00", (7, 0)) == (7, 0)


# ------------------------------------------------------------------- the in-progress metering day: R2-050 ----

def _flat(last: date, *, partial_share: float | None, days: int = 70) -> pd.DataFrame:
    """$1,000 every day up to ``last``; ``last`` itself carries ``partial_share`` of a day when given."""
    rows = [{"DAY": d.date(), "USD": 1000.0} for d in pd.date_range(end=last, periods=days)]
    if partial_share is not None:
        rows[-1]["USD"] = 1000.0 * partial_share
    return pd.DataFrame(rows)


@pytest.mark.parametrize("today", [date(2026, 10, 2), date(2026, 9, 15)])
def test_r2_050_newest_row_is_in_progress_before_the_load(today):
    from app.logic.forecast import month_end_projection
    from app.logic.formulas import budget_pace_variance, metering_complete_before, mtd_pace_vs_prior_month
    yesterday = date.fromordinal(today.toordinal() - 1)
    frame = _flat(yesterday, partial_share=0.45)          # 03:00 Central: yesterday is the 06:45 snapshot
    cut = metering_complete_before(frame, today)
    assert cut == yesterday
    _mtd, _prior, pct = mtd_pace_vs_prior_month(frame, today, complete_before=cut)
    if today.day == 2:
        assert pct is None          # no COMPLETE October day yet: no pace (was -55% on the partial 1st)
    else:
        assert pct == pytest.approx(0.0)
    mtd_complete = float(frame[(frame["DAY"] >= today.replace(day=1)) & (frame["DAY"] < cut)]["USD"].sum())
    var, expected = budget_pace_variance(mtd_complete, 31_000.0 if today.month == 10 else 30_000.0, today,
                                         complete_before=cut)
    assert abs(var) < 1e-6 and expected == pytest.approx(1000.0 * (cut - today.replace(day=1)).days)
    days_in_month = 31 if today.month == 10 else 30
    for engine in ("linear", "seasonal"):
        fc = month_end_projection(frame, today, engine=engine, complete_before=cut)
        assert fc.ok and fc.projected_usd == pytest.approx(1000.0 * days_in_month), (engine, fc)
        assert "still in progress" in fc.basis


def test_r2_050_a_still_loading_last_day_of_the_prior_month_is_no_baseline_and_no_mtd():
    from app.logic.forecast import month_end_projection
    from app.logic.formulas import metering_complete_before
    today = date(2026, 10, 1)                               # 03:00 Central on the 1st: Sep 30 is the snapshot
    frame = _flat(date(2026, 9, 30), partial_share=0.45)
    cut = metering_complete_before(frame, today)
    assert cut == date(2026, 9, 30)
    fc = month_end_projection(frame, today, complete_before=cut)
    assert fc.ok and fc.projected_usd == pytest.approx(31_000.0) and fc.mtd_usd == 0.0
    assert "still in progress" not in fc.basis               # nothing of October is being estimated early


def test_r2_050_after_the_load_nothing_changes():
    from app.logic.forecast import month_end_projection
    from app.logic.formulas import metering_complete_before, mtd_pace_vs_prior_month
    today = date(2026, 9, 15)
    frame = _flat(today, partial_share=0.3)                 # 08:00 Central: today's row is the partial one
    cut = metering_complete_before(frame, today)
    assert cut == today
    assert mtd_pace_vs_prior_month(frame, today, complete_before=cut) == mtd_pace_vs_prior_month(frame, today)
    with_cut = month_end_projection(frame, today, complete_before=cut)
    assert with_cut == month_end_projection(frame, today)
    assert with_cut.projected_usd == pytest.approx(30_000.0) and "still in progress" not in with_cut.basis
    assert metering_complete_before(pd.DataFrame(), today) == today
    assert metering_complete_before(None, today) == today


def test_r2_050_a_failed_load_names_the_real_newest_day_and_every_projected_day():
    """Oct 2 with the Oct 1 06:45 load failed: the newest row is Sep 30 (still partial). The basis called 2026-10-01
    -- a day with no row at all -- 'the newest metering day' and counted 'today + 29 remaining' while 31 days were
    projected."""
    from app.logic.forecast import month_end_projection
    from app.logic.formulas import metering_complete_before
    today = date(2026, 10, 2)
    frame = _flat(date(2026, 9, 30), partial_share=0.45)
    cut = metering_complete_before(frame, today)
    assert cut == date(2026, 9, 30)
    for engine in ("linear", "seasonal"):
        fc = month_end_projection(frame, today, engine=engine, complete_before=cut)
        assert fc.ok and fc.projected_usd == pytest.approx(31_000.0), (engine, fc)
        assert "No day of this month has loaded yet" in fc.basis and "loaded through 2026-09-30" in fc.basis
        assert "2026-10-01 is projected with today" in fc.basis
        assert "31 days (2026-10-01 through month end)" in fc.basis
        assert "newest metering day (2026-10-01)" not in fc.basis and "today + 29" not in fc.basis


def test_r2_050_a_stale_fact_projects_and_names_every_unloaded_day():
    """Mid-month with the fact 5 days stale (newest Sep 10, today Sep 15): Sep 10-30 (21 days) are projected; the
    basis named only 2026-09-10 and said 'today + 15 remaining'."""
    from app.logic.forecast import month_end_projection
    from app.logic.formulas import metering_complete_before
    today = date(2026, 9, 15)
    frame = _flat(date(2026, 9, 10), partial_share=0.45)
    cut = metering_complete_before(frame, today)
    for engine in ("linear", "seasonal"):
        fc = month_end_projection(frame, today, engine=engine, complete_before=cut)
        assert fc.ok and fc.projected_usd == pytest.approx(30_000.0), (engine, fc)
        assert "Metering is loaded through 2026-09-10" in fc.basis and "still in progress" in fc.basis
        assert "2026-09-10 to 2026-09-14 are projected with today" in fc.basis
        assert "21 days (2026-09-10 through month end)" in fc.basis and "today + 15" not in fc.basis
    # the routine pre-load hour (newest = yesterday) names one projected day; after the load nothing changes
    fc = month_end_projection(_flat(date(2026, 9, 14), partial_share=0.45), today, complete_before=date(2026, 9, 14))
    assert "Metering is loaded through 2026-09-14" in fc.basis and "2026-09-14 is projected with today" in fc.basis
    assert "17 days (2026-09-14 through month end)" in fc.basis
    assert "today + 15 remaining days" in month_end_projection(_flat(today, partial_share=0.3), today,
                                                                complete_before=today).basis
