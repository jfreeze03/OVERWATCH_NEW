"""Locks for the metrics-triage fixes (docs/reviews/METRICS_TRIAGE_2026-07-29.md).

HIGH #1 (v4.66.0) + the MEDIUM app batch and AI-prefix chip fix (v4.68.0).
"""
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]


def test_triage1_month_end_projection_uses_full_month_account_frame():
    """HIGH #1: the month-end projection must be fed the account-wide 150d frame
    (proj_daily, built from the already-loaded _bt_hist), not the 7d company-scoped
    exec-board `daily` frame that truncated MTD for most of the month and mismatched
    the account-wide Projected-month-end / MTD KPIs."""
    ov = (_ROOT / "app" / "ui" / "pages" / "overview.py").read_text(encoding="utf-8")
    assert 'proj_daily = _proj[["DAY", "USD"]]' in ov              # built from _bt_hist
    assert '_proj["USD"] = _proj["CREDITS_BILLED"].map' in ov      # account-wide billed frame
    assert "month_end_projection(proj_daily," in ov               # projection uses it
    assert "month_end_projection(daily," not in ov                # never the windowed board frame
    # the ml_forecast branch's MTD also derives from the full-month frame
    assert "proj_daily[pd.to_datetime(proj_daily.iloc[:, 0])" in ov
    assert "else:\n        proj_daily = daily" in ov              # graceful fallback when mart down


# ---------------------------------------------------------------------------
# MEDIUM app batch (v4.68.0)
# ---------------------------------------------------------------------------

def test_triage6_rate_card_model_is_compute_only():
    """MEDIUM #6: the rate-card model side excludes AI/Cortex (org COMPUTE_USD
    excludes AI), with a PREFIX 'AI%' match — '%AI%' would drop
    SNOWPARK_CONTAINER_SERVICES (contAIner), which the org buckets as COMPUTE."""
    from app.data import mart_sql
    sql = mart_sql.fact_daily_spend_compute(70)
    assert "NOT ILIKE '%CORTEX%'" in sql and "NOT ILIKE '%INTELLIGENCE%'" in sql
    assert "NOT ILIKE 'AI%'" in sql
    assert "NOT ILIKE '%AI%'" not in sql                      # the contAIner trap
    ct = (_ROOT / "app" / "ui" / "pages" / "cost_parts" / "contract.py").read_text(encoding="utf-8")
    assert "fact_daily_spend_compute(70)" in ct
    assert 'key="fact_daily_compute_70"' in ct                # own cache identity
    can = (_ROOT / "app" / "data" / "canary.py").read_text(encoding="utf-8")
    assert "mart.fact_daily_spend_compute" in can             # house law #4: builder has a canary


def test_triage8_cs_ratio_matches_live_exclusions():
    """MEDIUM #8: the mart CS-ratio drops the CLOUD_SERVICES_ONLY pseudo-warehouse
    and floors near-idle warehouses at 0.5 credits, matching the live builder —
    no more 100%-CS ELEVATED phantom row spuriously triggering the drill."""
    from app.data import mart_sql
    sql = mart_sql.fact_cloud_services_ratio(7, "ALFA")
    assert "UPPER(WAREHOUSE_NAME) <> 'CLOUD_SERVICES_ONLY'" in sql
    assert "HAVING SUM(CREDITS_TOTAL) >= 0.5" in sql
    assert "HAVING SUM(CREDITS_TOTAL) > 0\n" not in sql


def test_triage9_cs_cache_pct_scaled_to_percent():
    """MEDIUM #9: CACHE_PCT_SUM sums 0-1 fractions (PERCENTAGE_SCANNED_FROM_CACHE),
    so the shape drill multiplies by 100 — without it every row rendered 0% or 1%."""
    from app.data import mart_sql
    sql = mart_sql.cloud_svc_top_shapes(7, "ALFA")
    assert "SUM(CACHE_PCT_SUM) / NULLIF(SUM(RUNS), 0) * 100" in sql


def test_triage10_mfa_gap_single_definition():
    """MEDIUM #10: both Access-panel builders use HAS_MFA like governance_counts
    (native MFA counts; EXT_AUTHN_DUO was Duo-only and false-positived native MFA)."""
    sec = (_ROOT / "app" / "data" / "security_sql.py").read_text(encoding="utf-8")
    assert "EXT_AUTHN_DUO" not in sec
    assert sec.count("COALESCE(U.HAS_MFA, FALSE) = FALSE") == 3   # 2 fixed + governance_counts


def test_triage12_query_window_anchors_match_live():
    """MEDIUM #12: mart and live query-window summaries share the CURRENT_DATE
    anchor, so the same labeled tile covers the same span from either source."""
    from app.data import mart27_sql, mart_sql
    fact = mart_sql.fact_query_window_summary(30, "ALFA")
    schema = mart27_sql.schema_window_summary(1, "ALFA", "ALFA_EDW_PRD", "stage")
    assert "HOUR_TS >= DATEADD('day', -30, CURRENT_DATE())" in fact
    assert "HOUR_TS >= DATEADD('day', -1, CURRENT_DATE())" in schema


