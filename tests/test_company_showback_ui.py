"""#42 Part 1 — Company all-in showback on Cost ▸ Chargeback & AI: wiring, copy and render states.

Source locks: the read rides the Department chargeback batch (one round trip; both exits of
_chargeback_tab return the member), the section sits between Department chargeback and Query-tag
governance, the panel is mart-only and hourly with no write and no raw absence banner, and the copy
that must stay true is pinned. AppTest renders: a failed read is 'unavailable' (never clean, never
$0), missing objects are 'needs_setup', no closed metered day is a quiet caption, the ALL view shows
the company + account rows and the tie-out, and a company scope hides the account rows. Plus the
stale Spend & Attribution help fixed alongside, and the shared storage-tier rates.
"""

from __future__ import annotations

import html
import re

import pandas as pd
import pytest

from tests._source import read

_CB = "app/ui/pages/cost_parts/ai_chargeback.py"
_COST = "app/ui/pages/cost.py"
_SPEND = "app/ui/pages/cost_parts/spend.py"


def _fn(src: str, name: str) -> str:
    return src.split(f"def {name}(", 1)[1].split("\ndef ", 1)[0].split("\n@st.fragment", 1)[0]


# ---------------------------------------------------------------------------
# Source locks
# ---------------------------------------------------------------------------

def test_showback_rides_the_chargeback_batch():
    src = read(_CB)
    tab = _fn(src, "_chargeback_tab")
    spec = tab.index('_cb_specs.append({"key": "showback", "tier": "hourly",')
    assert spec < tab.index("run_batch_mixed(_cb_specs, page=_PAGE)")
    assert "chargeback_sql.company_allin_showback(days, company, bounds=bounds)" in tab
    assert src.count("run_batch_mixed(") == 1                              # still ONE round trip
    assert '_showback = _pf.get("showback")' in tab
    assert tab.count("return _showback") == 2                              # guard exit + normal exit
    assert "-> QueryResult | None:" in tab


def test_cost_dispatch_places_showback_after_department_chargeback():
    cost = read(_COST)
    block = cost.split('elif section == "Chargeback & AI":', 1)[1].split("elif section ==", 1)[0]
    i_dept = block.index('section_header("Department chargeback"')
    i_show = block.index('section_header("Company all-in showback", "", "chargeback", anchor="cost-showback")')
    i_tags = block.index('section_header("Query-tag governance"')
    assert i_dept < i_show < i_tags
    assert ('_showback_pre = _chargeback_tab(f["company"], f["days"], rate, is_operator, bounds=f["bounds"])'
            in block)
    assert "prefetched=_showback_pre" in block
    assert '_company_showback_panel(f["company"], f["days"], rate, ai_rate, settings, bounds=f["bounds"],' in block
    # same visibility as the tab: no operator gate, no toggle in front of it
    pre = block[:i_show]
    assert "is_operator and" not in block[i_show:i_tags] and "st.toggle(" not in block[i_show:i_tags]
    assert pre.count("st.divider()") == 1
    assert ('"note": "Company shapes chargeback, the all-in showback and AI users; Cortex service totals '
            'remain account-wide."') in cost


def test_panel_is_mart_only_and_hourly():
    body = _fn(read(_CB), "_company_showback_panel")
    assert 'tier="hourly"' in body and "source=_SHOWBACK_SOURCE" in body
    for banned in ("ACCOUNT_USAGE", "run_mart_first(", "cost_sql.", "execute_statement(", "st.info(",
                   "st.success(", "st.button(", "reconciliation_footer(", "sort_label="):
        assert banned not in body, banned
    assert 'if not guard(res, "The showback read returned no rows for this window."):' in body
    assert "ACCOUNT_USAGE" not in read(_CB).split("_SHOWBACK_SOURCE = ", 1)[1].split("_SHOWBACK_HELP", 1)[0]
    assert 'empty_state("unavailable",' in body and 'empty_state("needs_setup",' in body
    assert 'empty_state("no_data_yet",' in body and 'empty_state("clean",' in body


