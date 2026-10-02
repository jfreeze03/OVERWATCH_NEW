"""#26: the OVERWATCH task set is DERIVED from the migrations, for both task_audit.sql and the app.

task_audit.sql drifted twice (it still expected TASK_SNAPSHOT_FRESHNESS, dropped by V041, and never
learned five later tasks), and Admin ▸ Task health grades against ``ops_sql.OVERWATCH_TASKS``. Both
are hand-maintained, so this file replays every task DDL statement in the migrations — in file order,
then by offset within a file — and fails the moment either list disagrees with what the migrations
leave live. A new task-creating migration therefore fails here with a message naming both edits.

Replay rules (the ones that bit by hand):
* comments and string literals are blanked first, PRESERVING LENGTH, so every regex runs on SQL
  structure only and values are sliced from the original text at the same offsets — COMMENT = '…'
  text on TASK_LOAD_HOURLY / TASK_DAILY_DIGEST contains "after", and heads can contain "as";
* ``CREATE TASK IF NOT EXISTS`` is a no-op when the task is already live (TASK_LOAD_MARTS_V27_HOURLY
  is created by V027 IF NOT EXISTS, then re-created by V041 OR REPLACE);
* ``ALTER TASK … SET SCHEDULE`` / ``ADD AFTER`` / ``REMOVE AFTER`` (V071's re-chain, V114's cron move)
  apply in order; ``DROP TASK`` removes. Conditional scripting blocks replay unconditionally — the
  migrations' guards only make a re-run a no-op, the end state is the same.
"""

from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_MIGRATIONS = sorted((_ROOT / "snowflake" / "migrations").glob("V[0-9]*__*.sql"))
_AUDIT = _ROOT / "snowflake" / "task_audit.sql"

_NAME = r"((?:[A-Za-z_][\w$]*\.){0,2}[A-Za-z_][\w$]*)"
_NAME_LIST = r"((?:[A-Za-z_][\w$.]*\s*,\s*)*[A-Za-z_][\w$.]*)"
_CREATE = re.compile(rf"\bCREATE\s+(OR\s+REPLACE\s+)?TASK\s+(IF\s+NOT\s+EXISTS\s+)?{_NAME}", re.I)
_DROP = re.compile(rf"\bDROP\s+TASK\s+(?:IF\s+EXISTS\s+)?{_NAME}", re.I)
_ALTER = re.compile(rf"\bALTER\s+TASK\s+(?:IF\s+EXISTS\s+)?{_NAME}", re.I)
_AS = re.compile(r"\bAS\b", re.I)
_STMT_END = re.compile(r";")
_WAREHOUSE = re.compile(r"\bWAREHOUSE\s*=\s*([A-Za-z_][\w$]*)", re.I)
_SCHEDULE = re.compile(r"\bSCHEDULE\s*=\s*'", re.I)
_AFTER = re.compile(rf"\bAFTER\s+{_NAME_LIST}", re.I)
_ADD_AFTER = re.compile(rf"^\s*ADD\s+AFTER\s+{_NAME_LIST}", re.I)
_REMOVE_AFTER = re.compile(rf"^\s*REMOVE\s+AFTER\s+{_NAME_LIST}", re.I)
_SET = re.compile(r"^\s*SET\b", re.I)


def blank(text: str) -> str:
    """Blank comments (``--``, ``//``, ``/* */``) to spaces and string-literal CONTENTS to spaces
    (quotes kept), preserving every offset and newline. ``''`` inside a string is an escaped quote.
    A comment's apostrophe can never open a fake string, and a string's ``--`` is never a comment."""
    out = list(text)
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        two = text[i:i + 2]
        if two in ("--", "//"):
            j = text.find("\n", i)
            j = n if j < 0 else j
            out[i:j] = " " * (j - i)
            i = j
        elif two == "/*":
            j = text.find("*/", i + 2)
            j = n if j < 0 else j + 2
            out[i:j] = [c if c == "\n" else " " for c in text[i:j]]
            i = j
        elif ch == "'":
            j = i + 1
            while j < n:
                if text[j] == "'":
                    if j + 1 < n and text[j + 1] == "'":
                        j += 2
                        continue
                    break
                j += 1
            out[i + 1:j] = [c if c == "\n" else " " for c in text[i + 1:j]]
            i = j + 1
        else:
            i += 1
    return "".join(out)


