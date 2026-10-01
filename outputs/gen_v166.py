#!/usr/bin/env python3
"""Forward-generate V166: four fact loaders stop losing or understating history (V166-V172 round, cluster loaders).

  R2-007  SP_LOAD_SECURITY_FACTS' d<=3 change reload DELETEd from the extract's first row but re-read only from
          midnight(today - d); whenever the extract reached further back (a swallowed extract failure across
          midnight, a backfill-wide extract, a manual DAYS_BACK of 0/1/2) the rows in between were deleted, never
          re-read, and then trimmed out of the 72h extract for good. Both statements now share ONE bound.
  R2-009  SP_LOAD_DAILY_FACTS AVGed DATABASE_STORAGE_USAGE_HISTORY per name-day; a re-created / clone-refreshed
          database keeps one row per DATABASE_ID under the same name, so the fact divided the billed bytes by the
          row count. SUM, plus one bounded idempotent MERGE that repairs the multi-ID name-days already written.
  R2-011  SP_LOAD_APP_COST / SP_LOAD_STORAGE_TRUTH ran DELETE then INSERT under autocommit: a failed INSERT left
          the oldest reloaded day deleted for good (the next run's window starts a day later). One transaction,
          ROLLBACK, an APP_ERROR_LOG 'fact_load_failed' row, re-RAISE (the task still reads FAILED).
  C10     SP_LOAD_APP_COST's SESSIONS lookback was 7 days before the reload start, so the LAST reload of a day
          relabelled a 7-10 day old keep-alive / pooled session '(unknown)'. 30 days (= app_cost_sql.SESSION_PAD_DAYS,
          the live twin; tests/migrations/test_v166_* locks the parity).

Reads ONLY the four current definers (tests/test_proc_lineage.py; each verified latest through V165):
  V105__change_risk_create_or_replace_destructive.sql   SP_LOAD_SECURITY_FACTS (V075 -> V100 -> V105)
  V101__fact_task_daily_retry_collapse.sql              SP_LOAD_DAILY_FACTS    (V101's task_attempts CTE survives)
  V077__app_cost_ledger.sql                             SP_LOAD_APP_COST       (only definer)
  V046__storage_truth.sql                               SP_LOAD_STORAGE_TRUTH  (only definer)
and emits, in order:

  guard (-20166, v < 165) -> marker + SP_LOAD_SECURITY_FACTS -> marker + SP_LOAD_DAILY_FACTS -> marker +
  SP_LOAD_APP_COST -> marker + SP_LOAD_STORAGE_TRUTH -> the FACT_STORAGE_DAILY repair MERGE -> SCHEMA_VERSION 166.

Every delta is an anchored _swap with its count asserted; everything else is byte-identical to the base (the V166
test reverses each delta with its OWN copies of the old text back to the base proc). No task, table or DDL change,
no CALL at apply time, no UPDATE of SOURCE_FRESHNESS_STATE (the freshness MERGEs stay byte-identical and, in the
R2-011 procs, after the COMMIT).

Optional outputs (the byte-identity tests never set them), built from the SAME text as the migration:
  PREFLIGHT_OUT  read-only P166.1-P166.4 (the repair's own source SELECT previewed against the fact, the R2-011
                 calendar-gap and failed-run grids, the optional C10 session-age probe)
  PART_B_OUT     read-only V166.1-V166.4 verify grids
  REPAIR_OUT     the ordered owner-run V166 repair blocks for OWNER_REPAIRS_V166_V172.sql (first statement pins
                 the session to America/Chicago; each heal is a guarded block that reads the loader's own verdict)

Run: python outputs/gen_v166.py
"""

from __future__ import annotations

import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIG = ROOT / "snowflake" / "migrations"
NAME = "V166__fact_loader_window_integrity.sql"
V105 = (MIG / "V105__change_risk_create_or_replace_destructive.sql").read_text(encoding="utf-8")
V101 = (MIG / "V101__fact_task_daily_retry_collapse.sql").read_text(encoding="utf-8")
V077 = (MIG / "V077__app_cost_ledger.sql").read_text(encoding="utf-8")
V046 = (MIG / "V046__storage_truth.sql").read_text(encoding="utf-8")


def extract_proc(text: str, name: str) -> str:
    head = f"CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.{name}"
    assert text.count(head) == 1, name
    start = text.index(head)
    open_dd = text.index("$$", start)
    return text[start:text.index("$$;", open_dd + 2) + 3]


def _swap(text: str, old: str, new: str, label: str, n: int = 1) -> str:
    assert text.count(old) == n, f"{label}: expected {n} anchor(s), got {text.count(old)}"
    return text.replace(old, new)


# ---------------------------------------------------------------------------------------------------
# R2-007 -- SP_LOAD_SECURITY_FACTS (from V105): the d<=3 DELETE and INSERT share ONE lower bound.
# ---------------------------------------------------------------------------------------------------
sec = extract_proc(V105, "SP_LOAD_SECURITY_FACTS(DAYS_BACK FLOAT)")
_ELSE = "\n    ELSE\n"
assert sec.count(_ELSE) == 1
_else_before = sec[sec.index(_ELSE):sec.index("    END IF;\n", sec.index(_ELSE))]

sec = _swap(sec, "    trust_ok BOOLEAN DEFAULT TRUE;\nBEGIN\n",
            "    trust_ok BOOLEAN DEFAULT TRUE;\n"
            "    lo_ts TIMESTAMP_LTZ;        -- V166 (R2-007): the d<=3 change reload lower bound, DELETE and INSERT\n"
            "BEGIN\n", "R2-007 declare")
