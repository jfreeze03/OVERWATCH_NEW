"""rec#27: incident routing — failure -> error family -> remediation -> owner.

Locks the pure router (logic/incident.py), the family->remediation completeness,
and the Operations panel wiring.
"""

from pathlib import Path

import pandas as pd

from app.logic.incident import (
    INCIDENT_COLUMNS,
    REMEDIATION_BY_FAMILY,
    route_incidents,
    summarize_incidents,
)

_ROOT = Path(__file__).resolve().parents[1]
_OPS = (_ROOT / "app" / "ui" / "pages" / "operations.py").read_text(encoding="utf-8")


def _fail(db, sch, task, family, role="Root cause", t="2024-01-10 08:00"):
    return {"DATABASE_NAME": db, "SCHEMA_NAME": sch, "TASK_NAME": task,
            "ERROR_FAMILY": family, "ROLE_IN_GRAPH": role, "QUERY_START_TIME": t}


def _failures(rows):
    return pd.DataFrame(rows)


def _catalog(rows):
    return pd.DataFrame(rows, columns=["ENTITY_TYPE", "ENTITY_KEY", "OWNER_NAME",
                                       "ON_CALL", "CRITICALITY"])


# --- route_incidents -------------------------------------------------------

def test_empty_failures_returns_empty_schema():
    for frame in (None, pd.DataFrame()):
        out = route_incidents(frame, None)
        assert out.empty
        assert list(out.columns) == list(INCIDENT_COLUMNS)


def test_routes_to_task_owner_with_family_remediation():
    out = route_incidents(
        _failures([_fail("DB", "SCH", "T_LOAD", "Timeout / cancelled")]),
        _catalog([("TASK", "DB.SCH.T_LOAD", "alice", "oncall-a", "HIGH")]),
    )
    r = out.iloc[0]
    assert r["ENTITY"] == "DB.SCH.T_LOAD"
    assert r["OWNER"] == "alice" and r["ON_CALL"] == "oncall-a" and r["ROUTED_TO"] == "oncall-a"
    assert r["SEVERITY"] == "HIGH"
    assert "STATEMENT_TIMEOUT" in r["REMEDIATION"]


def test_owner_resolution_prefers_task_then_database():
    # tasks are catalogued under ENTITY_TYPE='TASK' (there is no SCHEMA type)
    fails = _failures([_fail("DB", "SCH", "T1", "Other")])
    db_only = _catalog([("DATABASE", "DB", "dbowner", "", "STANDARD")])
    assert route_incidents(fails, db_only).iloc[0]["OWNER"] == "dbowner"   # coarse fallback
    with_task = _catalog([("DATABASE", "DB", "dbowner", "", "LOW"),
                          ("TASK", "DB.SCH.T1", "taskowner", "", "CRITICAL")])
    routed = route_incidents(fails, with_task).iloc[0]
    assert routed["OWNER"] == "taskowner"          # TASK beats DATABASE
    assert routed["SEVERITY"] == "CRITICAL"        # and its criticality is used


def test_task_owner_not_shadowed_by_database_owner():
    # P1 regression: a task registered under TASK must win over the DB owner, else
    # the wrong team gets paged. This is the shape the app actually writes.
    cat = _catalog([("TASK", "DB.SCH.T_LOAD", "teamB", "teamB_oncall", "CRITICAL"),
                    ("DATABASE", "DB", "teamA", "teamA_oncall", "LOW")])
    r = route_incidents(_failures([_fail("DB", "SCH", "T_LOAD", "Timeout / cancelled")]), cat).iloc[0]
    assert r["ROUTED_TO"] == "teamB_oncall" and r["SEVERITY"] == "CRITICAL"


def test_standard_criticality_uses_app_vocabulary():
    out = route_incidents(_failures([_fail("DB", "SCH", "T", "Other")]),
                          _catalog([("TASK", "DB.SCH.T", "o", "oncall", "STANDARD")]))
    assert out.iloc[0]["SEVERITY"] == "STANDARD"    # not the non-vocabulary "MEDIUM"