def bare(name: str) -> str:
    return name.strip().strip('"').split(".")[-1].strip('"').upper()


def _names(group: str) -> list[str]:
    return [bare(p) for p in group.split(",") if p.strip()]


def replay(texts: list[tuple[str, str]] | None = None) -> dict[str, dict]:
    """Replay task DDL; ``texts`` is [(label, sql)] in apply order (default: the migrations).
    Returns {TASK: {"warehouse", "schedule", "after": [..], "created_in"}} for tasks left live."""
    if texts is None:
        texts = [(p.name, p.read_text(encoding="utf-8")) for p in _MIGRATIONS]
    live: dict[str, dict] = {}
    for label, text in texts:
        b = blank(text)
        events = [(m.start(), kind, m)
                  for kind, rx in (("create", _CREATE), ("drop", _DROP), ("alter", _ALTER))
                  for m in rx.finditer(b)]
        for _pos, kind, m in sorted(events, key=lambda e: e[0]):
            if kind == "create":
                name = bare(m.group(3))
                if m.group(2) and name in live:
                    continue                                   # IF NOT EXISTS on a live task: no-op
                as_m = _AS.search(b, m.end())
                head_end = as_m.start() if as_m else len(b)
                head = b[m.end():head_end]
                wh = _WAREHOUSE.search(head)
                sch = _SCHEDULE.search(head)
                aft = _AFTER.search(head)
                schedule = None
                if sch:
                    start = m.end() + sch.end()                # first char inside the quote
                    stop = b.index("'", start)                 # blanked contents -> next quote closes it
                    schedule = text[start:stop]
                live[name] = {"warehouse": wh.group(1).upper() if wh else None,
                              "schedule": schedule,
                              "after": _names(aft.group(1)) if aft else [],
                              "created_in": label}
            elif kind == "drop":
                live.pop(bare(m.group(1)), None)
            else:
                name = bare(m.group(1))
                end_m = _STMT_END.search(b, m.end())
                tail_b = b[m.end():end_m.start() if end_m else len(b)]
                if name not in live:
                    continue
                task = live[name]
                if (add := _ADD_AFTER.match(tail_b)):
                    task["after"] = [*task["after"], *[p for p in _names(add.group(1))
                                                       if p not in task["after"]]]
                elif (rem := _REMOVE_AFTER.match(tail_b)):
                    gone = set(_names(rem.group(1)))
                    task["after"] = [p for p in task["after"] if p not in gone]
                elif _SET.match(tail_b):
                    sch = _SCHEDULE.search(tail_b)
                    if sch:
                        start = m.end() + sch.end()
                        stop = b.index("'", start)
                        task["schedule"] = text[start:stop]
                    wh = _WAREHOUSE.search(tail_b)
                    if wh:
                        task["warehouse"] = wh.group(1).upper()
    return live


_VALUES_ROW = re.compile(r"^\s*\('(\w+)'", re.M)
_ROW_BODY = re.compile(r"^\s*\((.*)\)\s*,?\s*(?:--.*)?$")
_SQL_VALUE = re.compile(r"'((?:[^']|'')*)'|(NULL)", re.I)