sec = _swap(sec,
            "        -- the delete matches nothing (safe no-op).\n"
            "        DELETE FROM DBA_MAINT_DB.OVERWATCH.FACT_SECURITY_CHANGE\n"
            "         WHERE EVENT_TS >= (SELECT MIN(START_TIME) FROM DBA_MAINT_DB.OVERWATCH.OW_QH_EXTRACT);\n",
            "        -- the delete matches nothing (safe no-op).\n"
            "        -- V166 (R2-007): the DELETE and the INSERT share ONE lower bound, the later of the extract\n"
            "        -- first row and midnight(today - d). V100 deleted from MIN(START_TIME) alone while the INSERT\n"
            "        -- re-read only START_TIME >= today - d, so whenever the extract reached further back (a\n"
            "        -- swallowed extract failure across midnight keeps the old untrimmed fill; a backfill-wide\n"
            "        -- extract; a manual d of 1 or 2, DAYS_BACK 0 maps to 1) the rows in between were deleted,\n"
            "        -- never re-read, then trimmed out of the extract for good. GREATEST() propagates NULL: an\n"
            "        -- empty extract leaves lo_ts NULL and both statements match nothing (the V100 no-op is kept).\n"
            "        lo_ts := (SELECT GREATEST(MIN(START_TIME), DATEADD('day', -:d, CURRENT_DATE())::TIMESTAMP_LTZ)\n"
            "                    FROM DBA_MAINT_DB.OVERWATCH.OW_QH_EXTRACT);\n"
            "        DELETE FROM DBA_MAINT_DB.OVERWATCH.FACT_SECURITY_CHANGE\n"
            "         WHERE EVENT_TS >= :lo_ts;\n", "R2-007 delete")
sec = _swap(sec,
            "            FROM DBA_MAINT_DB.OVERWATCH.OW_QH_EXTRACT\n"
            "            WHERE START_TIME >= DATEADD('day', -:d, CURRENT_DATE())\n",
            "            FROM DBA_MAINT_DB.OVERWATCH.OW_QH_EXTRACT\n"
            "            WHERE START_TIME >= :lo_ts\n", "R2-007 insert")
# the d>3 arm (QUERY_HISTORY, whole-window DELETE) is untouched; GREATEST, never GREATEST_IGNORE_NULLS
assert sec[sec.index(_ELSE):sec.index("    END IF;\n", sec.index(_ELSE))] == _else_before
assert "GREATEST_IGNORE_NULLS" not in sec and "(SELECT MIN(START_TIME)" not in sec
assert sec.count("lo_ts") == 5 and sec.count("WHERE START_TIME >= DATEADD('day', -:d, CURRENT_DATE())") == 1

# ---------------------------------------------------------------------------------------------------
# R2-009 -- SP_LOAD_DAILY_FACTS (from V101): the FACT_STORAGE_DAILY arm SUMs per name-day.
# ---------------------------------------------------------------------------------------------------
daily = extract_proc(V101, "SP_LOAD_DAILY_FACTS()")
assert daily.count("WITH task_attempts AS (") == 1 and daily.count("GROUP BY 1, 2, 3;") == 2   # login + storage
daily = _swap(daily,
              "        AVG(COALESCE(AVERAGE_DATABASE_BYTES, 0)),\n"
              "        AVG(COALESCE(AVERAGE_FAILSAFE_BYTES, 0))\n",
              "        -- V166 (R2-009): SUM, not AVG. The view has one row per DATABASE_ID per day, and a dropped\n"
              "        -- (re-created / clone-refreshed) database keeps its own rows under the same name while it\n"
              "        -- holds Time Travel / fail-safe bytes. Each row is already the daily average of its own\n"
              "        -- DATABASE_ID, so the name-day billed bytes are the SUM (= storage_by_database_calendar_live).\n"
              "        SUM(COALESCE(AVERAGE_DATABASE_BYTES, 0)),\n"
              "        SUM(COALESCE(AVERAGE_FAILSAFE_BYTES, 0))\n", "R2-009 storage SUM")
assert "AVG(COALESCE(AVERAGE_" not in daily
assert daily.count("WITH task_attempts AS (") == 1 and daily.count("GROUP BY 1, 2, 3;") == 2   # V101 survives

# ---------------------------------------------------------------------------------------------------
# R2-011 + C10 -- SP_LOAD_APP_COST (from V077): one transaction; SESSIONS lookback 7 -> 30 days.
# R2-011 -- SP_LOAD_STORAGE_TRUTH (from V046): one transaction.
# Only DML inside each transaction (no implicit commit, B34); the freshness MERGE stays after the COMMIT.
# ---------------------------------------------------------------------------------------------------
SESSION_PAD_DAYS = 30      # copy of app/data/app_cost_sql.SESSION_PAD_DAYS (the V166 test locks the parity)
_EMSG_DECL = "    emsg VARCHAR;               -- V166 (R2-011): the rolled-back load error, logged then re-raised\n"


def _handler(page: str, fact: str) -> str:
    return ("    COMMIT;\n"
            "    EXCEPTION\n"
            "        WHEN OTHER THEN\n"
            "            ROLLBACK;\n"
            "            emsg := SQLERRM;\n"
            "            INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)\n"
            f"            SELECT '{page}', 'fact_load_failed', :emsg, '{fact} - previous fill retained on rollback, "
            "error re-raised', CURRENT_ROLE();\n"
            "            RAISE;\n"
            "    END;\n")


app = extract_proc(V077, "SP_LOAD_APP_COST(DAYS_BACK FLOAT)")
app = _swap(app, "    lo DATE;\nBEGIN\n", "    lo DATE;\n" + _EMSG_DECL + "BEGIN\n", "R2-011 app declare")
app = _swap(app,
            "    -- window is 365; keep a buffer). No central purge touches this fact.\n"
            "    DELETE FROM DBA_MAINT_DB.OVERWATCH.FACT_APP_COST_DAILY\n",
            "    -- window is 365; keep a buffer). No central purge touches this fact.\n"
            "    -- V166 (R2-011): ONE transaction. Under autocommit a failed INSERT left the DELETE committed,\n"
            "    -- and the next run starts a day later, so the oldest deleted day was never reloaded (a\n"
            "    -- permanent hole per failed run). A failure now rolls back to the previous fill, is logged as\n"
            "    -- fact_load_failed (the self-watch ERR leg keys on the first CONTEXT word) and is re-raised,\n"
            "    -- so the task still reads FAILED.\n"
            "    BEGIN\n"
            "    BEGIN TRANSACTION;\n"
            "    DELETE FROM DBA_MAINT_DB.OVERWATCH.FACT_APP_COST_DAILY\n", "R2-011 app begin")
app = _swap(app, "    GROUP BY DAY, APPLICATION, USER_NAME, COMPANY;\n",
            "    GROUP BY DAY, APPLICATION, USER_NAME, COMPANY;\n" + _handler("AppCost", "FACT_APP_COST_DAILY"),
            "R2-011 app commit")
