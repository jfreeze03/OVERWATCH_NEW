"""V166-V172 integration locks (v4.609): what the integrator landed in the shared, integration-only files.

The six cluster branches (loaders V166, marts V167, alerts V168 + V169, incidents V170, ops-dq V171, detection V172)
each lock their own migration and app half; Admin's _EXPECTED_MIGRATIONS, validate.sql, DEPLOYMENT.md, README.md and
CLAUDE.md are edited only at integration, so their per-migration text is locked here:
  * Admin lists V167, V168 and V169 with house-rule text (V166 / V170 / V171 / V172 are locked in their own files);
  * the Admin canary runner SKIPS an entry that reads a column a pending migration adds (canary.MIGRATION_GATED,
    correction 9): the app deploys before the owner applies V167, and a missing column is drift, never a GAP;
  * validate.sql carries the optional V167 / V170 checks (the V170 one by signature, never an overload count);
  * DEPLOYMENT.md has ONE combined V162 -> V172 apply note in the order the plan set, and every migration's run line;
  * CLAUDE.md's "Current definers" line names each proc's real latest definer (derived from the migrations).
No literal tip or APP_VERSION here (tests/test_release_lockstep.py forbids it): every version is derived.
"""

from __future__ import annotations

import re

from app.data import canary
from tests._source import changelog_entry, read
from tests.test_probe_read_honesty import _render_canary_tab
from tests.test_proc_lineage import _definers, _migrations

_GATED = tuple(sorted(canary.MIGRATION_GATED))


# ------------------------------------------------------------------------------------------- Admin entries ----

def test_v167_in_expected_migrations():
    """Integrator lockstep: Admin lists V167 with house-rule text (no $, no hand CALL, no trailing '.')."""
    from app.ui.pages.admin import _EXPECTED_MIGRATIONS
    text = str(_EXPECTED_MIGRATIONS[167])
    assert "CALL DBA_MAINT_DB.OVERWATCH.SP_" not in text and "$" not in text and not text.endswith(".")
    for frag in ("COVERAGE_FROM", "SP_LOAD_MARTS_V27", "SP_NIGHTLY_RECONCILE", "SP_LOAD_PATTERN_COST",
                 "MART_PATTERN_COST_DAILY", "No task change, no apply-time run"):
        assert frag in text, frag


def test_v168_v169_in_expected_migrations():
    from app.ui.pages.admin import _EXPECTED_MIGRATIONS
    for v, frags in ((168, ("PIPE_COPY_FAILURES", "SEC_NEW_ADMIN_NETWORK", "auto-clear")),
                     (169, ("COST_BUDGET_PACE", "CONTRACT_END_DATE", "COST_EGRESS_SPIKE"))):
        text = str(_EXPECTED_MIGRATIONS[v])
        assert "CALL DBA_MAINT_DB.OVERWATCH.SP_" not in text and "$" not in text and not text.endswith(".")
        assert all(f in text for f in frags), v


# ------------------------------------------------------------------------------------------- Admin canary ----

def test_admin_canary_skips_migration_gated_entries_until_applied():
    src = read("app/ui/pages/admin.py")
    assert "gated_out(name, _applied)" in src and '"STATUS": "SKIP"' in src
    assert canary.MIGRATION_GATED == {"mart27.fact_coverage_from": 167, "mart27.ai_fact_coverage": 167}