def test_copy_locks():
    from app.ui.pages.cost_parts import ai_chargeback as cb

    assert cb._SHOWBACK_SOURCE == (
        "FACT_METERING_DAILY + FACT_WAREHOUSE_DAILY + FACT_OBJECT_COST_DAILY + FACT_AI_USAGE_DAILY + "
        "FACT_STORAGE_DAILY + FACT_STORAGE_ACCOUNT_DAILY (marts, complete metered days)")
    assert cb._SHOWBACK_HELP == (
        "Metered spend plus estimated storage for the window, split by company wherever the data carries a "
        "company key: warehouse metering (by warehouse), serverless maintenance from the object-cost ledger "
        "(by database), Cortex Code in Snowsight and the CLI (by user) and storage (by database). Spend with "
        "no company key stays on account-level rows, so the rows add up to the all-in total. Showback, not "
        "an invoice: nothing is spread by a share.")
    note = cb._SHOWBACK_TABLE_NOTE
    assert note.startswith("Warehouse: exact warehouse metering before the cloud-services adjustment, by the "
                           "warehouse's company (the same metering as Department chargeback above, over this "
                           "panel's days).")
    for phrase in ("its query-compute arms are slices of warehouse compute and are left out so nothing counts "
                   "twice", "(Cortex Code Desktop is not read, so it stays on the unattributed row)",
                   "Storage: estimated from average daily database and fail-safe bytes, by database.",
                   "Other metered: metering with no company key."):
        assert phrase in note, phrase
    body = _fn(read(_CB), "_company_showback_panel")
    for phrase in ("Figures are rounded separately.", "The unattributed row is the remainder, so the rows "
                   "always add up; its size is", "Metering days are UTC while the warehouse and object-cost "
                   "facts \"\n        \"use Central days", "Data transfer, Marketplace and org-currency "
                   "adjustments are not included; the org rate card on",
                   "a credit that lowers the bill (a \"\n                     \"positive amount here, negative in "
                   "the table)"):
        assert phrase in body, phrase
    # the stale Chargeback-total help now points at the showback; the statement copy is untouched (Part 2)
    tab = _fn(read(_CB), "_chargeback_tab")
    assert ("are not allocated here. The Company all-in showback below adds serverless, \"\n"
            "                 \"Cortex Code and storage by company; transfer is not attributed anywhere.") in tab
    assert "Scope: warehouse compute only" in read(_CB)


def test_spend_help_no_longer_claims_warehouse_is_the_only_company_key():
    spend = read(_SPEND)
    assert "the only spend with a " not in spend and "the sole spend with a company key" not in spend
    assert "storage credits have none." not in spend
    assert "Chargeback & AI → Company all-in showback attributes" in spend
    assert "Chargeback & AI → \"\n                 \"Company all-in showback credits serverless" in spend
    assert "and Chargeback & AI → Company all-in showback adds serverless, Cortex \"" in spend
    # the cost_hunt3 locks are kept
    assert "Own-account warehouse metering only" in spend and "unattributed gap" in spend


def test_storage_tier_panel_uses_the_shared_rates():
    spend = read(_SPEND)
    body = _fn(spend, "_account_storage_tiers")
    assert "_r = storage_tier_rates(settings)" in body
    assert "348.16" not in spend
    from app.logic.showback import storage_tier_rates
    assert storage_tier_rates({})["HYBRID"] == 348.16                    # the default moved, not changed


# ---------------------------------------------------------------------------
# AppTest renders (no ButtonGroup on this path, so they run on the streamlit floor leg too)
# ---------------------------------------------------------------------------

def _render(monkeypatch, res, company="ALL", audit=False):
    pytest.importorskip("streamlit")
    from streamlit.testing.v1 import AppTest

    from app.ui.pages.cost_parts import ai_chargeback

    reads: list = []
    monkeypatch.setattr(ai_chargeback, "run", lambda *a, **k: reads.append(a) or res)
    monkeypatch.setattr(ai_chargeback, "_SHOWBACK_TEST_ARGS", {"company": company, "res": res}, raising=False)

    def _app():
        from app.config import DEFAULT_SETTINGS
        from app.ui.pages.cost_parts import ai_chargeback as _cb
        _a = _cb._SHOWBACK_TEST_ARGS
        _cb._company_showback_panel(_a["company"], 30, 3.68, 2.20, dict(DEFAULT_SETTINGS), prefetched=_a["res"])

    at = AppTest.from_function(_app, default_timeout=60)
    if audit:
        at.session_state["_ow_present_mode"] = "audit"
    at.run()
    assert not at.exception, at.exception
    assert reads == []                                     # prefetched: no second read
    return at


def _texts(at) -> str:
    """Every rendered text element, HTML-unescaped (KPI cards render as escaped HTML)."""
    return html.unescape(" ".join(str(e.value) for e in list(at.markdown) + list(at.caption) + list(at.info)
                                  + list(at.error) + list(at.warning) + list(at.success)))


def _qr(df=None, ok=True, error=""):
    from app.core.result import QueryResult
    return QueryResult(df=df if df is not None else pd.DataFrame(), ok=ok, error=error, source="showback stub")


def test_failed_read_renders_unavailable_not_clean(monkeypatch):
    at = _render(monkeypatch, _qr(ok=False, error="Statement timed out\nSQL: SELECT ..."))
    assert any("Query failed: Statement timed out" in str(e.value) for e in at.error)
    assert not list(at.success)
    blob = _texts(at)
    assert "$0.00" not in blob and "\\$0.00" not in blob and "Ties out" not in blob
    assert "Error detail" in " ".join(str(e.label) for e in at.expander)


