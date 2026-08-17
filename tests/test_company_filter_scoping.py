"""Company-filter scoping (owner ask 2026-08-17: "the triage filters need to
apply"). The owner/action queue is company-scopable (ACTION_QUEUE.COMPANY) but was
account-wide; this locks the company scope + that every page reading it passes the
active company. (The audit found 0 silent-mislead bugs — the rest are honestly
declared account-wide; see the session for the full list.)"""

from __future__ import annotations

import re
from pathlib import Path

from app.data import mart_sql

_ROOT = Path(__file__).resolve().parents[1]


def test_action_queue_scopes_to_company_plus_account_level():
    alfa = mart_sql.action_queue(200, "ALFA")
    # company's own actions PLUS account-level ('ALL') actions that apply to everyone.
    assert "UPPER(COALESCE(COMPANY, 'ALL')) IN ('ALL', 'ALFA')" in alfa
    trxs = mart_sql.action_queue(200, "Trexis")
    assert "IN ('ALL', 'TREXIS')" in trxs
    # 'ALL' (and the old no-arg form) stay account-wide — backward compatible.
    account_wide = mart_sql.action_queue(200, "ALL")
    assert "COALESCE(COMPANY, 'ALL')) IN" not in account_wide
    assert mart_sql.action_queue(200) == account_wide


def test_every_owner_queue_reader_passes_company():
    for rel in ("app/ui/pages/overview.py", "app/ui/workbench.py",
                "app/ui/decision_studio.py", "app/ui/pages/brief.py"):
        src = (_ROOT / rel).read_text(encoding="utf-8")
        assert "action_queue(" in src
        # no bare, unscoped action_queue(<n>) read remains on these pages.
        assert not re.search(r"action_queue\(\d+\)\s*[,)]", src), f"{rel}: unscoped action_queue read"


def test_pipeline_load_failures_scopes_to_company():
    ops = (_ROOT / "app" / "ui" / "pages" / "operations.py").read_text(encoding="utf-8")
    assert "copy_load_failures(7, company)" in ops           # was hardcoded 'ALL'
    assert "_pipeline_sla_tab(is_operator, f[\"company\"])" in ops