app = _swap(app,
            "        -- driver family (version stripped), else '(unknown)'.\n",
            "        -- driver family (version stripped), else '(unknown)'.\n"
            f"        -- V166 (C10): {SESSION_PAD_DAYS} days before lo, not 7. The task LAST reload of a day (lo = that day)\n"
            "        -- narrowed the lookback to 7 days and relabelled '(unknown)' a keep-alive / pooled session\n"
            "        -- the first load had resolved. Same pad as app_cost_sql.SESSION_PAD_DAYS (the live twin).\n",
            "C10 comment")
app = _swap(app, "        WHERE CREATED_ON >= DATEADD('day', -7, :lo)\n",
            f"        WHERE CREATED_ON >= DATEADD('day', -{SESSION_PAD_DAYS}, :lo)\n", "C10 pad")
assert app.count("COALESCE(s.APPLICATION, '(unknown)')") == 1 and "(no session record)" not in app

stor = extract_proc(V046, "SP_LOAD_STORAGE_TRUTH(DAYS_BACK FLOAT)")
stor = _swap(stor, "    lo DATE;\nBEGIN\n", "    lo DATE;\n" + _EMSG_DECL + "BEGIN\n", "R2-011 storage declare")
stor = _swap(stor,
             "    lo := DATEADD('day', -GREATEST(COALESCE(:DAYS_BACK, 3), 1)::INT, CURRENT_DATE());\n"
             "    DELETE FROM DBA_MAINT_DB.OVERWATCH.FACT_STORAGE_ACCOUNT_DAILY WHERE DAY >= :lo;\n",
             "    lo := DATEADD('day', -GREATEST(COALESCE(:DAYS_BACK, 3), 1)::INT, CURRENT_DATE());\n"
             "    -- V166 (R2-011): one transaction, so a failed INSERT rolls the DELETE back (see SP_LOAD_APP_COST).\n"
             "    BEGIN\n"
             "    BEGIN TRANSACTION;\n"
             "    DELETE FROM DBA_MAINT_DB.OVERWATCH.FACT_STORAGE_ACCOUNT_DAILY WHERE DAY >= :lo;\n",
             "R2-011 storage begin")
stor = _swap(stor, "    GROUP BY USAGE_DATE;\n",
             "    GROUP BY USAGE_DATE;\n" + _handler("StorageTruth", "FACT_STORAGE_ACCOUNT_DAILY"),
             "R2-011 storage commit")

for _p in (app, stor):
    _txn = _p[_p.index("    BEGIN TRANSACTION;\n"):_p.index("    COMMIT;\n")]
    assert not re.search(r"\b(?:CREATE|ALTER|DROP|TRUNCATE|GRANT|MERGE)\b", re.sub(r"--[^\n]*", "", _txn))
    assert (_p.index("    COMMIT;\n") < _p.index("            RAISE;\n")
            < _p.index("MERGE INTO DBA_MAINT_DB.OVERWATCH.SOURCE_FRESHNESS_STATE"))

# ---------------------------------------------------------------------------------------------------
# The R2-009 repair: the multi-ID name-days already written with AVG, recomputed as the new loader would.
# One statement, no DELETE, no WHEN NOT MATCHED, LOAD_TS untouched (freshness never falsely advances).
# REPAIR_SOURCE is shared with the PREFLIGHT preview and the PART B check.
# ---------------------------------------------------------------------------------------------------
REPAIR_SOURCE = """\
    SELECT USAGE_DATE AS DAY, DATABASE_NAME,
           SUM(COALESCE(AVERAGE_DATABASE_BYTES, 0)) AS DB_BYTES,
           SUM(COALESCE(AVERAGE_FAILSAFE_BYTES, 0)) AS FAILSAFE_BYTES
    FROM SNOWFLAKE.ACCOUNT_USAGE.DATABASE_STORAGE_USAGE_HISTORY
    WHERE USAGE_DATE >= DATEADD('day', -365, CURRENT_DATE())
    GROUP BY 1, 2
    HAVING COUNT(*) > 1"""
REPAIR_ON = "ON t.DAY = s.DAY AND t.DATABASE_NAME = s.DATABASE_NAME"
REPAIR = f"""\
-- R2-009 repair (bounded, idempotent): the name-days where the view still holds 2+ DATABASE_ID rows were
-- written as the AVG; rewrite exactly those two byte columns as the SUM the loader now writes. Single-ID
-- name-days are untouched (the AVG of one row is its SUM). No DELETE, no insert, LOAD_TS unchanged.
MERGE INTO DBA_MAINT_DB.OVERWATCH.FACT_STORAGE_DAILY t
USING (
{REPAIR_SOURCE}
) s
{REPAIR_ON}
WHEN MATCHED THEN UPDATE SET DB_BYTES = s.DB_BYTES, FAILSAFE_BYTES = s.FAILSAFE_BYTES;
"""