def test_capped_read_is_never_totalled(monkeypatch):
    from tests.test_company_showback import _synthetic_frame
    res = _qr(_synthetic_frame())
    res.truncated = True
    at = _render(monkeypatch, res)
    assert any("hit the row cap, so its company totals would be incomplete" in str(e.value) for e in at.error)
    assert "Ties out" not in _texts(at) and not list(at.dataframe)


def test_missing_objects_render_needs_setup(monkeypatch):
    at = _render(monkeypatch, _qr(ok=False, error="Object does not exist — run the migrations and roles.sql"))
    assert any("needs OVERWATCH's objects installed" in str(i.value) for i in at.info)
    assert not list(at.error) and not list(at.success)


def test_no_metering_day_is_quiet_caption_not_clean(monkeypatch):
    from tests.test_company_showback import _COVERAGE, _synthetic_frame
    cov = dict(_COVERAGE)
    cov["FACT_METERING_DAILY"] = ("2025-01-01", "2026-09-20")
    frame = _synthetic_frame(window=("2026-09-23", "2026-09-29"), span=(None, None, 0),
                             storage_span=(None, None, 0), metering=[], coverage=cov)
    at = _render(monkeypatch, _qr(frame))
    caps = " ".join(str(c.value) for c in at.caption)
    assert "No complete metered day falls in this Window yet" in caps
    assert "The newest daily-metering day is Sep 20, 2026, so the metering loader may be behind" in caps
    assert not list(at.success) and not list(at.error) and not list(at.info)
    assert "Ties out" not in caps
    # never loaded at all: a setup state, not a quiet empty
    cov["FACT_METERING_DAILY"] = (None, None)
    never = _render(monkeypatch, _qr(_synthetic_frame(span=(None, None, 0), storage_span=(None, None, 0),
                                                      metering=[], coverage=cov)))
    assert any("Daily metering (FACT_METERING_DAILY) has no rows yet" in str(i.value) for i in never.info)
    # a malformed frame: unavailable, with what is missing one click away
    shaped = _render(monkeypatch, _qr(frame.drop(columns=["TIB_MO"])))
    assert any("without the columns or rows this panel needs" in str(e.value) for e in shaped.error)


def test_populated_all_view_renders_rows_and_tie_out(monkeypatch):
    from app.logic import showback
    from tests.test_company_showback import _synthetic_frame
    at = _render(monkeypatch, _qr(_synthetic_frame()), audit=True)
    blob = _texts(at)
    for label in ("All-in total, Aug 31 – Sep 29", "Company-attributed share, before adjustment",
                  "Unattributed (no company key)", "Cloud-services adjustment"):
        assert label in blob, label
    tables = [d.value for d in at.dataframe]
    main = next(t for t in tables if "COMPANY" in getattr(t, "columns", []))
    assert main["COMPANY"].tolist() == ["ALFA", "Trexis", showback.UNKNOWN_ROW, showback.ADJUSTMENT_ROW,
                                        showback.UNATTRIBUTED_ROW]
    assert any("SERVICE_FAMILY" in getattr(t, "columns", []) for t in tables)      # the breakdown
    caps = " ".join(str(c.value) for c in at.caption)
    assert re.search(r"Ties out: company rows \\\$[\d,.]+ − cloud-services adjustment \\\$184\.00 \+ "
                     r"unattributed \\\$[\d,.]+ = all-in total \\\$[\d,.]+\.", caps), caps[:600]
    assert "Covers Aug 31 – Sep 29: 30 complete metered days of the selected Window (30d)." in caps
    assert "Billing basis: billed metering credits" in caps and "\\$3.68 per credit (\\$2.20 for AI)" in caps
    assert "How it is computed:" in caps                                         # audit mode only
    assert "What the unattributed row holds" in " ".join(str(e.label) for e in at.expander)
    assert not list(at.error) and not list(at.success)


def test_company_scope_view_hides_account_rows(monkeypatch):
    from tests.test_company_showback import _synthetic_frame
    at = _render(monkeypatch, _qr(_synthetic_frame(only="ALFA")), company="ALFA")
    blob = _texts(at)
    assert "ALFA all-in, Aug 31 – Sep 29" in blob and "Share of account spend, before adjustment" in blob
    main = next(d.value for d in at.dataframe if "COMPANY" in getattr(d.value, "columns", []))
    assert main["COMPANY"].tolist() == ["ALFA"]
    caps = " ".join(str(c.value) for c in at.caption)
    assert "Company scope: ALFA only." in caps and "Ties out" not in caps
    assert "Account-level" not in blob
    # nothing stamped for the scope: UNKNOWN reads verified-clean, a named company a quiet caption
    empty = _synthetic_frame(only="UNKNOWN", warehouse={}, serverless=[], coco=[], storage_db={})
    clean = _render(monkeypatch, _qr(empty), company="UNKNOWN")
    assert "Nothing in this span is stamped UNKNOWN" in _texts(clean)
    quiet = _render(monkeypatch, _qr(empty), company="Trexis")
    assert ("No warehouse, serverless, Cortex Code or storage spend is stamped Trexis in Aug 31 – Sep 29."
            in " ".join(str(c.value) for c in quiet.caption))


