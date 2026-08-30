"""Regenerate the Snowflake full-rebuild bundle from its source files."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SNOWFLAKE = ROOT / "snowflake"
REBUILD = SNOWFLAKE / "rebuild"
UNDERLINE = "-- ==========================================================================="


def _copy_body(bundle_name: str, source_name: str) -> None:
    bundle = REBUILD / bundle_name
    header = bundle.read_text(encoding="utf-8").split("\n\n", 1)[0]
    source = (SNOWFLAKE / source_name).read_text(encoding="utf-8")
    bundle.write_text(f"{header}\n\n{source}", encoding="utf-8")


def _migration_header(previous: Path, maximum: int, count: int) -> str:
    text = previous.read_text(encoding="utf-8")
    marker = f"{UNDERLINE}\n-- >>> V001__core.sql\n{UNDERLINE}\n"
    header = text[: text.index(marker)]
    header = re.sub(r"V001_V\d{3}", f"V001_V{maximum:03d}", header)
    header = re.sub(r"the \d+ migration files", f"the {count} migration files", header)
    return header


def main() -> None:
    migrations = sorted((SNOWFLAKE / "migrations").glob("V[0-9]*.sql"))
    maximum = max(int(re.match(r"V(\d+)", path.name).group(1)) for path in migrations)
    candidates = sorted(REBUILD.glob("02_migrations_V001_V*.sql"))
    previous = candidates[-1]
    target = REBUILD / f"02_migrations_V001_V{maximum:03d}.sql"
    parts = [
        (
            f"{UNDERLINE}\n-- >>> {migration.name}\n{UNDERLINE}\n"
            f"{migration.read_text(encoding='utf-8')}"
        )
        for migration in migrations
    ]
    target.write_text(
        _migration_header(previous, maximum, len(migrations)) + "\n".join(parts),
        encoding="utf-8",
    )
    for stale in candidates:
        if stale != target:
            stale.unlink()

    for bundle, source in (
        ("01_teardown_rebuildables.sql", "teardown.sql"),
        ("03_roles.sql", "roles.sql"),
        ("04_backfill_365.sql", "backfill_365.sql"),
        ("05_validate.sql", "validate.sql"),
    ):
        _copy_body(bundle, source)


if __name__ == "__main__":
    main()