def test_admin_canary_skip_runs_nothing_and_never_fails(monkeypatch):
    """Before V167 the two coverage entries are SKIPPED (never read, never FAIL, out of the pass count); every other
    entry still runs. Once V167 is in the applied set they run like any other entry."""
    from app.ui.pages import admin
    tip = max(admin._EXPECTED_MIGRATIONS)
    before = set(range(1, 167))
    ran: list[str] = []
    status, fake, _ = _render_canary_tab(monkeypatch, {}, applied=before, ran=ran)
    assert {status[n] for n in _GATED} == {"SKIP"}
    assert not set(_GATED) & set(ran)                                   # a skipped entry issues no statement
    assert {s for n, s in status.items() if n not in _GATED} == {"PASS"}
    assert f"{len(_GATED)} SKIP:" in fake.text("caption")
    assert "failed" not in fake.text("error")
    frame = fake.session_state["_adm_canary_results"]
    assert set(frame.loc[frame["STATUS"] == "SKIP", "ERROR"]) == {
        "reads a column a pending migration adds (canary.MIGRATION_GATED)"}
    # the same entries FAIL as drift once V167 is applied and the column is missing (never a calm GAP)
    status, fake, _ = _render_canary_tab(monkeypatch, dict.fromkeys(_GATED, "missing_column"),
                                         applied=set(range(1, tip + 1)))
    assert {status[n] for n in _GATED} == {"FAIL"}
    assert "SKIP:" not in fake.text("caption")
    # and pass when it is there
    ran.clear()
    status, _fake, _ = _render_canary_tab(monkeypatch, {}, applied=set(range(1, tip + 1)), ran=ran)
    assert {status[n] for n in _GATED} == {"PASS"} and set(_GATED) <= set(ran)


def test_admin_canary_skip_follows_the_fresh_header_read(monkeypatch):
    """A just-applied V167 un-skips at once: the runner unions the header's fresh SCHEMA_VERSION read into the
    startup gate's (4h) set, as the Migrations tab does."""
    from app.ui.pages import admin
    monkeypatch.setattr(admin, "_fresh_applied_versions", lambda: {167})
    status, _fake, _ = _render_canary_tab(monkeypatch, {}, applied=set(range(1, 167)))
    assert {status[n] for n in _GATED} == {"PASS"}


# --------------------------------------------------------------------------------------------- validate.sql ----

def test_validate_carries_the_v167_and_v170_checks():
    v = read("snowflake/validate.sql")
    head = v[:v.index("SELECT * FROM checks")]
    assert "'SOURCE_FRESHNESS_STATE has COVERAGE_FROM (V167)'" in head
    assert "TABLE_NAME = 'SOURCE_FRESHNESS_STATE'" in head and "COLUMN_NAME = 'COVERAGE_FROM'" in head
    # V170 by SIGNATURE (= 1 P_ACTOR overload), never COUNT(*) = 2: the follow-up drop of the 4-arg keeps it green
    decl = head[head.index("'SP_INCIDENT_DECLARE with P_ACTOR present (V170)'"):]
    decl = decl[:decl.index("UNION ALL")]
    assert "CONTAINS(ARGUMENT_SIGNATURE, 'P_ACTOR')) = 1" in decl and "= 2" not in decl
    assert "'INCIDENT_PROPOSALS classifies EXH / ALL as account-level (V170)'" in head
    # the GET_DDL needle is V170's own text (absent from V072's view, present in V170's)
    needle = "'EXH', 'ALL'"
    assert "'''EXH'', ''ALL'''" in head
    assert needle in read("snowflake/migrations/V170__incident_declare_actor_and_proposals.sql")
    assert needle not in read("snowflake/migrations/V072__entity_aware_incident_proposals.sql")
    # the -20013 message says to UPDATE the V171-seeded row, never INSERT a second one
    assert "seed SETTINGS(''CREDIT_PRICE_OVERRIDE'',''TRUE'')" not in v
    assert "(an UPDATE, never a second INSERT)" in v and "seeded FALSE by V171" in v


# ------------------------------------------------------------------------------------------------ run docs ----

_NEW = ("V166__fact_loader_window_integrity.sql", "V167__mart_loader_edges_ai_coverage_pattern_reload.sql",
        "V168__alert_scan_hourly_keys_and_sweeps.sql", "V169__alert_scan_daily_windows_and_keys.sql",
        "V170__incident_declare_actor_and_proposals.sql", "V171__ops_selfwatch_digest_refgaps_seed.sql",
        "V172__detection_scans_company_and_accuracy.sql")


