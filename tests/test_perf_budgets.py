"""Performance budgets — regressions fail CI, not a user's morning (v4.8.4).

Two gates:
1. The Admin migration contract can never trail the repo again (Codex r3 #1:
   the panel reported "all applied" while V021-V025 were missing from its
   expectation dict).
2. Hot pages (Brief/Overview/Control Room) carry a pinned budget of live
   ACCOUNT_USAGE references — every one that exists today is a labeled
   FALLBACK under a fact-first read. Adding a new live scan to a first-paint
   path fails here with instructions, instead of shipping a slow morning.
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
    "app/ui/pages/control_room.py": 2,    # -1 v4.42/r26: task live fallback removed with task monitoring
    # Wave 2 pins (v4.12.0) — every count below is labeled live fallbacks
    # under mart-first reads, or panels the marts genuinely cannot serve
    # (tag coverage needs user grain; pruning needs partition stats).
    "app/ui/pages/cost_parts/optimize.py": 3,   # +1 v4.30: toggled clustering-spend scan (COST_DB recon R7; on-demand, labeled)
    "app/ui/pages/cost_parts/spend.py": 9,      # +1 v4.30: CS-by-QUERY_TYPE drill inside the ELEVATED branch (COST_DB recon R6)
    "app/ui/pages/cost_parts/ai_chargeback.py": 4,  # v4.36.1: users tab reverted to live-first (owner: exact emails + timestamps); 4 is the true count, old 5 was slack
    "app/ui/pages/operations.py": 18,  # -4 v4.42/r26: task + task-graph monitoring removed (owner call)
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
