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

v4.605.0 (owner, 2026-09-30) closed the other half: SENTENCES. The query advisor said "Spent 7s queued (of
10.0s total)", Warehouse health "p95 runtime 940s", the platform score "145 queued minutes per day".
test_no_raw_duration_string_in_app_code walks every string expression in app/ (AST, f-strings climbed whole)
and fails on any interpolated number followed by a time unit (ms/s/min/h and their spellings) that did not
come from the shared humanizers, with a reasoned allowlist (Snowflake parameter values kept in the
parameter's own seconds, persisted notes mirroring a proc, counts that are not durations). The behaviour
tests below render each fixed sentence.
"""

from __future__ import annotations

import ast
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


# =====================================================================================================
# v4.605.0 (owner, 2026-09-30): no raw duration STRING anywhere in app code
# =====================================================================================================
# The table machinery above humanizes duration COLUMNS. Sentences are the other half: the query advisor said
# "Spent 7s queued (of 10.0s total)", Warehouse health "p95 runtime 145s", the platform score "145 queued minutes
# per day" -- the same raw-number class the owner keeps flagging. Every interpolated number followed by a time
# unit must come from the shared humanizers (formulas.humanize_duration / format_unit / humanize_age /
# humanize_minutes_ago), so the unit is picked by magnitude and reads like the tables and KPI cards.
# Days ("{n}d") are windows, not durations, and stay out of the unit set.
_DUR_UNIT = r"(?:ms|s|secs?|seconds?|m|mins?|minutes?|h|hrs?|hours?)"
# "{x}s", "{x} min", "{x}-second", and up to two lowercase words between ("{x} queued minutes")
_RAW_DURATION_RE = re.compile(r"\{[^{}]+\}(?P<gap>[ -]?|[ -](?:[a-z]+ ){1,2})" + _DUR_UNIT + r"(?![A-Za-z0-9_(])")
_PRINTF_DURATION_RE = re.compile(r"%[-+ 0#]*\d*(?:\.\d+)?[dfg] ?" + _DUR_UNIT + r"(?![A-Za-z0-9_])")
_SQL_ALIAS_BEFORE_RE = re.compile(r"\b(?:FROM|JOIN)\s*$")      # "FROM {tbl} s" is a table alias, not seconds

# Whole functions that ARE duration formatters: their output is the humanized form (or, for the printf map,
# the only form a large-frame NumberColumn accepts).
_DURATION_FORMATTERS = {
    ("app/logic/formulas.py", "humanize_duration"): "the shared Hr/Min/Sec humanizer itself",
    ("app/logic/formulas.py", "humanize_age"): "the shared relative-age humanizer ('5m ago')",
    ("app/logic/formulas.py", "humanize_minutes_ago"): "the shared minutes-since humanizer ('2h ago')",
    ("app/ui/pages/alerts.py", "_wakes_in"):
        "the snooze countdown's day-aware humanizer: humanize_duration caps at hours by design, so a >= 1 day "
        "wake reads '3d 4h' and anything shorter goes through humanize_duration",
    ("app/ui/components.py", "_duration_display_format"):
        "printf formats for the >400-row Arrow path, whose NumberColumn takes a printf, not a callable; "
        "styled_table pre-formats every detected duration column to Hr/Min/Sec strings before this map is "
        "consulted, so it only backs a callable column that pre-format did not cover (tests/test_ui_pipeline.py)",
}

# (file, enclosing function, fragment) -> reason. Each raw-duration match must lie INSIDE an occurrence of an
# allowlisted fragment of the same file + function, so an exemption never masks a new raw number added
# elsewhere in the same sentence.
_RAW_DURATION_ALLOWED = {
    # --- Snowflake parameter values, in the parameter's own unit (seconds) ------------------------------
    # AUTO_SUSPEND and STATEMENT_TIMEOUT_IN_SECONDS are SET in seconds; the same panels prefill
    # "ALTER WAREHOUSE ... SET AUTO_SUSPEND = 60", SHOW WAREHOUSES reports auto_suspend = 300, and
    # SP_ALERT_SCAN_DAILY's [24] COST_IDLE_OPPORTUNITY title reads "AUTO_SUSPEND 600s -> 60s" (SQL, V163;
    # tests/test_alert_evidence.py). Humanizing only the app half would put "10m" beside the alert's "600s"
    # for the same warehouse -- the cross-surface drift this rule exists to prevent. Owner default
    # (2026-09-30): keep parameter values in seconds; revisit together with the [24] title in a migration.
    ("app/logic/insights.py", "_advice", "is already at AUTO_SUSPEND={cur}s"):
        "AUTO_SUSPEND parameter value (seconds, as SET)",
    ("app/logic/insights.py", "_advice", "Enable AUTO_SUSPEND={IDLE_TARGET_SUSPEND_SEC}s on "):
        "AUTO_SUSPEND parameter value (seconds, as SET)",
    ("app/logic/insights.py", "_advice", "Reduce AUTO_SUSPEND to {target}s on "):
        "AUTO_SUSPEND parameter value (seconds, as SET)",
    ("app/logic/insights.py", "_advice", " (currently {cur}s)"):
        "AUTO_SUSPEND parameter value (seconds, as SET)",
    ("app/logic/remediation.py", "tighten_suspend_plan", "is already at AUTO_SUSPEND={cur}s. A {target}s change"):
        "AUTO_SUSPEND parameter values (seconds, as SET)",
    ("app/logic/sizing.py", "_recommend", "despite AUTO_SUSPEND={current}s;"):
        "AUTO_SUSPEND parameter value (seconds, as SET)",
    ("app/logic/sizing.py", "_susp_label", "never | {int(_sf(v, 0))}s"):
        "the what-if assumption 'Auto-suspend 600s -> 60s', the same shape as the [24] alert title "
        "(tests/test_bughunt_round4.py locks 'never -> 60s')",
    ("app/ui/pages/cost_parts/optimize.py", "_whatif_panel", "never suspends | {live_suspend}s suspend"):
        "AUTO_SUSPEND parameter value on the what-if KPI (the slider beside it offers the SET values 30..900)",
    ("app/ui/pages/cost_parts/optimize.py", "_whatif_panel", "Scenario ({sim['size_new']}, {int(sus_wi)}s)"):
        "AUTO_SUSPEND parameter value on the what-if KPI",
    ("app/ui/pages/cost_parts/optimize.py", "_optimization_tab", "~{IDLE_TARGET_SUSPEND_SEC}s resume/suspend tail"):
        "the modeled idle tail IS the AUTO_SUSPEND target (IDLE_TARGET_SUSPEND_SEC); the playbook and Admin "
        "prose state the same '60s'",
    ("app/ui/pages/cost_parts/optimize.py", "_optimization_tab", "~{IDLE_TARGET_SUSPEND_SEC}s suspend/resume tail"):
        "the modeled idle tail IS the AUTO_SUSPEND target (IDLE_TARGET_SUSPEND_SEC)",
    ("app/ui/pages/cost_parts/optimize.py", "_optimization_tab", "~{IDLE_TARGET_SUSPEND_SEC}s resume tail"):
        "the modeled idle tail IS the AUTO_SUSPEND target (IDLE_TARGET_SUSPEND_SEC)",
    ("app/ui/pages/admin.py", "_stmt_timeout_ceiling", "({_SNOWFLAKE_DEFAULT_STMT_TIMEOUT_S} s) applies"):
        "the exact STATEMENT_TIMEOUT_IN_SECONDS default, in parentheses AFTER its humanized form ('48h (172800 s)'); "
        "tests/test_admin_timeout_wording.py",
    # --- persisted text that mirrors a SQL proc's own format -------------------------------------------
    ("app/ui/pages/alerts.py", "_snooze_stmts", "snooze {hours}h"):
        "the legacy-fallback ALERT_AUDIT note; SP_ALERT_SNOOZE (V086) writes its note as :v_hours || 'h', and "
        "hours is always a whole-hour SNOOZE_PRESETS value",
    # --- not durations ----------------------------------------------------------------------------------
    ("app/logic/sizing.py", "_hour_count_txt", "{n} hour"):
        "a COUNT of hourly buckets at the cluster cap ('57 hours of the last 35 days'), not an elapsed time",
    ("app/logic/ask/registry.py", "_analyze_warehouse_waste", "idle {idle_h} of {met_h} metered hours"):
        "a tally of metered hour slices ('idle 57 of 120 metered hours'), not an elapsed time",
    ("app/ui/charts.py", "hour_heatmap", "{_r.iloc[_p]} at hour {int(_h.iloc[_p])}"):
        "an hour-of-day label ('at hour 14'), not a duration",
}


def _string_expressions(tree: ast.AST):
    """(enclosing function, text) for every string expression: each literal climbed to its whole
    concatenation (f-string incl. format specs, +, a conditional part); docstrings skipped. A non-literal
    part renders as {its source} -- the same rendering as test_cluster_cap_gate._str_text."""
    parent = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}

    def text(node) -> str:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.JoinedStr):
            return "".join(v.value if isinstance(v, ast.Constant) else "{" + ast.unparse(v.value) + "}"
                           for v in node.values)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            return text(node.left) + text(node.right)
        if isinstance(node, ast.IfExp):
            return text(node.body) + " | " + text(node.orelse)
        return "{" + ast.unparse(node) + "}"

    tops = {}
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
            continue
        if isinstance(parent.get(node), ast.Expr):
            continue                                                   # a docstring / bare string statement
        top = node
        while True:
            up = parent.get(top)
            if isinstance(up, ast.FormattedValue):                     # a format spec belongs to its f-string
                top = parent[up]
                continue
            if isinstance(up, ast.JoinedStr) or (isinstance(up, ast.BinOp) and isinstance(up.op, ast.Add)) \
                    or (isinstance(up, ast.IfExp) and top is not up.test):
                top = up
                continue
            break
        tops[id(top)] = top
    for top in tops.values():
        fn, up = "", parent.get(top)
        while up is not None and not fn:
            fn = up.name if isinstance(up, (ast.FunctionDef, ast.AsyncFunctionDef)) else ""
            up = parent.get(up)
        yield fn, text(top)


def _raw_duration_hits(source: str):
    """(function, text, [(start, end, match)]) for each string expression carrying a raw duration."""
    out = []
    for fn, text in _string_expressions(ast.parse(source)):
        hits = [(m.start(), m.end(), m.group(0)) for m in _RAW_DURATION_RE.finditer(text)
                if not _SQL_ALIAS_BEFORE_RE.search(text[:m.start()])
                and not m.group("gap").endswith("per ")]             # "{n} credits per hour" is a rate
        hits += [(m.start(), m.end(), m.group(0)) for m in _PRINTF_DURATION_RE.finditer(text)]
        if hits:
            out.append((fn, text, hits))
    return out


def _app_raw_duration_texts():
    """Every raw-duration string expression under app/, except app/data/: that is the SQL-builder layer (house
    law 10 -- builders return SQL strings only), where the pattern only ever matches a table alias
    ("FROM {tbl} s") or a SQL comment ("-- stale past {h}h")."""
    out = []
    for py in sorted((_ROOT / "app").rglob("*.py")):
        rel = py.relative_to(_ROOT).as_posix()
        if rel.startswith("app/data/") or "__pycache__" in py.parts:
            continue
        out.extend((rel, fn, text, hits) for fn, text, hits in _raw_duration_hits(py.read_text(encoding="utf-8")))
    return out


def test_no_raw_duration_string_in_app_code():
    texts = _app_raw_duration_texts()
    assert len(texts) >= 25, texts                    # the formatters + allowlisted sites are seen (not vacuous)
    used_fns, used_frags, bad = set(), set(), []
    for rel, fn, text, hits in texts:
        if (rel, fn) in _DURATION_FORMATTERS:
            used_fns.add((rel, fn))
            continue
        spans = [(m.start(), m.end(), key) for key in _RAW_DURATION_ALLOWED if key[:2] == (rel, fn)
                 for m in re.finditer(re.escape(key[2]), text)]
        for start, end, hit in hits:
            cover = [key for s, e, key in spans if s <= start and end <= e]
            if cover:
                used_frags.update(cover)
            else:
                bad.append(f"{rel} {fn}(): {hit!r} in {text[:160]!r}")
    assert not bad, (
        "A sentence renders a raw duration. Format it with formulas.humanize_duration(value, unit) (or "
        "format_unit / humanize_age / humanize_minutes_ago) so it reads Hr/Min/Sec like the tables and KPI "
        "cards, or -- only if it is not a duration a person reads as one -- allowlist it here WITH its reason:\n"
        + "\n".join(bad))
    assert used_fns == set(_DURATION_FORMATTERS), set(_DURATION_FORMATTERS) - used_fns        # no stale exemption
    assert used_frags == set(_RAW_DURATION_ALLOWED), set(_RAW_DURATION_ALLOWED) - used_frags  # no stale exemption


def test_the_duration_sweep_catches_the_raw_forms():
    """Negative control: the detector flags each raw shape the sweep fixed, and none of the safe shapes."""
    flagged = {text for _fn, text, _h in _raw_duration_hits('''
def f(x, q, n, tbl, reason):
    a = f"Spent {q:.0f}s queued (of {x:.1f}s total)"
    b = f"{q:.0f} min/day queued"
    c = "%.1f s"
    d = f"{n:,.0f} queued minutes across the day."
    e = f"p95 {x}ms"
    g = f"already {x:.1f}h old"
    h = f"{x}-second wait"
    k = f"{humanize_duration(x, 's')} per run"
    m = f"SELECT 1 FROM {tbl} s WHERE s.X = 1"
    p = f"{n} credits per hour"
    r = f"last {n}d, {n} runs"
    s = f"snooze" + (f" — {reason}" if reason else "")
''')}
    assert flagged == {"Spent {q}s queued (of {x}s total)", "{q} min/day queued", "%.1f s",
                       "{n} queued minutes across the day.", "p95 {x}ms", "already {x}h old",
                       "{x}-second wait"}, flagged


# --- the sentences the v4.605.0 sweep fixed, rendered (behaviour, not source text) ---------------------
def test_logic_sentences_render_humanized_durations():
    from datetime import datetime, timedelta

    from app.logic import insights, rca, replay, scoring, verdict, wh_health
    from app.logic.sizing import size_recommendations

    why = wh_health.warehouse_health(pd.DataFrame([{"WAREHOUSE_NAME": "WH", "QUEUED_MIN_PER_DAY": 145.0,
                                                    "P95_ELAPSED_SEC": 940.0, "IDLE_PCT": 0.0}]))
    assert "2h 25m/day queued" in why.iloc[0]["WHY"] and "p95 runtime 15m 40s" in why.iloc[0]["WHY"]
    sig = verdict.operations_signals(pd.DataFrame([{"QUERY_COUNT": 100, "QUEUED_SEC": 2400.0}]))
    assert any(s.phrase == "warehouse queueing 40m/day" for s in sig)
    drv = scoring.platform_score({"queue_minutes": 145.0}).drivers
    assert next(d for d in drv if d.driver == "Queueing").evidence.startswith("2h 25m queued per day.")
    heads = replay.replay_headlines(None, pd.DataFrame([{"QUEUED_SEC": 8700.0}]), 0, 0, 0, 0, 3.68)
    assert {"severity": "warn", "text": "2h 25m of queueing across the day."} in heads
    down = size_recommendations(pd.DataFrame([{
        "WAREHOUSE_NAME": "CALM", "COMPANY": "ALFA", "CREDITS_TOTAL": 100.0, "QUERY_COUNT": 1000,
        "ACTIVE_QUERY_DAYS": 7, "P95_ELAPSED_SEC": 3.0, "QUEUED_SEC": 0.0, "SPILL_REMOTE_GB": 0.0,
        "IDLE_PCT": 40.0}]), credit_rate_usd=3.68, window_days=7)
    assert "p95 3.0s," in down.iloc[0]["RATIONALE"]
    rep = insights.flag_repeat_candidates(pd.DataFrame([{"RUNS": 300, "TOTAL_ELAPSED_HOURS": 4.5,
                                                         "AVG_CACHE_PCT": 5.0, "TOTAL_TB_SCANNED": 1.0}]), 30)
    assert "4h 30m compute/30d" in rep.iloc[0]["WHY"]
    sla = insights.pipeline_sla_forecast(pd.DataFrame([
        {"DATABASE_NAME": "D", "SCHEMA_NAME": "S", "TABLE_NAME": "B", "MAX_AGE_HOURS": 24, "HOURS_SINCE": 30.5,
         "SLA_MET": False, "MEDIAN_GAP_MIN": 60, "REFRESHES": 10, "RUNWAY_HOURS": -6.5},
        {"DATABASE_NAME": "D", "SCHEMA_NAME": "S", "TABLE_NAME": "OK", "MAX_AGE_HOURS": 24, "HOURS_SINCE": 0.5,
         "SLA_MET": True, "MEDIAN_GAP_MIN": 1200, "REFRESHES": 10, "RUNWAY_HOURS": 23.5}]))
    detail = dict(zip(sla["TABLE_NAME"], sla["DETAIL"], strict=True))
    assert detail == {"B": "already 30h 30m old (past its 24h limit)", "OK": "~23h 30m runway"}
    onset = datetime(2026, 8, 18, 14, 0, 0)
    ranked = rca.rank_root_causes([{"kind": "object_change", "title": "c", "when": onset - timedelta(minutes=12),
                                    "entity": "WH_A", "magnitude": 0.5, "magnitude_text": "",
                                    "changed_by": "", "evidence": {}}], onset, entity_name="WH_A")
    assert ranked[0]["lead_text"] == "12m before onset"


def test_the_other_branches_render_humanized_durations_too():
    """The remaining v4.605.0 branches: SLA Overdue / At risk, the auto-suspend provisioning note, and a change
    that landed AFTER the onset."""
    from datetime import datetime, timedelta

    from app.logic import insights, rca
    from app.logic.sizing import size_recommendations

    sla = insights.pipeline_sla_forecast(pd.DataFrame([
        {"DATABASE_NAME": "D", "SCHEMA_NAME": "S", "TABLE_NAME": "LATE", "MAX_AGE_HOURS": 24, "HOURS_SINCE": 3.0,
         "SLA_MET": True, "MEDIAN_GAP_MIN": 60, "REFRESHES": 10, "RUNWAY_HOURS": 21.0},
        {"DATABASE_NAME": "D", "SCHEMA_NAME": "S", "TABLE_NAME": "RISK", "MAX_AGE_HOURS": 24, "HOURS_SINCE": 0.5,
         "SLA_MET": True, "MEDIAN_GAP_MIN": 90, "REFRESHES": 10, "RUNWAY_HOURS": 0.75}]))
    assert dict(zip(sla["TABLE_NAME"], zip(sla["FORECAST"], sla["DETAIL"], strict=True), strict=True)) == {
        "LATE": ("Overdue", "last refresh 3h ago vs ~1h typical — refresh is late"),
        "RISK": ("At risk", "breaches in ~45m; ~1h 30m typical cadence")}
    idle = size_recommendations(pd.DataFrame([{
        "WAREHOUSE_NAME": "IDLE", "COMPANY": "ALFA", "CREDITS_TOTAL": 100.0, "QUERY_COUNT": 1000,
        "ACTIVE_QUERY_DAYS": 7, "P95_ELAPSED_SEC": 3.0, "QUEUED_SEC": 0.0, "QUEUED_PROVISIONING_SEC": 63000.0,
        "SPILL_REMOTE_GB": 0.0, "IDLE_PCT": 80.0}]), credit_rate_usd=3.68, window_days=7)
    assert "(2h 30m/day provisioning — resume overhead, not concurrency)" in idle.iloc[0]["RATIONALE"]
    onset = datetime(2026, 8, 18, 14, 0, 0)
    ranked = rca.rank_root_causes([{"kind": "object_change", "title": "c", "when": onset + timedelta(minutes=90),
                                    "entity": "WH_A", "magnitude": 0.5, "magnitude_text": "",
                                    "changed_by": "", "evidence": {}}], onset, entity_name="WH_A")
    assert ranked[0]["lead_text"] == "1h 30m AFTER onset"


def test_query_advisor_numbers_are_humanized():
    """Owner decision 2026-09-30: the advisor's findings read Hr/Min/Sec; #17's wording is unchanged."""
    from app.logic.query_advisor import advise

    def detail(row, code):
        return next(f.detail for f in advise(row)[0] if f.code == code)

    assert detail({"ELAPSED_SEC": 0.55, "COMPILE_SEC": 0.54}, "metadata_chatter").startswith(
        "Compilation was 98% of a 550ms runtime with only 10ms of execution — ")
    assert detail({"ELAPSED_SEC": 145, "COMPILE_SEC": 100}, "compile_bound").startswith(
        "Compilation was 69% of the 2m 25s runtime — ")
    assert detail({"ELAPSED_SEC": 200, "QUEUED_SEC": 150, "QUEUED_OVERLOAD_SEC": 10,
                   "QUEUED_PROVISIONING_SEC": 140}, "cold_start").startswith(
        "Waited 2m 30s (of 3m 20s total), 2m 20s of it PROVISIONING — ")
    assert detail({"ELAPSED_SEC": 200, "QUEUED_SEC": 150, "QUEUED_OVERLOAD_SEC": 140,
                   "QUEUED_PROVISIONING_SEC": 10}, "queued").startswith(
        "Spent 2m 30s queued (of 3m 20s total), 2m 20s of it OVERLOAD — ")


def test_ai_grounding_prompt_carries_unit_named_keys():
    from app.logic.ai_prompts import query_optimization_prompt

    prompt = query_optimization_prompt({"ELAPSED_SEC": 145.0, "COMPILE_SEC": 1.25, "QUEUED_SEC": 0.0}, [])
    assert "elapsed_sec=145.0; compile_sec=1.2; queued_sec=0.0" in prompt or \
        "elapsed_sec=145.0; compile_sec=1.3; queued_sec=0.0" in prompt
    assert "elapsed=" not in prompt and "145.0s" not in prompt


def test_alert_recheck_humanizes_queued_minutes():
    from app.data import recheck_sql
    from app.ui.pages.alerts import _recheck_value_text

    assert _recheck_value_text("PERF_QUEUED_MINUTES", 145.0) == "2h 25m"
    assert _recheck_value_text("perf_queued_minutes", 30.0) == "30m"
    assert _recheck_value_text("COST_WH_DAILY_CREDITS", 12.345) == "12.35"
    assert recheck_sql.recheck_label("PERF_QUEUED_MINUTES") == "queued time today"
    alerts = (_ROOT / "app" / "ui" / "pages" / "alerts.py").read_text(encoding="utf-8")
    assert "{_rcv:,.2f}" not in alerts and "{safe_float(_rct):,.2f}" not in alerts


def test_session_refreshed_note_uses_the_shared_age(monkeypatch):
    from datetime import datetime, timedelta

    import app.ui.components as components

    monkeypatch.setattr(components.st, "session_state", {"_ow_refreshed_at": datetime.now() - timedelta(minutes=5)})
    assert components.last_refreshed_note() == "Session refreshed 5m ago"
    monkeypatch.setattr(components.st, "session_state", {"_ow_refreshed_at": datetime.now()})
    assert components.last_refreshed_note() == "Session refreshed just now"
    monkeypatch.setattr(components.st, "session_state", {})
    assert components.last_refreshed_note() == "Live · cached per tier"
