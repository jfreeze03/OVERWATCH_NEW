"""Upgrade Board P1 #19 — object lineage + downstream blast radius.

Locks the two builders (edge list + observed consumers), the pure BFS/merge logic
(transitive dependents, cycle-safety, NOT-MEASURED != 0), FQN injection-safety, and
the Entity-360 wiring gated to OBJECT.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import sqlglot

from app.data import graph_sql
from app.logic import lineage

_ROOT = Path(__file__).resolve().parents[1]


# ---- builders ----------------------------------------------------------------

def test_object_dependency_edges_contract():
    sql = graph_sql.object_dependency_edges()
    assert "SNOWFLAKE.ACCOUNT_USAGE.OBJECT_DEPENDENCIES" in sql
    assert "REFERENCED_OBJECT_ID" in sql and "REFERENCING_OBJECT_ID" in sql
    assert "REFERENCED_FQN" in sql and "REFERENCING_FQN" in sql
    assert "REFERENCING_OBJECT_DOMAIN" in sql
    assert "ORDER BY REFERENCED_FQN, REFERENCING_FQN" in sql   # deterministic truncation
    # limit is int-clamped, never interpolated raw
    assert "LIMIT 50000" in graph_sql.object_dependency_edges(999999)
    assert "LIMIT 1" in graph_sql.object_dependency_edges(0)


def test_object_blast_consumers_contract_and_injection():
    sql = graph_sql.object_blast_consumers(("DB.SCH.T", "DB.SCH.V"), days=30)
    # observed half must FLATTEN reads (BASE + DIRECT, so named-view reads match) + writes
    assert "BASE_OBJECTS_ACCESSED" in sql and "OBJECTS_MODIFIED" in sql
    assert "DIRECT_OBJECTS_ACCESSED" in sql   # views read by name land here, not BASE
    # objectName is UPPER-folded on the match side so quoted mixed-case dependents join
    assert 'UPPER(f.value:"objectName"::STRING) IN (' in sql
    assert "COUNT(DISTINCT QUERY_ID)" in sql and "COUNT(DISTINCT USER_NAME)" in sql
    assert "DATEADD('day', -30," in sql
    # FQNs are literal-guarded: an embedded quote is doubled (escaped) so it cannot
    # terminate the string, and the builder stays a SINGLE statement — injection is
    # contained INSIDE the string literal, never executed as a second statement.
    hostile = graph_sql.object_blast_consumers(("DB.SCH.T'); DROP TABLE X;--",))
    assert "''" in hostile                                    # the quote was escaped
    assert len(sqlglot.parse(hostile, read="snowflake")) == 1  # no injected 2nd statement
    # empty FQN list yields a valid, match-nothing query (never a syntax error)
    assert sqlglot.parse_one(graph_sql.object_blast_consumers(()), read="snowflake") is not None


def test_lineage_builders_parse_snowflake():
    for sql in (graph_sql.object_dependency_edges(),
                graph_sql.object_blast_consumers(("A.B.C", "A.B.D"))):
        assert sqlglot.parse_one(sql, read="snowflake") is not None


# ---- pure logic --------------------------------------------------------------

def _edges():
    # A -> B -> C, plus a cycle edge B -> A (back to the root) to prove cycle-safety.
    return pd.DataFrame({
        "REFERENCED_FQN": ["DB.SCH.A", "DB.SCH.B", "DB.SCH.B"],
        "REFERENCING_FQN": ["DB.SCH.B", "DB.SCH.C", "DB.SCH.A"],
        "REFERENCING_DOMAIN": ["VIEW", "VIEW", "TABLE"],
    })


def test_downstream_dependents_transitive_and_cycle_safe():
    out = lineage.downstream_dependents(_edges(), "DB.SCH.A")
    assert set(out["FQN"]) == {"DB.SCH.B", "DB.SCH.C"}   # root A excluded, C reached transitively
    depths = dict(zip(out["FQN"], out["DEPTH"], strict=False))
    assert depths["DB.SCH.B"] == 1 and depths["DB.SCH.C"] == 2
    # case-insensitive root match
    assert set(lineage.downstream_dependents(_edges(), "db.sch.a")["FQN"]) == {"DB.SCH.B", "DB.SCH.C"}


def test_downstream_dependents_empty_safe():
    assert lineage.downstream_dependents(pd.DataFrame(), "DB.SCH.A").empty
    assert lineage.downstream_dependents(None, "DB.SCH.A").empty
    assert lineage.downstream_dependents(_edges(), "").empty
    assert lineage.downstream_dependents(_edges(), "DB.SCH.NOPE").empty


def test_build_blast_radius_marks_unqueried_as_not_measured():
    consumers = pd.DataFrame({
        "FQN": ["DB.SCH.B"], "QUERIES": [5], "USERS": [2],
        "LAST_TOUCH": ["2026-08-24 10:00:00"],
    })
    br = lineage.build_blast_radius(_edges(), consumers, "DB.SCH.A", window_days=30)
    row_b = br[br["FQN"] == "DB.SCH.B"].iloc[0]
    row_c = br[br["FQN"] == "DB.SCH.C"].iloc[0]
    assert bool(row_b["MEASURED"]) is True and row_b["QUERIES"] == 5
    # C was never in ACCESS_HISTORY -> NOT-MEASURED, NA (never a measured 0)
    assert bool(row_c["MEASURED"]) is False and pd.isna(row_c["QUERIES"])
    # attention-first: the measured dependent sorts above the unmeasured one
    assert br.iloc[0]["FQN"] == "DB.SCH.B"


def test_build_blast_radius_no_consumers_all_unmeasured():
    br = lineage.build_blast_radius(_edges(), pd.DataFrame(), "DB.SCH.A", window_days=30)
    assert not br.empty and (~br["MEASURED"]).all()
    assert br["QUERIES"].isna().all()


def test_build_blast_radius_partial_consumers_frame_does_not_raise():
    # "never raises" contract: a consumers frame missing the QUERIES column (or USERS)
    # must degrade to NA, not raise (bare .get() -> scalar NaN -> zip() TypeError).
    partial = pd.DataFrame({"FQN": ["DB.SCH.B"], "USERS": [2]})   # no QUERIES column
    br = lineage.build_blast_radius(_edges(), partial, "DB.SCH.A", window_days=30)
    assert not br.empty
    row_b = br[br["FQN"] == "DB.SCH.B"].iloc[0]
    assert pd.isna(row_b["QUERIES"]) and bool(row_b["MEASURED"]) is False


def test_build_blast_radius_joins_consumers_case_insensitively():
    # a consumer row whose FQN is lower/mixed-case still joins to the uppercased dependent
    consumers = pd.DataFrame({"FQN": ["db.sch.b"], "QUERIES": [9], "USERS": [4]})
    br = lineage.build_blast_radius(_edges(), consumers, "DB.SCH.A", window_days=30)
    row_b = br[br["FQN"] == "DB.SCH.B"].iloc[0]
    assert bool(row_b["MEASURED"]) is True and row_b["QUERIES"] == 9


def test_blast_summary_counts_are_recorded_facts():
    consumers2 = pd.DataFrame({"FQN": ["DB.SCH.B"], "QUERIES": [7], "USERS": [3]})
    s = lineage.blast_summary(_edges(), consumers2, "DB.SCH.A", window_days=30)
    assert s["dependents"] == 2 and s["measured"] == 1 and s["unmeasured"] == 1
    assert s["observed_queries"] == 7 and s["deepest_level"] == 2
    empty = lineage.blast_summary(pd.DataFrame(), pd.DataFrame(), "DB.SCH.A", window_days=30)
    assert empty["dependents"] == 0 and empty["deepest_level"] == 0


# ---- wiring ------------------------------------------------------------------

def test_blast_radius_wired_into_entity_360():
    wb = (_ROOT / "app" / "ui" / "workbench.py").read_text(encoding="utf-8")
    assert "def _object_blast_radius_panel(" in wb
    assert "graph_sql.object_dependency_edges(" in wb
    assert "graph_sql.object_blast_consumers(" in wb
    assert "lineage.build_blast_radius(" in wb
    # gated to OBJECT entities, inside the evidence-gate block
    gated = wb.split("if kind == \"OBJECT\":", 1)
    assert len(gated) == 2 and "_object_blast_radius_panel(key)" in gated[1][:200]
    # honest framing — never a 'safe to change' verdict
    assert "safe to ALTER" in wb
    gsrc = (_ROOT / "app" / "data" / "graph_sql.py").read_text(encoding="utf-8")
    assert "def object_dependency_edges(" in gsrc and "def object_blast_consumers(" in gsrc
