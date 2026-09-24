"""Next-Fifty #22 (v4.588.0): an action-proc CALL (ack / resolve / snooze / incident declare / action
lifecycle / experiment verify) invalidates only the cache domains its proc actually writes, instead of
bumping the GLOBAL salt and cold-starting every cached read in the session. The proc -> domain map is
re-derived here from each proc's LATEST defining migration, so it cannot silently drift."""

from __future__ import annotations

import re
from pathlib import Path

import streamlit as st

import app.core.query as q
from app.data import mart_sql, security_sql

_ROOT = Path(__file__).resolve().parents[1]
_MIG = _ROOT / "snowflake" / "migrations"
_KNOWN = {"SP_ALERT_LIFECYCLE", "SP_ALERT_SNOOZE", "SP_ALERT_CLEAR_SCOPE", "SP_INCIDENT_DECLARE",
          "SP_ACTION_LIFECYCLE", "SP_VERIFY_EXPERIMENT", "SP_CHANGE_IMPACT_SCAN", "SP_WAREHOUSE_CHANGE_SCAN"}
_APP_CALL_RE = re.compile(r"""CALL \{core_object\(['"](SP_[A-Z0-9_]+)['"]\)\}"""
                          r"""|CALL DBA_MAINT_DB\.OVERWATCH\.(SP_[A-Z0-9_]+)\s*\(""")
_DML_RE = re.compile(r"\b(?:INSERT\s+INTO|UPDATE|DELETE\s+FROM|MERGE\s+INTO)\s+"
                     r"DBA_MAINT_DB\.OVERWATCH\.([A-Z0-9_]+)", re.IGNORECASE)
_NO_APP_READ = {"OW_ACTION_INTENTS"}      # written by the alert procs, never read by the app
# display-only copyable Snowsight SQL (rendered as markdown in the alert drawer, never executed)
_DISPLAY_ONLY = {"app/logic/playbooks.py"}


def _mig_num(p: Path) -> int:
    return int(p.name[1:].split("__")[0])


def _domain_of_table(table: str) -> set[str]:
    return {d for d, toks in q._DOMAIN_TOKENS.items()
            if any(t.split(".")[-1].upper() == table.upper() for t in toks)}


def _called_procs() -> set[str]:
    found: set[str] = set()
    for p in (_ROOT / "app").rglob("*.py"):
        if p.relative_to(_ROOT).as_posix() in _DISPLAY_ONLY:
            continue
        for m in _APP_CALL_RE.finditer(p.read_text(encoding="utf-8")):
            found.add(m.group(1) or m.group(2))
    return found


def _latest_proc_body(proc: str) -> str:
    pat = re.compile(r"CREATE\s+(?:OR\s+REPLACE\s+)?PROCEDURE\s+(?:IF\s+NOT\s+EXISTS\s+)?[\w.]*\b"
                     + proc + r"\s*\(", re.IGNORECASE)
    best: tuple[int, str] | None = None
    for p in _MIG.glob("V*.sql"):
        text = p.read_text(encoding="utf-8")
        for m in pat.finditer(text):
            rest = text[m.end():]
            a = rest.index("$$")
            body = rest[a + 2: rest.index("$$", a + 2)]
            if best is None or _mig_num(p) >= best[0]:
                best = (_mig_num(p), body)
    assert best is not None, f"no CREATE PROCEDURE found for {proc}"
    return best[1]


def test_every_called_proc_is_classified():
    found = _called_procs()
    assert found >= _KNOWN, f"the CALL grep went blind: missing {_KNOWN - found}"
    unmapped = found - set(q._PROC_DOMAINS)
    assert not unmapped, f"add these CALLed procs to query._PROC_DOMAINS: {unmapped}"


def test_proc_domain_values_are_known_domains_or_global():
    for proc, doms in q._PROC_DOMAINS.items():
        assert doms == q._GLOBAL_BUMP or (doms and set(doms) <= set(q._DOMAIN_TOKENS)), proc


