"""Tiered, cached query engine.

Design contracts (each closes an old-app finding):
- The cached functions RAISE on failure. Streamlit does not cache exceptions,
  so an error can never pin an empty frame for the TTL (finding H1).
- The cache key includes the caller-supplied scope string, which pages build
  from company/environment/window/filters AND current role (finding C2).
- Row caps fetch n+1 and mark ``truncated``; the UI banners it (finding M1).
- The public ``run()`` returns a typed QueryResult; pages branch on ``ok``.
"""

from __future__ import annotations

import time
from datetime import datetime

import pandas as pd
import streamlit as st

from app.config import DEFAULT_MAX_ROWS
from app.core.errors import format_snowflake_error, record_error
from app.core.result import QueryResult
from app.core.session import apply_query_tag, apply_statement_timeout, build_query_tag, get_session

CACHE_TTLS = {"live": 30, "recent": 300, "historical": 3600, "metadata": 14400}
STATEMENT_TIMEOUTS = {"live": 30, "recent": 120, "historical": 180, "metadata": 30}

_TELEMETRY_KEY = "_ow_query_telemetry"
_TELEMETRY_MAX = 200


def _normalize(df: pd.DataFrame) -> pd.DataFrame:
    if df is None:
        return pd.DataFrame()
    df.columns = [str(c).upper() for c in df.columns]
    return df


def _execute(sql: str, tier: str, page: str) -> pd.DataFrame:
    session = get_session()
    apply_query_tag(session, build_query_tag(page=page, tier=tier))
    apply_statement_timeout(session, STATEMENT_TIMEOUTS.get(tier, 120))
    return _normalize(session.sql(sql).to_pandas())


# One cached function per tier: st.cache_data TTL is fixed at decoration time.
# ``scope`` is part of the key on purpose — see module docstring.

@st.cache_data(ttl=CACHE_TTLS["live"], show_spinner=False)
def _fetch_live(sql: str, scope: str, page: str = "") -> pd.DataFrame:
    return _execute(sql, "live", page)


@st.cache_data(ttl=CACHE_TTLS["recent"], show_spinner=False)
def _fetch_recent(sql: str, scope: str, page: str = "") -> pd.DataFrame:
    return _execute(sql, "recent", page)


@st.cache_data(ttl=CACHE_TTLS["historical"], show_spinner=False)
def _fetch_historical(sql: str, scope: str, page: str = "") -> pd.DataFrame:
    return _execute(sql, "historical", page)


@st.cache_data(ttl=CACHE_TTLS["metadata"], show_spinner=False)
def _fetch_metadata(sql: str, scope: str, page: str = "") -> pd.DataFrame:
    return _execute(sql, "metadata", page)


_FETCHERS = {
    "live": _fetch_live,
    "recent": _fetch_recent,
    "historical": _fetch_historical,
    "metadata": _fetch_metadata,
}


def _cache_scope(extra: str) -> str:
    """Cache identity beyond the SQL text itself.

    The SQL string is a cache-key argument to every tier fetcher, and every
    filter a builder honors is baked into its SQL — so filters do NOT belong
    here. (They used to: the full filters signature cold-started every query
    on the page whenever ANY filter changed, even ones the query ignored.)
    Scope is what the SQL cannot express: who is asking (role decides row
    visibility under SiS) and the manual refresh generation.
    """
    role = str(st.session_state.get("_ow_current_role", "") or "")
    salt = str(st.session_state.get("_ow_refresh_salt", "") or "")
    return f"role={role}|salt={salt}|{extra}"


def _telemetry(page: str, tier: str, key: str, elapsed_ms: float, rows: int, ok: bool) -> None:
    try:
        entries = st.session_state.setdefault(_TELEMETRY_KEY, [])
        entries.append({
            "at": datetime.now().isoformat(timespec="seconds"),
            "page": page or "unknown",
            "tier": tier,
            "key": key[:60],
            "elapsed_ms": round(elapsed_ms, 1),
            "rows": int(rows),
            "ok": bool(ok),
        })
        del entries[:-_TELEMETRY_MAX]
    except Exception:
        pass


def query_telemetry() -> pd.DataFrame:
    return pd.DataFrame(st.session_state.get(_TELEMETRY_KEY, []))


def bump_refresh_salt() -> None:
    """Invalidate OVERWATCH's cached reads (the Refresh button)."""
    st.session_state["_ow_refresh_salt"] = datetime.now().isoformat()


def run(
    sql: str,
    *,
    page: str,
    key: str,
    tier: str = "recent",
    source: str = "",
    max_rows: int = DEFAULT_MAX_ROWS,
) -> QueryResult:
    """Execute through the tiered cache and return a typed QueryResult.

    Never raises: failures come back as ok=False with a friendly error string,
    and are recorded to the error buffer/sink. Failures are never cached.
    """
    tier = tier if tier in _FETCHERS else "recent"
    started = time.perf_counter()
    try:
        capped_sql = sql
        cap = int(max_rows) if max_rows else 0
        if cap > 0 and "LIMIT" not in sql.upper():
            capped_sql = f"{sql.rstrip().rstrip(';')}\nLIMIT {cap + 1}"
        df = _FETCHERS[tier](capped_sql, _cache_scope(key), page)
        truncated = bool(cap) and len(df) > cap
        if truncated:
            df = df.head(cap)
        elapsed = (time.perf_counter() - started) * 1000
        _telemetry(page, tier, key, elapsed, len(df), ok=True)
        return QueryResult(
            df=df, ok=True, truncated=truncated, source=source, tier=tier,
            fetched_at=datetime.now(), elapsed_ms=elapsed,
        )
    except Exception as exc:
        elapsed = (time.perf_counter() - started) * 1000
        _telemetry(page, tier, key, elapsed, 0, ok=False)
        record_error(page, exc, context=f"query key={key} tier={tier}")
        return QueryResult(
            df=pd.DataFrame(), ok=False, error=format_snowflake_error(exc),
            source=source, tier=tier, fetched_at=datetime.now(), elapsed_ms=elapsed,
        )


def execute_statement(sql: str, *, page: str) -> tuple[bool, str]:
    """Run a single state-changing statement (operator actions only).

    Callers gate this behind role + typed confirmation. Returns (ok, message).
    """
    try:
        session = get_session()
        apply_query_tag(session, build_query_tag(page=page, tier="write"))
        session.sql(sql).collect()
        return True, "Statement executed."
    except Exception as exc:
        record_error(page, exc, context=f"execute_statement: {sql[:200]}")
        return False, format_snowflake_error(exc)
