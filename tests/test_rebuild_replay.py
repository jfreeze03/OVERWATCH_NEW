"""A full-rebuild replay runs to the end and leaves the account as it found it (round 2, R2-001/002/004).

* R2-001: V006-V008 grant to OVERWATCH_MONITOR / OVERWATCH_OPERATOR, the roles roles.sql retired on
  2026-07-13. No migration creates them, so Run All of rebuild/02 stopped at V006 on every current or
  fresh account, after V002 had attached the 30-credit SUSPEND monitor and V004 had resumed the graph.
  02's generated header now carries a replay shim (CREATE ROLE IF NOT EXISTS for exactly those roles;
  03_roles.sql drops them again), and the migration bodies stay byte-identical between their banners.
* R2-002: V002 sets WH_ALFA_ADMIN's statement timeout back to 300 s and attaches OVERWATCH_RM until
  V045. FULL_REBUILD records the live value before the teardown and puts it back after the replay, and
  says how to detach the monitor when a run stops in between.
* R2-004: the replay re-runs one-time config DML on the operator tables the teardown keeps (route
  company filters, rule flags and thresholds, re-seeded rows). FULL_REBUILD step 3b restores those
  tables from the step-1 clones; the 02 and 00 headers no longer promise the opposite.

The bundle is generated (house law 6): the headers come from outputs/gen_rebuild_bundle.py, and the
first lock below fails on any hand edit of a generated part: all of 01-05 (holistic #14: 03/04/05 read
their banner back from disk until then), 00's header and the README's notes block (00's CLONE list and
the rest of the README are hand-kept by design).
"""

from __future__ import annotations

import builtins
import importlib.util
import pathlib
import re
import shutil

import pytest

from tests._source import ROOT, read

_SF = ROOT / "snowflake"
_RB = _SF / "rebuild"


