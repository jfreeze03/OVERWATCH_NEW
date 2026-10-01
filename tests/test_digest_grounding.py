"""The morning digest's measured grounding (Next-Fifty #24, V165): app/logic/digest_grounding + the app wiring.

check_digest is the Python mirror of SP_DAILY_DIGEST's figure check (the SQL literals and a sqlite run of the
proc's own matching SELECT are locked in tests/test_digest_grounding_parity.py). digest_provenance is the label the
Brief and Overview expanders show -- the MEASURED result, never a fixed "grounded". latest_digest reads the V165
columns only when the shared schema gate says V165 is applied.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from app.logic.digest_grounding import (
    AI_TITLE,
    TEMPLATE_TITLE,
    check_digest,
    digest_provenance,
    digest_source,
    strip_non_figures,
)
from tests._source import read

# the FACTS string V165 builds (named facts, one unit per key; SPEND_USD and CREDITS separate)
FACTS = ("WINDOW_DAYS=7; SPEND_USD=12345.67; CREDITS=3354.80; QUERIES=1234567; FAILED_QUERIES=321; "
         "FAILED_QUERY_PCT=0.03; QUERY_SUCCESS_PCT=99.97; QUEUED_MINUTES=12.3; SPILL_GB=4.56; TASK_RUNS=900; "
         "TASK_FAILURES=3; TASK_FAILURE_PCT=0.33; TASK_SUCCESS_PCT=99.67; ALERT_WINDOW_HOURS=24; "
         "OPEN_CRITICAL_ALERTS=0; OPEN_HIGH_ALERTS=2; ALERTS_RAISED_24H=5")


# -- the rule ------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("body", [
    "Over the last 7 days the platform spent $12,345.67 (3,354.8 credits) across 1,234,567 queries.",
    "Spend was about $12.3K; 321 queries failed (0.03%). 3 of 900 task runs failed.",
    "There are 2 high alerts open and 0 critical; 5 alerts were raised in the last 24 hours.",
    "Queries: 1.2 million; 12.3 minutes queued; 4.56 GB spilled; 99.7% task success.",
    "Spend was $12,346 this week.",                          # rounded to the dollar
    "Roughly 3,355 credits.",                                # rounded credits
    "99.97% of queries succeeded.",
])
def test_grounded_figures_pass(body):
    res = check_digest(body, FACTS)
    assert res.ok and res.checked > 0, res


@pytest.mark.parametrize("body, bad", [
    ("Attention: 2 critical alerts.", "2 critical"),                   # 0 open critical
    ("Spend was 12,345.67 credits this week.", "12,345.67 credits"),   # dollars called credits
    ("Spend was $3,354.80 overall.", "$3,354.80 overall"),             # credits called dollars
    ("There are 3 high alerts.", "3 high"),                            # 2 open high
    ("Spend rose 15% week over week.", "15%"),                         # a derived percentage
    ("Average daily spend was $1,763.67.", "$1,763.67"),               # a derived average
    ("Failures: 12m queued.", "12m"),                                  # minutes vs million: errs to the template
    ("Task success was 99.7 percent at 900 tasks and 7% worse.", "7%"),
])
def test_ungrounded_figures_fail(body, bad):
    res = check_digest(body, FACTS)
    assert not res.ok and bad in res.ungrounded, res


@pytest.mark.parametrize("body", [
    "(1) Health is good. (2) Nothing urgent. (3) Keep watching.",
    "1. Spend is stable.\n2. Nothing urgent.\n3. Keep watching.",
    "#1 priority: keep watching.",
    "On 2026-09-29 at 07:20 the digest ran; Q3 is on track.",
    "Focus on WH_X1 and p95 latency; V112 skips paging routes.",
    "No numbers here at all.",
    "",
])
def test_dates_times_ids_and_list_markers_are_not_figures(body):
    res = check_digest(body, FACTS)
    assert res.checked == 0 and res.ok, (res, strip_non_figures(body))


def test_distinct_figures_counted_once_in_first_seen_order():
    res = check_digest("2 critical, 2 critical and 9 critical; 2 high.", FACTS)
    assert res.checked == 3 and res.ungrounded == ("2 critical", "9 critical")


def test_units_bind():
    assert check_digest("$12,345.67", FACTS).ok                      # $ -> SPEND_USD
    assert not check_digest("$0.03", FACTS).ok                       # $ never licensed by a *_PCT fact
    assert check_digest("0.03%", FACTS).ok                           # % -> FAILED_QUERY_PCT
    assert not check_digest("321%", FACTS).ok                        # % never licensed by a count
    assert check_digest("321", FACTS).ok                             # a bare figure: any fact
    assert check_digest("24 hours", FACTS).ok and check_digest("7 days", FACTS).ok
    assert not check_digest("5 days", FACTS).ok                      # 'days' binds to WINDOW_DAYS only


# V171 R1-228: the spend keys say warehouse compute; they still bind through the _USD suffix and the CREDIT substring
WAREHOUSE_FACTS = FACTS.replace("SPEND_USD=", "WAREHOUSE_SPEND_USD=").replace("; CREDITS=", "; WAREHOUSE_CREDITS=")


@pytest.mark.parametrize("body, ok", [
    ("Warehouse compute spend was $12,345.67 over the 7 complete days.", True),   # $ -> WAREHOUSE_SPEND_USD
    ("Warehouse compute used 3,354.80 credits.", True),                           # credits -> WAREHOUSE_CREDITS
    ("Roughly 3,355 credits and $12,346 of warehouse compute.", True),
    ("Warehouse compute was 12,345.67 credits.", False),                          # dollars called credits
    ("Warehouse compute cost $3,354.80.", False),                                 # credits called dollars
])
def test_the_warehouse_spend_keys_bind_like_the_v165_keys(body, ok):
    assert "SPEND_USD=12345.67" in WAREHOUSE_FACTS and "WAREHOUSE_CREDITS=3354.80" in WAREHOUSE_FACTS
    res = check_digest(body, WAREHOUSE_FACTS)
    assert res.ok is ok and res.checked > 0, res
    assert (res.checked, res.ungrounded) == ((r := check_digest(body, FACTS)).checked, r.ungrounded)


def test_na_and_missing_facts_license_nothing():
    facts = "WINDOW_DAYS=7; SPEND_USD=n/a; CREDITS=n/a; OPEN_CRITICAL_ALERTS=0"
    assert not check_digest("Spend was $12,345.67.", facts).ok
    assert check_digest("0 critical over 7 days.", facts).ok
    assert not check_digest("Spend was $5.", "").ok
    assert check_digest("", "").checked == 0


def test_scale_words_and_tolerance():
    assert check_digest("about $12.3 thousand", FACTS).ok
    assert check_digest("1.2 million queries", FACTS).ok
    assert not check_digest("1.1 million queries", FACTS).ok        # 0.05 M half-step, 1.23 M is out
    assert not check_digest("$12.2K", FACTS).ok                     # 12,200 vs 12,345.67: > 0.5% and > half-step


@pytest.mark.parametrize("facts, body", [
    ("FAILED_QUERY_PCT=1.25", "1.3% of queries failed"),       # half up: |1.25-1.3| = 0.050000000000000044
    ("FAILED_QUERY_PCT=1.25", "1.2% of queries failed"),       # half down
    ("FAILED_QUERY_PCT=0.75", "0.8% of queries failed"),
    ("TASK_FAILURE_PCT=0.15", "0.2% of tasks failed"),
    ("QUERIES=8250000", "8.3 million queries"),                # the same edge after a scale word
    ("QUERIES=8250000000", "8.2 billion queries"),             # an absolute 1e-9 epsilon still fails this one
])
def test_an_exact_half_step_rounding_is_grounded(facts, body):
    """W3 (review r1): the half-step rule is inclusive; DOUBLE noise must not fail a correctly rounded figure."""
    res = check_digest(body, facts)
    assert res.ok and res.checked == 1, res


def test_every_half_step_fact_passes_in_both_rounding_directions():
    from decimal import ROUND_HALF_DOWN, ROUND_HALF_UP, Decimal
    for scale_word, scale in (("", 1), (" thousand", 1000), (" million", 10 ** 6), (" billion", 10 ** 9)):
        for i in range(1, 200, 2):                            # 0.05, 0.15, ... 9.95: every x.x5 below 10
            fact = Decimal(i) / 20
            for mode in (ROUND_HALF_UP, ROUND_HALF_DOWN):
                shown = fact.quantize(Decimal("0.1"), rounding=mode)
                body = f"{shown}{scale_word} queries"
                res = check_digest(body, f"QUERIES={fact * scale}")
                assert res.ok, (body, str(fact * scale), res)


def test_the_half_step_slack_does_not_widen_the_rule():
    assert not check_digest("1.4% of queries failed", "FAILED_QUERY_PCT=1.25").ok      # 1.5 half steps away
    assert not check_digest("1.3% of queries failed", "FAILED_QUERY_PCT=1.2499").ok    # just past the half step
    assert not check_digest("1.4 million queries", "QUERIES=1250000").ok


# -- provenance ----------------------------------------------------------------------------------------------------

def test_provenance_ai_all_matched():
    p = digest_provenance({"BODY_SOURCE": "AI", "GROUNDING_OK": True, "FIGURES_CHECKED": 4})
    assert (p.ai_written, p.title, p.tone, p.show_draft, p.detail) == (True, AI_TITLE, "ok", False, None)
    assert p.chip == "AI-written; all 4 figures match the exec-board facts"


def test_provenance_ai_no_figures():
    p = digest_provenance({"BODY_SOURCE": "AI", "GROUNDING_OK": True, "FIGURES_CHECKED": 0})
    assert p.ai_written is True and p.tone == "" and "no figures" in p.chip


def test_provenance_template_on_mismatch_offers_the_withheld_draft():
    p = digest_provenance({"BODY_SOURCE": "TEMPLATE", "GROUNDING_OK": False, "FIGURES_CHECKED": 5,
                           "UNGROUNDED": "2 critical, 15%", "AI_BODY": "draft text"})
    assert (p.ai_written, p.title, p.tone, p.show_draft) == (False, TEMPLATE_TITLE, "warn", True)
    assert "not in the exec-board facts" in p.chip and p.chip.startswith("Templated, not AI-written")
    assert p.detail == "Unmatched figures in the withheld draft: 2 critical, 15%"


def test_provenance_template_on_cortex_failure():
    p = digest_provenance({"BODY_SOURCE": "TEMPLATE", "GROUNDING_OK": None, "FIGURES_CHECKED": None,
                           "UNGROUNDED": None, "AI_BODY": None})
    assert p.ai_written is False and "Cortex returned no digest" in p.chip
    assert p.detail is None and p.show_draft is False


def test_provenance_not_measured_before_v165():
    for row in ({"DIGEST_DATE": "2026-09-29", "MODEL": "llama3.1-8b", "BODY": "old"},     # pre-V165 read shape
                {"BODY_SOURCE": None, "GROUNDING_OK": None},
                pd.Series({"BODY": "x"}), None, object()):
        p = digest_provenance(row)
        assert p.ai_written is None and p.title == AI_TITLE and p.chip == "Figures not checked" and p.tone == ""
        assert "written before V165, or V165 is not applied yet" in p.detail and p.show_draft is False


@pytest.mark.parametrize("row", [
    # the test_pages_shaped types: float GROUNDING_OK / FIGURES_CHECKED / UNGROUNDED / AI_BODY, a junk source
    {"BODY_SOURCE": "BODY_SOURCE_0", "GROUNDING_OK": 1.0, "FIGURES_CHECKED": 1.0, "UNGROUNDED": 1.0, "AI_BODY": 1.0},
    {"BODY_SOURCE": float("nan"), "GROUNDING_OK": float("nan"), "FIGURES_CHECKED": float("nan")},
    {"BODY_SOURCE": "TEMPLATE", "GROUNDING_OK": 0.0, "FIGURES_CHECKED": 2.0, "UNGROUNDED": float("nan"),
     "AI_BODY": float("nan")},
    {"BODY_SOURCE": "ai", "GROUNDING_OK": np.bool_(True), "FIGURES_CHECKED": np.float64(3.0)},
    {"BODY_SOURCE": "AI", "GROUNDING_OK": pd.NA, "FIGURES_CHECKED": pd.NA},
    {"BODY_SOURCE": "AI", "GROUNDING_OK": "true", "FIGURES_CHECKED": "3"},
])
def test_provenance_never_raises_on_odd_types(row):
    p = digest_provenance(pd.Series(row))
    assert p.title in (AI_TITLE, TEMPLATE_TITLE) and p.tone in ("ok", "warn", "")


def test_provenance_odd_types_resolve_sensibly():
    t = digest_provenance(pd.Series({"BODY_SOURCE": "TEMPLATE", "GROUNDING_OK": 0.0, "UNGROUNDED": float("nan"),
                                     "AI_BODY": float("nan")}))
    assert "not in the exec-board facts" in t.chip and t.detail is None and t.show_draft is False
    a = digest_provenance(pd.Series({"BODY_SOURCE": "ai", "GROUNDING_OK": np.bool_(True),
                                     "FIGURES_CHECKED": np.float64(3.0)}))
    assert a.chip == "AI-written; all 3 figures match the exec-board facts"
    u = digest_provenance(pd.Series({"BODY_SOURCE": "AI", "GROUNDING_OK": pd.NA}))
    assert u.chip == "AI-written; figures not verified" and u.tone == "warn"


def test_the_source_label_names_the_check_only_once_v165_is_applied():
    """Review r1 W4: before V165 the live proc checks nothing, so the label must not say it does."""
    assert digest_source(True) == ("DAILY_DIGEST (Cortex draft; figures checked against the exec board, "
                                   "templated on mismatch)")
    assert digest_source(False) == "DAILY_DIGEST (Cortex draft)"
    for label in (digest_source(True), digest_source(False)):
        assert "grounded" not in label.lower()


# -- app wiring ----------------------------------------------------------------------------------------------------

def test_latest_digest_reads_the_v165_columns_only_when_asked():
    from app.data import mart_sql
    bare, grounded = mart_sql.latest_digest(), mart_sql.latest_digest(grounded=True)
    cols = ("BODY_SOURCE", "GROUNDING_OK", "FIGURES_CHECKED", "UNGROUNDED", "AI_BODY", "FACTS")
    assert cols == mart_sql.DIGEST_GROUNDING_COLUMNS
    for c in cols:
        assert c not in bare and c in grounded, c
    assert mart_sql.latest_digest(grounded=False) == bare
    # the pre-V165 read is byte-identical to the one before this wave (same cache entry, canary unchanged)
    assert "SELECT DIGEST_DATE, MODEL, BODY, CREATED_AT\nFROM " in bare
    for sql in (bare, grounded):
        assert sql.count(";") == 0 and sql.rstrip().endswith("LIMIT 1") and "ORDER BY DIGEST_DATE DESC" in sql
    sqlglot = pytest.importorskip("sqlglot")
    names = [sqlglot.parse_one(s, read="snowflake").named_selects for s in (bare, grounded)]
    assert names[0] == ["DIGEST_DATE", "MODEL", "BODY", "CREATED_AT"]
    assert names[1] == names[0] + list(cols)
    canary = read("app/data/canary.py")
    assert '("mart.latest_digest", mart_sql.latest_digest),' in canary        # the canary reads it bare


@pytest.mark.parametrize("page", ["brief", "overview"])
def test_pages_gate_the_read_and_show_the_measured_label(page):
    src = read(f"app/ui/pages/{page}.py")
    assert "has_migration(165, _PAGE)" in src and "from app.ui.schema_gate import has_migration" in src
    assert "mart_sql.latest_digest()" not in src                  # every digest read goes through the gate
    assert "prov = digest_provenance(" in src and "status_chips([(prov.chip, prov.tone)])" in src
    assert 'with st.popover("Show the withheld AI draft"):' in src and "if prov.show_draft:" in src
    assert "if prov.detail:" in src and 'with st.expander(f"{prov.title} — ' in src
    # no fixed "grounded" claim is left in the source string or the caption
    assert "Cortex, grounded" not in src and "grounded in the exec board" not in src
    # the source label names the check only once V165 is applied (review r1 W4): one gated helper, no literal
    assert "figures checked against the exec board" not in src and src.count("digest_source(") == 1
    # the unmatched-figure line keeps the '$' of its tokens: escaped at the sink (review r1 W12)
    assert "st.caption(md_dollars(prov.detail))" in src and "st.caption(prov.detail)" not in src
    # the body line stays verbatim (tests/test_ai_grounding.py::test_digest_bodies_escape_dollars)
    var = "drow" if page == "brief" else "row"
    assert f'st.markdown(md_dollars(str({var}.get("BODY") or "")))' in src
    assert f'st.markdown(md_dollars(str({var}.get("AI_BODY") or "")))' in src
    # collapsed by default, like before
    exp = src.index('with st.expander(f"{prov.title} — ')
    assert "expanded=False" in src[exp:exp + 120]


def test_overview_keeps_its_cache_key_and_drops_the_model_for_a_template():
    ov = read("app/ui/pages/overview.py")
    assert 'key="daily_digest", tier="hourly"' in ov
    assert """_model = "" if prov.ai_written is False else f" ({row.get('MODEL')})\"""" in ov
    assert "every figure is checked against those facts, and a templated digest is sent" in ov
    assert "does not change with the company filter" in ov
    # the check is claimed only when V165 is applied AND the row carries a grounding record (review r1 W4/W15;
    # rendered both ways in tests/test_digest_render_shaped.py)
    assert "_grounded = has_migration(165, _PAGE)" in ov and "latest_digest(grounded=_grounded)" in ov
    assert "_checked = _grounded and prov.ai_written is not None" in ov
    assert 'source=digest_source(_grounded))' in ov


def test_brief_digest_stays_in_its_recent_batch():
    b = read("app/ui/pages/brief.py")
    assert '{"key": "digest", "sql": _digest_sql, "source": _digest_source},' in b
    assert "_digest_grounded = has_migration(165, _PAGE)" in b
    assert "_digest_sql = mart_sql.latest_digest(grounded=_digest_grounded)" in b
    assert "_digest_source = digest_source(_digest_grounded)" in b
    assert 'run(_digest_sql, page=_PAGE, key="daily_digest", tier="recent",' in b
    assert "source=_digest_source)" in b
    assert b.index("_digest_sql = mart_sql.latest_digest(") < b.index("_b_rec = run_batch([")
