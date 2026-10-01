"""Company-filter scoping (owner ask 2026-08-17: "the triage filters need to
apply"). The owner/action queue is company-scopable (ACTION_QUEUE.COMPANY) but was
account-wide; this locks the company scope + that every page reading it passes the
active company. (The audit found 0 silent-mislead bugs — the rest are honestly
declared account-wide; see the session for the full list.)"""

from __future__ import annotations

import re
from pathlib import Path

from app.data import mart_sql, workbench_sql

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
    for rel in ("app/ui/pages/overview.py", "app/ui/workbench.py", "app/ui/pages/brief.py"):
        src = (_ROOT / rel).read_text(encoding="utf-8")
        # R1-206: Action Center dropped its dead pre-V074 legacy read (a failed read rendered "V074 is
        # pending"; REQUIRED_SCHEMA_FLOOR is past V074), so its one queue read is the company-scoped
        # V074 action_center builder.
        assert ("action_queue(" in src) or ("workbench_sql.action_center(company," in src), rel
        # no bare, unscoped action_queue(<n>) read remains on these pages.
        assert not re.search(r"action_queue\(\d+\)\s*[,)]", src), f"{rel}: unscoped action_queue read"
    # v4.597 (Option C): Proof dropped the dead "Apply V074" legacy fallback (REQUIRED_SCHEMA_FLOOR is
    # past V074), so it no longer reads mart_sql.action_queue at all; its one queue read — the
    # Pipeline's queued work — is the company-scoped V074 action_center builder.
    ds = (_ROOT / "app/ui/decision_studio.py").read_text(encoding="utf-8")
    assert not re.search(r"action_queue\(\d+\)\s*[,)]", ds)
    assert "mart_sql.action_queue(" not in ds
    pipe = ds.split("def _pipeline_tab(", 1)[1].split("\ndef ", 1)[0]
    assert "workbench_sql.action_center(company, False, 500, with_totals=True)" in pipe
    # review r1: the queued headline reads the uncapped window totals, computed before the LIMIT
    sql = workbench_sql.action_center("ALL", False, 500, with_totals=True)
    assert "COUNT(*) OVER () AS OPEN_TOTAL" in sql and "AS QUEUED_MONTHLY_TOTAL" in sql
    assert sql.index("OVER ()") < sql.index("LIMIT 500")
    assert "OVER ()" not in workbench_sql.action_center("ALL", False, 500)    # Action Center unchanged
    assert "UPPER(COMPANY) IN ('TREXIS', 'ALL')" in workbench_sql.action_center("Trexis", False, 500)


def test_pipeline_load_failures_scopes_to_company():
    ops = (_ROOT / "app" / "ui" / "pages" / "operations.py").read_text(encoding="utf-8")
    assert "copy_load_failures(7, company)" in ops           # was hardcoded 'ALL'
    # database added v4.499.0: the reference-data-gap panel honors the scope-bar Database filter;
    # days added v4.520.0: the ETL workflow-runtimes reader honors the scope-bar Window;
    # schema_contains added v4.562.0: Volume drops honors the scope-bar company/database/schema
    # (was account-wide and mixed companies in one table).
    assert ("_pipeline_sla_tab(is_operator, f[\"company\"], f[\"database\"], f[\"days\"], "
            "f[\"schema_contains\"])") in ops
    assert "ops_sql.volume_deltas(company, database, schema_contains)" in ops
