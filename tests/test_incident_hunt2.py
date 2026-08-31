"""Incident-layer bug-hunt #2 locks (2026-08-30, v4.369.0).

Second adversarial incident pass (5 finders). Nine surfaced, seven confirmed (six distinct), two
refuted (the V099 auto-declare guard is company-scoped by design; the RCA 20% entity weight is not
inert). Five app-side fixes here; the sixth (incident-timeline TASK_FAIL timestamp parity) is
migration-bearing and ships in the migrations release.
  - [MED] manual declare guard was family-only, silently dropping a second distinct-entity incident
    and falsely reporting success -> guard is now entity-aware.
  - [MED] the RCA auto-investigation task-failure feed was clamped to 14d while onset windows reach 30d.
  - [LOW] a spend COLLAPSE (z<0) entered RCA as a full-magnitude candidate cause.
  - [LOW] the incident Gantt "now" rule landed in the past when no incident is open.
  - [LOW] the blast-radius observed-consumer half was silently LIMIT-capped.
"""

from __future__ import annotations

from pathlib import Path

from app.data import insights_sql
from app.logic.rca import candidates_from_anomalies

_ROOT = Path(__file__).resolve().parents[1]


def _src(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


def test_declare_guard_is_entity_aware() -> None:
    src = _src("app/ui/pages/control_room.py")
    assert "guard_entity_filter" in src
    assert 'proposal_parts[2].upper() != "ACCOUNT"' in src
    assert "AND UPPER(SPLIT_PART(COALESCE(a.DEDUPE_KEY, a.EVENT_ID), '|', 2)) = " in src


def test_spend_collapse_is_not_a_candidate_cause() -> None:
    hits = [
        {"label": "WH_A", "z": 5.0, "value": 900.0, "day": "2026-08-20"},   # spike -> cause candidate
        {"label": "WH_B", "z": -6.0, "value": 10.0, "day": "2026-08-20"},   # collapse -> NOT a cause
    ]
    out = candidates_from_anomalies(hits)
    entities = {c["entity"] for c in out}
    assert "WH_A" in entities
    assert "WH_B" not in entities, "a spend collapse (z<0) must not enter RCA as a candidate cause"
    assert all(c["evidence"]["z"] > 0 for c in out)


def test_task_failure_details_covers_the_onset_window() -> None:
    sql = insights_sql.task_failure_details(30, "ALFA")
    assert "DATEADD('day', -30, CURRENT_DATE())" in sql
    # still bounded: a larger request clamps to the 30d cap
    assert "DATEADD('day', -30, CURRENT_DATE())" in insights_sql.task_failure_details(100, "ALFA")
    assert "DATEADD('day', -60" not in insights_sql.task_failure_details(100, "ALFA")


def test_gantt_now_rule_uses_account_now() -> None:
    charts = _src("app/ui/charts.py")
    assert "def incident_gantt(df: pd.DataFrame, now: object = None)" in charts
    assert "_now_anchor = max(_end_max, pd.Timestamp(now)) if now is not None else _end_max" in charts
    cr = _src("app/ui/pages/control_room.py")
    assert "charts.incident_gantt(_ig.df, now=account_now())" in cr


def test_blast_radius_observed_half_surfaces_truncation() -> None:
    wb = _src("app/ui/workbench.py")
    assert "_cons_capped = measured_half and len(consumers_df) >= _BLAST_CONS_LIMIT" in wb
    assert '" (lower bound)" if _cons_capped else ""' in wb
