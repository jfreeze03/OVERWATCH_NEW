"""Teardown/migration sync guard.

Every object the migrations (and native templates) create must be handled by
snowflake/teardown.sql — dropped in Section A, or listed in the commented
Section B/C blocks. A migration that adds an object without updating the
teardown fails here, not during a 2 a.m. drop-and-restore.
"""

import re
from pathlib import Path

SNOWFLAKE_DIR = Path(__file__).resolve().parents[1] / "snowflake"

_CREATE_RE = re.compile(
    r"CREATE\s+(?:OR\s+REPLACE\s+)?(?:TRANSIENT\s+)?"
    r"(TABLE|VIEW|TASK|PROCEDURE|FUNCTION|WAREHOUSE|RESOURCE\s+MONITOR|ALERT"
    r"|SECRET|NOTIFICATION\s+INTEGRATION|SNOWFLAKE\.ML\.FORECAST)"
    r"\s+(?:IF\s+NOT\s+EXISTS\s+)?([A-Z0-9_.]+)",
    re.IGNORECASE,
)

# Created by migrations but intentionally NOT in teardown.sql (none today).
_EXEMPT: set[str] = set()


def _created_objects() -> set[str]:
    created: set[str] = set()
    sources = sorted((SNOWFLAKE_DIR / "migrations").glob("V[0-9]*.sql"))
    sources.append(SNOWFLAKE_DIR / "native_alert_templates.sql")
    sources.append(SNOWFLAKE_DIR / "webhook_delivery.sql")
    sources.append(SNOWFLAKE_DIR / "ml_forecast_option.sql")
    sources.append(SNOWFLAKE_DIR / "alert_drill.sql")
    for path in sources:
        text = path.read_text(encoding="utf-8")
        # strip comment lines so commented examples don't count as created
        live = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("--"))
        for _kind, name in _CREATE_RE.findall(live):
            name = name.upper().rstrip(";")
            if name.startswith("DBA_MAINT_DB.") or "." not in name:
                created.add(name.split("(")[0])
    return created - _EXEMPT


def test_every_created_object_is_covered_by_teardown():
    teardown = (SNOWFLAKE_DIR / "teardown.sql").read_text(encoding="utf-8").upper()
    missing = sorted(
        name for name in _created_objects()
        if name not in teardown
    )
    assert not missing, f"teardown.sql does not mention: {missing}"


def test_teardown_never_drops_the_shared_schema():
    teardown = (SNOWFLAKE_DIR / "teardown.sql").read_text(encoding="utf-8").upper()
    # The schema is shared with the old app: dropping it must be impossible
    # to do by running this file, even with every comment removed.
    assert "DROP SCHEMA" not in teardown
    assert "DROP DATABASE" not in teardown


def test_destructive_sections_are_commented_out():
    text = (SNOWFLAKE_DIR / "teardown.sql").read_text(encoding="utf-8")
    live = [line for line in text.splitlines() if not line.lstrip().startswith("--")]
    live_sql = "\n".join(live).upper()
    # Operator-data tables and shared infra must not have LIVE drop statements.
    for protected in ("SETTINGS", "SAVINGS_LEDGER", "ACTION_QUEUE", "ALERT_AUDIT",
                      "COMPANY_SCOPE", "SCHEMA_VERSION"):
        assert f"DROP TABLE IF EXISTS DBA_MAINT_DB.OVERWATCH.{protected}" not in live_sql, protected
    assert "DROP WAREHOUSE" not in live_sql
    assert "DROP ROLE" not in live_sql
    assert "DROP STREAMLIT" not in live_sql


# -- delivery objects are opt-in drops (owner decision 2026-10-02: "do not overwrite again") -----------------
# The OVERWATCH_* notification integrations (OVERWATCH_EMAIL holds ALLOWED_ / DEFAULT_RECIPIENTS, which V164's
# escalation email needs), the webhook secrets (the Teams URL lives only there) and the four NATIVE_ALERT_*
# email alerts are account-level or hand-installed, and no migration re-creates them. teardown.sql keeps them:
# the PREFLIGHT only SUSPENDs the alerts, and every DROP of them sits inside the DELIVERY GATE, a scripting
# block that returns before its first DROP unless drop_delivery_objects is set TRUE in a Snowsight copy.

_DELIVERY_DROP = re.compile(r"^\s*DROP\s+(?:ALERT|NOTIFICATION\s+INTEGRATION|SECRET)\b", re.I | re.M)
_GATE_OPEN = "EXECUTE IMMEDIATE $$"
_GATE_FLAG = "drop_delivery_objects BOOLEAN DEFAULT FALSE;"
_KEPT = ("NATIVE_ALERT_NEW_EVENTS", "NATIVE_ALERT_STALE_FACTS", "NATIVE_ALERT_SCAN_HEARTBEAT",
         "NATIVE_ALERT_DELIVERY_FAILING", "OVERWATCH_EMAIL", "OVERWATCH_WEBHOOK_TEAMS", "OVERWATCH_TEAMS_URL",
         "OVERWATCH_WEBHOOK", "OVERWATCH_WEBHOOK_PAGERDUTY", "OVERWATCH_WEBHOOK_FINOPS", "OVERWATCH_WEBHOOK_URL")