# ---------------------------------------------------------------------------------------------------
# The file.
# ---------------------------------------------------------------------------------------------------
HEADER = f"""-- {NAME}
--
-- WHY: four fact loaders could lose or understate history that no later run ever reloaded.
--   R2-007  The hourly security change reload (d <= 3) deleted from the first row of OW_QH_EXTRACT but
--           re-read only from midnight(today - d). When the extract reached further back -- a swallowed
--           extract failure across midnight keeps its old untrimmed fill, backfill_365 widens it to 90
--           days, a manual DAYS_BACK of 0/1/2 -- the rows in between were deleted, never re-read, and
--           then trimmed out of the 72h extract for good: holes in the CHANGE RISK queue, the destructive
--           breakdown and Who changed what, below the day-grain coverage check of the app.
--   R2-009  FACT_STORAGE_DAILY averaged DATABASE_STORAGE_USAGE_HISTORY per name-day. The view has one row
--           per DATABASE_ID, and a re-created or clone-refreshed database keeps its dropped IDs (Time Travel
--           and fail-safe bytes) under the same name, so the fact divided the billed bytes by the row count.
--           The live twin (storage_by_database_calendar_live) already SUMs, so mart and live disagreed.
--   R2-011  SP_LOAD_APP_COST and SP_LOAD_STORAGE_TRUTH ran DELETE then INSERT under autocommit. A failed
--           INSERT left the trailing window deleted; the next run starts a day later, so the oldest
--           deleted day was never reloaded (the v4.371.0 deferral assumed it self-healed; it does not).
--   C10     SP_LOAD_APP_COST resolved sessions only 7 days before its reload start. The task reloads each
--           day four times and the LAST reload is the narrowest, so a query in a keep-alive, pooled or
--           service session opened 7-10 days earlier was relabelled (unknown) for good.
--
--   ~ SP_LOAD_SECURITY_FACTS re-derived from V105, byte-identical except: the d <= 3 DELETE and INSERT
--     share ONE bound, lo_ts = GREATEST(MIN(extract START_TIME), midnight(today - d)). GREATEST propagates
--     NULL, so an empty extract stays a no-op. The d > 3 arm and the DAYS_BACK clamp are unchanged.
--   ~ SP_LOAD_DAILY_FACTS re-derived from V101, byte-identical except: the FACT_STORAGE_DAILY arm SUMs
--     AVERAGE_DATABASE_BYTES / AVERAGE_FAILSAFE_BYTES per name-day. The V101 task_attempts CTE stays.
--   ~ SP_LOAD_APP_COST re-derived from V077, byte-identical except: the DELETE + INSERT run in ONE
--     transaction (ROLLBACK, an APP_ERROR_LOG fact_load_failed row with PAGE AppCost, re-RAISE so the task
--     still reads FAILED), and the SESSIONS lookback is {SESSION_PAD_DAYS} days before the reload start, not 7. The
--     (unknown) label stays (SP_SCAN_SLEEP_POLLING and cloud_svc_billed_families filter on it).
--   ~ SP_LOAD_STORAGE_TRUTH re-derived from V046, byte-identical except the same one-transaction wrap
--     (PAGE StorageTruth). In both, the SOURCE_FRESHNESS_STATE MERGE stays after the COMMIT.
--   + one bounded repair: FACT_STORAGE_DAILY name-days that the view still holds as 2+ DATABASE_ID rows
--     (365-day retention) get the SUM. Older rows cannot be recomputed and stay understated.
--
-- COST: the daily TASK_LOAD_APP_COST CALL(3) now scans {SESSION_PAD_DAYS + 3} days of SESSIONS instead of 10 (small next to
-- its QUERY_HISTORY and QUERY_ATTRIBUTION_HISTORY scans; watch its first scheduled run in TASK_HISTORY). The
-- other loaders scan what they scanned before. The repair reads about 365 days of
-- DATABASE_STORAGE_USAGE_HISTORY once (a few thousand rows) and updates only the multi-ID name-days.
-- LATENCY: unchanged; the procs swap under the running graph (no task change, no suspend).
-- FIRST RUN: the next hourly SP_LOAD_SECURITY_FACTS(3), then the daily runs: storage truth 06:30 CT,
-- SP_LOAD_DAILY_FACTS() 06:45 CT (TASK_LOAD_DAILY, then the nightly reconcile), app cost 06:55 CT. Apply
-- outside 06:30-07:15 CT so no run straddles the swap. Nothing runs at apply time except the storage repair. Owner-run
-- heals, in a Central session after V166 is applied (OWNER_REPAIRS): SP_LOAD_SECURITY_FACTS(180) at about
-- :35 past the hour, the R2-011 gap grids, then SP_LOAD_STORAGE_TRUTH(N) only when they show holes and ONE
-- off-peak SP_LOAD_APP_COST of at least 30 days (relabels sessions, fills any R2-011 hole, atomic now).
-- ROLLBACK: re-run the base CREATE PROCEDURE of each proc (V105, V101, V077, V046). The repaired storage
-- rows can stay: they are what Snowflake bills, and the live twin already shows them.
-- Apply AFTER V165. Idempotent; safe to re-run.

EXECUTE IMMEDIATE
$$
DECLARE
    v NUMBER;
    not_ready EXCEPTION (-20166, 'V166 requires V165 first - apply migrations in order.');
BEGIN
    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 165) THEN
        RAISE not_ready;
    END IF;
END;
$$;

"""

MARKERS = {
    "SP_LOAD_SECURITY_FACTS": ("-- >>> derived:SP_LOAD_SECURITY_FACTS  (from V105; d<=3 change reload DELETE + INSERT "
                               "share one bound GREATEST(MIN(extract.START_TIME), midnight(today-d)), V166)\n"),
    "SP_LOAD_DAILY_FACTS": ("-- >>> derived:SP_LOAD_DAILY_FACTS  (from V101; FACT_STORAGE_DAILY arm AVG -> SUM per "
                            "name-day, V166)\n"),
    "SP_LOAD_APP_COST": ("-- >>> derived:SP_LOAD_APP_COST  (from V077; DELETE+INSERT in one transaction, ROLLBACK + "
                         "APP_ERROR_LOG + re-RAISE; SESSIONS lookback 7 -> 30 days, V166)\n"),
    "SP_LOAD_STORAGE_TRUTH": ("-- >>> derived:SP_LOAD_STORAGE_TRUTH  (from V046; DELETE+INSERT in one transaction, "
                              "ROLLBACK + APP_ERROR_LOG + re-RAISE, V166)\n"),
}

DESCRIPTION = (
    "Fact loader window integrity: SP_LOAD_SECURITY_FACTS re-derived from V105, the d<=3 change reload DELETE and "
    "INSERT share one bound GREATEST(MIN(extract START_TIME), midnight(today-d)) so a swallowed extract failure, a "
    "backfill-wide extract or a manual DAYS_BACK below 3 no longer deletes rows the INSERT never re-reads; "
    "SP_LOAD_DAILY_FACTS re-derived from V101, FACT_STORAGE_DAILY SUMs the per-DATABASE_ID rows per name-day "
    "(re-created and clone-refreshed databases were averaged) plus one bounded MERGE that repairs the multi-ID "
    "name-days still in the 365-day view; SP_LOAD_APP_COST (V077) and SP_LOAD_STORAGE_TRUTH (V046) run DELETE and "
    "INSERT in one transaction with ROLLBACK, a fact_load_failed APP_ERROR_LOG row and a re-raise (a failed run "
    "lost its oldest reloaded day for good); SP_LOAD_APP_COST resolves sessions 30 days before its reload start, "
    "not 7. No task, table or DDL change; no procedure run at apply time.")
assert len(DESCRIPTION) <= 4000 and "'" not in DESCRIPTION and "\n" not in DESCRIPTION