def _gen():
    spec = importlib.util.spec_from_file_location("gen_rebuild_bundle", ROOT / "outputs" / "gen_rebuild_bundle.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _smoke():
    """The smoke rewriter's SQL splitter: top-level statements with comments, strings and $$ bodies
    blanked (``statements``), and a code view of a $$ body (``code_view``)."""
    spec = importlib.util.spec_from_file_location(
        "snowflake_smoke_rewrite", ROOT / ".github" / "scripts" / "snowflake_smoke_rewrite.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _bundle_02() -> str:
    (path,) = sorted(_RB.glob("02_migrations_V001_V*.sql"))
    return path.read_text(encoding="utf-8")


def _preamble() -> str:
    text = _bundle_02()
    return text[:text.index("-- >>> V001__core.sql")]


def _section(text: str, start: str, end: str) -> str:
    return text[text.index(start):text.index(end)]


def _prose(text: str) -> str:
    """A SQL comment block as one line of prose: line-leading ``--`` markers dropped, whitespace collapsed."""
    return " ".join(re.sub(r"(?m)^\s*--", " ", text).split())


def _uncommented(text: str) -> list[str]:
    return [line for line in text.splitlines() if not line.lstrip().startswith("--")]


def test_every_generated_bundle_file_is_what_the_generator_renders():
    gen = _gen()
    name, text = gen.render_migration_bundle()
    assert (_RB / name).read_text(encoding="utf-8") == text, f"{name}: run python outputs/gen_rebuild_bundle.py"
    for bundle, source, header in gen.COPIES:
        assert (_RB / bundle).read_text(encoding="utf-8") == gen.render_copy(bundle, source, header), bundle
    assert (_RB / "00_backup_operator_data.sql").read_text(encoding="utf-8") == gen.render_backup()
    assert (_RB / "README.md").read_text(encoding="utf-8") == gen.render_readme()


def test_a_hand_edit_to_any_copied_bundle_header_fails_the_lock(tmp_path):
    """Holistic #14: render_copy read the 03/04/05 header back from the bundle file itself, so the lock
    above compared a hand-edited header with itself (an ALTER WAREHOUSE added there stayed green). Every
    copy's banner is now the generator's own, so an edit anywhere in 01/03/04/05 shows up."""
    gen = _gen()
    for bundle, _source, _header in gen.COPIES:
        head, body = (_RB / bundle).read_text(encoding="utf-8").split("\n\n", 1)
        tampered = f"{head}\nALTER WAREHOUSE WH_ALFA_ADMIN SET STATEMENT_TIMEOUT_IN_SECONDS = 300;\n\n{body}"
        (tmp_path / bundle).write_text(tampered, encoding="utf-8")
    gen.REBUILD = tmp_path                       # the generator now sees only the tampered copies
    for bundle, source, header in gen.COPIES:
        assert gen.render_copy(bundle, source, header) != (tmp_path / bundle).read_text(encoding="utf-8"), bundle


# The bundle files the generator reads back from disk, by design, and how the docs name each one.
_HAND_KEPT = {"00_backup_operator_data.sql": "00's CLONE list",
              "README.md": "the README outside its generated notes block"}
_RENDERS = ("render_backup", "render_copy", "render_migration_bundle", "render_readme")


def _bundle_reads(gen, scratch, monkeypatch) -> set[str]:
    """Every bundle file the generator reads back from disk, found by running it rather than by reading
    its source (recheck #7: a regex for a quoted ``(REBUILD / "name").read_text`` missed the variable
    path ``(REBUILD / bundle_name)`` that holistic #14 was). The bundle is copied to ``scratch`` and the
    generator pointed at it; Path.read_text / read_bytes / open (read modes) and open() are recorded
    while every render_* runs, then while ``main()`` runs (it writes only into ``scratch``). A read of
    the real bundle folder, past REBUILD, counts too."""
    shutil.copytree(_RB, scratch)
    monkeypatch.setattr(gen, "REBUILD", scratch)
    bases = (pathlib.Path(scratch).resolve(), _RB.resolve())
    seen: set[str] = set()

    def note(path, mode="r") -> None:
        if isinstance(path, int) or any(flag in mode for flag in "wax"):
            return
        resolved = pathlib.Path(path).resolve()
        seen.update(resolved.relative_to(base).as_posix() for base in bases if resolved.is_relative_to(base))

    def recorded(original, takes_mode: bool):
        def wrapper(self, *args, **kwargs):
            note(self, (args[0] if args else kwargs.get("mode", "r")) if takes_mode else "r")
            return original(self, *args, **kwargs)
        return wrapper

    real_open = builtins.open

    def open_recorder(file, *args, **kwargs):
        note(file, args[0] if args else kwargs.get("mode", "r"))
        return real_open(file, *args, **kwargs)

    renders = sorted(name for name in vars(gen) if name.startswith("render_") and callable(getattr(gen, name)))
    assert renders == sorted(_RENDERS), f"{renders}: call every new render_* here so its read-backs are seen"
    with pytest.MonkeyPatch.context() as io:
        for method, takes_mode in (("read_text", False), ("read_bytes", False), ("open", True)):
            io.setattr(pathlib.Path, method, recorded(getattr(pathlib.Path, method), takes_mode))
        io.setattr(builtins, "open", open_recorder)
        gen.render_migration_bundle()
        for bundle, source, header in gen.COPIES:
            gen.render_copy(bundle, source, header)
        gen.render_backup()
        gen.render_readme()
        gen.main()
    return seen


def test_the_generator_docs_name_every_bundle_file_it_reads_back_from_disk(tmp_path, monkeypatch):
    """Holistic #14 follow-up: render_readme reads README.md back from the bundle folder, so a hand edit
    outside its notes block is compared with itself, yet the generator docstring and CLAUDE.md law 6 named
    00's CLONE list as the only hand-kept part ("never hand-edit"). Every bundle file the generator reads
    back must be named as hand-kept in both, so neither can claim more coverage than the lock gives
    (recheck #7: the read-backs are now found by running the generator, see _bundle_reads)."""
    reads = _bundle_reads(_gen(), tmp_path / "bundle", monkeypatch)
    assert reads == set(_HAND_KEPT), (
        f"the generator reads {sorted(reads)} back from the bundle folder: a generated file must be rendered "
        "from its sources, or named as hand-kept here and in the docs")
    docstring = " ".join(_gen().__doc__.split())
    law_6 = " ".join(_section(read("CLAUDE.md"), "6. **Rebuild bundle", "7. **Task-graph").split())
    for name, phrase in _HAND_KEPT.items():
        assert phrase in docstring, f"gen_rebuild_bundle.py docstring omits {name}: {phrase!r}"
        assert phrase in law_6, f"CLAUDE.md law 6 omits {name}: {phrase!r}"
    # recheck #9: the README's own rule leaves room for the hand-kept parts it then names (it said "never
    # this folder"), and it names the lock that re-renders every file, not only the copy-body lock
    readme = " ".join((_RB / "README.md").read_text(encoding="utf-8").split())
    assert "never this folder" not in readme
    assert "never a generated part of this folder; the two hand-kept parts below are edited here" in readme
    assert "tests/test_rebuild_replay.py" in readme and "tests/test_rebuild_bundle.py" in readme


def test_the_read_back_check_sees_a_banner_read_through_a_variable_path(tmp_path, monkeypatch):
    """Recheck #7: the generator before holistic #14 (83e32ae6^) read the 03/04/05 banner back through
    ``(REBUILD / bundle_name).read_text``, and the old regex check returned the clean set for it. Put that
    shape back (its render_copy and COPIES, verbatim) and the check must name the three files."""
    gen = _gen()

    def render_copy(bundle_name: str, source_name: str, header: str | None = None) -> str:
        if header is None:
            header = (gen.REBUILD / bundle_name).read_text(encoding="utf-8").split("\n\n", 1)[0]
        source = (gen.SNOWFLAKE / source_name).read_text(encoding="utf-8")
        return f"{header}\n\n{source}"

    monkeypatch.setattr(gen, "render_copy", render_copy)
    monkeypatch.setattr(gen, "COPIES", (
        ("01_teardown_rebuildables.sql", "teardown.sql", gen.TEARDOWN_BANNER),
        ("03_roles.sql", "roles.sql", None),
        ("04_backfill_365.sql", "backfill_365.sql", None),
        ("05_validate.sql", "validate.sql", None)))
    reads = _bundle_reads(gen, tmp_path / "bundle", monkeypatch)
    assert reads - set(_HAND_KEPT) == {"03_roles.sql", "04_backfill_365.sql", "05_validate.sql"}, reads


def test_replay_grants_only_to_live_or_shimmed_roles():
    text = _bundle_02()
    roles_sql = read("snowflake/roles.sql")
    live = {m.group(1) for line in _uncommented(roles_sql) for m in re.finditer(r"\bTO ROLE (\w+)", line)}
    retired = set(re.findall(r"^DROP ROLE IF EXISTS (\w+);$", roles_sql, re.M))
    assert live == {"SNOW_ACCOUNTADMINS", "SNOW_SYSADMINS"}             # owner decision 2026-07-13
    preamble = _preamble()
    shim = re.findall(r"^CREATE ROLE IF NOT EXISTS (\w+);$", preamble, re.M)
    targets = {m.group(1) for line in _uncommented(text) for m in re.finditer(r"\bTO ROLE (\w+)", line)}
    # every grant the replay runs names a live role or one the shim created first
    assert not targets - live - set(shim), sorted(targets - live - set(shim))
    # the shim re-creates exactly the retired roles the chain still grants to, and nothing else
    assert set(shim) == targets - live and shim, shim
    assert set(shim) <= retired
    # ...and 03 (byte copy of roles.sql) drops them again, after 02
    r03 = (_RB / "03_roles.sql").read_text(encoding="utf-8")
    for role in shim:
        assert f"DROP ROLE IF EXISTS {role};" in r03, role
    # the shim is the only SQL that runs before the V001 banner
    assert [line for line in _uncommented(preamble) if line.strip()] == [
        f"CREATE ROLE IF NOT EXISTS {role};" for role in shim]


def test_the_shim_fallback_drops_the_roles_before_roles_sql_runs():
    """Holistic #13: roles.sql (03, FULL_REBUILD step 4) opens by dropping the retired roles. A deployment
    role that lacks CREATE ROLE does not own the roles the fallback creates, so that DROP stops 03 before
    its first grant and before the audit-table REVOKEs. The creating role drops them BEFORE 03, never
    after it."""
    shim = re.findall(r"^CREATE ROLE IF NOT EXISTS (\w+);$", _preamble(), re.M)
    roles_sql = [line for line in _uncommented(read("snowflake/roles.sql")) if line.strip()]
    assert set(roles_sql[:len(shim)]) == {f"DROP ROLE IF EXISTS {role};" for role in shim}   # the reason
    preamble = _prose(_preamble())
    assert "drop them with that role before 03" in preamble
    assert "after 03" not in preamble
    step3 = " ".join(_section(read("docs/FULL_REBUILD.md"), "## 3. Migrations", "## 3b.").split())
    assert "drop them with that role before step 4" in step3
    assert "after step 4" not in step3


def test_the_shim_lives_in_the_header_only_and_the_migrations_stay_byte_identical():
    text = _bundle_02()
    migrations = sorted((_SF / "migrations").glob("V[0-9]*.sql"))
    for path in migrations:
        assert "CREATE ROLE" not in path.read_text(encoding="utf-8").upper(), path.name
    assert text.count("CREATE ROLE IF NOT EXISTS") == len(re.findall(r"^CREATE ROLE", _preamble(), re.M))
    body = text[text.index("-- ===========================================================================\n-- >>> V001"):]
    joined = "\n".join(
        "-- ===========================================================================\n"
        f"-- >>> {p.name}\n"
        "-- ===========================================================================\n"
        + p.read_text(encoding="utf-8") for p in migrations)
    assert body == joined


def test_bundle_headers_say_what_a_replay_changes():
    preamble = _preamble()
    flat = " ".join(preamble.split())
    assert "re-running a completed file is safe" not in flat
    assert "A REPLAY IS NOT A NO-OP ON KEPT OPERATOR DATA" in flat
    assert "STATEMENT_TIMEOUT_IN_SECONDS back to 300" in flat and "OVERWATCH_RM" in flat
    assert "step 3b" in flat
    bak = (_RB / "00_backup_operator_data.sql").read_text(encoding="utf-8")
    head = " ".join(bak.split("\n\n", 1)[0].split())
    assert "never touches these tables" not in head and "pure insurance" not in head
    assert "step 3b" in head
    readme = (_RB / "README.md").read_text(encoding="utf-8")
    assert "step 3b" in readme and "STATEMENT_TIMEOUT_IN_SECONDS" in readme
    assert "close the events the replay raised for rules that are off again" in " ".join(readme.split())
    # R2-003: the paste-and-run README also says delivery must be put back after 05
    assert "**After 05** (step 7b)" in readme and "notification integrations" in readme


# Kept operator tables the replay's one-time DML (top-level statements and apply-time EXECUTE IMMEDIATE
# blocks, see _replay_dml_targets) may touch WITHOUT a step-3b restore, and why.
_REPLAY_DML_NEEDS_NO_RESTORE = {
    # retire idioms (V034, V140, V157, V161) close events of retired rules / self-objects as EXPECTED;
    # re-running them closes only what no scan can raise again
    "ALERT_EVENTS",
    # V140 deletes the scan's own DBA_MAINT_DB rows, which the hardened scan never registers again
    "OBJECT_CHANGE_REGISTRY",
}

# Kept operator tables the replay writes through what it runs at apply time (CALL / EXECUTE TASK, see
# _replay_call_writes) WITHOUT a step-3b restore, and why (recheck #8). Every entry must still be written
# that way and none may be restored, so the list cannot go stale.
_REPLAY_CALL_WRITES_NEED_NO_RESTORE = {
    "ALERT_EVENTS": "the scans the replay CALLs (SP_ALERT_SCAN, SP_ALERT_SCAN_DAILY, SP_ANOMALY_SWEEP, "
                    "SP_SCAN_CLOUD_SVC_ANOMALY) raise what their scheduled runs would; step 3b closes what "
                    "they raised for a rule that is off again and lists the rest for review",
    "APP_ERROR_LOG": "the scan and loader procs log their own failures, as their scheduled runs do: error "
                     "history, not config",
    "DQ_SCHEMA_SNAPSHOT": "V133's SP_ANOMALY_SWEEP takes the column baseline the daily sweep takes anyway",
    "WAREHOUSE_CHANGE_REGISTRY": "V033's SP_CHANGE_ATTRIBUTION fills CHANGED_BY with the UPDATE the current "
                                 "(V159) body runs on every scheduled pass; V159 only adds a skip gate",
    "OPERATOR_BACKUP_LOG": "V158's EXECUTE TASK logs one backup generation, and V161 retires the table later "
                           "in the same replay",
}


def _restored_tables(fr: str) -> set[str]:
    step3b = _section(fr, "## 3b. Put back what the replay rewrote", "## 4. Grants")
    return set(re.findall(r"INSERT OVERWRITE INTO DBA_MAINT_DB\.OVERWATCH\.(\w+)\s+SELECT \* FROM "
                          r"DBA_MAINT_DB\.OVERWATCH\.\1_BAK_<date>;", step3b))


# A DML statement and its target; ``UPDATE SET`` is a MERGE's WHEN MATCHED arm, not a target.
_DML_TARGET = re.compile(r"\b(?:UPDATE\s+(?!SET\b)|DELETE\s+FROM\s+|MERGE\s+INTO\s+|INSERT\s+INTO\s+"
                         r"|INSERT\s+OVERWRITE\s+INTO\s+)([\w$.\"]+)", re.I)


def _overwatch_table(name: str) -> str | None:
    """X for X, OVERWATCH.X or DBA_MAINT_DB.OVERWATCH.X; None for a table in another schema."""
    parts = name.replace('"', "").upper().split(".")
    return parts[-1] if parts[:-1] in ([], ["OVERWATCH"], ["DBA_MAINT_DB", "OVERWATCH"]) else None


# The variable an apply-time block reads SCHEMA_VERSION's tip into; ``IF (v < N)`` on it is a first-apply
# branch, which a replay skips (SCHEMA_VERSION already holds every version).
_TIP_VARIABLE = re.compile(r"\bSELECT\s+MAX\s*\(\s*VERSION\s*\)\s+INTO\s+:(\w+)\s+FROM\s+[\w$.\"]*\bSCHEMA_VERSION\b",
                           re.I)


def _apply_time_view(smoke, raw: str) -> str:
    """The code a top-level ``EXECUTE IMMEDIATE $$...$$`` block runs on a replay: its body with comments
    and strings blanked, and every branch behind a first-apply version check blanked too (V038's
    ``IF (v < 38) THEN CALL SP_LEDGER_AUTOBOOK()`` never runs on a replay; that guard exists so the old
    autobook cannot settle V153's in-flight rows)."""
    assert raw.count("$$") >= 2, f"EXECUTE IMMEDIATE without a $$ block; teach this scan: {raw[:80]}"
    body = smoke.code_view(raw[raw.index("$$") + 2:raw.rindex("$$")])
    for variable in _TIP_VARIABLE.findall(body):
        body = re.sub(rf"\bIF\s*\(\s*:?{variable}\s*<\s*\d+\s*\)\s*THEN\b.*?\bEND\s+IF\b",
                      lambda m: re.sub(r"[^\n]", " ", m.group(0)), body, flags=re.I | re.S)
    return body


def _replay_dml_targets(text: str) -> set[str]:
    """The OVERWATCH tables one migration's replay writes with one-time DML: every top-level DML
    statement, at any indentation, plus the DML inside every top-level ``EXECUTE IMMEDIATE $$...$$``
    block (the apply-time anonymous block, a common migration idiom: V070, V118, V145) outside a
    first-apply version branch. DML inside a CREATE PROCEDURE / TASK body does not run here; what an
    apply-time CALL runs is _replay_call_writes (holistic #15: the scan matched column-0 DML only and
    missed those three blocks)."""
    smoke = _smoke()
    targets: set[str] = set()
    for start, end, code in smoke.statements(text):
        if code.startswith("EXECUTE IMMEDIATE"):
            names = [m.group(1) for m in _DML_TARGET.finditer(_apply_time_view(smoke, text[start:end]))]
        else:
            m = _DML_TARGET.match(code)
            names = [m.group(1)] if m else []
        targets |= {table for table in map(_overwatch_table, names) if table}
    return targets


_CALL_TARGET = re.compile(r"\bCALL\s+([\w$.\"]+)", re.I)
_CREATE_PROCEDURE = re.compile(r"CREATE (?:OR REPLACE )?(?:SECURE )?PROCEDURE (?:IF NOT EXISTS )?([\w$.\"]+)")
_CREATE_TASK = re.compile(r"CREATE (OR REPLACE )?TASK (IF NOT EXISTS )?([\w$.\"]+)")
_EXECUTE_TASK = re.compile(r"EXECUTE TASK ([\w$.\"]+)")


def _replay_call_writes(chain: list[tuple[str, str]]) -> dict[str, set[tuple[str, str]]]:
    """{table: {(version, procedure or task)}}: the OVERWATCH tables the replay of ``chain`` ((version,
    text) in apply order) writes through what it runs at apply time without spelling the DML out
    (recheck #8: V153's tail ``CALL SP_LEDGER_AUTOBOOK()`` adopts, books and settles SAVINGS_LEDGER rows
    during the replay, and the DML scan never saw it).

    Counted: every top-level CALL, every CALL in a top-level EXECUTE IMMEDIATE block outside a first-apply
    version branch (_apply_time_view), and every EXECUTE TASK (the task's body). A CALL runs the bodies
    in effect at that point of the replay (the latest definition of every overload of that name), read
    with their dynamic-SQL strings as SQL, and the procedures those CALL in turn."""
    smoke = _smoke()
    procedures: dict[str, dict[str, str]] = {}          # name -> {signature: body}, as defined so far
    tasks: dict[str, str] = {}                          # name -> body, as defined so far
    writes: dict[str, set[tuple[str, str]]] = {}

    def called(code: str) -> list[str]:
        return [name for name in map(_overwatch_table, (m.group(1) for m in _CALL_TARGET.finditer(code))) if name]

    def run(code: str, seen: frozenset[str]) -> set[str]:
        tables = {table for table in map(_overwatch_table, (m.group(1) for m in _DML_TARGET.finditer(code)))
                  if table}
        return tables | {table for name in called(code) for table in call(name, seen)}

    def call(name: str, seen: frozenset[str]) -> set[str]:
        if name in seen:
            return set()
        return {table for body in procedures.get(name, {}).values() for table in run(body, seen | {name})}

    for version, text in chain:
        for start, end, code in smoke.statements(text):
            raw = text[start:end]
            if m := _CREATE_PROCEDURE.match(code):
                assert raw.count("$$") >= 2, f"{version}: a procedure body without $$; teach this scan"
                signature = code[m.end():code.index(" RETURNS ")]
                procedures.setdefault(_overwatch_table(m.group(1)) or m.group(1), {})[signature] = smoke.code_view(
                    raw[raw.index("$$") + 2:raw.rindex("$$")], strings=True, dynamic=True)
                continue
            if m := _CREATE_TASK.match(code):
                name = _overwatch_table(m.group(3)) or m.group(3)
                if m.group(1) or not m.group(2) or name not in tasks:     # IF NOT EXISTS keeps the live one
                    header = re.search(r"\bAS\b", smoke.code_view(raw), re.I)
                    assert header, f"{version}: a task without AS; teach this scan"
                    tasks[name] = smoke.code_view(raw, strings=True, dynamic=True)[header.end():]
                continue
            if code.startswith("CALL "):
                runs = [(name, call(name, frozenset())) for name in called(code)]
            elif code.startswith("EXECUTE IMMEDIATE"):
                runs = [(name, call(name, frozenset())) for name in called(_apply_time_view(smoke, raw))]
            elif m := _EXECUTE_TASK.match(code):
                name = _overwatch_table(m.group(1)) or m.group(1)
                runs = [(name, run(tasks.get(name, ""), frozenset()))]
            else:
                assert not _CALL_TARGET.search(code), f"{version}: a CALL inside {code[:60]}; teach this scan"
                continue
            for name, tables in runs:
                assert name in procedures or name in tasks, (
                    f"{version} runs {name} before any migration defines it; teach this scan")
                for table in tables:
                    writes.setdefault(table, set()).add((version, name))
    return writes


def _chain_tables() -> set[str]:
    """Every OVERWATCH table a migration creates (TEMPORARY ones excluded): the names a write can really
    land in, so a phrase in a dynamic-SQL string ('... update downstream consumers.') is not a table."""
    smoke = _smoke()
    create = re.compile(r"\bCREATE\s+(?:OR\s+REPLACE\s+)?(?:TRANSIENT\s+)?TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?"
                        r"([\w$.\"]+)", re.I)
    return {table
            for path in sorted((_SF / "migrations").glob("V[0-9]*.sql"))
            for table in map(_overwatch_table, (m.group(1) for m in create.finditer(
                smoke.code_view(path.read_text(encoding="utf-8")))))
            if table}


def test_replay_dml_scan_reads_apply_time_blocks_but_not_proc_or_task_bodies():
    text = (
        "CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_X() RETURNS VARCHAR LANGUAGE SQL AS\n"
        "$$\nBEGIN\n    UPDATE DBA_MAINT_DB.OVERWATCH.IN_PROC SET A = 1;\n    RETURN 'ok';\nEND;\n$$;\n"
        "EXECUTE IMMEDIATE\n$$\nBEGIN\n"
        "    UPDATE DBA_MAINT_DB.OVERWATCH.ONE_TIME l SET A = 1 WHERE l.B = 'x;y';\n"
        "    MERGE INTO OVERWATCH.MERGED t USING (SELECT 1 AS X) s ON TRUE WHEN MATCHED THEN UPDATE SET A = 1;\n"
        "    -- UPDATE DBA_MAINT_DB.OVERWATCH.COMMENTED SET A = 1;\n"
        "END;\n$$;\n"
        "CREATE TASK DBA_MAINT_DB.OVERWATCH.T AS UPDATE DBA_MAINT_DB.OVERWATCH.IN_TASK SET A = 1;\n"
        "  DELETE FROM DBA_MAINT_DB.OVERWATCH.TOP_LEVEL WHERE 1 = 0;\n"
        "INSERT INTO OTHER_DB.S.ELSEWHERE SELECT 1;\n")
    assert _replay_dml_targets(text) == {"ONE_TIME", "MERGED", "TOP_LEVEL"}


def test_replay_call_scan_runs_the_bodies_in_effect_at_each_apply_time_call():
    """Recheck #8: a top-level CALL, or one in an apply-time block outside a first-apply branch, runs the
    body defined so far (never a later one), its dynamic SQL and the procedures it CALLs; EXECUTE TASK
    runs the live task (CREATE TASK IF NOT EXISTS keeps the first). A task body or a procedure that is
    only defined runs nothing, and neither does a branch behind ``IF (v < N)`` on SCHEMA_VERSION's tip."""
    v1 = (
        "CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_INNER() RETURNS VARCHAR LANGUAGE SQL AS\n"
        "$$\nBEGIN\n    UPDATE DBA_MAINT_DB.OVERWATCH.INNER_T SET A = 1;\n    RETURN 'ok';\nEND;\n$$;\n"
        "CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_OUTER() RETURNS VARCHAR LANGUAGE SQL AS\n"
        "$$\nBEGIN\n    EXECUTE IMMEDIATE 'DELETE FROM DBA_MAINT_DB.OVERWATCH.DYNAMIC_T WHERE A = ''x''';\n"
        "    CALL DBA_MAINT_DB.OVERWATCH.SP_INNER();\n    RETURN 'ok';\nEND;\n$$;\n"
        "CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_GUARDED() RETURNS VARCHAR LANGUAGE SQL AS\n"
        "$$\nBEGIN\n    INSERT INTO DBA_MAINT_DB.OVERWATCH.GUARDED_T SELECT 1;\n    RETURN 'ok';\nEND;\n$$;\n"
        "CREATE TASK IF NOT EXISTS DBA_MAINT_DB.OVERWATCH.TASK_X\n    WAREHOUSE = WH\n"
        "    SCHEDULE = 'USING CRON 0 5 * * * UTC'\nAS\n    CALL DBA_MAINT_DB.OVERWATCH.SP_INNER();\n"
        "CALL DBA_MAINT_DB.OVERWATCH.SP_OUTER();\n")
    v2 = (
        "EXECUTE IMMEDIATE\n$$\nDECLARE\n    v NUMBER;\nBEGIN\n"
        "    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;\n"
        "    IF (v < 2) THEN\n        CALL DBA_MAINT_DB.OVERWATCH.SP_GUARDED();\n    END IF;\n"
        "    CALL DBA_MAINT_DB.OVERWATCH.SP_INNER();\nEND;\n$$;\n"
        "CREATE TASK IF NOT EXISTS DBA_MAINT_DB.OVERWATCH.TASK_X WAREHOUSE = WH AS\n"
        "    CALL DBA_MAINT_DB.OVERWATCH.SP_GUARDED();\n"
        "EXECUTE TASK DBA_MAINT_DB.OVERWATCH.TASK_X;\n"
        "CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_INNER() RETURNS VARCHAR LANGUAGE SQL AS\n"
        "$$\nBEGIN\n    MERGE INTO DBA_MAINT_DB.OVERWATCH.LATER_T t USING (SELECT 1 AS X) s ON TRUE\n"
        "        WHEN MATCHED THEN UPDATE SET A = 1;\n    RETURN 'ok';\nEND;\n$$;\n")
    assert _replay_call_writes([("V001", v1), ("V002", v2)]) == {
        "INNER_T": {("V001", "SP_OUTER"), ("V002", "SP_INNER"), ("V002", "TASK_X")},
        "DYNAMIC_T": {("V001", "SP_OUTER")},
    }


def test_full_rebuild_restores_every_kept_table_the_replay_rewrites():
    teardown = read("snowflake/teardown.sql")
    dropped = set(re.findall(r"^DROP (?:DYNAMIC )?TABLE IF EXISTS DBA_MAINT_DB\.OVERWATCH\.(\w+);", teardown, re.M))
    chain = [(path.name[:4], path.read_text(encoding="utf-8"))
             for path in sorted((_SF / "migrations").glob("V[0-9]*.sql"))]
    one_time: dict[str, set[str]] = {}
    for version, text in chain:
        for table in _replay_dml_targets(text):
            if table not in dropped and table != "SCHEMA_VERSION":
                one_time.setdefault(table, set()).add(version)
    assert {"ALERT_CONFIG", "ALERT_ROUTES", "SETTINGS"} <= set(one_time), "scan went vacuous"
    # ...and it reads the apply-time EXECUTE IMMEDIATE blocks (V070's route disable, V118/V145's one-time
    # SAVINGS_LEDGER corrections), which a column-0 scan never saw
    assert "V070" in one_time["ALERT_ROUTES"] and {"V118", "V145"} <= one_time.get("SAVINGS_LEDGER", set())
    # recheck #8: ...and what the procedures and tasks the replay runs at apply time write to kept tables
    kept = _chain_tables() - dropped - {"SCHEMA_VERSION"}
    called = {table: hits for table, hits in _replay_call_writes(chain).items() if table in kept}
    rewritten = {table: set(versions) for table, versions in one_time.items()}
    for table, hits in called.items():
        rewritten.setdefault(table, set()).update(version for version, _name in hits)
    assert ("V153", "SP_LEDGER_AUTOBOOK") in called.get("SAVINGS_LEDGER", set()), called.get("SAVINGS_LEDGER")
    assert "V153" in rewritten["SAVINGS_LEDGER"], sorted(rewritten["SAVINGS_LEDGER"])
    # V038's first autobook sits behind IF (v < 38), which a replay skips
    assert not any(version == "V038" for version, _name in called["SAVINGS_LEDGER"]), called["SAVINGS_LEDGER"]
    fr = read("docs/FULL_REBUILD.md")
    restored = _restored_tables(fr)
    missing = (set(one_time) - restored - _REPLAY_DML_NEEDS_NO_RESTORE) | (
        set(called) - restored - set(_REPLAY_CALL_WRITES_NEED_NO_RESTORE))
    assert not missing, {t: sorted(rewritten[t]) for t in missing}
    # the CALL-path exemptions stay true: each is still written that way and none is restored
    for table, reason in _REPLAY_CALL_WRITES_NEED_NO_RESTORE.items():
        assert table in called and table not in restored, (table, reason)
    # every restored table has a step-1 clone to restore from
    bak = (_RB / "00_backup_operator_data.sql").read_text(encoding="utf-8")
    for table in restored:
        assert f"CLONE DBA_MAINT_DB.OVERWATCH.{table};" in bak, table
    gen = _gen()
    for table in restored:                       # the generated headers name the same set
        assert table in gen.BACKUP_HEADER and table in gen.README_NOTES, table
    # the old promise is gone, and step 3 sends the keep path to 3b
    assert "That is the designed restore path" not in fr
    step3 = " ".join(_section(fr, "## 3. Migrations", "## 3b.").split())
    assert "V034 sets every 'ALL' route's COMPANY_FILTER to 'ALFA'" in step3 and "Step 3b" in step3


def _step3b_statements(fr: str) -> list[str]:
    """Step 3b's SQL in document order: its indented code lines, trailing comments dropped, one
    whitespace-normalized string per statement."""
    step3b = _section(fr, "## 3b. Put back what the replay rewrote", "## 4. Grants")
    code = "\n".join(line.split("--", 1)[0] for line in step3b.splitlines() if line.startswith("    "))
    return [" ".join(stmt.split()) for stmt in code.split(";") if stmt.strip()]


def test_step_3b_closes_resurrected_rules_and_keeps_dead_routes_off():
    fr = read("docs/FULL_REBUILD.md")
    step3b = _section(fr, "## 3b. Put back what the replay rewrote", "## 4. Grants")
    # the replay-only rules are captured BEFORE ALERT_CONFIG is overwritten, their events closed after
    capture = step3b.index("CREATE TEMPORARY TABLE OW_REPLAY_ONLY_RULES")
    overwrite = step3b.index("INSERT OVERWRITE INTO DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG")
    close = step3b.index("UPDATE DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS")
    assert capture < overwrite < close
    assert "MINUS SELECT RULE_ID FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG_BAK_<date>" in step3b
    assert "AND RULE_ID IN (SELECT RULE_ID FROM OW_REPLAY_ONLY_RULES)" in step3b
    # p608 review: a rule the replay switched on or re-tuned is in both tables, so a RULE_ID-only MINUS
    # never sees it. The replay scans under that config (V045 enables PIPE_TASK_FAILURES and then
    # CALLs SP_ALERT_SCAN; V020/V028 re-arm SEC_CRED_EXPIRY at 10 days), so step 3b also keeps every
    # rule whose ROW changed, and closes what the replay raised for a rule that is off again.
    stmts = _step3b_statements(fr)
    config_overwrite = next(i for i, s in enumerate(stmts)
                            if s.startswith("INSERT OVERWRITE INTO DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG "))
    (touched,) = [i for i, s in enumerate(stmts) if s.startswith("CREATE TEMPORARY TABLE OW_REPLAY_TOUCHED_RULES")]
    assert touched < config_overwrite
    assert ("SELECT * FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG "
            "MINUS SELECT * FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG_BAK_<date>") in stmts[touched]
    (off_again,) = [i for i, s in enumerate(stmts) if s.startswith("UPDATE DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS")
                    and "ALERT_CONFIG WHERE ENABLED" in s]
    assert off_again > config_overwrite
    for clause in ("SET STATUS = 'RESOLVED', RESOLUTION_KIND = 'EXPECTED'",
                   "WHERE STATUS IN ('OPEN', 'ACK', 'SNOOZED')",
                   "RULE_ID NOT IN (SELECT RULE_ID FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG WHERE ENABLED)",
                   # only what the replay raised: operator history from before the step-1 clone stays
                   "EVENT_ID NOT IN (SELECT EVENT_ID FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS_BAK_<date>)"):
        assert clause in stmts[off_again], clause
    # ...and the events of a rule that is on again are listed for review, not closed
    (review,) = [i for i, s in enumerate(stmts) if s.startswith("SELECT EVENT_ID")]
    assert review > config_overwrite and "RULE_ID IN (SELECT RULE_ID FROM OW_REPLAY_TOUCHED_RULES)" in stmts[review]
    # the clone the close reads exists on the step-1 path (00 and teardown B0 both take it)
    assert "CLONE DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS;" in (_RB / "00_backup_operator_data.sql").read_text(
        encoding="utf-8")
    assert re.search(r"^-- CREATE TRANSIENT TABLE DBA_MAINT_DB\.OVERWATCH\.ALERT_EVENTS_BAK_\d+\s+CLONE "
                     r"DBA_MAINT_DB\.OVERWATCH\.ALERT_EVENTS;", read("snowflake/teardown.sql"), re.M)
    # the reason still holds: the replay switches rules on, and V045 scans after it does
    switched_on = set()
    for path in sorted((_SF / "migrations").glob("V[0-9]*.sql")):
        switched_on |= set(re.findall(r"^UPDATE DBA_MAINT_DB\.OVERWATCH\.ALERT_CONFIG\s+SET ENABLED = TRUE\s+"
                                      r"WHERE RULE_ID = '(\w+)';", path.read_text(encoding="utf-8"), re.M))
    assert {"PIPE_TASK_FAILURES", "SEC_CRED_EXPIRY"} <= switched_on, switched_on
    v045 = read("snowflake/migrations/V045__task_monitoring_restored.sql")
    assert (v045.index("SET ENABLED = TRUE\n WHERE RULE_ID = 'PIPE_TASK_FAILURES';")
            < v045.index("\nCALL DBA_MAINT_DB.OVERWATCH.SP_ALERT_SCAN();"))
    # a restored ENABLED route whose integration the teardown dropped goes back off until step 7b
    routes = step3b.index("INSERT OVERWRITE INTO DBA_MAINT_DB.OVERWATCH.ALERT_ROUTES")
    show = step3b.index("SHOW NOTIFICATION INTEGRATIONS;")
    assert routes < show < step3b.index("UPDATE DBA_MAINT_DB.OVERWATCH.ALERT_ROUTES SET ENABLED = FALSE")
    assert 'NOT IN\n           (SELECT UPPER("name") FROM TABLE(RESULT_SCAN(LAST_QUERY_ID())));' in step3b
    # step 7b still re-enables exactly the routes step 0 recorded
    assert re.search(r"SET ENABLED = TRUE\s+WHERE ROUTE_ID IN \(", _section(fr, "## 7b.", "## 8."))


def _sqlite_dialect(stmt: str) -> list[str]:
    """One step-3b statement in sqlite's dialect (same set semantics; the clone suffix pinned)."""
    stmt = stmt.replace("DBA_MAINT_DB.OVERWATCH.", "").replace("<date>", "20261001")
    stmt = re.sub(r"\bMINUS\b", "EXCEPT", stmt)
    stmt = stmt.replace("CURRENT_TIMESTAMP()", "CURRENT_TIMESTAMP").replace("CURRENT_USER()", "'tester'")
    overwrite = re.match(r"INSERT OVERWRITE INTO (\w+) (SELECT .*)", stmt)
    if overwrite:
        return [f"DELETE FROM {overwrite.group(1)}", f"INSERT INTO {overwrite.group(1)} {overwrite.group(2)}"]
    return [stmt]


def test_step_3b_sql_closes_what_the_replay_raised_for_rules_that_are_off_again():
    """Run step 3b's own SQL (the restore + event block) against a replayed account in miniature.

    Owner config (the step-1 clones): PIPE_TASK_FAILURES off, SEC_CRED_EXPIRY on at 5 days,
    RESEEDED_RULE deleted, COST_RULE and OFF_RULE untouched. The replay switched PIPE_TASK_FAILURES
    on, reset SEC_CRED_EXPIRY to 10, re-seeded RESEEDED_RULE, and its scans raised events for all
    three. Before the fix, the PIPE_TASK_FAILURES events stayed OPEN/SNOOZED and step 7b paged them.
    """
    import sqlite3

    fr = read("docs/FULL_REBUILD.md")
    stmts = _step3b_statements(fr)
    runnable = [s for s in stmts if not s.startswith(("SHOW ", "ALTER WAREHOUSE")) and "RESULT_SCAN" not in s]
    db = sqlite3.connect(":memory:")
    plain = sorted(_restored_tables(fr) - {"ALERT_CONFIG"})    # every other table step 3b restores
    assert {"SETTINGS", "COMPANY_SCOPE", "ALERT_ROUTES", "DEPARTMENT_MAP"} <= set(plain), plain
    for table in plain:
        for name in (table, f"{table}_BAK_20261001"):
            db.execute(f"CREATE TABLE {name} (K TEXT, V TEXT)")
        db.execute(f"INSERT INTO {table}_BAK_20261001 VALUES ('k', 'owner')")
        db.execute(f"INSERT INTO {table} VALUES ('k', 'replay')")
    for name in ("ALERT_CONFIG", "ALERT_CONFIG_BAK_20261001"):
        db.execute(f"CREATE TABLE {name} (RULE_ID TEXT, ENABLED INTEGER, THRESHOLD_NUM REAL)")
    owner = [("PIPE_TASK_FAILURES", 0, 1), ("SEC_CRED_EXPIRY", 1, 5), ("COST_RULE", 1, 100), ("OFF_RULE", 0, 3)]
    replay = [("PIPE_TASK_FAILURES", 1, 1), ("SEC_CRED_EXPIRY", 1, 10), ("COST_RULE", 1, 100), ("OFF_RULE", 0, 3),
              ("RESEEDED_RULE", 1, 1)]
    db.executemany("INSERT INTO ALERT_CONFIG_BAK_20261001 VALUES (?, ?, ?)", owner)
    db.executemany("INSERT INTO ALERT_CONFIG VALUES (?, ?, ?)", replay)
    for name in ("ALERT_EVENTS", "ALERT_EVENTS_BAK_20261001"):
        db.execute(f"CREATE TABLE {name} (EVENT_ID TEXT, RULE_ID TEXT, STATUS TEXT, RESOLUTION_KIND TEXT, "
                   "RESOLVED_AT TEXT, ACK_BY TEXT, ACK_AT TEXT, RAISED_AT TEXT, TITLE TEXT)")
    before_clone = [("old_pipe_ack", "PIPE_TASK_FAILURES", "ACK"),       # from before the owner switched it off
                    ("old_cost", "COST_RULE", "OPEN"),
                    ("old_reseeded", "RESEEDED_RULE", "OPEN")]          # orphan of the rule the owner deleted
    since_clone = [("new_pipe", "PIPE_TASK_FAILURES", "OPEN"),
                   ("new_pipe_snoozed", "PIPE_TASK_FAILURES", "SNOOZED"),
                   ("new_pipe_cleared", "PIPE_TASK_FAILURES", "RESOLVED"),
                   ("new_sec", "SEC_CRED_EXPIRY", "OPEN"),
                   ("new_reseeded", "RESEEDED_RULE", "OPEN"),
                   ("new_cost", "COST_RULE", "OPEN")]                   # a live scan between steps 1 and 2
    db.executemany("INSERT INTO ALERT_EVENTS_BAK_20261001 (EVENT_ID, RULE_ID, STATUS) VALUES (?, ?, ?)", before_clone)
    db.executemany("INSERT INTO ALERT_EVENTS (EVENT_ID, RULE_ID, STATUS) VALUES (?, ?, ?)", before_clone + since_clone)
    listed: list[str] | None = None
    for stmt in runnable:
        for part in _sqlite_dialect(stmt):
            rows = db.execute(part).fetchall()
            if part.startswith("SELECT EVENT_ID"):
                listed = sorted(row[0] for row in rows)
    # the config tables are the owner's again
    assert sorted(db.execute("SELECT * FROM ALERT_CONFIG").fetchall()) == sorted(owner)
    for table in plain:
        assert db.execute(f"SELECT V FROM {table}").fetchall() == [("owner",)], table
    state = {row[0]: (row[1], row[2]) for row in db.execute("SELECT EVENT_ID, STATUS, RESOLUTION_KIND FROM ALERT_EVENTS")}
    closed = ("RESOLVED", "EXPECTED")
    assert state["new_pipe"] == closed and state["new_pipe_snoozed"] == closed   # the review finding
    assert state["new_reseeded"] == closed and state["old_reseeded"] == closed   # replay-only rule, as before
    assert state["new_pipe_cleared"] == ("RESOLVED", None)                      # already closed: untouched
    assert state["old_pipe_ack"] == ("ACK", None)                               # operator history: untouched
    assert state["new_sec"] == ("OPEN", None)        # the rule is on again: listed for review, not closed
    assert state["old_cost"] == ("OPEN", None) and state["new_cost"] == ("OPEN", None)
    assert listed == ["new_sec"]


def test_full_rebuild_records_and_restores_the_warehouse_settings_v002_resets():
    v002 = read("snowflake/migrations/V002__facts.sql")
    assert "ALTER WAREHOUSE WH_ALFA_ADMIN SET STATEMENT_TIMEOUT_IN_SECONDS = 300;" in v002   # still replayed
    fr = read("docs/FULL_REBUILD.md")
    step0 = _section(fr, "## 0. Decide what survives", "## 1. Backups")
    assert "SHOW PARAMETERS LIKE 'STATEMENT_TIMEOUT_IN_SECONDS' IN WAREHOUSE WH_ALFA_ADMIN;" in step0
    assert "SHOW WAREHOUSES LIKE 'WH_ALFA_ADMIN';" in step0
    step3 = _section(fr, "## 3. Migrations", "## 3b.")
    assert "ALTER WAREHOUSE WH_ALFA_ADMIN SET RESOURCE_MONITOR = NULL;" in step3
    assert "DROP RESOURCE MONITOR IF EXISTS OVERWATCH_RM;" in step3
    step3b = _section(fr, "## 3b.", "## 4. Grants")
    assert "ALTER WAREHOUSE WH_ALFA_ADMIN SET STATEMENT_TIMEOUT_IN_SECONDS = <step-0 value>;" in step3b
    # the per-file path creates the shim roles itself (rebuild/02 does it in its header)
    shim = re.findall(r"^CREATE ROLE IF NOT EXISTS (\w+);$", _preamble(), re.M)
    for role in shim:
        assert f"CREATE ROLE IF NOT EXISTS {role};" in step3, role


def _replayed_warehouse_settings() -> set[str]:
    """Every WH_ALFA_ADMIN setting a top-level ALTER WAREHOUSE in the chain changes on replay."""
    smoke = _smoke()
    return {m.group(1)
            for path in sorted((_SF / "migrations").glob("V[0-9]*.sql"))
            for _start, _end, code in smoke.statements(path.read_text(encoding="utf-8"))
            if (m := re.match(r"ALTER WAREHOUSE (?:IF EXISTS )?WH_ALFA_ADMIN SET (\w+)", code))}


def test_full_rebuild_puts_back_every_warehouse_setting_the_replay_changes():
    """Holistic #16: V002 swaps any attached resource monitor for OVERWATCH_RM and V045 sets it to NULL,
    so the replay detaches whatever monitor WH_ALFA_ADMIN had. Step 0 recorded only the timeout; now it
    records every setting the replay changes, and step 3b puts each one back (the monitor only on the
    owner's yes: the owner decision is no monitor on WH_ALFA_ADMIN)."""
    settings = _replayed_warehouse_settings()
    assert {"STATEMENT_TIMEOUT_IN_SECONDS", "RESOURCE_MONITOR"} <= settings, settings     # not vacuous
    fr = read("docs/FULL_REBUILD.md")
    step0 = _section(fr, "## 0. Decide what survives", "## 1. Backups")
    stmts = _step3b_statements(fr)
    for setting in settings:
        assert setting.lower() in step0.lower(), f"step 0 does not record {setting}"
        assert any(s.startswith(f"ALTER WAREHOUSE WH_ALFA_ADMIN SET {setting} = <step-0 ") for s in stmts), setting
    flat0 = " ".join(step0.split())
    assert "the replay detaches any monitor" in flat0 and "ask the owner" in flat0 and "OVERWATCH_RM" in flat0
    step3b = " ".join(_section(fr, "## 3b.", "## 4. Grants").split())
    assert "only on the owner's yes" in step3b
    # the generated README notes and the 02 header say the same
    readme = " ".join((_RB / "README.md").read_text(encoding="utf-8").split())
    assert "resource_monitor" in readme
    assert "the replay detaches any monitor" in _prose(_preamble())


def test_teardown_restore_notes_and_runbook_name_the_replay_steps():
    td = read("snowflake/teardown.sql")
    restore = " ".join(_section(td, "-- RESTORE", "-- Run as the deployment role").replace("--", " ").split())
    assert "replay shim" in restore and "FULL_REBUILD.md step 3b" in restore
    assert "STATEMENT_TIMEOUT_IN_SECONDS" in restore and "resource monitor" in restore
    dr = " ".join(_section(read("RUNBOOK.md"), "## 16. Disaster recovery", "## 17. Glossary").split())
    assert "CREATE ROLE IF NOT EXISTS OVERWATCH_MONITOR" in dr
    # p608 review: the schema-gone replay runs V002 too, and WH_ALFA_ADMIN is account-level, so the
    # live timeout is reset to 300 and the SUSPEND monitor attached on that path as well
    schema_gone = dr[dr.index("**Schema gone:**"):dr.index("**Bad deploy:**")]
    assert "SHOW PARAMETERS LIKE 'STATEMENT_TIMEOUT_IN_SECONDS' IN WAREHOUSE WH_ALFA_ADMIN;" in schema_gone
    assert "ALTER WAREHOUSE WH_ALFA_ADMIN SET STATEMENT_TIMEOUT_IN_SECONDS = <value>;" in schema_gone
    assert "ALTER WAREHOUSE WH_ALFA_ADMIN SET RESOURCE_MONITOR = NULL;" in schema_gone
    assert "DROP RESOURCE MONITOR IF EXISTS OVERWATCH_RM;" in schema_gone
    # holistic #16: the replay detaches any monitor, so this path records it too and re-attaches only
    # on the owner's yes
    assert "SHOW WAREHOUSES LIKE 'WH_ALFA_ADMIN';" in schema_gone
    assert "ALTER WAREHOUSE WH_ALFA_ADMIN SET RESOURCE_MONITOR = <monitor>;" in schema_gone
    assert "ask the owner" in schema_gone
    # ...and the RUNBOOK's numbers are V002's
    v002 = read("snowflake/migrations/V002__facts.sql")
    assert "SET STATEMENT_TIMEOUT_IN_SECONDS = 300;" in v002 and "back to 300" in schema_gone
    assert "CREDIT_QUOTA = 30" in v002 and "ON 100 PERCENT DO SUSPEND;" in v002
    assert "30 credits a month, SUSPEND at 100%" in schema_gone
