"""Wave 2 adoption locks: entity-drilling (#25) and the audit-mode caption sweep (#1)."""
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]


def _src(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


def test_security_identity_tables_drill_to_entity_360():
    # #25: the four user-identity tables on Security now drill to Entity 360 instead of
    # rendering as an inert styled_table (failed_logins is included — its builder does
    # GROUP BY USER_NAME, one row per user, which the scoping skeptic caught).
    src = _src("app/ui/pages/security.py")
    for key in (
        'key=f"sec_mfa_{company}"',
        'key=f"sec_single_factor_{company}_{days}"',
        'key=f"sec_faillog_{company}_{days}"',
        'key=f"sec_reawakening_{company}"',
    ):
        assert key in src, key
    # each of the four (plus the pre-existing dormant-users table) is a USER drill
    assert src.count('key_col="USER_NAME", entity_type="USER"') >= 5


def test_overview_methodology_captions_are_audit_gated():
    # #1 (post re-audit): pure how-computed / provenance captions on Overview render only
    # in audit mode via methodology_note. The re-audit reverted two over-conversions
    # (the score-defaults note — decodes the '(capped)' token + action — and the
    # score-trend "judge the trend, not the level" caveat), so exactly 3 remain.
    src = _src("app/ui/pages/overview.py")
    assert "    methodology_note,\n" in src  # imported in the components block
    assert src.count("methodology_note(") == 3
    # The account-wide SCOPE disclaimer stays a plain caption — hiding it in operator
    # mode would strand the runway bar with no cue that it's account-wide.
    assert 'st.caption("Whole-account contract commitment' in src
    # reverted on re-audit — must stay visible in operator mode
    assert "'(capped)' means the" in src and 'methodology_note("Point values' not in src
    assert 'judge the trend, not the level' in src and "methodology_note(\n                \"Live-score" not in src


def test_caption_sweep_second_increment_brief_and_security():
    # #1 (increment 2): only PURE methodology/provenance captions convert to the
    # audit-only methodology_note; conclusions, actions, scope cues, and interpretation
    # caveats stay visible in operator mode.
    brief = _src("app/ui/pages/brief.py")
    sec = _src("app/ui/pages/security.py")
    assert "    methodology_note,\n" in brief and "    methodology_note,\n" in sec
    assert brief.count("methodology_note(") >= 1
    assert sec.count("methodology_note(") >= 2
    assert 'methodology_note("Spend covers credit-billed services' in brief
    assert 'methodology_note("The hourly scan raises SEC_CRED_EXPIRY' in sec
    assert 'methodology_note("Ranked largest deduction first.")' in sec
    # KEEP guard (a ground-truth override of a workflow CONVERT): the "flags, not
    # verdicts" interpretation caveat stays visible — hiding it risks an operator
    # over-reacting to a behavioral flag (the same reason the sibling heuristic-score
    # caveat is a plain caption).
    assert 'st.caption("Flagged rows first' in sec
    assert "Heuristic score, not a verdict" in sec
    # Brief's company-scope disclaimer stays a plain caption (data-bearing + scope cue).
    assert "Scoped to {company}" in brief


def test_caption_sweep_cost_part1_four_files():
    # #1 (Cost sweep, part 1 — post re-audit): only truly pure basis/provenance captions
    # stay converted; misread caveats, wayfinding, and display legends were reverted to
    # plain captions on the re-audit (they must stay visible in operator mode).
    cost = _src("app/ui/pages/cost.py")
    spend = _src("app/ui/pages/cost_parts/spend.py")
    contract = _src("app/ui/pages/cost_parts/contract.py")
    unit = _src("app/ui/pages/cost_parts/unit_costs.py")
    for src in (spend, contract):
        assert "    methodology_note,\n" in src
    # cost.py's only conversion (the chargeback caveat) was reverted — it's a
    # misread-prevention line, so the file no longer imports or uses methodology_note.
    assert "methodology_note" not in cost
    assert 'st.caption("Chargeback precision is capped by tag coverage' in cost
    assert 'methodology_note("Per-user notebook-runtime cost on this pool' in spend
    assert contract.count("methodology_note(") >= 1 and "Org-level billed spend in currency" in contract
    # unit_costs: EVERY conversion was reverted across the re-audits — the wayfinding
    # measured-basis line, the $0-day display legend, and the "cheap-but-constant"
    # interpretation — so the file no longer imports or uses methodology_note.
    assert "methodology_note" not in unit
    assert 'st.caption(\n        "Measured price tags:' in unit   # wayfinding, reverted
    assert '"$0 days with calls' in unit                          # $0.0000 legend, reverted
    assert "cheap-but-constant often out-bills" in unit           # interpretation, reverted
    assert "Parent-before-child execution order" in unit          # tree indentation legend
    assert "model = Cortex Code" in unit                          # "n/a" column legend
    assert "not per-run metered" in unit                          # $/run misread caveat


def test_caption_sweep_cost_part2_optimize():
    # #1 (Cost sweep, part 2 — post re-audits): optimize.py keeps 4 pure basis/model/
    # provenance conversions; the re-audits reverted three misread caveats ("not a promise",
    # "Measured, not allocated", and the Live-scan "estimate, not the hour-share allocation").
    src = _src("app/ui/pages/cost_parts/optimize.py")
    assert "    methodology_note,\n" in src
    for conv in (
        "Complete days only, fixed 90-day evidence window",   # forecast gating
        "Credits billed in warehouse-hours with zero queries",  # idle definition
        "Mechanical scenario model:",                # scenario model
        "Books itself since V038",                   # self-booking provenance
    ):
        assert conv in src, conv
    # exactly 4 conversions — guards against re-converting the kept legends/caveats
    assert src.count("methodology_note(") == 4
    # reverted on re-audit (misread caveats — must stay visible)
    assert 'st.caption(\n            "Replays this window' in src   # "not a promise"
    assert 'st.caption(\n            "Measured, not allocated:' in src
    assert "Live scan of SUCCESS SELECTs" in src   # reverted: "estimate, not the allocation"
    # KEEP guards (never converted)
    assert "Actionable $ = idle minus one" in src         # column definition/legend
    assert "not a dollarized saving" in src               # eligibility misread caveat
    assert "the cost of building the object" in src       # cost-arm label legend
    assert "No ETA is intentional" in src                 # blank-column legend


def test_caption_sweep_remaining_pages():
    # #1 (remaining pages): a conservative pass converted only pure provenance/sort/
    # descriptor captions on Operations (3) and Admin (3). The heavily-operational pages
    # (Control Room, Alerts, Decision Studio bodies, Ask, Workbench) had NONE — their
    # captions are legends, actions, conclusions, scope cues, and data-bearing lines.
    ops = _src("app/ui/pages/operations.py")
    admin = _src("app/ui/pages/admin.py")
    assert "    methodology_note,\n" in ops and "    methodology_note,\n" in admin
    assert ops.count("methodology_note(") == 3
    assert admin.count("methodology_note(") == 3
    assert 'methodology_note("Elapsed-time ranking.")' in ops
    assert "methodology_note(_SCAN_NOTE)" in admin
    # KEEP guard: the per-selection detection-timing note stays a plain caption.
    assert 'st.caption("Change detected within a day of the ALTER.")' in ops
    # pages with no pure-methodology captions gained no methodology_note usage
    for rel in ("app/ui/pages/control_room.py", "app/ui/pages/alerts.py",
                "app/ui/decision_studio.py", "app/ui/pages/ask.py", "app/ui/workbench.py"):
        assert "methodology_note(" not in _src(rel), rel


def test_object_cost_table_reconciles_against_an_independent_parent():
    # #30: the top-objects cost table now discloses coverage against the object-attributed
    # total. The parent is the by-ARM aggregation minus the non-object residual arm — an
    # INDEPENDENT read from _tdf (the by-object top-N), so this is a real coverage check,
    # not a same-frame tautology (the law in test_footers_only_claim_independent_parents).
    src = _src("app/ui/pages/cost_parts/optimize.py")
    assert "    reconciliation_footer,\n" in src  # imported
    assert 'reconciliation_footer(float(_tdf["USD"].sum())' in src
    assert '_adf[_adf["COST_ARM"] != "QUERY_COMPUTE_RESIDUAL"]["USD"].sum()' in src


def test_app_user_measured_table_stays_footer_free():
    # #30 (skeptic + design law): the app×user measured table was a candidate, but its
    # only available parent (the full _adf) is the SAME frame its shown head(300) comes
    # from — a same-frame parent is a tautological ratio, so it deliberately gets NO
    # footer. spend.py keeps exactly the one billed-KPI footer (an independent parent).
    sp = _src("app/ui/pages/cost_parts/spend.py")
    assert sp.count("reconciliation_footer(") == 1
    assert 'reconciliation_footer(float(coverage["BILLED_USD"].sum()), billed_usd' in sp