VERSION_ROW = f"""
INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)
SELECT 166 AS VERSION,
       '{DESCRIPTION}' AS DESCRIPTION
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 166);
"""

out = (HEADER
       + MARKERS["SP_LOAD_SECURITY_FACTS"] + sec + "\n\n"
       + MARKERS["SP_LOAD_DAILY_FACTS"] + daily + "\n\n"
       + MARKERS["SP_LOAD_APP_COST"] + app + "\n\n"
       + MARKERS["SP_LOAD_STORAGE_TRUTH"] + stor + "\n\n"
       + REPAIR
       + VERSION_ROW)

# ---- post-asserts ----------------------------------------------------------------------------------
assert out.startswith(f"-- {NAME}\n")
assert "'" not in HEADER[:HEADER.index("EXECUTE IMMEDIATE")], "the header comments stay apostrophe-free"
assert out.count("CREATE OR REPLACE PROCEDURE") == 4 and out.count("$$") == 10
for _proc in (sec, daily, app, stor):
    _body = _proc[_proc.index("$$") + 2:_proc.rindex("$$")]
    assert "\\" not in _body and "$$" not in _body
assert "\r" not in out and "\\" not in out
assert "V166 requires V165 first" in out and "SELECT 166 AS VERSION" in out
assert not re.search(r"^\s*CALL\s", out, re.M), "nothing CALLs at apply time"
assert not re.search(r"(?:CREATE(?:\s+OR\s+REPLACE)?\s+(?:TASK|TABLE)|ALTER\s+(?:TASK|TABLE)|EXECUTE\s+TASK)", out)
assert not re.search(r"UPDATE\s+DBA_MAINT_DB\.OVERWATCH\.SOURCE_FRESHNESS_STATE", out)
assert out.count("MERGE INTO DBA_MAINT_DB.OVERWATCH.FACT_STORAGE_DAILY") == 1
assert out.index("MERGE INTO DBA_MAINT_DB.OVERWATCH.FACT_STORAGE_DAILY") > out.rindex("$$;")
assert "@" not in out, "no address of any kind"
for _name, _mark in MARKERS.items():
    assert out.count(_mark) == 1 and out[out.index(_mark) + len(_mark):].startswith(
        f"CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.{_name}("), _name

target = Path(os.environ.get("V166_OUT") or MIG / NAME)
target.write_text(out, encoding="utf-8", newline="\n")


# ---------------------------------------------------------------------------------------------------
# PREFLIGHT / PART B / REPAIR -- read-only previews and the owner-run heals, built from the same text.
# ---------------------------------------------------------------------------------------------------
# R2-011 Grid 1: a calendar gap inside a fact's [MIN(DAY), MAX(DAY)] is a hole (STORAGE_USAGE has a row every
# day; the account has attributed warehouse queries every day). DAYS_BACK_TO_HEAL = the CALL argument that
# reaches that day.
GAP_CTES = """\
    f AS (
        SELECT 'FACT_APP_COST_DAILY' AS FACT, DAY FROM DBA_MAINT_DB.OVERWATCH.FACT_APP_COST_DAILY GROUP BY DAY
        UNION ALL
        SELECT 'FACT_STORAGE_ACCOUNT_DAILY', DAY FROM DBA_MAINT_DB.OVERWATCH.FACT_STORAGE_ACCOUNT_DAILY GROUP BY DAY
    ),
    b AS (SELECT FACT, MIN(DAY) AS LO, MAX(DAY) AS HI FROM f GROUP BY FACT),
    n AS (SELECT ROW_NUMBER() OVER (ORDER BY SEQ4()) - 1 AS I FROM TABLE(GENERATOR(ROWCOUNT => 401))),
    gaps AS (
        SELECT b.FACT, DATEADD('day', n.I, b.LO) AS MISSING_DAY,
               DATEDIFF('day', DATEADD('day', n.I, b.LO), CURRENT_DATE()) + 1 AS DAYS_BACK_TO_HEAL
        FROM b
        JOIN n ON DATEADD('day', n.I, b.LO) <= b.HI
        LEFT JOIN f ON f.FACT = b.FACT AND f.DAY = DATEADD('day', n.I, b.LO)
        WHERE f.DAY IS NULL
    )"""
GRID_GAPS = f"""\
WITH
{GAP_CTES}
SELECT FACT, MISSING_DAY, DAYS_BACK_TO_HEAL
  FROM gaps
 ORDER BY FACT, MISSING_DAY;
"""
GRID_FAILED_RUNS = """\
SELECT NAME, SCHEDULED_TIME, STATE, LEFT(ERROR_MESSAGE, 300) AS ERROR_MESSAGE
  FROM SNOWFLAKE.ACCOUNT_USAGE.TASK_HISTORY
 WHERE DATABASE_NAME = 'DBA_MAINT_DB' AND SCHEMA_NAME = 'OVERWATCH'
   AND NAME IN ('TASK_LOAD_APP_COST', 'TASK_LOAD_STORAGE_TRUTH') AND STATE = 'FAILED'
 ORDER BY SCHEDULED_TIME;
"""
_STORAGE_PREVIEW = f"""\
WITH s AS (
{REPAIR_SOURCE}
)
SELECT t.DATABASE_NAME,
       COUNT(*) AS NAME_DAYS_REPAIRED,
       MIN(t.DAY) AS FIRST_DAY, MAX(t.DAY) AS LAST_DAY,
       ROUND(SUM(s.DB_BYTES - t.DB_BYTES) / POWER(1024, 4), 3) AS DB_TB_DAYS_ADDED,
       ROUND(SUM(s.FAILSAFE_BYTES - t.FAILSAFE_BYTES) / POWER(1024, 4), 3) AS FAILSAFE_TB_DAYS_ADDED
  FROM DBA_MAINT_DB.OVERWATCH.FACT_STORAGE_DAILY t
  JOIN s {REPAIR_ON}
 GROUP BY t.DATABASE_NAME
 ORDER BY DB_TB_DAYS_ADDED DESC;
"""
_C10_PROBE = f"""\
WITH q AS (
    SELECT QUERY_ID, SESSION_ID, START_TIME
      FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
     WHERE START_TIME >= DATEADD('day', -7, CURRENT_DATE())
),
c AS (
    SELECT QUERY_ID,
           SUM(COALESCE(CREDITS_ATTRIBUTED_COMPUTE, 0) + COALESCE(CREDITS_USED_QUERY_ACCELERATION, 0)) AS CR
      FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_ATTRIBUTION_HISTORY
     WHERE START_TIME >= DATEADD('day', -7, CURRENT_DATE())
     GROUP BY 1
),
s AS (
    SELECT SESSION_ID, MIN(CREATED_ON) AS CREATED_ON
      FROM SNOWFLAKE.ACCOUNT_USAGE.SESSIONS
     GROUP BY 1
)
SELECT CASE WHEN s.SESSION_ID IS NULL THEN 'no SESSIONS row (stays unknown)'
            WHEN s.CREATED_ON >= DATEADD('day', -7, q.START_TIME::DATE) THEN 'within 7 days (resolved before V166)'
            WHEN s.CREATED_ON >= DATEADD('day', -{SESSION_PAD_DAYS}, q.START_TIME::DATE) THEN '7-{SESSION_PAD_DAYS} days (fixed by V166)'
            ELSE 'older than {SESSION_PAD_DAYS} days (stays unknown)' END AS SESSION_AGE,
       COUNT(*) AS QUERIES,
       ROUND(SUM(c.CR), 2) AS CREDITS
  FROM q
  JOIN c ON c.QUERY_ID = q.QUERY_ID
  LEFT JOIN s ON s.SESSION_ID = q.SESSION_ID
 GROUP BY 1
 ORDER BY CREDITS DESC;
"""