def test_run_docs_list_v166_to_v172_in_order():
    for rel in ("DEPLOYMENT.md", "README.md"):
        text = read(rel)
        at = [text.index(f"snowflake/migrations/{n}") for n in _NEW]
        assert at == sorted(at), rel
        assert text.index("snowflake/migrations/V165__daily_digest_grounding.sql") < at[0], rel
    readme = read("README.md")
    assert "V166__fact_loader_window_integrity.sql -- Fact loader window integrity" in readme
    assert "V167__mart_loader_edges_ai_coverage_pattern_reload.sql -- Mart-loader window edges" in readme
    for n, prev in zip(_NEW, ("V165", *(f"V{int(x[1:4])}" for x in _NEW[:-1])), strict=True):
        line = next(ln for ln in readme.splitlines() if ln.startswith(f"snowflake/migrations/{n} -- "))
        assert line.endswith(f"Owner applies after {prev}."), n


def _combined_note() -> str:
    dep = read("DEPLOYMENT.md")
    start = dep.index("> **V162-V172 (")
    return dep[start:dep.index("\n\n", start)]


def test_one_combined_apply_note_orders_the_whole_apply():
    """Plan integration c: deploy the app first; V162 -> V172 in order, stop on the first error; V164 still needs
    the escalation email; nothing CALLs at apply; the in-migration repairs are V166 / V167 / V172; then the
    ordered owner-run repairs in a Central session."""
    dep = read("DEPLOYMENT.md")
    assert dep.count("> **V162-V172 (") == 1
    assert "> **V162-V165 (Next-Fifty wave 4) — order and first runs:**" not in dep      # folded into the one note
    note = " ".join(ln.lstrip("> ").strip() for ln in _combined_note().splitlines())
    # the app release named first is the one that shipped V172's app half (its CHANGELOG entry says so); derived,
    # so a later version bump never has to touch this note or this test
    deploy = re.search(r"\*\*Deploy app (\d+\.\d+\.\d+) first\*\*", note)
    assert deploy and "V172" in changelog_entry(deploy.group(1)) and "V166" in changelog_entry(deploy.group(1))
    assert "apply V162 → V172 in order" in note and "stop on the first error" in note
    assert "ALTER SESSION SET TIMEZONE = 'America/Chicago';" in note
    assert "V164 still needs the escalation email" in note
    assert "Nothing is CALLed at apply time." in note
    for repair in ("**V166** (a MERGE", "**V167** (a scan-free DELETE", "**V172** (the change registry"):
        assert repair in note, repair
    order = [note.index(k) for k in ("**V166** (R166.1-R166.6)", "**V167** (steps 0-4)", "**V172**: R172.0",
                                     "`backfill_365.sql` opt-in heals")]
    assert order == sorted(order)
    assert "OWNER_REPAIRS_V166_V172.sql" in note and "Central session" in note
    assert "before the next 06:45 CT DAILY marts run" in note                # V167 step 1's timing
    assert "ALWAYS run the RESUME pair" in note                                # R166.2's escape hatch
    # the stale wave-4 deploy instruction is gone (the app ships later than the wave-4 note assumed)
    assert "deploy app 4.602.0 first" not in dep  # release-pin-ok: a retired instruction must not come back


def test_per_migration_notes_follow_the_combined_note():
    dep = read("DEPLOYMENT.md")
    start = dep.index("> **V162-V172 (")
    for head in ("> **V166 (fact loader window integrity):**", "> **V166 verify:**",
                 "> **V167 (mart-loader window edges, AI coverage, atomic pattern reload):**", "> **V167 verify:**",
                 "> **V168 / V169 (hourly and nightly alert keys and windows):**",
                 "> **V170 (incident declare + proposals):**", "> **V171 verify (no smoke test to apply):**",
                 "> **V172 (detection scans: company and accuracy):**"):
        assert dep.count(head) == 1 and dep.index(head) > start, head
    # the V072 note says PIPE_TASK_FAILURES' own failures no longer raise CONFIDENCE to HIGH (V170, R2-031)
    v072 = dep[dep.index("> **V072 verify"):]
    v072 = " ".join(ln.lstrip("> ") for ln in v072[:v072.index("\n\n")].splitlines())
    assert "since V170 a PIPE_TASK_FAILURES proposal's own task failures are labelled its alert source" in v072


