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


def test_no_test_pins_the_tip_or_the_version_again():
    """Asserts and assignments only -- a comment or docstring may still NAME a version.
    Regex-prefiltered so only the few files that mention a tip/version at all are parsed."""
    offenders = []
    for p in sorted((ROOT / "tests").rglob("test_*.py")):
        text = p.read_text(encoding="utf-8")
        if p.name == "test_release_lockstep.py" or not (_LITERAL_TIP.search(text) or _LITERAL_VERSION.search(text)):
            continue
        lines = text.splitlines()
        for node in ast.walk(ast.parse(text)):
            if isinstance(node, (ast.Assert, ast.Assign, ast.AnnAssign)):
                seg = "\n".join(lines[node.lineno - 1:node.end_lineno])
                if _LITERAL_TIP.search(seg) or _LITERAL_VERSION.search(seg):
                    offenders.append(f"{p.relative_to(ROOT).as_posix()}:{node.lineno}")
    assert not offenders, (
        "derive the migration tip / APP_VERSION (tests/_source.py, tests/test_release_lockstep.py) "
        f"instead of pinning it: {offenders}")


def test_page_source_covers_every_parts_package():
    owned = set(PAGE_PARTS.values())
    on_disk = {p.name for p in PAGES.iterdir() if p.is_dir() and p.name.endswith("_parts")}
    assert owned == on_disk, f"claim new parts packages in tests/_source.py PAGE_PARTS: {on_disk - owned}"
    for page, parts in PAGE_PARTS.items():
        files = page_files(page)
        assert files[0].name == f"{page}.py"
        assert {p.name for p in files[1:]} == {
            p.name for p in (PAGES / parts).glob("*.py") if p.name != "__init__.py"}
