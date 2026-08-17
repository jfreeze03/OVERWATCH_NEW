"""Performance budgets — regressions fail CI, not a user's morning (v4.8.4).

Two gates:
1. The Admin migration contract can never trail the repo again (Codex r3 #1:
   the panel reported "all applied" while V021-V025 were missing from its
   expectation dict).
2. Hot pages (Brief/Overview/Control Room) carry a pinned budget of live
   ACCOUNT_USAGE references — every one that exists today is a labeled
   FALLBACK under a fact-first read.

Honesty note (v4.51, Codex P2): this budget counts the LITERAL in page
source — a lint proxy. It cannot see scans reached through app/data builders
(unit_costs pins 0 here while its builders reach 7 ACCOUNT_USAGE tables).
The builder-level companion gate lives in tests/test_v451_trust.py
(test_reachable_account_usage_tables_per_page): it renders each page's
referenced builders and pins the true reachable table set. Keep both — this
one polices page-file hygiene, that one polices the actual scan surface.
"""

from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]

# ---------------------------------------------------------------------------
# 1. Migration contract stays in lockstep with the repo
# ---------------------------------------------------------------------------

def test_admin_migration_contract_matches_the_repo():
    from app.ui.pages.admin import _EXPECTED_MIGRATIONS

    repo_versions = {
        int(re.match(r"V(\d+)__", p.name).group(1))
        for p in (_ROOT / "snowflake" / "migrations").glob("V0*.sql")
    }
    assert set(_EXPECTED_MIGRATIONS) == repo_versions, (
        "app/ui/pages/admin.py _EXPECTED_MIGRATIONS is out of sync with "
        "snowflake/migrations/ — update the dict (and validate.sql) whenever "
        f"a migration lands. Repo: {sorted(repo_versions)}; "
        f"dict: {sorted(_EXPECTED_MIGRATIONS)}"
    )


def test_validate_matches_the_repo_tip():
    validate = (_ROOT / "snowflake" / "validate.sql").read_text(encoding="utf-8")
    tip = max(
        int(re.match(r"V(\d+)__", p.name).group(1))
        for p in (_ROOT / "snowflake" / "migrations").glob("V0*.sql")
    )
    m = re.search(r"V001\.\.V0(\d+) applied", validate)
    assert m and int(m.group(1)) == tip, (
        f"validate.sql expects V001..V0{m.group(1) if m else '?'} but the repo "
        f"tip is V0{tip} — update the first check in validate.sql."
    )


# ---------------------------------------------------------------------------
# 2. Live-scan budgets on hot pages (Codex r3 #20, the test half)
# ---------------------------------------------------------------------------

# Every occurrence below is a labeled live FALLBACK under a fact-first read.
# Raising a budget requires justifying a new live ACCOUNT_USAGE scan on a
# first-paint path — prefer a fact/mart + fallback (see control_room for the
# pattern). Lowering is always welcome.
_LIVE_SCAN_BUDGETS = {
    "app/ui/pages/brief.py": 0,
    "app/ui/pages/overview.py": 1,  # v4.36/V041: only _live_fallback_daily remains (score inputs went mart-first)
    "app/ui/pages/control_room.py": 4,    # restored v4.45 (owner correction) — pulse/movers/task live fallbacks; +1 v4.210 (CoCo CR9): hourly-credit overlay on the incident timeline (WAREHOUSE_METERING_HISTORY already reachable per test_v451_trust; lazy Timeline & movers section, non-first-paint, beside the timeline + movers live fallbacks)
    # Wave 2 pins (v4.12.0) — every count below is labeled live fallbacks
    # under mart-first reads, or panels the marts genuinely cannot serve
    # (tag coverage needs user grain; pruning needs partition stats).
    "app/ui/pages/cost_parts/optimize.py": 3,   # +1 v4.30: toggled clustering-spend scan (COST_DB recon R7; on-demand, labeled)
    "app/ui/pages/cost_parts/spend.py": 10,     # +1 v4.30: CS-by-QUERY_TYPE drill (COST_DB recon R6); +1 v4.50: the v4.46 storage-tier live fallback moved here with the storage panels (probe-gated, non-first-paint, unchanged)
    "app/ui/pages/cost_parts/ai_chargeback.py": 4,  # -1 v4.50: the storage-tier live fallback moved to spend.py with the storage panels
    # +2 (2026-07-31, P6): NOT new scans — this budget counts the literal string (see the
    # honesty note above). The t_rca tier fix added one explanatory COMMENT naming
    # ACCOUNT_USAGE.TASK_HISTORY's ~45-min lag and one `source=` label naming the same view.
    # The builder-level gate (test_v451_trust) confirms the reachable table set is UNCHANGED.
    "app/ui/pages/operations.py": 27,  # v4.188 (gap-audit #26): +1 for the robust-z row-volume DQ monitor (lazy Pipeline SLA section, beside the existing TABLE_DML_HISTORY volume-drops read); +1 v4.208 (CoCo Tier-2 O16): release auto-detect DDL scan (lazy Release compare section; QUERY_HISTORY is already reachable per test_v451_trust — non-first-paint, cached, beside the existing before/after release scans); +1 v4.224 (CoCo Tier-3 O10): pipeline-SLA forecast folds a TABLE_DML_HISTORY refresh-cadence scan into the lazy Pipeline SLA tab (already-reachable table per test_v451_trust; the tab's read moved from the reactive PIPELINE_SLA_STATUS mart to this cadence-joined reader — non-first-paint, tier=recent)
    "app/ui/pages/cost_parts/unit_costs.py": 0,
    "app/ui/pages/cost_parts/compare.py": 0,   # compare is mart-only by design (r11/Compare Phase 1)
    "app/ui/pages/security.py": 22,  # +4 v4.41/r25 (owner picked #6+#7): new-network batch rider on Access + Egress lazy section (DATA_TRANSFER_HISTORY, UNLOAD scan) — zero first-paint cost, all click-gated
}


def test_hot_pages_stay_within_their_live_scan_budgets():
    for rel, budget in _LIVE_SCAN_BUDGETS.items():
        count = (_ROOT / rel).read_text(encoding="utf-8").count("ACCOUNT_USAGE")
        assert count <= budget, (
            f"{rel} now references ACCOUNT_USAGE {count}x (budget {budget}). "
            "New live scans on hot pages regress first paint — add a fact/mart "
            "read with a live fallback instead, or justify raising the budget "
            "in this file."
        )
