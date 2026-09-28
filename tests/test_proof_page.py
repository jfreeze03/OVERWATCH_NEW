"""v4.597 (Option C): Decision Studio -> Proof. Locks for the restructure's page-level contracts:

- the retired page label survives ONLY as the navigate remap table (grep gate);
- Proof is READ-ONLY (safe for EXECUTIVE) and its cross-links are profile-gated;
- the Pipeline projection fragment never touches the per-render proof memo and seeds its sliders
  without value= (floor-compat);
- the Pipeline reads share their cache entries with Cost ▸ Optimize and never fall back live;
- old ?page=decision-studio deep links and saved views remap (behavioural, via a fake runtime);
- the breadcrumb never stutters "Proof ▸ Proof";
- the pure helpers behind Settling and the measured slider defaults.
"""

from __future__ import annotations

import ast
import importlib
import inspect
import re
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pandas as pd

from app.config import PAGES_BY_PROFILE
from app.data import mart27_sql
from app.logic.proof import settle_schedule

_ROOT = Path(__file__).resolve().parents[1]
_BODY_REL = "app/ui/decision_studio.py"
_SHELL_REL = "app/ui/pages/decision_studio.py"


def _src(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


def _fn(src: str, name: str) -> str:
    return src.split(f"def {name}(", 1)[1].split("\ndef ", 1)[0]


# --- 1. grep gate: the retired label lives only in the remap table ---------------------------

def _string_constants(path: Path) -> list[tuple[int, str]]:
    """Every str constant in a module EXCEPT docstrings (module / class / function)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if (body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                docstrings.add(id(body[0].value))
    return [(n.lineno, n.value) for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docstrings]


def test_no_decision_studio_literal_outside_the_remap_table():
    allowed = _ROOT / "app" / "logic" / "navigate.py"
    offenders = []
    for py in sorted((_ROOT / "app").rglob("*.py")):
        if py == allowed:
            continue
        for line, value in _string_constants(py):
            # no quoted "Decision Studio" literal, and no user-facing copy pointing at the old page
            if "Decision Studio" in value:
                offenders.append(f"{py.relative_to(_ROOT)}:{line}: {value[:70]!r}")
    assert not offenders, offenders
    # ... and in the remap module it appears exactly once, as the LEGACY_PAGE constant
    nav = [v for _, v in _string_constants(allowed) if "Decision Studio" in v]
    assert nav == ["Decision Studio"]
    assert 'LEGACY_PAGE = "Decision Studio"' in allowed.read_text(encoding="utf-8")


# --- 2. Proof is read-only and EXECUTIVE-safe -------------------------------------------------

def test_proof_has_no_write_path():
    for rel in (_BODY_REL, _SHELL_REL):
        src = _src(rel)
        for token in ("execute_statement(", "execute_action(", "write_gate_open(", "stamp_write(",
                      "is_operator", "INSERT INTO", "UPDATE ", "MERGE INTO", "CALL "):
            assert token not in src, f"{rel} carries a write token {token!r}"
        # no button is gated on an operator check — there is nothing to operate here
        assert not re.search(r"is_operator\w*\s*(\(\))?\s*and\s*st\.button\(", src), rel


def test_proof_cross_links_are_profile_gated():
    body = _src(_BODY_REL)
    proof = _fn(body, "_proof_tab")
    pipe = _fn(body, "_pipeline_tab")
    # Alerts (READER lacks it) and Operations (EXECUTIVE lacks it) doorways render only when openable
    assert 'can_open("Alerts") and st.button("Per-rule alert precision → Alerts ▸ Rules"' in proof
    assert 'can_open("Operations") and st.button("Change detail → Operations ▸ Change impact"' in proof
    assert 'can_open("Operations") and st.button("Track more query work → Operations ▸ Optimize"' in pipe
    # the Entity 360 row drill only for a profile with Control Room; others get a plain table
    assert 'if can_open("Control Room"):' in pipe and "styled_table(display," in pipe
    # every request_navigation target in the body is either gated, the page itself, or a page every
    # profile offers (Cost Intelligence) — never a silent clamp to Overview
    for page in re.findall(r'request_navigation\(\s*"([^"]+)"', body):
        everyone = all(page in pages for pages in PAGES_BY_PROFILE.values())
        assert everyone or f'can_open("{page}")' in body, page
    # the shared gate is the state helper (the Optimize stand-in was retired for it)
    assert "from app.core.state import can_open, request_navigation" in body
    opt = _src("app/ui/pages/ops_parts/optimize_queue.py")
    assert "def _can_open" not in opt and 'can_open("Control Room")' in opt


def test_can_open_follows_the_viewer_profile(monkeypatch):
    import app.core.session as session
    from app.core import state

    monkeypatch.setattr(session, "current_role", lambda: "ROLE")
    monkeypatch.setattr(session, "active_profile", lambda _role="": "EXECUTIVE")
    assert state.viewer_pages() == PAGES_BY_PROFILE["EXECUTIVE"]
    assert state.can_open("Proof") and state.can_open("Cost Intelligence")
    assert not state.can_open("Control Room") and not state.can_open("Operations")
    monkeypatch.setattr(session, "active_profile", lambda _role="": "READER")
    assert state.can_open("Operations") and not state.can_open("Alerts")

    def _boom(_role=""):
        raise RuntimeError("no session")

    monkeypatch.setattr(session, "active_profile", _boom)
    assert state.viewer_pages() == () and state.can_open("Admin")    # fail-open; the clamp still holds


# --- 3. the projection fragment -----------------------------------------------------------------

def test_projection_fragment_is_memo_free_and_floor_compatible():
    body = _src(_BODY_REL)
    assert body.count("@st.fragment") == 1
    assert "@st.fragment\ndef _pipeline_projection(frame: pd.DataFrame, defaults: dict) -> None:" in body
    frag = body.split("def _pipeline_projection(", 1)[1].split("\ndef _pipeline_tab(", 1)[0]
    assert "_proof_signals(" not in frag and "reset_proof_memo(" not in frag and "run(" not in frag
    # seed-if-absent, then key-only sliders (never value= alongside a pre-seeded key)
    assert "if _key not in st.session_state:" in frag
    sliders = re.findall(r"st\.slider\((.*?)\)\n", frag, re.S)
    assert len(sliders) == 3
    for call in sliders:
        assert "value=" not in call and "key=" in call
    for key in ("proof_adoption", "proof_realization", "proof_conf_floor"):
        assert f'key="{key}"' in frag
    # Reset to measured rewrites the keys in an on_click callback (before the widgets render)
    assert 'st.button("Reset to measured"' in frag and "on_click=_reset_to_measured" in frag
    # the page calls the fragment with values computed in the full run, after the memo-hit read
    pipe = _fn(body, "_pipeline_tab")
    assert pipe.index("sig = _proof_signals(rate)") < pipe.index(
        "_pipeline_projection(pipeline, _projection_defaults(sig, carried))")


def test_projection_defaults_are_measured_first_and_labelled():
    from app.ui import decision_studio as ds

    assumed = ds._projection_defaults(None, None)
    assert (assumed["adoption"], assumed["realization"], assumed["conf_floor"]) == (60, 70, 0.6)
    assert assumed["adoption_help"].startswith("Assumed")
    assert assumed["realization_help"].startswith("Assumed")
    sig = {"acc": {"ACCEPTANCE_PCT": 66.6, "DONE_N": 2, "DROPPED_N": 1}, "realization": 83.4}
    measured = ds._projection_defaults(sig, {"carried_pct": 10.0, "items": 3})
    assert (measured["adoption"], measured["realization"]) == (67, 83)
    assert measured["adoption_help"].startswith("Measured") and "2 done" in measured["adoption_help"]
    assert measured["realization_help"].startswith("Measured:")        # the rate wins over carried
    carried = ds._projection_defaults({"acc": {"ACCEPTANCE_PCT": None}, "realization": None},
                                      {"carried_pct": 140.0, "items": 2})
    assert carried["realization"] == 100                                # clamped into the slider
    assert carried["realization_help"].startswith("Measured (carried)")
    assert carried["adoption"] == 60 and carried["adoption_help"].startswith("Assumed")
    for d in (assumed, measured, carried):                              # slider-typed values
        assert isinstance(d["adoption"], int) and isinstance(d["realization"], int)


# --- 4. Pipeline reads: shared caches, mart-only ------------------------------------------------

def test_pipeline_reads_share_the_optimize_cache_and_never_go_live():
    body = _src(_BODY_REL)
    pipe = _fn(body, "_pipeline_tab")
    # the idle / sizing mart reads are Cost ▸ Optimize's mart legs verbatim (run_mart_first reads
    # its mart leg at tier "hourly" with the default row cap), so the run() cache entry is shared
    assert "run(mart27_sql.eff_idle_analysis(days, company, bounds=bounds), page=_PAGE," in pipe
    assert "run(mart27_sql.eff_sizing_profile(days, company, bounds=bounds), page=_PAGE," in pipe
    assert pipe.count('tier="hourly"') == 2
    comp = _src("app/ui/components.py")
    assert 'mart_tier: str = "hourly"' in comp
    opt = _src("app/ui/pages/cost_parts/optimize.py")
    assert "mart27_sql.eff_idle_analysis(days, company, bounds=bounds)," in opt
    # SHOW WAREHOUSES is the shared jump_wh metadata read; the queue reads at "recent"
    assert 'key="jump_wh",\n                       tier="metadata"' in pipe
    assert 'key=f"proof_queue_{company}", tier="recent"' in pipe
    # mart-only: no live fallback builder anywhere on the page (a miss is shown as unavailable)
    assert "insights_sql" not in body and "run_mart_first" not in body
    assert 'empty_state("unavailable", "The warehouse-efficiency mart could not be read' in pipe
    # the shared rollup helpers, exactly as Cost ▸ Optimize builds the addressable figure
    assert "idle_opportunities(" in pipe and "resize_opportunities(" in pipe
    assert "roll = rollup_savings(opps)" in pipe
    assert "queued, qsum = monthly_equivalent(" in pipe
    assert "pipeline = pipeline_frame(roll.items, " in pipe
    # the idle builder is the mart twin (no ACCOUNT_USAGE)
    assert "ACCOUNT_USAGE" not in mart27_sql.eff_idle_analysis(30, "ALL")


def test_live_proof_sections_reach_no_account_usage():
    """The v451 reach pin allows the body ACCESS_HISTORY only because of the HIDDEN _products;
    the dispatched sections themselves reach no ACCOUNT_USAGE table."""
    import sys
    sys.path.insert(0, str(_ROOT / "tests" / "history_locks"))
    trust = importlib.import_module("test_v451_trust")
    body = _src(_BODY_REL)
    live = body.split("def _products(", 1)[0] + body.split("def _products(", 1)[1].split(
        "\n_PROOF_MEMO: dict = {}", 1)[1]
    assert "def _proof_tab(" in live and "def _pipeline_tab(" in live and "def _products(" not in live
    tables: set = set()
    for mod_name, fn_name in sorted(set(trust._BUILDER_RE.findall(live))):
        try:
            mod = importlib.import_module(f"app.data.{mod_name}")
        except ModuleNotFoundError:
            continue
        fn = getattr(mod, fn_name, None)
        if fn is None or not inspect.isfunction(fn):
            continue
        try:
            sql = trust._render_builder(fn)
        except (KeyError, TypeError, ValueError):
            continue
        tables |= set(trust._AU_RE.findall(sql))
    assert tables == set()
    # ... and the hidden body really is unreachable: nothing dispatches it
    assert "_products(" not in _src(_SHELL_REL)
    assert body.count("_products(") == 1                 # its own def only


def test_proof_headlines_are_sql_aggregates():
    body = _src(_BODY_REL)
    proof = _fn(body, "_proof_tab")
    assert 'attr = run(mart_sql.ledger_attribution(), page=_PAGE, key="proof_attribution", tier="recent",' in proof
    assert "split = evidence_split(attr_df)" in proof
    assert "carried = carried_realization(ledger_with_attribution(ledger.df, attr_df))" in proof
    assert "evid = evidence_rows(ledger.df, attr_df)" in proof
    # the run-rate headline + narrative are the SQL figure, never the capped-frame pandas sum
    assert '"value": f"{format_usd(verified_active)}/mo"' in proof
    assert "totals['verified_active_usd']" not in proof and 'totals["verified_active_usd"]' not in proof
    # the split sentence is built from the SQL window columns only (no .sum() in this section)
    evidence = proof.split("# ---- What each saving rests on", 1)[1].split("month_df =", 1)[0]
    assert ".sum(" not in evidence
    # the page discloses a row-capped ledger instead of letting a count silently shrink
    assert "if ledger.truncated:" in proof


# --- 5. old links remap (behavioural, with a fake runtime) --------------------------------------

def _fake_st(query: dict, session: dict) -> SimpleNamespace:
    return SimpleNamespace(query_params=query, session_state=session)


def test_requested_page_maps_retired_deep_links_and_seeds_the_section(monkeypatch):
    from app.core import state

    dba = PAGES_BY_PROFILE["DBA"]
    session: dict = {}
    monkeypatch.setattr(state, "st", _fake_st({"page": "decision-studio", "section": "portfolio"}, session))
    assert state.requested_page(dba) == "Operations"
    assert session == {"ops_section": "Optimize"}
    session.clear()
    monkeypatch.setattr(state, "st", _fake_st({"page": "decision-studio", "section": "scenarios"}, session))
    assert state.requested_page(dba) == "Proof" and session == {"decision_section": "Pipeline"}
    session.clear()
    monkeypatch.setattr(state, "st", _fake_st({"page": "decision-studio"}, session))
    assert state.requested_page(dba) == "Proof" and session == {}      # bare -> Proof, nothing seeded
    # EXECUTIVE sent to the Portfolio lands on Proof (its Operations target is off-profile)
    monkeypatch.setattr(state, "st", _fake_st({"page": "decision-studio", "section": "portfolio"}, session))
    assert state.requested_page(PAGES_BY_PROFILE["EXECUTIVE"]) == "Proof" and session == {}
    # a live slug still wins and never touches the section keys; an unknown slug is still None
    monkeypatch.setattr(state, "st", _fake_st({"page": "proof", "section": "portfolio"}, session))
    assert state.requested_page(dba) == "Proof" and session == {}
    monkeypatch.setattr(state, "st", _fake_st({"page": "nope"}, session))
    assert state.requested_page(dba) is None


def test_saved_default_view_naming_a_retired_section_lands_on_its_new_home(monkeypatch):
    import app.core.session as session_mod
    from app.core import state

    monkeypatch.setattr(session_mod, "current_role", lambda: "SNOW_SYSADMINS")
    monkeypatch.setattr(session_mod, "active_profile", lambda _role="": "DBA")
    for (page, section), (want_page, key, want_section) in {
        ("Decision Studio", "SLOs"): ("Operations", "ops_section", "Pipeline SLA"),
        ("Decision Studio", "Cost Truth"): ("Cost Intelligence", "cost_section", "Spend & Attribution"),
        ("Decision Studio", "ROI"): ("Proof", "decision_section", "Proof"),
        ("Control Room", "Decision Studio"): ("Operations", "ops_section", "Optimize"),
    }.items():
        session: dict = {"_ow_nav_pending": {"page": page, "section": section, "filters": {}}}
        monkeypatch.setattr(state, "st", _fake_st({}, session))
        state.consume_pending_navigation()
        assert session["_ow_page"] == want_page, (page, section)
        assert session[key] == want_section, (page, section)


def test_request_navigation_remaps_before_the_clamp(monkeypatch):
    import app.core.session as session_mod
    from app.core import state

    monkeypatch.setattr(session_mod, "current_role", lambda: "ROLE")
    monkeypatch.setattr(session_mod, "active_profile", lambda _role="": "READER")
    session: dict = {"_ow_page": "Brief"}
    fake = _fake_st({}, session)
    fake.rerun = lambda: None
    monkeypatch.setattr(state, "st", fake)
    state.request_navigation("Decision Studio", "Portfolio")
    assert session["_ow_nav_pending"]["page"] == "Operations"          # READER has Operations
    assert session["_ow_nav_pending"]["section"] == "Optimize"
    state.request_navigation("Decision Studio", "ROI")
    assert session["_ow_nav_pending"]["page"] == "Proof"                # not clamped to Overview


# --- 6. breadcrumb dedupe --------------------------------------------------------------------------

def test_breadcrumb_skips_a_section_named_like_its_page(monkeypatch):
    from app.ui import components

    session: dict = {"decision_section": "Proof"}
    monkeypatch.setattr(components, "st", SimpleNamespace(session_state=session))
    assert components._page_breadcrumb("Proof") == "Analyze ▸ Proof"
    session["decision_section"] = "Pipeline"
    assert components._page_breadcrumb("Proof") == "Analyze ▸ Proof ▸ Pipeline"
    session.clear()                                   # fresh visit -> first section (== page) skipped
    assert components._page_breadcrumb("Proof") == "Analyze ▸ Proof"
    session["ops_section"] = "Optimize"
    assert components._page_breadcrumb("Operations") == "Analyze ▸ Operations ▸ Optimize"
    assert components._PAGE_SECTION_KEY["Proof"] == "decision_section"
    assert "Decision Studio" not in components._PAGE_SECTION_KEY


# --- 7. pure helper: when the measuring changes settle --------------------------------------------

def test_settle_schedule_counts_pending_auto_rows_and_the_next_settle_day():
    ledger = pd.DataFrame({
        "ITEM_ID": [1, 2, 3, 4, 5, 6],
        "STATE": ["ESTIMATED", "ESTIMATED", "ESTIMATED", "VERIFIED", "ESTIMATED", "ESTIMATED"],
        "SOURCE_CHANGE_ID": ["c1", "c2", "c3", "c4", None, "c6"],
        "SOURCE": ["auto", "auto", "auto", "auto", "manual", "auto"],
        "TRACKING_UNTIL": ["2026-09-30", "2026-10-05", "2026-09-20", "2026-09-01", "2026-09-29", None],
        "SUPERSEDED_BY_CHANGE_ID": [None, None, None, None, None, None],
    })
    out = settle_schedule(ledger, date(2026, 9, 27))
    # c1/c2/c3/c6 are pending auto rows; the manual ESTIMATED row and the VERIFIED one are not
    assert out["pending"] == 4
    assert out["next"] == date(2026, 10, 1)              # c1: TRACKING_UNTIL + 1 day
    assert out["overdue"] == 1                           # c3 closed before today, not settled yet
    assert out["undated"] == 1                           # c6 has no linked window
    # a superseded twin is excluded, like from every other ledger figure
    twin = ledger.assign(SUPERSEDED_BY_CHANGE_ID=["x", None, None, None, None, None])
    assert settle_schedule(twin, date(2026, 9, 27))["next"] == date(2026, 10, 6)
    empty = {"pending": 0, "next": None, "overdue": 0, "undated": 0}
    assert settle_schedule(None) == empty and settle_schedule(pd.DataFrame()) == empty
    assert settle_schedule(ledger[ledger["STATE"] == "VERIFIED"], date(2026, 9, 27)) == empty