def audit_rows() -> dict[str, tuple]:
    """task_audit.sql's expected rows: {NAME: (STATE, WAREHOUSE, SCHEDULE, PREDECESSOR)} (None = NULL)."""
    text = _AUDIT.read_text(encoding="utf-8")
    block = text.split("SELECT * FROM VALUES", 1)[1].split("AS t(", 1)[0]
    rows: dict[str, tuple] = {}
    names = []
    for line in block.splitlines():
        m = _VALUES_ROW.match(line)
        if not m:
            continue
        body = _ROW_BODY.match(line)
        assert body, f"unparseable task_audit.sql row: {line!r}"
        vals = [None if v[1] else v[0].replace("''", "'") for v in _SQL_VALUE.findall(body.group(1))]
        assert len(vals) == 5, f"task_audit.sql row needs 5 values: {line!r}"
        names.append(m.group(1))
        rows[m.group(1)] = tuple(vals[1:5])
    assert len(names) == len(set(names)), f"duplicate task_audit.sql rows: {sorted(n for n in names if names.count(n) > 1)}"
    return rows


def _diff_message(where: str, got: set, want: set) -> str:
    return (f"{where} disagrees with the migrations — add {sorted(want - got)}, "
            f"remove {sorted(got - want)}")


def test_task_audit_lists_exactly_the_migration_task_set():
    want = set(replay())
    got = set(audit_rows())
    assert got == want, _diff_message("snowflake/task_audit.sql expected VALUES", got, want)


def test_task_audit_pins_match_current_definitions():
    live = replay()
    bad = []
    for name, (_state, wh, sched, pred) in audit_rows().items():
        task = live.get(name)
        if task is None:
            continue                                      # reported by the set test above
        if wh is not None and wh.upper() != (task["warehouse"] or ""):
            bad.append(f"{name}: warehouse {wh} != {task['warehouse']}")
        if sched is not None and sched != task["schedule"]:
            bad.append(f"{name}: schedule {sched!r} != {task['schedule']!r}")
        if pred is not None and task["after"] != [pred.upper()]:
            bad.append(f"{name}: predecessor {pred} != {task['after']} (must be exactly one parent)")
    assert not bad, bad


def test_app_task_set_matches_migrations():
    from app.data import ops_sql
    want = set(replay())
    got = set(ops_sql.OVERWATCH_TASKS)
    assert got == want, (_diff_message("app/data/ops_sql.py OVERWATCH_TASKS", got, want)
                         + " — update BOTH app/data/ops_sql.py OVERWATCH_TASKS and "
                           "snowflake/task_audit.sql's expected VALUES")


def test_opt_in_tasks_are_known_and_not_expected():
    from app.data import ops_sql
    live = set(replay())
    for name, script in ops_sql.OPT_IN_OVERWATCH_TASKS.items():
        assert name not in live and name not in ops_sql.OVERWATCH_TASKS, name
        src = (_ROOT / script).read_text(encoding="utf-8")
        assert re.search(rf"\bCREATE\s+(?:OR\s+REPLACE\s+)?TASK\s+(?:IF\s+NOT\s+EXISTS\s+)?\S*\b{name}\b",
                         blank(src), re.I), f"{script} does not create {name}"
        assert name not in audit_rows(), f"opt-in {name} must not be an expected task_audit row"
    for name in ops_sql.SUSPENDED_OK_OVERWATCH_TASKS:
        assert name in ops_sql.OVERWATCH_TASKS, name


def test_retired_tasks_were_dropped():
    from app.data import ops_sql
    live = replay()
    every_created = set()
    for p in _MIGRATIONS:
        every_created |= {bare(m.group(3)) for m in _CREATE.finditer(blank(p.read_text(encoding="utf-8")))}
    for name in ops_sql._RETIRED_OVERWATCH_TASKS:
        assert name in every_created and name not in live and name not in ops_sql.OVERWATCH_TASKS, name