def test_owner_match_is_case_insensitive():
    out = route_incidents(_failures([_fail("db", "sch", "t1", "Other")]),
                          _catalog([("TASK", "DB.SCH.T1", "o", "oncall", "STANDARD")]))
    assert out.iloc[0]["ROUTED_TO"] == "oncall"


def test_unrouted_when_no_catalog_match():
    out = route_incidents(_failures([_fail("DB", "SCH", "T", "Missing object")]), _catalog([]))
    assert out.iloc[0]["ROUTED_TO"] == "unassigned"
    assert out.iloc[0]["OWNER"] == ""


def test_root_only_excludes_cascades_but_keeps_all_when_no_roots():
    mixed = _failures([
        _fail("DB", "SCH", "ROOT", "Syntax / SQL", "Root cause"),
        _fail("DB", "SCH", "CASC", "Missing object", "Cascade"),
    ])
    assert list(route_incidents(mixed, None)["ENTITY"]) == ["DB.SCH.ROOT"]
    # if the distinction is absent (all cascade), don't drop everything
    only_cascade = _failures([_fail("DB", "SCH", "C", "Other", "Cascade")])
    assert len(route_incidents(only_cascade, None)) == 1


def test_collapses_repeated_failures_keeps_latest_and_count():
    fails = _failures([
        _fail("DB", "SCH", "T", "Timeout / cancelled", "Root cause", "2024-01-10 06:00"),
        _fail("DB", "SCH", "T", "Data quality", "Root cause", "2024-01-10 09:00"),
    ])
    out = route_incidents(fails, None)
    assert len(out) == 1
    assert out.iloc[0]["FAILURES"] == 2
    assert out.iloc[0]["ERROR_FAMILY"] == "Data quality"     # most recent wins
    assert out.iloc[0]["LAST_FAILED"].startswith("2024-01-10 09:00")


def test_cascade_severity_is_one_notch_below_owner_criticality():
    out = route_incidents(
        _failures([_fail("DB", "SCH", "T", "Other", "Cascade")]),
        _catalog([("TASK", "DB.SCH.T", "o", "", "CRITICAL")]),
        root_only=False,
    )
    assert out.iloc[0]["SEVERITY"] == "HIGH"      # CRITICAL(3) - 1 = HIGH(2)


def test_sorted_most_severe_first():
    fails = _failures([_fail("DB", "SCH", "LOW1", "Other"), _fail("DB", "SCH", "CRIT1", "Other")])
    cat = _catalog([("TASK", "DB.SCH.LOW1", "o", "", "LOW"),
                    ("TASK", "DB.SCH.CRIT1", "o", "", "CRITICAL")])
    assert list(route_incidents(fails, cat)["ENTITY"]) == ["DB.SCH.CRIT1", "DB.SCH.LOW1"]


# --- summarize_incidents ---------------------------------------------------

def test_summarize_incidents():
    fails = _failures([_fail("DB", "SCH", "A", "Other"), _fail("DB", "SCH", "B", "Other")])
    cat = _catalog([("TASK", "DB.SCH.A", "o", "oncall", "HIGH")])   # A routed HIGH, B unrouted
    stats = summarize_incidents(route_incidents(fails, cat))
    assert stats["incidents"] == 2
    assert stats["routed"] == 1 and stats["unrouted"] == 1
    assert stats["critical"] == 1


def test_summarize_empty():
    assert summarize_incidents(pd.DataFrame())["incidents"] == 0
    assert summarize_incidents(None)["routed"] == 0


# --- remediation completeness ----------------------------------------------

def test_every_error_family_has_a_remediation():
    from app.logic.insights import _ERROR_FAMILIES
    families = {fam for fam, _ in _ERROR_FAMILIES} | {"No error text", "Other"}
    missing = families - set(REMEDIATION_BY_FAMILY)
    assert not missing, f"error families with no remediation: {missing}"


# --- panel wiring ----------------------------------------------------------

def test_panel_wired_on_operations():
    assert "def _incident_routing_panel(" in _OPS
    assert "_incident_routing_panel(timeline)" in _OPS
    assert "route_incidents(" in _OPS
    assert "entity_catalog(" in _OPS
    assert "Incident routing" in _OPS