def _delivery_gate_problems(text: str) -> list[str]:
    """Why a teardown text would drop a delivery object by default ([] = it cannot)."""
    live = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("--"))
    if _GATE_FLAG not in live:
        return ["no DELIVERY GATE (drop_delivery_objects BOOLEAN DEFAULT FALSE) in teardown.sql"]
    flag = live.index(_GATE_FLAG)
    start = live.rindex(_GATE_OPEN, 0, flag)
    end = live.index("$$;", flag)
    gate, outside = live[start:end], live[:start] + live[end:]
    problems = [f"live outside the gate: {m.group(0).strip()}" for m in _DELIVERY_DROP.finditer(outside)]
    first_drop = _DELIVERY_DROP.search(gate)
    guard = re.search(r"IF \(NOT drop_delivery_objects\) THEN\s+RETURN '", gate)
    if not guard or not first_drop or guard.start() > first_drop.start():
        problems.append("the gate does not RETURN before its first DROP when drop_delivery_objects is FALSE")
    dropped = set(re.findall(r"^\s*DROP\s+(?:ALERT|NOTIFICATION\s+INTEGRATION|SECRET)\s+IF\s+EXISTS\s+"
                             r"(?:DBA_MAINT_DB\.OVERWATCH\.)?(\w+);", gate, re.I | re.M))
    problems += [f"the gate does not drop {name}" for name in _KEPT if name not in dropped]
    return problems


def test_teardown_drops_delivery_objects_only_inside_the_opt_in_gate():
    text = (SNOWFLAKE_DIR / "teardown.sql").read_text(encoding="utf-8")
    assert _delivery_gate_problems(text) == []
    # exactly one gate, and its flag is never committed flipped
    assert text.count(_GATE_FLAG) == 1
    assert not re.search(r"drop_delivery_objects\s+BOOLEAN\s+DEFAULT\s+TRUE", text, re.I)
    # the alerts are still stopped (live SUSPEND outside the gate), just not destroyed
    live = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("--"))
    for name in _KEPT[:4]:
        assert f"ALTER ALERT IF EXISTS DBA_MAINT_DB.OVERWATCH.{name}" in live, name


def test_the_delivery_gate_check_fails_on_the_old_live_drops():
    """The pre-2026-10-02 shapes: a live DROP of the email integration / Teams secret / an email alert, a gate
    that drops before it checks its flag, and a gate left out entirely."""
    gate = (f"{_GATE_OPEN}\nDECLARE\n    {_GATE_FLAG}\nBEGIN\n"
            "    IF (NOT drop_delivery_objects) THEN\n        RETURN 'kept';\n    END IF;\n"
            + "".join(f"    DROP ALERT IF EXISTS DBA_MAINT_DB.OVERWATCH.{n};\n" for n in _KEPT[:4])
            + "".join(f"    DROP NOTIFICATION INTEGRATION IF EXISTS {n};\n"
                      for n in ("OVERWATCH_EMAIL", "OVERWATCH_WEBHOOK_TEAMS", "OVERWATCH_WEBHOOK",
                                "OVERWATCH_WEBHOOK_PAGERDUTY", "OVERWATCH_WEBHOOK_FINOPS"))
            + "".join(f"    DROP SECRET IF EXISTS DBA_MAINT_DB.OVERWATCH.{n};\n"
                      for n in ("OVERWATCH_TEAMS_URL", "OVERWATCH_WEBHOOK_URL"))
            + "    RETURN 'dropped';\nEND;\n$$;\n")
    assert _delivery_gate_problems(gate) == []
    for old in ("DROP NOTIFICATION INTEGRATION IF EXISTS OVERWATCH_EMAIL;\n",
                "DROP SECRET IF EXISTS DBA_MAINT_DB.OVERWATCH.OVERWATCH_TEAMS_URL;\n",
                "DROP ALERT IF EXISTS DBA_MAINT_DB.OVERWATCH.NATIVE_ALERT_NEW_EVENTS;\n"):
        assert _delivery_gate_problems(old + gate), old              # live before the gate
        assert _delivery_gate_problems(gate + old), old              # live after the gate
        assert _delivery_gate_problems(old)                          # no gate at all (the old teardown)
    unguarded = gate.replace("    IF (NOT drop_delivery_objects) THEN\n        RETURN 'kept';\n    END IF;\n", "")
    assert any("RETURN before" in p for p in _delivery_gate_problems(unguarded))
    assert any("does not drop OVERWATCH_EMAIL" in p
               for p in _delivery_gate_problems(gate.replace("IF EXISTS OVERWATCH_EMAIL;", "IF EXISTS X;")))