def test_replay_is_not_vacuous():
    live = replay()
    assert len(live) >= 30, sorted(live)
    assert "TASK_LOAD_HOURLY" in live and live["TASK_LOAD_HOURLY"]["schedule"].startswith("USING CRON")
    assert "TASK_SNAPSHOT_FRESHNESS" not in live          # V041 DROP
    assert "TASK_BACKUP_OPERATOR" not in live             # V161 DROP
    # V071 re-chain + V114 schedule move actually replayed
    assert live["TASK_REFRESH_EXEC_BOARD"]["after"] == ["TASK_QH_EXTRACT"]
    assert live["TASK_LOAD_MARTS_V27_DAILY"]["after"] == ["TASK_NIGHTLY_RECONCILE"]
    assert live["TASK_ANOMALY_SWEEP"]["schedule"] == "USING CRON 0 7 * * * America/Chicago"
    # V041 OR REPLACE re-created the V027 IF NOT EXISTS hourly marts task
    assert live["TASK_LOAD_MARTS_V27_HOURLY"]["created_in"].startswith("V041")


def test_replay_ignores_comments_and_strings_and_honours_if_not_exists():
    """Negative controls for the parser: 'after' in a COMMENT and 'as' in a string are not structure,
    an apostrophe in a -- comment opens no string, and IF NOT EXISTS on a live task changes nothing."""
    v1 = ("-- the task's first cut\n"
          "CREATE TASK IF NOT EXISTS DB.S.T_ROOT WAREHOUSE = WH_A\n"
          "    SCHEDULE = 'USING CRON 7 * * * * America/Chicago'\n"
          "    COMMENT = 'children chain after this; runs as root'\nAS CALL P();\n"
          "CREATE TASK IF NOT EXISTS DB.S.T_CHILD WAREHOUSE = WH_A\n"
          "    COMMENT = 'it''s after -- not a comment'\n"
          "    AFTER DB.S.T_ROOT\nAS CALL Q();\n")
    v2 = ("CREATE TASK IF NOT EXISTS DB.S.T_CHILD WAREHOUSE = WH_B AFTER DB.S.T_OTHER AS CALL R();\n"
          "ALTER TASK IF EXISTS DB.S.T_ROOT SET SCHEDULE = 'USING CRON 0 7 * * * America/Chicago';\n"
          "CREATE TASK IF NOT EXISTS DB.S.T_GONE WAREHOUSE = WH_A AS CALL S();\n"
          "DROP TASK IF EXISTS DB.S.T_GONE;\n")
    live = replay([("V001__a.sql", v1), ("V002__b.sql", v2)])
    assert set(live) == {"T_ROOT", "T_CHILD"}
    assert live["T_ROOT"] == {"warehouse": "WH_A", "schedule": "USING CRON 0 7 * * * America/Chicago",
                              "after": [], "created_in": "V001__a.sql"}
    assert live["T_CHILD"]["after"] == ["T_ROOT"] and live["T_CHILD"]["warehouse"] == "WH_A"
    # a future task-creating migration fails the app/audit contract with both edits named
    live3 = replay([("V001__a.sql", v1), ("V003__c.sql",
                     "CREATE TASK IF NOT EXISTS DB.S.T_NEW WAREHOUSE = WH_A AS CALL N();")])
    msg = _diff_message("app/data/ops_sql.py OVERWATCH_TASKS", {"T_ROOT", "T_CHILD"}, set(live3))
    assert "add ['T_NEW']" in msg


# --- the alert-pipeline runbook resumes every root that feeds ALERT_EVENTS (p607 cleanup review) -------
_APC = _ROOT / "snowflake" / "alert_pipeline_check.sql"
_PROC = re.compile(r"\bCREATE\s+(?:OR\s+REPLACE\s+)?PROCEDURE\s+(?:IF\s+NOT\s+EXISTS\s+)?"
                   r"((?:[A-Za-z_][\w$]*\.){0,2}[A-Za-z_][\w$]*)\s*\(", re.I)
_CALL = re.compile(r"\bCALL\s+((?:[A-Za-z_][\w$]*\.){0,2}[A-Za-z_][\w$]*)\s*\(", re.I)
_RAISES = re.compile(r"\b(?:INSERT|MERGE)\s+INTO\s+(?:[A-Za-z_][\w$]*\.){0,2}ALERT_EVENTS\b", re.I)


