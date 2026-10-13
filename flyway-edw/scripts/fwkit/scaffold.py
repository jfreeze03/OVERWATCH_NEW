"""``fw.py new``: create a correctly named migration file with the house header, so naming is never hand-typed.

Versioned files get a UTC timestamp version. Versions are global across every schema folder, so two migrations
created in the same minute would collide: `new` adds a .1, .2 ... suffix against every folder, and the guard
(FG002) catches a collision with a file on another branch. Repeatable files get a template for their layer
that already satisfies the guard (schema-qualified, COPY GRANTS, task gate + resume).
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path

from .guard import LAYERS, SCHEMA_RE, version_key

DESC_RE = re.compile(r"^[a-z0-9]+(?:_[a-z0-9]+)*$")

HEADER = """-- {kind}: {schema}.{subject}
-- Ticket : <JIRA-KEY>          Author: <name>
-- Why    : <one or two lines: what this changes and why>
-- Rules  : docs/CONVENTIONS.md. Schema-qualify every object (SCHEMA.OBJECT); never name the database;
--          wrap Snowflake Scripting in EXECUTE IMMEDIATE $$ ... $$; run `python3 scripts/fw.py guard` before the PR.
"""

TEMPLATES: dict[int, str] = {
    10: "CREATE SEQUENCE IF NOT EXISTS {schema}.{obj_upper} START = 1 INCREMENT = 1;\n",
    20: (
        "CREATE OR REPLACE FUNCTION {schema}.{obj_upper}(P_VALUE NUMBER(38, 2))\n"
        "    COPY GRANTS\n    RETURNS NUMBER(38, 2)\n    LANGUAGE SQL\n"
        "    COMMENT = '<what it returns>'\nAS\n$$\n    P_VALUE\n$$;\n"
    ),
    30: (
        "CREATE OR REPLACE VIEW {schema}.{obj_upper}\n    COPY GRANTS\n    COMMENT = '<what the view is for>'\nAS\n"
        "SELECT\n    <column_list>                -- list columns; no SELECT *\nFROM {schema}.<TABLE>;\n"
    ),
    40: (
        "CREATE OR ALTER DYNAMIC TABLE {schema}.{obj_upper}\n    TARGET_LAG = '1 hour'\n"
        "    WAREHOUSE = ${{etl_wh}}\nAS\nSELECT <column_list>\nFROM {schema}.<TABLE>;\n"
    ),
    50: (
        "CREATE OR REPLACE PROCEDURE {schema}.{obj_upper}()\n    COPY GRANTS\n    RETURNS VARCHAR\n"
        "    LANGUAGE SQL\n    EXECUTE AS OWNER\nAS\n$$\nBEGIN\n    -- schema-qualified names only\n"
        "    RETURN 'ok';\nEND;\n$$;\n"
    ),
    70: "CREATE OR REPLACE STREAM {schema}.{obj_upper}\n    COPY GRANTS\n    ON TABLE {schema}.<TABLE>;\n",
    75: (
        "CREATE PIPE IF NOT EXISTS {schema}.{obj_upper}\n    AUTO_INGEST = FALSE\nAS\n"
        "COPY INTO <TABLE>                        -- bare table name: a cloned pipe must load its own table\n"
        "FROM @{schema}.<STAGE>\nFILE_FORMAT = (FORMAT_NAME = '{schema}.<FILE_FORMAT>');\n"
    ),
    80: (
        "-- One file per task graph, named for its root. Suspend the root before changing any task in the graph.\n"
        "ALTER TASK IF EXISTS {schema}.{obj_upper} SUSPEND;\n\n"
        "CREATE OR ALTER TASK {schema}.{obj_upper}\n    WAREHOUSE = ${{etl_wh}}\n"
        "    SCHEDULE = 'USING CRON 0 6 * * * America/Chicago'\nAS\n    CALL {schema}.<PROCEDURE>();\n\n"
        "-- Resume the whole graph only where this runway runs tasks (flyway.toml tasks_enabled).\n"
        "EXECUTE IMMEDIATE $$\nBEGIN\n    IF ('${{tasks_enabled}}' = 'true') THEN\n"
        "        LET r VARCHAR := (SELECT SYSTEM$TASK_DEPENDENTS_ENABLE('{schema}.{obj_upper}'));\n"
        "    END IF;\nEND;\n$$;\n"
    ),
    85: (
        "-- Wrapped in EXECUTE IMMEDIATE $$ ... $$: Flyway's Snowflake parser opens a block at the alert's own IF\n"
        "-- (ALERT is not one of its creatable object types) and would report 'Incomplete statement' (guard FG015).\n"
        "EXECUTE IMMEDIATE $$\nCREATE OR REPLACE ALERT {schema}.{obj_upper}\n    WAREHOUSE = ${{etl_wh}}\n"
        "    SCHEDULE = '60 MINUTE'\n    IF (EXISTS (SELECT 1 FROM {schema}.<TABLE> WHERE <condition>))\n"
        "    THEN CALL {schema}.<PROCEDURE>()\n$$;\n\n"
        "EXECUTE IMMEDIATE $$\nBEGIN\n    IF ('${{tasks_enabled}}' = 'true') THEN\n"
        "        ALTER ALERT {schema}.{obj_upper} RESUME;\n    END IF;\nEND;\n$$;\n"
    ),
    90: (
        "GRANT USAGE ON SCHEMA {schema} TO ROLE ${{role_prefix}}_{schema}_READ;\n"
        "GRANT SELECT ON ALL VIEWS IN SCHEMA {schema} TO ROLE ${{role_prefix}}_{schema}_READ;\n"
        "GRANT SELECT ON FUTURE VIEWS IN SCHEMA {schema} TO ROLE ${{role_prefix}}_{schema}_READ;\n"
    ),
}

VERSIONED_BODY = (
    "-- Tables and data: ALTER TABLE ... ADD COLUMN IF NOT EXISTS, CREATE TABLE IF NOT EXISTS, idempotent MERGE.\n"
    "-- Never CREATE OR REPLACE TABLE (guard FG006). Keep one logical change per file: Snowflake DDL autocommits.\n"
    "CREATE TABLE IF NOT EXISTS {schema}.<TABLE_NAME> (\n    ID NUMBER(38, 0) NOT NULL,\n"
    "    LOAD_TS TIMESTAMP_LTZ NOT NULL DEFAULT CURRENT_TIMESTAMP()\n);\n"
)
UNDO_BODY = (
    "-- FLYWAY-WAIVER: FG006 <JIRA-KEY> undo of {version}: reverses the versioned migration with the same version\n"
    "-- Undo scripts run with `flyway undo` (Enterprise). Make them idempotent (IF EXISTS).\n"
)


class ScaffoldError(Exception):
    pass


def version_for(at: datetime) -> str:
    at = at.astimezone(UTC)
    return f"{at:%Y.%m.%d.%H%M}"


def new_versioned(root: Path, schema: str, desc: str, *, at: datetime | None = None, undo: bool = False) -> list[Path]:
    schema = schema.upper()
    if not SCHEMA_RE.match(schema):
        raise ScaffoldError(f"schema {schema!r} must be an upper-case Snowflake schema name")
    desc = desc.lower()
    if not DESC_RE.match(desc):
        raise ScaffoldError("desc must be lower_snake_case (letters, digits, underscores)")
    full_desc = desc if desc.startswith(schema.lower() + "_") else f"{schema.lower()}_{desc}"
    version = version_for(at or datetime.now(UTC))
    folder = root / "migrations" / schema
    taken = {version_key(m.group(0)[1:]) for p in (root / "migrations").glob("*/[VU]*.sql")
             if (m := re.match(r"^[VU][0-9.]+(?=__)", p.name))}
    seq = 0
    while True:
        v = version if seq == 0 else f"{version}.{seq}"
        path = folder / f"V{v}__{full_desc}.sql"
        if version_key(v) not in taken:
            break
        seq += 1
    folder.mkdir(parents=True, exist_ok=True)
    head = HEADER.format(kind="Versioned migration", schema=schema, subject=full_desc)
    path.write_text(head + "\n" + VERSIONED_BODY.format(schema=schema), encoding="utf-8")
    out = [path]
    if undo:
        upath = folder / f"U{v}__{full_desc}.sql"
        upath.write_text(HEADER.format(kind="Undo migration", schema=schema, subject=full_desc) + "\n"
                         + UNDO_BODY.format(version=v), encoding="utf-8")
        out.append(upath)
    return out


def new_repeatable(root: Path, schema: str, layer: int, obj: str) -> Path:
    schema = schema.upper()
    if not SCHEMA_RE.match(schema):
        raise ScaffoldError(f"schema {schema!r} must be an upper-case Snowflake schema name")
    obj = obj.lower()
    if not DESC_RE.match(obj):
        raise ScaffoldError("object must be lower_snake_case")
    band = layer // 10 * 10
    if not 10 <= layer <= 99 or band not in LAYERS:
        raise ScaffoldError(f"layer must be 10-99 in one of the bands {sorted(LAYERS)}")
    path = root / "repeatable" / schema / f"R__{layer:02d}_{schema}__{obj}.sql"
    if path.exists():
        raise ScaffoldError(f"{path} already exists: edit it in place (repeatables are state, not history)")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmpl = TEMPLATES.get(layer, TEMPLATES.get(band, ""))
    head = HEADER.format(kind=f"Repeatable (layer {layer}: {LAYERS[band]})", schema=schema, subject=obj.upper())
    path.write_text(head + "\n" + tmpl.format(schema=schema, obj_upper=obj.upper()), encoding="utf-8")
    return path
