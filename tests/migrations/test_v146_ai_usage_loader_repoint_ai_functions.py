"""Locks for V146 — repoint the FACT_AI_USAGE_DAILY loader's ai_functions arm to the canonical view.

The [9] ai_functions arm of SP_LOAD_MARTS_V27 read the FROZEN
ACCOUNT_USAGE.CORTEX_FUNCTIONS_USAGE_HISTORY ("no longer updated"), so the mart's 'Functions' rows
went stale. V146 re-derives the proc from V142, repointing ONLY that arm onto the canonical
CORTEX_AI_FUNCTIONS_USAGE_HISTORY. That view is not a drop-in: CREDITS (not TOKEN_CREDITS);
START_TIME is TIMESTAMP_LTZ (so FIRST_TS/LAST_TS cast ::TIMESTAMP_NTZ); and there is no scalar
TOKENS column — token counts live in the METRICS array, summed via LATERAL FLATTEN, with
CREDITS/REQUESTS deduped to once per source row across the fan-out.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_MIG = _ROOT / "snowflake" / "migrations"
_V146 = (_MIG / "V146__ai_usage_loader_repoint_ai_functions.sql").read_text(encoding="utf-8")
_V142 = (_MIG / "V142__posture_arm_single_scan.sql").read_text(encoding="utf-8")


def _read(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


def _proc(text: str) -> str:
    s = text.index("CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_LOAD_MARTS_V27")
    o = text.index("$$", s)
    return text[s:text.index("$$;", o + 2) + 3]


# The one repointed arm, verbatim (kept in step with outputs/gen_v146.py).
_OLD_ARM = """                SELECT START_TIME::DATE AS DAY,
                       'ACCOUNT' AS USER_NAME,
                       'Functions' AS SOURCE,
                       COALESCE(MODEL_NAME, 'n/a') AS MODEL_NAME,
                       NULL AS EMAIL,
                       MIN(START_TIME) AS FIRST_TS,
                       MAX(START_TIME) AS LAST_TS,
                       COUNT(*) AS REQUESTS,
                       SUM(COALESCE(TOKENS, 0)) AS TOKENS,
                       ROUND(SUM(COALESCE(TOKEN_CREDITS, 0)), 6) AS CREDITS
                FROM SNOWFLAKE.ACCOUNT_USAGE.CORTEX_FUNCTIONS_USAGE_HISTORY
                WHERE START_TIME >= DATEADD('day', -:d, CURRENT_DATE())
                GROUP BY 1, 2, 3, 4"""

_NEW_ARM = """                -- V146: repointed off the FROZEN CORTEX_FUNCTIONS_USAGE_HISTORY onto the canonical
                -- CORTEX_AI_FUNCTIONS_USAGE_HISTORY. Not a drop-in: TOKEN_CREDITS -> CREDITS; START_TIME
                -- is TIMESTAMP_LTZ (was NTZ) so FIRST_TS/LAST_TS cast ::TIMESTAMP_NTZ (same TZ->NTZ MERGE
                -- guard as the ai_code arm, V078); and there is NO scalar TOKENS column -- token counts
                -- live in the METRICS ARRAY as {"key":{"metric":"input"|"output","unit":"tokens"},"value":N},
                -- so LATERAL FLATTEN sums value where unit='tokens'. CREDITS + REQUESTS are deduped to
                -- once per source row via COALESCE(m.INDEX,0)=0 (OUTER=>TRUE emits a NULL-index row for
                -- empty METRICS, still counted once) so the FLATTEN fan-out cannot multiply them.
                SELECT f.START_TIME::DATE AS DAY,
                       'ACCOUNT' AS USER_NAME,
                       'Functions' AS SOURCE,
                       COALESCE(NULLIF(f.MODEL_NAME, ''), 'n/a') AS MODEL_NAME,
                       NULL AS EMAIL,
                       MIN(f.START_TIME)::TIMESTAMP_NTZ AS FIRST_TS,
                       MAX(f.START_TIME)::TIMESTAMP_NTZ AS LAST_TS,
                       COUNT(CASE WHEN COALESCE(m.INDEX, 0) = 0 THEN 1 END) AS REQUESTS,
                       SUM(CASE WHEN m.VALUE:key:unit::STRING = 'tokens'
                                THEN m.VALUE:value::NUMBER ELSE 0 END) AS TOKENS,
                       ROUND(SUM(CASE WHEN COALESCE(m.INDEX, 0) = 0
                                      THEN COALESCE(f.CREDITS, 0) ELSE 0 END), 6) AS CREDITS
                FROM SNOWFLAKE.ACCOUNT_USAGE.CORTEX_AI_FUNCTIONS_USAGE_HISTORY f,
                     LATERAL FLATTEN(input => f.METRICS, OUTER => TRUE) m
                WHERE f.START_TIME >= DATEADD('day', -:d, CURRENT_DATE())
                GROUP BY 1, 2, 3, 4"""


def test_v146_guarded_and_ordered():
    assert "EXCEPTION (-20146" in _V146 and "IF (v < 145) THEN" in _V146
    assert "SELECT 146 AS VERSION" in _V146 and "WHERE VERSION = 146)" in _V146


def test_v146_repoints_the_ai_functions_arm_to_the_canonical_view():
    p = _proc(_V146)
    # the frozen view is gone from the proc; the canonical view is the sole AI-functions read
    assert "SNOWFLAKE.ACCOUNT_USAGE.CORTEX_FUNCTIONS_USAGE_HISTORY" not in p
    assert p.count("SNOWFLAKE.ACCOUNT_USAGE.CORTEX_AI_FUNCTIONS_USAGE_HISTORY f") == 1
    # V142 (the baseline) DID read the frozen view — guards against a stale baseline
    assert "SNOWFLAKE.ACCOUNT_USAGE.CORTEX_FUNCTIONS_USAGE_HISTORY" in _proc(_V142)


def test_v146_token_math_and_tz_cast():
    p = _proc(_V146)
    # tokens summed from the METRICS array where unit='tokens' (no scalar TOKENS column)
    assert "LATERAL FLATTEN(input => f.METRICS, OUTER => TRUE) m" in p
    assert "m.VALUE:key:unit::STRING = 'tokens'" in p and "m.VALUE:value::NUMBER" in p
    # CREDITS/REQUESTS deduped once per source row across the fan-out
    assert "COALESCE(m.INDEX, 0) = 0" in p
    # the fact's FIRST_TS/LAST_TS are NTZ but START_TIME is LTZ -> explicit cast
    assert "MIN(f.START_TIME)::TIMESTAMP_NTZ" in p and "MAX(f.START_TIME)::TIMESTAMP_NTZ" in p
    # credits come from CREDITS (the canonical view has no TOKEN_CREDITS column). The parity test
    # pins the exact arm; TOKEN_CREDITS still legitimately appears elsewhere (the ai_code arm's
    # Cortex Code views keep that column, and this arm's comment documents the rename).
    assert "COALESCE(f.CREDITS, 0)" in p


def test_v146_ai_code_arm_untouched():
    # the sibling arm (Cortex Code views) must be byte-identical — only ai_functions moves
    p = _proc(_V146)
    assert p.count("CORTEX_CODE_SNOWSIGHT_USAGE_HISTORY") == 1
    assert p.count("CORTEX_CODE_CLI_USAGE_HISTORY") == 1


def test_v146_proc_matches_v142_except_the_ai_functions_arm():
    # proc-only re-derive: reverting the one repointed arm reproduces the V142 proc exactly.
    p142, p146 = _proc(_V142), _proc(_V146)
    assert p142 != p146
    assert _OLD_ARM in p142 and _NEW_ARM in p146
    assert p146.replace(_NEW_ARM, _OLD_ARM) == p142, \
        "V146 changed SP_LOAD_MARTS_V27 beyond the ai_functions arm"


def test_validate_and_docs_track_v146():
    val = _read("snowflake/validate.sql")
    assert "V001..V155 applied" in val and "VERSION BETWEEN 1 AND 155) = 155" in val
    for rel in ("DEPLOYMENT.md", "README.md"):
        assert "V146__ai_usage_loader_repoint_ai_functions.sql" in _read(rel)


def test_v146_in_expected_migrations():
    from app.ui.pages.admin import _EXPECTED_MIGRATIONS
    assert 146 in _EXPECTED_MIGRATIONS


def test_v146_plain_sql_parses():
    sqlglot = pytest.importorskip("sqlglot")
    from tests.test_migrations_parse import _plain_statements
    for statement in _plain_statements(_V146):
        sqlglot.parse(statement, dialect="snowflake")