PREFLIGHT = f"""\
-- PREFLIGHT -- V166 section (READ-ONLY). Run before applying V166; changes nothing.
-- What the grids answer: P166.1 which FACT_STORAGE_DAILY rows V166's repair MERGE rewrites (its own source SELECT,
-- joined on its own ON clause) and how many TB-days each database gains; P166.2 / P166.3 whether a failed
-- app-cost or storage-truth run already left a hole (R2-011; V166 stops new ones, the owner-run heal fills old
-- ones); P166.4 OPTIONAL (7 days of QUERY_HISTORY x QUERY_ATTRIBUTION_HISTORY x SESSIONS) how many of the last
-- week's attributed credits the 30-day session lookback relabels from (unknown).

-- P166.1 the R2-009 repair, previewed. One row per database with a re-created or clone-refreshed day in the
--        view's 365 days. expect a handful of rows (none = nothing to repair); every value positive.
{_STORAGE_PREVIEW}
-- P166.2 R2-011 calendar gaps in the two facts. expect no rows; a row is a day a failed run deleted for good.
{GRID_GAPS}
-- P166.3 R2-011 failed runs of the two daily tasks (ACCOUNT_USAGE, up to ~45 min behind). A FAILED run on day D
--        whose error came from the INSERT predicts a P166.2 hole at D-3.
{GRID_FAILED_RUNS}
-- P166.4 OPTIONAL C10 session-age buckets for the last 7 days of attributed queries. The 7-30 day bucket is what
--        V166 relabels from (unknown) on newly loaded days.
{_C10_PROBE}"""

_PROCS = (
    ("SP_LOAD_SECURITY_FACTS(FLOAT)", sec,
     ("lo_ts TIMESTAMP_LTZ;", "WHERE EVENT_TS >= :lo_ts;", "WHERE START_TIME >= :lo_ts"),
     ("WHERE EVENT_TS >= (SELECT MIN(START_TIME)", "GREATEST_IGNORE_NULLS")),
    ("SP_LOAD_DAILY_FACTS()", daily,
     ("SUM(COALESCE(AVERAGE_DATABASE_BYTES, 0))", "SUM(COALESCE(AVERAGE_FAILSAFE_BYTES, 0))",
      "WITH task_attempts AS ("),
     ("AVG(COALESCE(AVERAGE_DATABASE_BYTES", "AVG(COALESCE(AVERAGE_FAILSAFE_BYTES")),
    ("SP_LOAD_APP_COST(FLOAT)", app,
     ("BEGIN TRANSACTION;", "fact_load_failed", "AppCost", f"-{SESSION_PAD_DAYS}, :lo)"),
     ("-7, :lo)",)),
    ("SP_LOAD_STORAGE_TRUTH(FLOAT)", stor,
     ("BEGIN TRANSACTION;", "fact_load_failed", "StorageTruth"), ()),
)
_ddl_rows = []
for _sig, _src, _present, _absent in _PROCS:
    for _frag in _present + _absent:
        assert not set(_frag) & {"'", "\\", "\n", "\r"}, _frag
    for _frag in _present:
        assert _frag in _src, (_sig, _frag)
    for _frag in _absent:
        assert _frag not in _src, (_sig, _frag)
    _ddl = f"GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.{_sig}')"
    _cond = " AND ".join([f"CONTAINS({_ddl}, '{f}')" for f in _present]
                         + [f"NOT CONTAINS({_ddl}, '{f}')" for f in _absent])
    _ddl_rows.append((f"V166.1 {_sig.split('(')[0]} is the V166 proc", _cond))

_ddl_union = "\nUNION ALL\n".join(
    (f"SELECT '{label}' AS CHECK_NAME,\n       IFF({cond},\n           'OK', 'FAIL: not the V166 text') AS RESULT"
     if i == 0 else
     f"SELECT '{label}',\n       IFF({cond},\n           'OK', 'FAIL: not the V166 text')")
    for i, (label, cond) in enumerate(_ddl_rows))

