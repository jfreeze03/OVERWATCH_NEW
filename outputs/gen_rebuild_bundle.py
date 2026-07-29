#!/usr/bin/env python3
"""Regenerate snowflake/rebuild/ from its sources (locked by tests/test_rebuild_bundle.py).

The rebuild bundle is GENERATED, never hand-edited:
  - 01/03/04/05 are byte-identical copies of their snowflake/ sources
    (a numbered header, a blank line, then the source verbatim).
  - 02_migrations_V001_V0NN.sql is the ordered byte-concatenation of every
    migration, each under a `-- >>> <name>` banner.
  - 00 (operator backup) and README.md are maintained separately.

Run after adding a migration or editing teardown/roles/backfill/validate:
    python outputs/gen_rebuild_bundle.py
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SF = ROOT / "snowflake"
MIG = SF / "migrations"
RB = SF / "rebuild"
SEP = "-- " + "=" * 75

COPIES = {
    "01_teardown_rebuildables.sql": ("teardown.sql",
        "-- 01_teardown_rebuildables.sql — BYTE-IDENTICAL copy of snowflake/\n"
        "-- teardown.sql (locked by tests/test_rebuild_bundle.py). Section A runs;\n"
        "-- Sections B/C stay commented — operator data survives."),
    "03_roles.sql": ("roles.sql",
        "-- 03_roles.sql — BYTE-IDENTICAL copy of snowflake/roles.sql (locked by\n"
        "-- tests/test_rebuild_bundle.py); numbered for the rebuild order."),
    "04_backfill_365.sql": ("backfill_365.sql",
        "-- 04_backfill_365.sql — BYTE-IDENTICAL copy of snowflake/backfill_365.sql (locked by\n"
        "-- tests/test_rebuild_bundle.py); numbered for the rebuild order."),
    "05_validate.sql": ("validate.sql",
        "-- 05_validate.sql — BYTE-IDENTICAL copy of snowflake/validate.sql (locked by\n"
        "-- tests/test_rebuild_bundle.py); numbered for the rebuild order."),
}


def write_copies() -> None:
    for name, (src, header) in COPIES.items():
        (RB / name).write_text(header + "\n\n" + (SF / src).read_text(encoding="utf-8"),
                               encoding="utf-8")


# Operator hint (editorial, not byte-locked): the heavy migrations that end with
# a first-fill CALL and so pause for a minute+ during Run All. Extend when a new
# migration adds a heavy first-fill (V054 repopulates the exec board).
CALL_NOTE = ("V027/V029/V030/V031/V041/V042/V043/V044/V045/V048/V049/V050/V051/"
             "V052/V053/V054/V055")  # V056 is proc-swaps only, no first-fill CALL


def write_migrations_bundle() -> tuple[str, int]:
    migs = sorted(MIG.glob("V0*.sql"))
    maxv = max(int(re.match(r"V(\d+)", m.name).group(1)) for m in migs)
    fname = f"02_migrations_V001_V{maxv:03d}.sql"
    header = (
        f"-- {fname} — GENERATED: the {len(migs)} migration files,\n"
        "-- byte-concatenated in order (locked by tests/test_rebuild_bundle.py).\n"
        "-- Snowsight 'Run All' executes top to bottom and HALTS at the first\n"
        "-- error — exactly the rule from docs/FULL_REBUILD.md. If it halts,\n"
        "-- read the failing statement, fix, and resume FROM THAT FILE's banner;\n"
        "-- everything is idempotent, so re-running a completed file is safe.\n"
        f"-- Expect several minutes total: {CALL_NOTE} end\n"
        "-- with first-fill CALLs.\n"
    )
    blocks = [f"{SEP}\n-- >>> {m.name}\n{SEP}\n{m.read_text(encoding='utf-8').rstrip(chr(10))}"
              for m in migs]
    text = header + "\n" + "\n".join(blocks) + "\n"
    for old in RB.glob("02_migrations_V001_V*.sql"):
        if old.name != fname:
            old.unlink()
    (RB / fname).write_text(text, encoding="utf-8")
    return fname, len(migs)


def write_readme_row(fname: str, n: int) -> None:
    """Keep the README's step-02 row in sync with the bundle (it drifted V042 ->
    V054 -> V055 by hand). Only the `| 02 | ... |` row is generated; the rest of
    the README is authored."""
    readme = RB / "README.md"
    text = readme.read_text(encoding="utf-8")
    new = re.sub(
        r"\| 02 \| 02_migrations_V001_V\d+\.sql \| all \d+ migrations,",
        f"| 02 | {fname} | all {n} migrations,", text, count=1)
    if new != text:
        readme.write_text(new, encoding="utf-8")


if __name__ == "__main__":
    write_copies()
    fname, n = write_migrations_bundle()
    write_readme_row(fname, n)
    print(f"regenerated rebuild bundle: {fname} ({n} migrations) + 4 copies + README row")
