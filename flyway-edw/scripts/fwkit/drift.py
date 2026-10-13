"""Out-of-band change (drift) detection that does not depend on preview features, plus rehearsal-clone SQL.

``drift_sql`` builds one INFORMATION_SCHEMA query for a runway database: every table/view whose last DDL, and every
function/procedure whose last change, is newer than the last successful Flyway migration recorded in
``FLYWAY."flyway_schema_history"``. INFORMATION_SCHEMA is real-time and per database (ACCOUNT_USAGE lags ~45 min).
``evaluate`` applies the runway's policy from runways.toml: ignored schemas / name patterns / allowed users, then
"warn" (report, exit 0) or "fail" (exit 1 unless an approver acknowledged it with ACK_DRIFT).

Limits, stated in docs/SHORTCOMINGS.md: tasks, streams, pipes and alerts expose no last-DDL time in
INFORMATION_SCHEMA, so they are covered only by Flyway Enterprise ``check -drift`` (preview on Snowflake) when
enabled; LAST_DDL_BY is the user that ran the DDL (verify on the first run in your account).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .manifest import REHEARSAL_PREFIX, Kit, Runway

_IDENT_OK = re.compile(r"^[A-Z][A-Z0-9_]*$")


class DriftError(Exception):
    pass


def _ident(name: str) -> str:
    if not _IDENT_OK.match(name):
        raise DriftError(f"refusing identifier {name!r}: expected an unquoted upper-case Snowflake name")
    return name


def drift_sql(kit: Kit, rw: Runway) -> str:
    db = _ident(rw.database)
    hs = _ident(kit.history_schema)
    return f"""-- drift candidates for {db}: DDL newer than the last successful Flyway migration
WITH last_deploy AS (
    SELECT COALESCE(MAX(h."installed_on"), TO_TIMESTAMP_LTZ('1970-01-01 00:00:00')) AS ts
    FROM {db}.{hs}."flyway_schema_history" h
    WHERE h."success"
)
SELECT 'TABLE' AS OBJECT_KIND, t.TABLE_SCHEMA AS SCHEMA_NAME, t.TABLE_NAME AS OBJECT_NAME,
       t.TABLE_TYPE AS DETAIL, t.LAST_DDL AS CHANGED_AT, t.LAST_DDL_BY AS CHANGED_BY, d.ts AS LAST_DEPLOY
FROM {db}.INFORMATION_SCHEMA.TABLES t CROSS JOIN last_deploy d
WHERE t.LAST_DDL > d.ts
UNION ALL
SELECT 'FUNCTION', f.FUNCTION_SCHEMA, f.FUNCTION_NAME || f.ARGUMENT_SIGNATURE, f.FUNCTION_LANGUAGE,
       f.LAST_ALTERED, NULL, d.ts
FROM {db}.INFORMATION_SCHEMA.FUNCTIONS f CROSS JOIN last_deploy d
WHERE f.LAST_ALTERED > d.ts
UNION ALL
SELECT 'PROCEDURE', p.PROCEDURE_SCHEMA, p.PROCEDURE_NAME || p.ARGUMENT_SIGNATURE, p.PROCEDURE_LANGUAGE,
       p.LAST_ALTERED, NULL, d.ts
FROM {db}.INFORMATION_SCHEMA.PROCEDURES p CROSS JOIN last_deploy d
WHERE p.LAST_ALTERED > d.ts
ORDER BY 5 DESC;
"""


@dataclass(frozen=True)
class DriftRow:
    kind: str
    schema: str
    name: str
    changed_at: str
    changed_by: str

    def render(self) -> str:
        who = self.changed_by or "unknown"
        return f"{self.kind:<9} {self.schema}.{self.name}  changed {self.changed_at} by {who}"


def load_rows(path: Path) -> list[dict[str, Any]]:
    """Accept the Snowflake CLI's JSON (a list of row objects, or a list with one such list per statement)."""
    data = json.loads(path.read_text(encoding="utf-8") or "[]")
    if isinstance(data, list) and data and all(isinstance(x, list) for x in data):
        data = data[-1]
    if not isinstance(data, list):
        raise DriftError(f"{path}: expected a JSON list of rows")
    return [r for r in data if isinstance(r, dict)]


def evaluate(kit: Kit, rw: Runway, rows: list[dict[str, Any]]) -> list[DriftRow]:
    cfg = kit.drift
    ignore_schemas = {s.upper() for s in cfg.get("ignore_schemas", [])}
    patterns = [re.compile(p) for p in cfg.get("ignore_name_patterns", [])]
    allowed = {u.upper() for u in cfg.get("allowed_ddl_users", [])} | {rw.service_user.upper()}
    out: list[DriftRow] = []
    for r in rows:
        u = {str(k).upper(): ("" if v is None else str(v)) for k, v in r.items()}
        schema, name = u.get("SCHEMA_NAME", ""), u.get("OBJECT_NAME", "")
        if schema.upper() in ignore_schemas or any(p.search(name.upper()) for p in patterns):
            continue
        if u.get("CHANGED_BY", "").upper() in allowed:
            continue
        out.append(DriftRow(u.get("OBJECT_KIND", ""), schema, name, u.get("CHANGED_AT", ""), u.get("CHANGED_BY", "")))
    return out


def rehearsal_sql(kit: Kit, rw: Runway, action: str) -> str:
    """Exact SQL to create or drop the zero-copy rehearsal clone ALFA_EDW_RH_<ID>. Identifiers are validated so the
    pipeline can never be pointed at a real runway database for the DROP."""
    if not rw.rehearsal:
        raise DriftError(f"runway {rw.id} does not rehearse (runways.toml rehearsal = false)")
    target = _ident(rw.rehearsal_db)
    source = _ident(rw.database)
    if not target.startswith(REHEARSAL_PREFIX) or target == source or target in {r.database for r in kit.runways.values()}:
        raise DriftError(f"refusing rehearsal database name {target!r}")
    if action == "create":
        return (
            f"-- rehearsal for {rw.id}: zero-copy clone, run as {rw.deploy_role} (it owns the tasks/pipes/streams,\n"
            "-- which a clone only includes for their owner). Cloned tasks, alerts, dynamic tables and pipes arrive\n"
            "-- suspended; hybrid tables cannot be cloned and are skipped.\n"
            f"CREATE OR REPLACE DATABASE {target} CLONE {source} IGNORE HYBRID TABLES;\n"
            f"COMMENT ON DATABASE {target} IS 'Flyway rehearsal clone of {source}; dropped by the deploy job';\n"
        )
    if action == "drop":
        return f"DROP DATABASE IF EXISTS {target};\n"
    raise DriftError(f"unknown action {action!r} (create|drop)")