PART_B = f"""\
-- PART B -- V166 verify (READ-ONLY). Run after applying V166 (a Central session; the integrator's combined
-- PART B opens with ALTER SESSION SET TIMEZONE). Every CHECK should read OK; paste the grids back.
-- V166.1 the four procs are the V166 text.
{_ddl_union};

-- V166.2 the R2-009 repair landed: every multi-ID name-day older than 2 days now holds the SUM (the MERGE's own
--        source SELECT and ON clause). expect OK; MISMATCHED > 0 means the MERGE did not run (re-run the file).
WITH s AS (
{REPAIR_SOURCE}
)
SELECT 'V166.2 FACT_STORAGE_DAILY multi-ID name-days hold the SUM' AS CHECK_NAME,
       COUNT(*) AS MULTI_ID_NAME_DAYS,
       COUNT_IF(ABS(t.DB_BYTES - s.DB_BYTES) > 1 OR ABS(t.FAILSAFE_BYTES - s.FAILSAFE_BYTES) > 1) AS MISMATCHED,
       IFF(COUNT_IF(ABS(t.DB_BYTES - s.DB_BYTES) > 1 OR ABS(t.FAILSAFE_BYTES - s.FAILSAFE_BYTES) > 1) = 0,
           'OK', 'FAIL: AVG rows remain') AS RESULT
  FROM DBA_MAINT_DB.OVERWATCH.FACT_STORAGE_DAILY t
  JOIN s {REPAIR_ON}
 WHERE s.DAY < DATEADD('day', -2, CURRENT_DATE());

-- V166.3 the next morning (after the 06:30 / 06:55 CT runs; ACCOUNT_USAGE is up to ~45 min behind). expect
--        SUCCEEDED for both tasks, and note TASK_LOAD_APP_COST's duration (the SESSIONS scan grew from 10 to 33
--        days). RETURN_VALUE stays NULL: a task that CALLs a proc does not publish its return string.
SELECT NAME, SCHEDULED_TIME, STATE, DATEDIFF('second', QUERY_START_TIME, COMPLETED_TIME) AS RUN_SEC,
       LEFT(ERROR_MESSAGE, 200) AS ERROR_MESSAGE
  FROM SNOWFLAKE.ACCOUNT_USAGE.TASK_HISTORY
 WHERE DATABASE_NAME = 'DBA_MAINT_DB' AND SCHEMA_NAME = 'OVERWATCH'
   AND NAME IN ('TASK_LOAD_APP_COST', 'TASK_LOAD_STORAGE_TRUTH')
   AND SCHEDULED_TIME >= DATEADD('day', -3, CURRENT_TIMESTAMP())
 ORDER BY NAME, SCHEDULED_TIME DESC;

-- V166.4 any loader failure V166 now logs (expect no rows). A row means a run rolled back to its previous
--        fill and the task read FAILED; the self-watch ERR leg raises OPS_PIPELINE_DEGRADED for it.
SELECT LOGGED_AT, PAGE, ERROR_TYPE, CONTEXT, LEFT(ERROR_MESSAGE, 300) AS ERROR_MESSAGE
  FROM DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
 WHERE PAGE IN ('AppCost', 'StorageTruth') AND ERROR_TYPE = 'fact_load_failed'
   AND LOGGED_AT >= DATEADD('day', -7, CURRENT_TIMESTAMP())
 ORDER BY LOGGED_AT DESC;
"""


def _guarded_block(call: str, label: str, prelude: str, ok_test: str, declare: str = "") -> str:
    """One owner-run CALL in an anonymous block that reads the loader's own verdict (the backfill_365 idiom) and
    logs a failure as PAGE 'OwnerRepair'. ``label`` is a scripting expression naming the call as run (a variable
    bare, as RETURN reads it); inside the APP_ERROR_LOG INSERTs the same variable is bound (:n). ``prelude`` may
    RETURN early (a SKIP / WAIT pane)."""
    label_sql = label.replace("|| n ||", "|| :n ||")
    return f"""\
EXECUTE IMMEDIATE $$
DECLARE
    rv VARCHAR;
    emsg VARCHAR;
{declare}BEGIN
{prelude}    CALL DBA_MAINT_DB.OVERWATCH.{call};
    SELECT $1 INTO :rv FROM TABLE(RESULT_SCAN(LAST_QUERY_ID()));
    IF (rv IS NULL OR NOT ({ok_test})) THEN
        INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
        SELECT 'OwnerRepair', 'repair_verdict_failed', LEFT(COALESCE(:rv, 'no verdict returned'), 2000), 'V166 ' || {label_sql}, CURRENT_ROLE();
        RETURN 'FAILED: ' || {label} || ' -> ' || COALESCE(rv, 'no verdict returned');
    END IF;
    RETURN 'ok: ' || {label} || ' -> ' || rv;
EXCEPTION
    WHEN OTHER THEN
        emsg := SQLERRM;
        INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
        SELECT 'OwnerRepair', 'repair_call_failed', LEFT(:emsg, 2000), 'V166 ' || {label_sql}, CURRENT_ROLE();
        RETURN 'FAILED: ' || {label} || ' - ' || emsg;
END;
$$;
"""


# R166.1 admits exactly what the V166 d<=3 arm admits: its EXECUTION_STATUS + QUERY_TYPE lines, re-indented.
_d3 = sec[sec.index("    IF (d <= 3) THEN\n"):sec.index(_ELSE)]
_adm_at = _d3.index("              AND EXECUTION_STATUS = 'SUCCESS'\n")
ADMISSION = _d3[_adm_at:_d3.index("OR QUERY_TYPE ILIKE 'TRUNCATE%')\n", _adm_at) + len("OR QUERY_TYPE ILIKE 'TRUNCATE%')\n")]
assert ADMISSION.count("\n") == 9 and all(ln.startswith(" " * 14) for ln in ADMISSION.splitlines())
_SEC_HOLE_PROBE = """\
WITH qh AS (
    SELECT DATE_TRUNC('hour', START_TIME) AS HOUR_TS, COUNT(*) AS QH_ROWS
      FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
     WHERE START_TIME >= DATEADD('day', -180, CURRENT_DATE())
       AND START_TIME < DATEADD('hour', -3, CURRENT_TIMESTAMP())
""" + "".join(ln[7:] + "\n" for ln in ADMISSION.splitlines()) + """\
     GROUP BY 1
),
fact AS (
    SELECT DATE_TRUNC('hour', EVENT_TS) AS HOUR_TS, COUNT(*) AS FACT_ROWS
      FROM DBA_MAINT_DB.OVERWATCH.FACT_SECURITY_CHANGE
     WHERE EVENT_TS >= DATEADD('day', -180, CURRENT_DATE())
     GROUP BY 1
)
SELECT qh.HOUR_TS, qh.QH_ROWS, COALESCE(fact.FACT_ROWS, 0) AS FACT_ROWS,
       qh.QH_ROWS - COALESCE(fact.FACT_ROWS, 0) AS MISSING
  FROM qh
  LEFT JOIN fact ON fact.HOUR_TS = qh.HOUR_TS
 WHERE COALESCE(fact.FACT_ROWS, 0) < qh.QH_ROWS
 ORDER BY qh.HOUR_TS;
"""


