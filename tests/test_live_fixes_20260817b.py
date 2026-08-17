"""Owner live-screenshot fixes 2026-08-17 (batch B): grant-feed self-ownership
noise, data-transfer humanization + total, DS-portfolio self-exclusion."""

from __future__ import annotations

from pathlib import Path

from app.data import security_sql, workbench_sql
from app.logic.formulas import humanize_bytes

_ROOT = Path(__file__).resolve().parents[1]


def _src(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


def test_grant_feed_excludes_ownership_self_grants():
    # Owner: every proc CREATE records an OWNERSHIP self-grant on its TMP_* objects —
    # object-lifecycle noise that buried the feed. Excluded on BOTH role arms; a
    # real ownership TRANSFER (different grantor) still shows.
    sql = security_sql.recent_grant_changes(30, "ALL")
    assert sql.count("NOT (PRIVILEGE = 'OWNERSHIP'") == 2
    assert "COALESCE(GRANTED_BY, '') = GRANTEE_NAME" in sql
    # only the two GRANTS_TO_ROLES arms carry it (OWNERSHIP is a privilege->role grant,
    # never a role->user grant), and the user arms are untouched.
    assert sql.count("GRANTS_TO_USERS") == 2 and sql.count("GRANTS_TO_ROLES") == 2


def test_humanize_bytes_scales_below_tb():
    assert humanize_bytes(0) == "0 B"
    assert humanize_bytes(512) == "512 B"
    assert humanize_bytes(891.3 * 1024 ** 2) == "891.3 MB"     # the exact screenshot value
    assert humanize_bytes(3.6 * 1024 ** 4).endswith("TB")
    assert humanize_bytes(1024 ** 3) == "1.0 GB"
    assert humanize_bytes(float("nan")) == "—"
    assert humanize_bytes(250 * 1024 ** 2) == "250.0 MB"       # always 1 decimal (Snowsight style)


def test_egress_panel_shows_total_and_humanizes():
    spend = _src("app/ui/pages/cost_parts/spend.py")
    assert "Total transferred" in spend and "humanize_bytes(total_bytes)" in spend
    assert "humanize_bytes(billable_bytes)" in spend
    # the table shows a humanized Volume, not the 3-decimal TB that read 0.000.
    assert '"VOLUME": st.column_config.TextColumn("Volume")' in spend
    assert "reconciles to Snowsight" in spend


def test_portfolio_excludes_overwatch_own_runtime():
    sql = workbench_sql.workload_portfolio(30, "ALL")
    assert "NOT LIKE 'EXECUTE STREAMLIT%'" in sql
    assert "NOT LIKE '%OVERWATCH_APP%'" in sql
    # COALESCE guard so a LEFT-JOIN miss (NULL preview) stays IN the portfolio.
    assert sql.count("UPPER(COALESCE(f.QUERY_PREVIEW, ''))") == 2