def test_proc_domains_match_latest_migration_dml():
    for proc, doms in q._PROC_DOMAINS.items():
        if doms == q._GLOBAL_BUMP:
            continue
        body = _latest_proc_body(proc)
        assert not re.search(r"CALL\s+DBA_MAINT_DB\.OVERWATCH\.SP_", body, re.IGNORECASE), \
            f"{proc} calls another proc — its nested writes would be invisible to this map"
        for table in {m.group(1).upper() for m in _DML_RE.finditer(body)} - _NO_APP_READ:
            owners = _domain_of_table(table)
            assert owners & set(doms), f"{proc} writes {table} (domains {owners}) but maps to {doms}"


def test_app_read_views_carry_their_base_domains():
    view_re = re.compile(r"CREATE\s+(?:OR\s+REPLACE\s+)?(?:SECURE\s+)?VIEW\s+(?:IF\s+NOT\s+EXISTS\s+)?"
                         r"DBA_MAINT_DB\.OVERWATCH\.([A-Z0-9_]+)", re.IGNORECASE)
    end_re = re.compile(r";[ \t]*(--.*)?$", re.MULTILINE)
    latest: dict[str, tuple[int, str]] = {}
    for p in _MIG.glob("V*.sql"):
        text = p.read_text(encoding="utf-8")
        for m in view_re.finditer(text):
            end = end_re.search(text, m.end())
            body = text[m.end(): end.start() if end else len(text)]
            name = m.group(1).upper()
            if name not in latest or _mig_num(p) >= latest[name][0]:
                latest[name] = (_mig_num(p), body)
    app_src = "\n".join(p.read_text(encoding="utf-8") for p in (_ROOT / "app").rglob("*.py"))
    for view, (_, body) in latest.items():
        if f"core_object('{view}')" not in app_src and f'core_object("{view}")' not in app_src:
            continue
        refs = {t.upper() for t in re.findall(r"DBA_MAINT_DB\.OVERWATCH\.([A-Z0-9_]+)", body, re.I)} - {view}
        for table in refs:
            for dom in _domain_of_table(table):
                assert any(t.split(".")[-1].upper() == view for t in q._DOMAIN_TOKENS[dom]), \
                    f"view {view} reads {table} ({dom}) — add '{view}' to _DOMAIN_TOKENS['{dom}']"


def test_action_proc_call_bumps_only_its_domain():
    st.session_state.clear()
    q._bump_refresh("CALL DBA_MAINT_DB.OVERWATCH.SP_ALERT_LIFECYCLE('e1', 'ACK', 'note USER_PREFS', '', 'U', 'k1');")
    assert "_ow_refresh_salt" not in st.session_state
    assert set(st.session_state["_ow_domain_salts"]) == {"alerts"}     # the note's literal isn't scanned
    st.session_state.clear()
    q._bump_refresh("CALL DBA_MAINT_DB.OVERWATCH.SP_VERIFY_EXPERIMENT('x1', 'U')")
    assert set(st.session_state["_ow_domain_salts"]) == {"experiments", "ledger", "queue"}
    st.session_state.clear()


def test_scan_unknown_call_and_alter_still_bump_global():
    for sql in ("CALL DBA_MAINT_DB.OVERWATCH.SP_CHANGE_IMPACT_SCAN()",
                "CALL DBA_MAINT_DB.OVERWATCH.SP_NOT_MAPPED()",
                "ALTER WAREHOUSE WH_X SUSPEND"):
        st.session_state.clear()
        q._bump_refresh(sql)
        assert "_ow_refresh_salt" in st.session_state, sql
        assert not st.session_state.get("_ow_domain_salts"), sql
    st.session_state.clear()


def test_view_reads_carry_domain_salts():
    assert set(q._domains_in(mart_sql.incident_proposals(20))) >= {"alerts", "incidents"}
    assert "queue" in q._domains_in(security_sql.security_exception_queue())