def _alert_raiser_roots() -> dict[str, set[str]]:
    """{live task whose CALLed proc writes ALERT_EVENTS (directly or via a nested CALL): its graph roots}.
    The proc body is the LATEST definition in the migrations; the task's CALL is read from the migration
    the replay says left it live; comments and strings never count (``blank``)."""
    live = replay()
    texts = {p.name: p.read_text(encoding="utf-8") for p in _MIGRATIONS}
    bodies: dict[str, str] = {}
    for p in _MIGRATIONS:
        text = texts[p.name]
        for m in _PROC.finditer(blank(text)):
            start = text.find("$$", m.end())
            bodies[bare(m.group(1))] = blank(text[start:text.find("$$", start + 2)])

    def raises(proc: str, seen: frozenset[str] = frozenset()) -> bool:
        body = bodies.get(proc)
        if body is None or proc in seen:
            return False
        return bool(_RAISES.search(body)) or any(raises(bare(c), seen | {proc}) for c in _CALL.findall(body))

    def roots(name: str) -> set[str]:
        parents = [p for p in live[name]["after"] if p in live]
        return set().union(*(roots(p) for p in parents)) if parents else {name}

    out: dict[str, set[str]] = {}
    for name, task in live.items():
        b = blank(texts[task["created_in"]])
        create = [m for m in _CREATE.finditer(b) if bare(m.group(3)) == name][-1]
        as_end = _AS.search(b, create.end()).end()
        stmt_end = _STMT_END.search(b, as_end)
        call = _CALL.search(b, as_end, stmt_end.start() if stmt_end else len(b))
        if call and raises(bare(call.group(1))):
            out[name] = roots(name)
    return out


def test_alert_pipeline_runbook_resumes_every_alert_raiser_root():
    """snowflake/alert_pipeline_check.sql FIX A once said the pipeline had only two scans, and its resume
    lines reached only the hourly and daily graphs, so an operator following it after "no alerts" left the
    standalone raisers (TASK_ANOMALY_SWEEP, TASK_CANARY_SENTINEL, ...) suspended. Derived: every graph
    root of a task that writes ALERT_EVENTS is resumed by FIX A and listed in STEP 2's TASK_HISTORY read;
    a chained raiser's root must use SYSTEM$TASK_DEPENDENTS_ENABLE (a plain RESUME leaves children)."""
    raisers = _alert_raiser_roots()
    every_root = set().union(*raisers.values())
    # not vacuous: both scan graphs and the standalone raisers are found
    assert {"TASK_ALERT_SCAN", "TASK_ALERT_SCAN_DAILY", "TASK_ANOMALY_SWEEP",
            "TASK_CANARY_SENTINEL"} <= set(raisers), sorted(raisers)
    assert raisers["TASK_ALERT_SCAN"] == {"TASK_LOAD_HOURLY"}
    assert raisers["TASK_ALERT_SCAN_DAILY"] == {"TASK_LOAD_DAILY"}
    apc = _APC.read_text(encoding="utf-8")
    fix_a = apc.split("-- FIX A", 1)[1].split("-- FIX B", 1)[0]
    enabled = {bare(n) for n in re.findall(r"SYSTEM\$TASK_DEPENDENTS_ENABLE\('([\w$.]+)'\)", fix_a)}
    resumed = {bare(n) for n in re.findall(r"ALTER TASK IF EXISTS ([\w$.]+) RESUME;", fix_a)}
    missing = sorted(f"{root} (feeds {name})" for name, rts in raisers.items() for root in rts
                     if root not in enabled and not (root == name and root in resumed))
    assert not missing, f"alert_pipeline_check.sql FIX A does not resume: {missing}"
    step2 = apc.split("-- STEP 2", 1)[1].split("-- STEP 3", 1)[0]
    listed = set(re.findall(r"'(TASK_\w+)'", step2.split("WHERE NAME IN (", 1)[1].split(")", 1)[0]))
    assert every_root <= listed, f"STEP 2 does not read TASK_HISTORY for {sorted(every_root - listed)}"
