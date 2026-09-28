"""Release lockstep, derived -- never a literal tip (Next-Fifty #49).

The migration tip and APP_VERSION used to be hardcoded in ~50 per-migration tests, so every
migration or version bump edited ~52 test files (V160 and V161 each did). These locks DERIVE the
tip from snowflake/migrations and the version from CHANGELOG.md, so a new migration or release
touches only the files it genuinely changes. Per-migration tests lock their own content and their
own run-doc line; they must not pin the tip or APP_VERSION again (the last test fails if they do).
Admin's _EXPECTED_MIGRATIONS == the migrations directory is already derived in
tests/test_perf_budgets.py, so it is not repeated here.
"""

from __future__ import annotations

import ast
import re

from tests._source import PAGE_PARTS, PAGES, ROOT, changelog_entry, migration_tip, page_files, read

_MIGS = sorted((ROOT / "snowflake" / "migrations").glob("V[0-9]*__*.sql"))
# a literal tip: validate's header label or count check, the rebuild bundle name, or a module-level _TIP
_LITERAL_TIP = re.compile(
    r"V001\.\.V\d{3} applied|BETWEEN 1 AND \d+\) = \d+|02_migrations_V001_V\d{3}\b|^_TIP\s*(?::\s*int\s*)?=\s*\d+",
    re.M)
# a literal APP_VERSION, as the config.py line ('APP_VERSION = "4.598.0"') or a comparison (APP_VERSION == "...")
_LITERAL_VERSION = re.compile(r"APP_VERSION\s*==?\s*\\?[\"']\d+\.\d+\.\d+")


def test_migrations_are_contiguous_from_v001():
    # validate.sql's COUNT(DISTINCT VERSION) BETWEEN 1 AND N = N only means "all applied" when 1..N
    nums = sorted(int(re.match(r"V(\d+)__", p.name).group(1)) for p in _MIGS)
    assert nums == list(range(1, len(nums) + 1)), "snowflake/migrations must be V001..VNNN with no gap"
    assert nums[-1] == migration_tip()


def test_validate_pins_the_migration_tip_in_both_literals():
    """The header row's label AND its count check both equal the tip, in validate.sql and in the
    generated rebuild copy (test_rebuild_bundle also byte-compares the two). Anchored on the SELECT
    so a comment that NAMES an older replay point ('V001..V161 applied') is not the pin."""
    tip = migration_tip()
    for rel in ("snowflake/validate.sql", "snowflake/rebuild/05_validate.sql"):
        text = read(rel)
        labels = re.findall(r"SELECT 'V001\.\.V(\d{3}) applied' AS CHECK_NAME", text)
        assert labels == [f"{tip:03d}"], f"{rel}: header label {labels} != V001..V{tip:03d}"
        counts = re.findall(r"WHERE VERSION BETWEEN 1 AND (\d+)\) = (\d+),", text)
        assert counts == [(str(tip), str(tip))], f"{rel}: count check {counts} != 1..{tip} = {tip}"


def test_every_migration_is_listed_in_the_run_docs():
    for rel in ("DEPLOYMENT.md", "README.md"):
        text = read(rel)
        missing = [p.name for p in _MIGS if f"snowflake/migrations/{p.name}" not in text]
        assert not missing, f"{rel} run-list lacks {missing}"


def test_the_rebuild_bundle_is_named_for_the_tip_only():
    names = sorted(p.name for p in (ROOT / "snowflake" / "rebuild").glob("02_migrations_*.sql"))
    assert names == [f"02_migrations_V001_V{migration_tip():03d}.sql"], names


def test_app_version_is_the_top_changelog_entry():
    from app.config import APP_VERSION
    changelog = read("CHANGELOG.md")
    # every numbered heading, whichever separator it uses (older ones use an em-dash)
    versions = re.findall(r"^## (\d+\.\d+\.\d+)\b", changelog, re.M)
    assert versions and versions[0] == APP_VERSION, (
        f"app/config.py APP_VERSION={APP_VERSION} but the top CHANGELOG entry is {versions[:1]}")
    keyed = [tuple(map(int, v.split("."))) for v in versions]
    assert len(set(keyed)) == len(keyed) and keyed == sorted(keyed, reverse=True), (
        "CHANGELOG version headings must be unique and newest-first")
    top = changelog.split("\n## ", 2)[1].splitlines()[0]
    assert re.fullmatch(rf"{re.escape(APP_VERSION)} - .+ \(\d{{4}}-\d{{2}}-\d{{2}}\)", top), top
    assert changelog_entry(APP_VERSION).startswith(f"## {APP_VERSION} - ")
    # the helper also finds an em-dash-era entry by its heading
    old = re.search(r"^## (\d+\.\d+\.\d+) — ", changelog, re.M)
    assert old and changelog_entry(old.group(1)).startswith(f"## {old.group(1)} — ")