def _green_rows(at) -> int:
    """The compact verified-clean rows empty_state('clean') paints (exception_summary's ok row)."""
    return sum(str(m.value).count("ow-exception--ok") for m in at.markdown)


def test_unknown_scope_is_never_clean_while_a_keyed_source_has_a_gap(monkeypatch):
    # R1-14: an empty UNKNOWN table is verified clear only when every keyed source covers the span
    from tests.test_company_showback import _COVERAGE, _synthetic_frame

    def _empty_unknown(**kw):
        return _synthetic_frame(only="UNKNOWN", warehouse={}, serverless=[], coco=[], storage_db={}, **kw)

    never = dict(_COVERAGE)
    never["FACT_WAREHOUSE_DAILY"] = (None, None)                         # (A) never loaded
    stale = dict(_COVERAGE)
    stale["FACT_OBJECT_COST_DAILY"] = ("2026-06-30", "2026-07-15")       # (B) frozen loader
    none_loaded = {t: (None, None) for t in _COVERAGE if t != "FACT_METERING_DAILY"}
    none_loaded["FACT_METERING_DAILY"] = _COVERAGE["FACT_METERING_DAILY"]   # (C) metering only
    cases = {
        "A": (_empty_unknown(coverage=never), "FACT_WAREHOUSE_DAILY does not cover all of Aug 31 – Sep 29"),
        "B": (_empty_unknown(coverage=stale), "FACT_OBJECT_COST_DAILY does not cover all of Aug 31 – Sep 29"),
        "C": (_empty_unknown(coverage=none_loaded, storage_span=(None, None, 0)),
              # no account-storage day: the per-database line rides it, so the account fact is the one named
              "FACT_WAREHOUSE_DAILY, FACT_OBJECT_COST_DAILY, FACT_AI_USAGE_DAILY and FACT_STORAGE_ACCOUNT_DAILY "
              "do not cover all of Aug 31 – Sep 29"),
        # (D) R1-13: the Cortex Code fact still holds only the morning of the span's last day
        "D": (_empty_unknown(loaded={"FACT_AI_USAGE_DAILY": "2026-09-29 07:40:00"}),
              "FACT_AI_USAGE_DAILY does not cover all of Aug 31 – Sep 29"),
    }
    for name, (frame, named) in cases.items():
        at = _render(monkeypatch, _qr(frame), company="UNKNOWN")
        assert _green_rows(at) == 0, name
        assert "Nothing in this span is stamped UNKNOWN:" not in _texts(at), name
        caps = " ".join(str(c.value) for c in at.caption)
        assert f"Nothing read here is stamped UNKNOWN, but {named} in full" in caps, (name, caps[:400])
        assert "so UNKNOWN cannot be confirmed clear." in caps, name
        assert not list(at.success) and not list(at.error), name
    # the fully covered, fully loaded read keeps its verified-clean row
    covered = _render(monkeypatch, _qr(_empty_unknown(loaded={"FACT_AI_USAGE_DAILY": "2026-09-30 07:40:00"})),
                      company="UNKNOWN")
    assert _green_rows(covered) == 1
    assert "cannot be confirmed clear" not in " ".join(str(c.value) for c in covered.caption)
    # a named company stays a quiet caption either way
    quiet = _render(monkeypatch, _qr(cases["A"][0]), company="Trexis")
    assert _green_rows(quiet) == 0
    assert ("No warehouse, serverless, Cortex Code or storage spend is stamped Trexis in Aug 31 – Sep 29."
            in " ".join(str(c.value) for c in quiet.caption))


def test_partly_loaded_day_note_renders_under_the_all_view(monkeypatch):
    # R1-13: the note reaches the page (the notes loop renders every one under the table)
    from tests.test_company_showback import _synthetic_frame
    at = _render(monkeypatch, _qr(_synthetic_frame(loaded={"FACT_OBJECT_COST_DAILY": "2026-09-29 07:05:00"})))
    caps = " ".join(str(c.value) for c in at.caption)
    assert ("Serverless: FACT_OBJECT_COST_DAILY's rows for Sep 29, 2026 were loaded before that day was "
            "complete") in caps