# ------------------------------------------------------------------------------------------------ CLAUDE.md ----

def test_claude_md_current_definers_are_the_real_latest_definers():
    """Every proc / view the 'Current definers' line names maps to the migration that last CREATE OR REPLACEs
    it (derived, so the next re-derivation fails here until the line moves with it)."""
    text = read("CLAUDE.md")
    para = text[text.index("- Current definers (re-derive forward from THESE):"):]
    para = " ".join(para[:para.index("Hr/Min/Sec CASE template")].split())
    latest = {k: v[-1] for k, v in _definers(_migrations()).items()}
    named = re.findall(r"\b(SP_[A-Z0-9_]+|INCIDENT_PROPOSALS)\b", para)
    assert len(set(named)) >= 20
    for name in dict.fromkeys(named):
        claim = re.search(r"= V(\d{3})", para[para.index(name):])
        assert claim, name
        assert int(claim.group(1)) == latest[name], (name, claim.group(1), latest[name])
    assert "TWO overloads" in para and "re-derive BOTH" in para
    assert "the next free is still [30]" in para
    assert "(SP_NIGHTLY_RECONCILE is still the V064 shape)" not in text


# ------------------------------------------------------------------------ cross-cluster SQL twins (V171 x V172) ----

_HD_CASE = re.compile(r"CASE WHEN \((?P<s>[^\n]*?)\) IS NULL THEN '(?P<nul>[^']*)'\n(?P<body>.*?)\n\s*END\b", re.S)


def _hd_blocks(body: str) -> list[tuple[str, str]]:
    """Each Hr/Min/Sec CASE block as (its NULL text, its body with the operand replaced by S, whitespace folded)."""
    return [(m.group("nul"), " ".join(m.group("body").replace(f"({m.group('s')})", "(S)").split()))
            for m in _HD_CASE.finditer(body)]


def test_v171_title_and_v172_verdict_detail_share_one_hr_min_sec_template():
    """ops-dq's OPS_SLOW_RENDER title (V171) and detection's VERDICT_DETAIL (V172) render durations with the same SQL
    twin of formulas.humanize_duration; a fix to one that skips the other would show two duration styles."""
    from tests.test_alert_rule_consistency import _latest_proc_bodies
    bodies = _latest_proc_bodies()
    blocks = {name: _hd_blocks(bodies[name])
              for name in ("SP_CANARY_SENTINEL", "SP_CHANGE_IMPACT_SCAN", "SP_WAREHOUSE_CHANGE_SCAN")}
    assert [len(b) for b in blocks.values()] == [1, 2, 4], {k: len(v) for k, v in blocks.items()}
    templates = {tpl for b in blocks.values() for _nul, tpl in b}
    assert len(templates) == 1, templates
    assert "'HALF_TO_EVEN'" in templates.pop()
    assert blocks["SP_CANARY_SENTINEL"][0][0] == "?"


def test_digest_and_sweep_read_cortex_model_with_one_literal():
    """CORTEX-NULLIF: the digest (V171) and the anomaly sweep (V172) normalize CORTEX_MODEL with the same literal,
    each locked against app.core.ai in its own cluster test; this keeps the two server reads identical."""
    from tests.test_alert_rule_consistency import _latest_proc_bodies
    bodies = _latest_proc_bodies()
    read_ = "IFF(RLIKE(cm, '[a-z0-9][a-z0-9.-]{1,60}'), cm, 'llama3.1-8b')"
    src = "FROM (SELECT LOWER(TRIM(MAX(IFF(KEY = 'CORTEX_MODEL', VALUE, NULL)))) AS cm"
    for name in ("SP_DAILY_DIGEST", "SP_ANOMALY_SWEEP"):
        assert bodies[name].count(read_) == 1 and bodies[name].count(src) == 1, name
