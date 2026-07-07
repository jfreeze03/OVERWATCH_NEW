"""Cortex COMPLETE execution for in-app AI evaluations.

Runtime rules:
- Never auto-runs: pages call this only from an explicit button press, and
  every surface labels that the call spends Cortex credits.
- The prompt is passed as a bound literal (sqlsafe), the model name is
  validated, and failures come back as friendly errors — never a broken page.
"""

from __future__ import annotations

import re

from app.core.errors import format_snowflake_error, record_error
from app.core.session import (
    apply_query_tag,
    apply_statement_timeout,
    build_query_tag,
    get_session,
)
from app.core.sqlsafe import sql_literal

_MODEL_RE = re.compile(r"^[a-z0-9][a-z0-9.\-]{1,60}$")
_DEFAULT_MODEL = "llama3.1-8b"
MAX_PROMPT_CHARS = 6000
# Cortex COMPLETE runs from an explicit button press with a spinner on screen;
# if the model hangs, cut it off rather than freezing the page indefinitely.
CORTEX_TIMEOUT_SECONDS = 90


def normalize_model(model: object) -> str:
    text = str(model or "").strip().lower()
    return text if _MODEL_RE.match(text) else _DEFAULT_MODEL


def cortex_complete(prompt: str, model: object = _DEFAULT_MODEL, *, page: str = "AI") -> tuple[bool, str]:
    """Run SNOWFLAKE.CORTEX.COMPLETE; returns (ok, answer_or_error)."""
    text = str(prompt or "").strip()[:MAX_PROMPT_CHARS]
    if not text:
        return False, "Nothing to evaluate: the evidence set is empty."
    model_name = normalize_model(model)
    sql = (
        f"SELECT SNOWFLAKE.CORTEX.COMPLETE('{model_name}', {sql_literal(text)}) AS ANSWER"
    )
    try:
        session = get_session()
        apply_query_tag(session, build_query_tag(page=page, tier="cortex"))
        apply_statement_timeout(session, CORTEX_TIMEOUT_SECONDS)
        rows = session.sql(sql).collect()
        answer = str(rows[0]["ANSWER"]) if rows else ""
        return (True, answer) if answer.strip() else (False, "Cortex returned an empty answer.")
    except Exception as exc:
        record_error(page, exc, context=f"cortex_complete model={model_name}")
        return False, format_snowflake_error(exc)