def _gap_n(fact: str) -> str:
    """n := the CALL argument that reaches a fact's OLDEST calendar gap (0 = none): the same answer as
    MAX(DAYS_BACK_TO_HEAL) of the gap grid for that fact, without a generator (the harness checks both agree)."""
    return f"""\
    SELECT COALESCE(MAX(DATEDIFF('day', DATEADD('day', 1, d.DAY), CURRENT_DATE()) + 1), 0) INTO :n
      FROM (SELECT DAY, LEAD(DAY) OVER (ORDER BY DAY) AS NEXT_DAY
              FROM (SELECT DISTINCT DAY FROM DBA_MAINT_DB.OVERWATCH.{fact})) d
     WHERE DATEDIFF('day', d.DAY, d.NEXT_DAY) > 1;
"""


REPAIR_SEC = _guarded_block(
    "SP_LOAD_SECURITY_FACTS(180)", "'SP_LOAD_SECURITY_FACTS(180)'",
    prelude=("    -- never alongside the hourly graph (TASK_LOAD_SECURITY_FACTS runs after the :07 marts): the d>3\n"
             "    -- DELETE + INSERT is not one transaction and the fact key is not enforced, so an overlap doubles rows\n"
             "    IF (MINUTE(CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP())) NOT BETWEEN 30 AND 50) THEN\n"
             "        RETURN 'WAIT: run this block between :30 and :50 past the hour (Central), never :07-:25';\n"
             "    END IF;\n"),
    ok_test="rv = 'security facts loaded 180d'")
REPAIR_STORAGE = _guarded_block(
    "SP_LOAD_STORAGE_TRUTH(:n)", "'SP_LOAD_STORAGE_TRUTH(' || n || ')'",
    declare="    n INT;\n",
    prelude=(_gap_n("FACT_STORAGE_ACCOUNT_DAILY")
             + "    IF (n = 0) THEN\n"
               "        RETURN 'SKIP: FACT_STORAGE_ACCOUNT_DAILY has no calendar gap';\n"
               "    END IF;\n"
               "    n := LEAST(n, 360);\n"),
    ok_test="rv = 'OK'")
REPAIR_APP = _guarded_block(
    "SP_LOAD_APP_COST(:n)", "'SP_LOAD_APP_COST(' || n || ')'",
    declare="    n INT;\n",
    prelude=(_gap_n("FACT_APP_COST_DAILY")
             + f"    n := LEAST(GREATEST(n, {SESSION_PAD_DAYS}), 360);   -- at least {SESSION_PAD_DAYS} days: relabels the C10 sessions too\n"),
    ok_test="rv = 'OK'")

REPAIR_OUT_TEXT = f"""\
-- OWNER_REPAIRS -- V166 section. Run AFTER V166 is applied (with the rest of V166-V172), off-peak, one block at a
-- time, reading each pane. The session is pinned to Central first: the loaders key days on DATE() / CURRENT_DATE,
-- and the owner worksheet runs in UTC.
ALTER SESSION SET TIMEZONE = 'America/Chicago';

-- R166.1 OPTIONAL (read-only; 180 days of QUERY_HISTORY): the hours where FACT_SECURITY_CHANGE holds fewer
--        admitted DDL/DCL rows than QUERY_HISTORY (the V166 d<=3 arm admission). Hour grain on purpose: the
--        R2-007 holes are shorter than a day. R166.2 is idempotent, so the probe only tells you it is needed.
{_SEC_HOLE_PROBE}
-- R166.2 the R2-007 heal: the d>3 arm rebuilds 180 days of FACT_SECURITY_CHANGE from QUERY_HISTORY with the
--        current classifier. The block runs only between :30 and :50 past the hour (Central) and otherwise
--        returns WAIT. expect 'ok: SP_LOAD_SECURITY_FACTS(180) -> security facts loaded 180d'.
{REPAIR_SEC}
-- R166.3 R2-011: calendar gaps a failed run already left (the same grid as PREFLIGHT P166.2), and the failed
--        runs that predict them. R166.4 / R166.5 read the gap depth themselves.
{GRID_GAPS}
{GRID_FAILED_RUNS}
-- R166.4 storage-truth heal (cheap; about one STORAGE_USAGE row a day): runs only when FACT_STORAGE_ACCOUNT_DAILY
--        has a gap, reaching its oldest gap (at most 360 days). expect 'SKIP: ...' or 'ok: ... -> OK'.
{REPAIR_STORAGE}
-- R166.5 app-cost heal, ONE call, OFF-PEAK (the heavy SESSIONS x QUERY_HISTORY x QUERY_ATTRIBUTION_HISTORY join):
--        at least {SESSION_PAD_DAYS} days so the {SESSION_PAD_DAYS}-day session lookback relabels recent days (C10), deeper when
--        R166.3 shows an older gap (at most 360 days). Since V166 the reload is one transaction, so a timeout
--        rolls back and keeps the previous fill; re-run it on a warehouse whose STATEMENT_TIMEOUT_IN_SECONDS
--        allows the join. expect 'ok: SP_LOAD_APP_COST(<n>) -> OK'.
{REPAIR_APP}
-- R166.6 OPTIONAL heals in snowflake/backfill_365.sql (not V166 SQL; nothing here runs them):
--        * the MART_CLOUD_SVC_DAILY arm (the INSERT right after FACT_QUERY_DAILY) fills the cloud-services
--          statement mart's history from QUERY_HISTORY; on an existing install select and run only that INSERT
--          (idempotent: it writes only days before the mart's first day);
--        * the commented SP_LOAD_OBJECT_COST(365) block after the hourly-graph RESUME reloads a year of the
--          object-cost ledger; run it once, off-peak, away from the 06:45 CT daily task.
"""

for _env, _text in (("PREFLIGHT_OUT", PREFLIGHT), ("PART_B_OUT", PART_B), ("REPAIR_OUT", REPAIR_OUT_TEXT)):
    _path = os.environ.get(_env)
    if _path:
        assert "\\" not in _text and "@" not in _text, _env
        Path(_path).write_text(_text, encoding="utf-8", newline="\n")
        print(f"wrote {_env} {_path}")

print(f"V166 written: {target} ({len(out)} bytes)")