def test_triage13_spend_lens_label_is_account_wide():
    """MEDIUM #13 (+ verify round): the 'why totals differ' expander no longer
    presents the account-wide rebate-netted warehouse total as Overview's
    company-scoped KPI — and makes no strict directional claim (at ALL scope
    Overview prices UNADJUSTED usage and reads ABOVE the rebate-netted figure)."""
    sp = (_ROOT / "app" / "ui" / "pages" / "cost_parts" / "spend.py").read_text(encoding="utf-8")
    assert "Warehouse portion of that billed spend" in sp
    assert "Overview's company-scoped spend KPI — warehouse-exact" not in sp
    assert "different basis" in sp                          # honest: not a slice/inequality
    assert "reads at or below it" not in sp                 # the disproven strict claim
    assert "reader metering included" in sp                 # reader sits INSIDE wh_usd
    assert "(serverless, AI, replication, reader)" not in sp  # ...not in the remainder


def test_ai_service_match_is_prefix_everywhere():
    """Chip fix (with #6): 'AI' service matching is PREFIX-form in every SQL
    builder — the contains-form '%AI%' matches SNOWPARK_CONTAINER_SERVICES
    (contAIner) and would misprice container compute as Cortex. Mart + live
    twins changed together to preserve parity."""
    from app.data import mart_sql
    m = mart_sql.fact_cortex_daily_spend(7)
    assert "ILIKE 'AI%'" in m
    for f in sorted((_ROOT / "app" / "data").glob("*.py")):   # every SQL builder module
        assert "ILIKE '%AI%'" not in f.read_text(encoding="utf-8"), f.name


def test_triage3_live_score_gets_all_nine_signals():
    """MEDIUM #3 (v4.70.0): the live platform score's Stale-telemetry (cap 12) and
    Owner-queue (cap 9) drivers can now fire — the caller passes stale_sources
    (via the shell-shared health_strip entry, zero extra queries warm) and
    open_high_actions (ACTION_QUEUE hoisted above the score and reused by the
    Top-actions panel — still exactly one action_queue read on the page)."""
    ov = (_ROOT / "app" / "ui" / "pages" / "overview.py").read_text(encoding="utf-8")
    assert '"stale_sources": stale_sources,' in ov
    assert '"open_high_actions": open_high_actions,' in ov
    assert 'key="health_strip"' in ov                              # shared cache entry
    assert ov.count('key="action_queue"') == 1                     # hoisted, not duplicated
    assert ov.index('key="action_queue"') < ov.index("scoring.platform_score")
    hs = (_ROOT / "app" / "data" / "mart_sql.py").read_text(encoding="utf-8")
    assert "'STALE_SOURCES'" in hs                                  # new health-strip arm
    # the strip's SQL cadence rule mirrors the Control Room freshness board
    assert "SOURCE_NAME LIKE '%DAILY%' OR SOURCE_NAME LIKE '%METERING%'" in hs


def test_role_share_anchor_matches_live_twin():
    """Verify round: role_share (mart) now anchors on CURRENT_DATE like its live
    twin chargeback_sql.role_share_within_warehouse, so the role-share tile
    covers the same span whichever source serves it."""
    from app.data import chargeback_sql, mart27_sql
    mart = mart27_sql.role_share(7, "ALFA")
    live = chargeback_sql.role_share_within_warehouse(7, "ALFA")
    assert "HOUR_TS >= DATEADD('day', -7, CURRENT_DATE())" in mart
    assert "START_TIME >= DATEADD('day', -7, CURRENT_DATE())" in live
    assert "CURRENT_TIMESTAMP())" not in mart.split("WITH scoped")[0]   # rolling anchor gone


def test_cache_pct_readers_scale_fraction_to_percent():
    """Chip fix: PERCENTAGE_SCANNED_FROM_CACHE is a 0-1 fraction (empirically
    confirmed on live rows, owner 2026-07-29), so BOTH repeat-query read points
    scale x100 — otherwise flag_repeat_candidates' percent threshold
    (REPEAT_LOW_CACHE_PCT=25.0) passes every row and captions misreport cache.
    Matches the x100-at-read pattern established for MART_CLOUD_SVC_DAILY (#9)."""
    ins = (_ROOT / "app" / "data" / "insights_sql.py").read_text(encoding="utf-8")
    assert "AVG(COALESCE(PERCENTAGE_SCANNED_FROM_CACHE, 0)) * 100 AS AVG_CACHE_PCT" in ins
    m27 = (_ROOT / "app" / "data" / "mart27_sql.py").read_text(encoding="utf-8")
    assert "NULLIF(SUM(f.RUNS), 0) * 100, 1) AS AVG_CACHE_PCT" in m27