def _live_needles() -> tuple[str, ...]:
    """The CURRENT release values -- only a pin equal to these breaks on the next migration or bump."""
    from app.config import APP_VERSION
    tip = migration_tip()
    return (APP_VERSION, f"V001..V{tip:03d}", f"BETWEEN 1 AND {tip}", f"_V001_V{tip:03d}")


def _string_constants(tree: ast.AST):
    """Every str constant in the module (f-string parts included) except docstrings and the literal
    argument of changelog_entry(...), which names a fixed release by design."""
    skip: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                skip.add(id(body[0].value))
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "changelog_entry"):
            skip.update(id(a) for a in node.args)
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in skip:
            yield node


def _pins_in(text: str, needles: tuple[str, ...]) -> list[int]:
    """Line numbers of release pins in one test file's source: (a) any string constant, in any statement
    shape (assert, loop tuple, call argument, reversed compare, startswith), containing a CURRENT value;
    (b) the legacy literal shapes (any number) in an assert or assignment."""
    tree = ast.parse(text)
    found = {node.lineno for node in _string_constants(tree) if any(n in node.value for n in needles)}
    if _LITERAL_TIP.search(text) or _LITERAL_VERSION.search(text):
        lines = text.splitlines()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Assert, ast.Assign, ast.AnnAssign)):
                seg = "\n".join(lines[node.lineno - 1:node.end_lineno])
                if _LITERAL_TIP.search(seg) or _LITERAL_VERSION.search(seg):
                    found.add(node.lineno)
    return sorted(found)


def test_no_test_pins_the_tip_or_the_version_again():
    """A comment or docstring may still NAME a version. Prefiltered on the raw text, so only the few
    files that mention a current value or a legacy pin shape at all are parsed (review r1: the first
    guard checked only two regexes inside asserts and missed loops, startswith and reversed compares)."""
    needles = _live_needles()
    offenders = []
    for p in sorted((ROOT / "tests").rglob("test_*.py")):
        text = p.read_text(encoding="utf-8")
        if p.name == "test_release_lockstep.py":
            continue
        if not (any(n in text for n in needles) or _LITERAL_TIP.search(text) or _LITERAL_VERSION.search(text)):
            continue
        offenders += [f"{p.relative_to(ROOT).as_posix()}:{n}" for n in _pins_in(text, needles)]
    assert not offenders, (
        "derive the migration tip / APP_VERSION (tests/_source.py, tests/test_release_lockstep.py) "
        f"instead of pinning it: {offenders}")


def test_the_pin_guard_catches_every_pin_shape():
    """Negative control (review r1): the shapes a hand-written pin actually takes, all flagged; names in
    docstrings, comments and changelog_entry("x") arguments are not."""
    needles = ("4.599.0", "V001..V161", "BETWEEN 1 AND 161", "_V001_V161")
    shapes = [
        'assert "V001..V161 applied" in val',
        'head = read("CHANGELOG.md")\nassert head.startswith("4.599.0 - Something")',
        'for needle in ("V001..V161 applied", "BETWEEN 1 AND 161) = 161"):\n    assert needle in val',
        'assert "4.599.0" == APP_VERSION',
        'assert \'APP_VERSION = "4.599.0"\' in read("app/config.py")',
        'assert (ROOT / "snowflake/rebuild/02_migrations_V001_V161.sql").exists()',
        'x = check("VERSION BETWEEN 1 AND 161")',
        '_TIP = 160',
    ]
    for src in shapes:
        assert _pins_in(src, needles), src
    ok = ['"""Mentions 4.599.0 and V001..V161 in a docstring."""\nx = 1',
          'def f():\n    """V001..V161 applied, as history."""\n    return changelog_entry("4.599.0")',
          '# assert "V001..V161 applied" in val\nx = 1']
    for src in ok:
        assert not _pins_in(src, needles), src


def test_page_source_covers_every_parts_package():
    owned = set(PAGE_PARTS.values())
    on_disk = {p.name for p in PAGES.iterdir() if p.is_dir() and p.name.endswith("_parts")}
    assert owned == on_disk, f"claim new parts packages in tests/_source.py PAGE_PARTS: {on_disk - owned}"
    for page, parts in PAGE_PARTS.items():
        files = page_files(page)
        assert files[0].name == f"{page}.py"
        assert {p.name for p in files[1:]} == {
            p.name for p in (PAGES / parts).glob("*.py") if p.name != "__init__.py"}
