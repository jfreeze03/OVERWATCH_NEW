"""Deterministic remediation hints for query failures (pure, no AI, zero cost).

The "Failures by error" panel groups ACCOUNT_USAGE.QUERY_HISTORY failures by error family
(ERROR_CODE + a 140-char slice of ERROR_MESSAGE) — it shows the PATTERN but not the FIX. This
maps a family to a plain-English next step so each row is actionable. Mirrors the house
deterministic-advisor pattern of ``query_advisor.advise`` (pure, tested, no Cortex).

Keyed on the message SUBSTRING first (one ERROR_CODE like 002003 carries many distinct messages
— "current database", "Cortex Agent", "Schema … does not exist", "Database … does not exist"),
with ERROR_CODE as an optional extra gate. First match wins, so order most-specific first.
"""

from __future__ import annotations

# (error_code, message_substring_lower, fix). code "" matches any code; substring "" matches any
# message. Ordered specific -> general; the first row that matches both conditions wins.
_FIXES: tuple[tuple[str, str, str], ...] = (
    # --- session / deployment state (not a data or SQL problem) ---
    ("", "already a live version",
     "A Streamlit app or notebook has an uncommitted live edit — commit it (or discard the draft) "
     "before deploying/altering again."),
    ("", "does not have a current database",
     "The session has no current database — run USE DATABASE <db> (or set a default on the role) "
     "before the statement (e.g. SHOW GRANTS/SHOW TASKS need a current DB)."),
    ("", "no active warehouse",
     "No warehouse is set/running for the session — USE WAREHOUSE <wh> (and resume it), or set a "
     "default warehouse on the role."),
    ("", "statement or warehouse timeout",
     "The statement hit STATEMENT_TIMEOUT_IN_SECONDS — raise the timeout on the warehouse/session, "
     "or make the query cheaper (filter earlier, prune, avoid the exploding join)."),
    # --- Cortex / feature availability ---
    ("", "cortex agent",
     "A Cortex Agent is referenced by an empty/invalid name, or Cortex isn't granted to this role "
     "— check the agent name and the SNOWFLAKE.CORTEX privileges."),
    ("", "data_quality_monitoring",
     "A SNOWFLAKE.LOCAL data-quality view isn't available — the DMF/data-metric feature may be off, "
     "or the role lacks IMPORTED PRIVILEGES ON DATABASE SNOWFLAKE."),
    # R3: match CORTEX_CODE only as a QUOTED database identifier ('cortex_code'), not the bare
    # token — a bare "cortex_code" also matched existence/grant errors on OVERWATCH's own
    # ACCOUNT_USAGE.CORTEX_CODE_* views (fix_for is first-match-wins) and mis-routed them here
    # instead of to the generic grant fix below; the quotes distinguish the database from the views.
    ("", "'cortex_code'",
     "The CORTEX_CODE database isn't provisioned/visible for this role — enable Cortex Code or grant "
     "access; the tool is calling a database that doesn't exist here."),
    # --- missing object / privilege ---
    ("", "schema '",
     "The referenced schema is missing or invisible to the role — fully qualify it, create it, or "
     "GRANT USAGE on the schema."),
    ("", "database '",
     "The referenced database is missing or invisible to the role — check the name/qualification and "
     "GRANT/enable it for the role running this."),
    ("", "invalid identifier",
     "Unknown column/identifier — check the spelling and that the column exists on the referenced "
     "table/alias (a renamed or dropped column, or a missing alias)."),
    ("", "does not exist or not authorized",
     "The object was dropped/renamed, or the role can't see it — verify the name/qualification and "
     "the GRANT on the role running it."),
    ("", "object does not exist",
     "The object was dropped/renamed or the role lacks a grant — verify the fully-qualified name and "
     "the privileges."),
    # --- SQL text (never compiled — a code fix, not a warehouse/data fix) ---
    ("", "invalid parameter",
     "A function/procedure was called with a wrong argument — check its signature and the value "
     "passed (a bad table-function parameter)."),
    ("", "syntax error",
     "SQL syntax error — the statement never compiled (often a reserved word used unquoted, e.g. a "
     "table named TASKS, or SHOW … misspelled). Fix the SQL text."),
    # --- resource / data-shape (dual-use with the optimization advisor) ---
    ("", "cartesian",
     "A join produced a Cartesian product — add the ON/join predicate that ties the two tables so "
     "rows don't multiply."),
    ("", "memory",
     "Ran out of memory (often an exploding/Cartesian join) — add the missing join key, size the "
     "warehouse up one step, or shrink the working set."),
    ("", "division by zero",
     "Division by zero — guard the denominator with NULLIF(<expr>, 0) or a CASE."),
    ("", "numeric value",
     "A value didn't fit its numeric type/cast — widen the target type or clean the source value "
     "(TRY_CAST/TRY_TO_NUMBER surfaces the bad rows)."),
    ("", "duplicate row detected",
     "A MERGE matched more than one target row per source — dedupe the source or tighten the ON "
     "condition so each target matches at most once."),
)


def fix_for(error_code: object, error_message: object) -> str:
    """Return a plain-English remediation for one failure family, or "" when the family isn't
    recognized (the panel then renders a blank/em-dash cell). Never raises."""
    code = str(error_code or "").strip()
    msg = str(error_message or "").lower()
    for c, sub, fix in _FIXES:
        if (not c or c == code) and (not sub or sub in msg):
            return fix
    return ""
