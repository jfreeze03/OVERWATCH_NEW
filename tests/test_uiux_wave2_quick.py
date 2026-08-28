"""UI/UX master list — Wave 2 batch 7: the quick cluster (C37 + C15 + C16 + C18).

Locks: C15 the header scope chip shows RESOLVED calendar boundaries for
calendar windows (the raw day-offset chip read "27d" for a month window) ·
C16 sections park decision-bearing counts in a scope-keyed stash the section
bar badges (zero extra queries, never a stale-scope number) · C18 the Cost3
"since your last visit" opener is a shared component on five surfaces with
profile-gated jumps to the changed items · C37 additive cost tables carry a
reconciliation footer (visible Σ · expected parent · variance · coverage;
no parent → sum only, never a fabricated ratio).
"""

from __future__ import annotations

from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]


def _src(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


# ---- C15: resolved calendar dates in the scope chip --------------------------

def test_scope_chip_carries_resolved_calendar_boundaries():
    comp = _src("app/ui/components.py")
    body = comp.split("def _scope_chip_html(", 1)[1].split("\ndef ", 1)[0]
    # calendar windows ("Current month (Aug 1 - Aug 28)") render their label;
    # plain rolling windows keep the compact "30d" chip
    assert 'chip(html.escape(_wl) if "(" in _wl else f"{f[\'days\']}d")' in body
    assert 'f.get("window_label")' in body


# ---- C16: scope-keyed badge stash --------------------------------------------

def test_badge_stash_is_scope_keyed_and_honest():
    comp = _src("app/ui/components.py")
    stash = comp.split("def stash_section_count(", 1)[1].split("\ndef ", 1)[0]
    read = comp.split("def stashed_counts(", 1)[1].split("\ndef ", 1)[0]
    # review fixes: each stash DECLARES the filter dims its count varies with
    # (a fixed key both went stale on un-keyed filters and over-invalidated
    # fixed-scope counts), entries expire after one recent-cache tier, and a
    # mismatch/expiry renders unbadged — never a wrong number.
    assert "_badge_scope(tuple(dims))" in stash
    assert "_badge_scope(dims) == scope" in read
    assert "_BADGE_TTL_S" in read
    # a write can drain the very queue a badge counts — the post-write bump
    # drops every stash so the next paint is unbadged, not one run behind
    q = _src("app/core/query.py")
    assert '_ow_badge_' in q.split("def _bump_refresh(", 1)[1].split("\ndef ", 1)[0]


def test_badge_dims_match_what_each_count_varies_with():
    ops = _src("app/ui/pages/operations.py")
    assert 'dims=("company", "days", "database", "schema_contains")' in ops
    assert "_streaks_known and _fresh_known" in ops    # half the evidence must not badge
    sec = _src("app/ui/security_center.py")
    assert 'dims=("company",)' in sec                  # window-independent queue
    ds = _src("app/ui/decision_studio.py")
    assert "dims=()" in ds                             # account-wide experiments


def test_three_pages_badge_from_stashed_counts():
    for rel, label in (
        ("app/ui/pages/operations.py", '"Tasks"'),
        ("app/ui/security_center.py", '"Decision queue"'),
        ("app/ui/decision_studio.py", '"Experiments"'),
    ):
        assert f"stash_section_count(_PAGE, {label}" in _src(rel), rel
    for rel in ("app/ui/pages/operations.py", "app/ui/pages/security.py",
                "app/ui/pages/decision_studio.py"):
        assert "counts=stashed_counts(_PAGE) or None" in _src(rel), rel


# ---- C18: the shared since-last-visit opener ---------------------------------

def test_opener_is_shared_and_on_five_surfaces():
    comp = _src("app/ui/components.py")
    body = comp.split("def since_last_visit_opener(", 1)[1].split("\ndef ", 1)[0]
    assert "since_last_visit_summary" in body
    assert "mart_sql.since_last_visit(company)" in body
    # jumps: profile-gated, never to the page you are on; gated on QUIET, not
    # severity — new actions alone keep severity "ok" but must still offer the
    # Action Center doorway (review fix)
    assert "PAGES_BY_PROFILE" in body
    assert 'page != "Alerts"' in body and 'page != "Control Room"' in body
    assert 'if bool(s.get("quiet")):' in body
    assert 'if sev == "ok":\n        return' not in body
    for rel in ("app/ui/pages/cost.py", "app/ui/pages/alerts.py",
                "app/ui/pages/security.py", "app/ui/pages/decision_studio.py",
                "app/ui/pages/control_room.py"):
        assert "since_last_visit_opener(_PAGE," in _src(rel), rel
    # the Cost3 inline block is fully retired — the shared component owns it
    assert "since_last_visit_summary" not in _src("app/ui/pages/cost.py")


# ---- C37: reconciliation footers ---------------------------------------------

def test_reconciliation_footer_never_fabricates_a_ratio():
    comp = _src("app/ui/components.py")
    body = comp.split("def reconciliation_footer(", 1)[1].split("\ndef ", 1)[0]
    assert "if e == e and e > 0:" in body        # NaN/zero parent -> sum only
    assert "variance" in body and "coverage" in body


def test_footers_only_claim_independent_parents():
    # review fixes: a "parent" summed from the SAME frame is a tautological
    # 100%, not a check — those sites render the additive sum alone; the one
    # site with a genuinely independent parent (the section's own billed KPI)
    # keeps the full reconciliation; the attribution table's rec33 totals
    # caption IS its sum line, so no footer duplicates it.
    cb = _src("app/ui/pages/cost_parts/ai_chargeback.py")
    assert 'reconciliation_footer(float(df["USD"].sum()), label="department rows")' in cb
    assert 'reconciliation_footer(float(enriched["SPEND_USD"].sum()), label="user rows")' in cb
    sp = _src("app/ui/pages/cost_parts/spend.py")
    assert 'reconciliation_footer(float(coverage["BILLED_USD"].sum()), billed_usd' in sp
    assert 'reconciliation_footer(window_usd' not in sp
