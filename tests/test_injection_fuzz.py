"""Adversarial injection fuzz — the pen-test compensating control.

Invariant: any surviving fragment of a hostile input may exist ONLY inside a
properly quoted SQL string literal. We strip all literals ('...' with ''
doubling) and assert the residue contains no marker, no quote, no comment
token, and no statement separator. Builders that reject input (ValueError)
pass by definition. This file is the artifact you hand a pen tester.
"""

import re

import pytest

from app.data import (
    change_impact_sql,
    chargeback_sql,
    cost_sql,
    insights_sql,
    mart_sql,
    ops_sql,
    prefs_sql,
    security_sql,
)
from app.logic import remediation

_LITERAL_RE = re.compile(r"'(?:[^']|'')*'")

PAYLOADS = (
    "ZZINJZZ'",
    "ZZINJZZ''--",
    "ZZINJZZ' OR '1'='1",
    "ZZINJZZ%;DROP TABLE X",
    "ZZINJZZ_~pattern",
    'ZZINJZZ"double',
    "ZZINJZZ\\backslash",
    "ZZINJZZ’smart",
    "ZZINJZZ; SELECT 1 --",
    "ZZINJZZ\nnewline'",
)

# (name, callable taking the payload) — every filter-accepting builder.
TARGETS = [
    ("ops.summary.wh", lambda p: ops_sql.query_window_summary(7, "ALFA", p, "", "", "")),
    ("ops.summary.user", lambda p: ops_sql.query_window_summary(7, "ALFA", "", p, "", "")),
    ("ops.summary.db", lambda p: ops_sql.query_window_summary(7, "ALFA", "", "", p, "")),
    ("ops.summary.schema", lambda p: ops_sql.query_window_summary(7, "ALFA", "", "", "", p)),
    ("ops.top.wh", lambda p: ops_sql.top_queries_by_elapsed(7, "ALFA", 50, p, "", "", "")),
    ("ops.pruning.db", lambda p: ops_sql.poor_pruning_queries(7, "ALFA", p, "")),
    ("ops.pruning.schema", lambda p: ops_sql.poor_pruning_queries(7, "ALFA", "", p)),
    ("ops.company", lambda p: ops_sql.query_window_summary(7, p)),
    ("sec.ddl.db", lambda p: security_sql.recent_ddl_changes(7, "ALFA", p, "")),
    ("sec.ddl.schema", lambda p: security_sql.recent_ddl_changes(7, "ALFA", "", p)),
    ("sec.logins.company", lambda p: security_sql.failed_login_reasons(7, p)),
    ("sec.creds.company", lambda p: security_sql.expiring_credentials(30, p)),
    ("ins.repeat.db", lambda p: insights_sql.repeat_query_fingerprints(7, "ALFA", database=p)),
    ("ins.repeat.schema", lambda p: insights_sql.repeat_query_fingerprints(7, "ALFA", schema_contains=p)),
    ("ins.waste.company", lambda p: insights_sql.storage_waste(p)),
    ("ins.hourly.company", lambda p: insights_sql.warehouse_hourly_activity(7, p)),
    ("ins.evidence.wh", lambda p: insights_sql.anomaly_evidence("2026-07-06", p)),
    ("chg.registry.company", lambda p: change_impact_sql.change_registry(30, p, "", "")),
    ("chg.registry.db", lambda p: change_impact_sql.change_registry(30, "ALL", p, "")),
    ("chg.registry.schema", lambda p: change_impact_sql.change_registry(30, "ALL", "", p)),
    ("cost.csr.company", lambda p: cost_sql.cloud_services_ratio_by_warehouse(7, p)),
    ("cost.compile.company", lambda p: cost_sql.compile_heavy_families(7, p)),
    ("cb.window.company", lambda p: chargeback_sql.department_window_credits(7, p)),
    ("mart.factsum.wh", lambda p: mart_sql.fact_query_window_summary(7, "ALFA", p, "", "")),
    ("mart.factsum.user", lambda p: mart_sql.fact_query_window_summary(7, "ALFA", "", p, "")),
    ("prefs.value", lambda p: prefs_sql.upsert_pref_sql("VIEW:fuzz", p)),
]

# Builders whose ONLY correct response to hostile input is refusal.
REFUSERS = [
    ("chg.run_history.name", lambda p: change_impact_sql.object_run_history("PROCEDURE", p)),
    ("ins.evidence.date", lambda p: insights_sql.anomaly_evidence(p)),
    ("mart.ledger.event", lambda p: mart_sql.ledger_for_event(p)),
    ("prefs.key", lambda p: prefs_sql.upsert_pref_sql(p, "{}")),
    ("rem.warehouse", lambda p: remediation.auto_suspend_fix(p)),
    ("rem.user", lambda p: remediation.disable_user(p)),
    ("rem.cortex", lambda p: remediation.cortex_allowlist(p)),
]


def _residue(sql: str) -> str:
    return _LITERAL_RE.sub("", sql.rstrip().rstrip(";"))


@pytest.mark.parametrize("payload", PAYLOADS)
def test_no_payload_escapes_a_literal(payload):
    executed = 0
    for name, fn in TARGETS:
        try:
            sql = fn(payload)
        except (ValueError, TypeError):
            continue  # refusing hostile input is a pass
        executed += 1
        residue = _residue(sql)
        assert "ZZINJZZ" not in residue, f"{name}: marker escaped the literal"
        assert "'" not in residue, f"{name}: unbalanced quoting"
        assert ";" not in residue, f"{name}: statement separator in residue"
        assert "--" not in residue, f"{name}: comment token in residue"
        assert "DROP TABLE" not in residue.upper(), name
    assert executed >= 15  # the corpus genuinely exercises the surface


@pytest.mark.parametrize("payload", PAYLOADS)
def test_refusers_refuse(payload):
    for _name, fn in REFUSERS:
        with pytest.raises(ValueError):
            fn(payload)


def test_like_metacharacters_neutralized():
    sql = ops_sql.query_window_summary(7, "ALFA", "WH_%", "", "", "")
    assert "~%" in sql and "~_" in sql and "ESCAPE '~'" in sql
