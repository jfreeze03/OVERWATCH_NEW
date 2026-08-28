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

import re
import time
from collections import OrderedDict
from datetime import datetime
from threading import RLock

import pandas as pd
import streamlit as st

from app.config import DEFAULT_MAX_ROWS, core_object
from app.core.errors import format_snowflake_error, record_error
from app.core.result import QueryResult
from app.core.session import apply_query_tag, apply_statement_timeout, build_query_tag, get_session
from app.core.sqlsafe import sql_literal

CACHE_TTLS = {"live": 30, "recent": 300, "hourly": 3600, "historical": 3600, "metadata": 14400}
# "hourly" (r13 #3): mart/fact reads whose SOURCES load hourly or daily -
# a 300s TTL re-paid them 12x/hour (fleet evidence 2026-07-11: 1.5-3.4%
# cache hits with one viewer). Refresh button still clears instantly.
STATEMENT_TIMEOUTS = {"live": 30, "recent": 120, "hourly": 120, "historical": 180, "metadata": 30}
BATCH_MAX_CONCURRENCY = 4
_BATCH_MEMBER_CACHE_MAX_ENTRIES = 128
_BATCH_MEMBER_CACHE_MAX_BYTES = 128 * 1024 * 1024

_TELEMETRY_KEY = "_ow_query_telemetry"
_TELEMETRY_MAX = 200

# A real row cap already present in the statement, not just the word "limit"
# somewhere in a column name (RATE_LIMIT) or comment — those used to disable
# the cap silently, leaving the query unbounded.
_LIMIT_RE = re.compile(r"\bLIMIT\s+\d+", re.IGNORECASE)
# r10 #6: only a TRAILING limit bounds the OUTER result — a subquery's
# LIMIT deep inside the text used to disable the cap and leave the outer
# statement unbounded.
_TAIL_LIMIT_RE = re.compile(r"\bLIMIT\s+\d+\s*;?\s*$", re.IGNORECASE)

# Fleet telemetry (V021): persist only what matters for regressions — slow or
# failed fetches — so viewers' sessions feed Admin > Performance without an
# INSERT per query. Fire-and-forget; first failure disables for the session.
TELEMETRY_PERSIST_MS = 2000.0
_TELEMETRY_PERSIST_CAP = 60  # NON-failure rows per session: a broken page can't spam
_TELEMETRY_FAIL_CAP = 20     # #28: reserved FAILURE budget beyond the row cap — failures are
                             #      the most valuable rows, so chatty healthy/slow traffic must
                             #      not crowd them out, but a broken page still can't spam forever
_TELEMETRY_SAMPLE_RATE = 0.02  # rec18: the healthy-baseline sample; persisted as SAMPLE_PROB so Admin can re-weight


def should_persist_telemetry(elapsed_ms: float, ok: bool, persisted: int,
                             threshold_ms: float = TELEMETRY_PERSIST_MS,
                             cap: int = _TELEMETRY_PERSIST_CAP,
                             sample_roll: float | None = None,
                             sample_rate: float = 0.02,
                             failed_persisted: int = 0,
                             fail_cap: int = _TELEMETRY_FAIL_CAP) -> bool:
    """Pure gate: failed qualifies (on its own reserved budget), slow qualifies, capped.

    ``sample_roll`` (caller passes random()) additionally persists ~2% of ALL
    fetches so the fleet view sees the healthy baseline, not just the tail —
    without it, p50 by page is invisible in APP_QUERY_TELEMETRY (Codex #19).

    #28: a FAILURE is short-circuited AHEAD of the ``cap`` and drawn from its own
    ``fail_cap`` reserved budget (counted by ``failed_persisted``), so chatty
    healthy/slow rows that filled ``cap`` early can no longer suppress the very rows
    an operator most needs — while a broken page still can't spam without bound.
    ``persisted`` is the NON-failure count and governs only the healthy/slow streams.
    """
    if not ok:
        return int(failed_persisted) < int(fail_cap)
    if persisted >= cap:
        return False
    if float(elapsed_ms) >= float(threshold_ms):
        return True
    return sample_roll is not None and float(sample_roll) < float(sample_rate)


def _resolve_telemetry_shape() -> None:
    """Determine the LIVE APP_QUERY_TELEMETRY column shape ONCE, synchronously and
    OBSERVED, and cache the result as the same downgrade flags the buffered writer reads.

    Codex #30: the buffered flush submits async (collect_nowait), which returns 'submitted'
    BEFORE the server validates the statement — so a 12-col INSERT against the live 10-col
    table failed server-side UNOBSERVED and every telemetry row was silently dropped for the
    whole session, while the 12->10->6 downgrade (keyed on a SYNCHRONOUS failure that never
    happened) never fired. Reading the real column list is a describe — no data scanned, and
    the result IS observed — so the shape is resolved deterministically on the first persist
    and rows are formatted at the correct shape from the very first one, never mis-shaped.
    Cached in session_state; the shape is committed at most once per session, but ONLY
    after the column list actually resolves — a transient describe failure leaves the
    resolved flag UNSET so the next persist retries (#27)."""
    if st.session_state.get("_ow_qtel_shape_resolved"):
        return
    try:
        session = get_session()
        # .columns triggers a describe (synchronous, observed) without scanning rows.
        cols = {str(c).upper() for c in
                session.sql(f"SELECT * FROM {core_object('APP_QUERY_TELEMETRY')} LIMIT 0").columns}
    except Exception:
        # #27: transient describe failure (session blip / momentary no-grant). Do NOT mark
        # the shape resolved — a guessed shape cached for the whole session silently drops
        # every telemetry row against an older/mismatched table. Leave the flag unset; the
        # next persist retries. A truly absent table still trips _ow_qtel_off on the write.
        return
    if not cols:
        return                                            # nothing resolved -> retry next time
    # The column list resolved for real: NOW commit the shape decision, exactly once.
    st.session_state["_ow_qtel_shape_resolved"] = True
    if {"SAMPLE_PROB", "QUERY_ID"} <= cols:
        return                                            # newest 12-col shape: no flag
    if {"CACHE_HIT", "SQL_HASH", "BATCH_SIZE", "TRUNCATED"} <= cols:
        st.session_state["_ow_qtel_prev64shape"] = True   # 10-col (pre-SAMPLE_PROB/QUERY_ID)
    else:
        st.session_state["_ow_qtel_oldshape"] = True      # 6-col legacy (pre-V027)


def _persist_telemetry(page: str, tier: str, key: str, elapsed_ms: float,
                       rows: int, ok: bool, cache_hit: bool | None = None,
                       sql_hash: str | None = None, batch_size: int | None = None,
                       truncated: bool | None = None, query_id: str | None = None) -> None:
    def _b(v):
        return "NULL" if v is None else ("TRUE" if v else "FALSE")

    try:
        if st.session_state.get("_ow_qtel_off"):
            return
        done = int(st.session_state.get("_ow_qtel_n", 0))
        fail_done = int(st.session_state.get("_ow_qtel_fail_n", 0))
        import random as _random
        if not should_persist_telemetry(elapsed_ms, ok, done, sample_roll=_random.random(),
                                        sample_rate=_TELEMETRY_SAMPLE_RATE,
                                        failed_persisted=fail_done):
            # #28: expose a dropped-rows counter so the fleet view can tell 'quiet' from
            # 'sampling-capped'. A dropped FAILURE (budget exhausted) is the loudest signal.
            st.session_state["_ow_qtel_dropped"] = int(st.session_state.get("_ow_qtel_dropped", 0)) + 1
            if not ok:
                st.session_state["_ow_qtel_dropped_fail"] = \
                    int(st.session_state.get("_ow_qtel_dropped_fail", 0)) + 1
            return
        # #28: failures draw from their own reserved budget, healthy/slow from the row cap.
        if ok:
            st.session_state["_ow_qtel_n"] = done + 1
        else:
            st.session_state["_ow_qtel_fail_n"] = fail_done + 1
        base = (
            f"{sql_literal(str(page)[:80])}, {sql_literal(str(tier)[:20])}, "
            f"{sql_literal(str(key)[:120])}, {round(float(elapsed_ms), 1)}, "
            f"{int(rows)}, {'TRUE' if ok else 'FALSE'}"
        )
        v27 = (f", {_b(cache_hit)}, "
               f"{sql_literal(str(sql_hash)[:64]) if sql_hash else 'NULL'}, "
               f"{int(batch_size) if batch_size is not None else 'NULL'}, {_b(truncated)}")
        # rec18: this row's SAMPLE_PROB is the probability it was persisted -- 1.0 for
        # the must-persist stream (failed OR slow), the sample rate for the healthy
        # stream -- so Admin > Performance can re-weight healthy rows (1/prob) and read
        # an UNBIASED fleet p50/p95. QUERY_ID joins the row to ACCOUNT_USAGE.QUERY_HISTORY.
        sample_prob = (1.0 if ((not ok) or float(elapsed_ms) >= TELEMETRY_PERSIST_MS)
                       else _TELEMETRY_SAMPLE_RATE)
        qid = sql_literal(str(query_id)[:64]) if query_id else "NULL"
        col = f"INSERT INTO {core_object('APP_QUERY_TELEMETRY')} "
        # #30: resolve the real column shape ONCE, synchronously/observed, BEFORE formatting
        # this row — so the buffered async flush (which cannot observe a shape mismatch) never
        # serializes rows at the wrong shape and silently loses them. Sets the SAME downgrade
        # flags below; the lazy 2-strikes ladder in _flush_group remains as a secondary net.
        _resolve_telemetry_shape()
        # N12: enqueue, not one INSERT round trip per row; the buffered flush is a
        # single multi-row INSERT ... SELECT ... UNION ALL. THREE-level downgrade so
        # the app degrades cleanly against an older schema: V064 (12-col) -> V027
        # (10-col, pre-SAMPLE_PROB/QUERY_ID) -> legacy (6-col, pre-V027). Each flush
        # shape-mismatch sets the next-lower flag for the following call.
        if st.session_state.get("_ow_qtel_oldshape"):
            prefix = col + "(PAGE, TIER, QUERY_KEY, ELAPSED_MS, ROWS_RETURNED, OK) "
            _buffer_write(prefix, "SELECT " + base, off_flag="_ow_qtel_off")
        elif st.session_state.get("_ow_qtel_prev64shape"):
            prefix = (col + "(PAGE, TIER, QUERY_KEY, ELAPSED_MS, ROWS_RETURNED, OK, "
                      "CACHE_HIT, SQL_HASH, BATCH_SIZE, TRUNCATED) ")
            _buffer_write(prefix, "SELECT " + base + v27,
                          off_flag="_ow_qtel_off", downgrade_flag="_ow_qtel_oldshape")
        else:
            prefix = (col + "(PAGE, TIER, QUERY_KEY, ELAPSED_MS, ROWS_RETURNED, OK, "
                      "CACHE_HIT, SQL_HASH, BATCH_SIZE, TRUNCATED, SAMPLE_PROB, QUERY_ID) ")
            _buffer_write(prefix, "SELECT " + base + v27 + f", {sample_prob}, {qid}",
                          off_flag="_ow_qtel_off", downgrade_flag="_ow_qtel_prev64shape")
    except Exception:
        # Table missing (pre-V021) or no INSERT grant: stop trying this session.
        st.session_state["_ow_qtel_off"] = True


def _classify_error(exc: object) -> str:
    """Typed failure kind from the RAW exception text (Codex r10 #4) —
    classify BEFORE format_snowflake_error prettifies the markers away."""
    s = str(exc or "").lower()
    if "does not exist or not authorized" in s:
        return "absent"
    if "unknown function" in s:
        return "unknown_function"
    # wave-2 review #3: an optional COLUMN missing on an EXISTING view (the
    # documented degrade path for TOKENS_GRANULAR / QUERY_INSIGHTS schema drift)
    # is a compilation "invalid identifier" — a probe read must treat it like
    # an absent object, not error-log it on every render.
    if "invalid identifier" in s:
        return "missing_column"
    if "statement reached its statement or warehouse timeout" in s or "timeout" in s:
        return "timeout"
    return "other"


def _quarantine_key(page: str, key: str, sql: str) -> str:
    """Namespace quarantine entries (r20 #2). Short member keys like 'act'
    repeat across pages, so a Control Room failure must not force an
    unrelated member with the same key onto the serial path elsewhere —
    page + key + a hash of the actual SQL is the identity that failed."""
    import hashlib
    return f"{page}:{key}:{hashlib.sha1(str(sql).encode()).hexdigest()[:8]}"


def _with_row_cap(sql: str, cap: int) -> str:
    """Make max_rows authoritative (Codex r18 #1).

    No trailing LIMIT: append ``LIMIT cap+1``. A trailing LIMIT larger than
    cap+1 is rewritten DOWN to cap+1 — a 20,000-row detail reader must honor
    a 1-row canary cap. A smaller trailing LIMIT already answers within
    budget and is kept. Fetching cap+1 lets the caller detect truncation
    honestly (n+1 rows back means the cap was hit) — see run()/run_batch().
    """
    if cap <= 0:
        return sql
    tail = _TAIL_LIMIT_RE.search(sql.rstrip())
    if tail:
        n = int(re.search(r"\d+", tail.group(0)).group(0))
        if n <= cap + 1:
            return sql
        return _TAIL_LIMIT_RE.sub(f"LIMIT {cap + 1}", sql.rstrip())
    return f"{sql.rstrip().rstrip(';')}\nLIMIT {cap + 1}"


def _normalize(df: pd.DataFrame) -> pd.DataFrame:
    if df is None:
        return pd.DataFrame()
    df.columns = [str(c).upper() for c in df.columns]
    return df


# Cache-hit detection (V027 telemetry rider): the tier fetchers are
# st.cache_data-wrapped, so their BODY only runs on a miss. _execute flips
# this sentinel; run() resets it before the fetch and reads it after.
from contextvars import ContextVar  # noqa: E402  (kept beside its single use)

# Context-local (Codex r9 #4): the old module dict raced across concurrent
# Streamlit session threads, corrupting cache-hit telemetry either way.
_FETCH_MISS: ContextVar[bool] = ContextVar("ow_fetch_miss", default=False)
_LAST_QUERY_ID: ContextVar[str] = ContextVar("ow_last_query_id", default="")
# P3: per-member gather time for the batch just executed, {member index: ms}.
# It cannot ride the RETURN value: that value is st.cache_data-cached, so a
# cache hit would replay the original miss's durations and re-inflate exactly
# what this fix removes. Same sentinel discipline as _FETCH_MISS — _execute_batch
# fills it, run_batch resets it before the fetch and reads it after, and an empty
# dict (cache hit) means "no server work happened", so wall time stands in.
_BATCH_MEMBER_MS: ContextVar[dict | None] = ContextVar("ow_batch_member_ms", default=None)
# #43: per-member Snowflake QUERY_ID for the batch just executed, {member index: qid}.
# The batch path submits every member async (each job handle carries its own query_id),
# but the member telemetry never captured it — so the QUERY_HISTORY correction the timing
# comments promise (batch member gather time is submission-order, not the query's true
# duration) had nothing to join on. Captured off the job handles here, threaded into the
# member telemetry in run_batch. Same sentinel discipline as _BATCH_MEMBER_MS: it cannot
# ride the cached RETURN value (a cache hit would replay a stale run's ids), and an empty
# dict (cache hit) means no job ran, so the id is honestly blank.
_BATCH_MEMBER_QID: ContextVar[dict | None] = ContextVar("ow_batch_member_qid", default=None)

# A batch used to cache only the full SQL tuple. Changing one filter therefore
# cold-started every sibling even when six of seven statements were unchanged.
# Keep a small process-local member cache in front of the tuple cache so a batch
# submits only genuine misses. The key includes the fetcher identity (tests and
# hot-reloads cannot inherit an old implementation), tier, capped SQL and the
# same role/user/salt scope used by run(). Frames are copied on both sides, like
# st.cache_data, so display-layer mutation cannot poison another viewer.
_BatchMemberEntry = tuple[pd.DataFrame, bool, float, int]
_BATCH_MEMBER_CACHE: OrderedDict[tuple, _BatchMemberEntry] = OrderedDict()
_BATCH_MEMBER_CACHE_BYTES = 0
_BATCH_MEMBER_CACHE_LOCK = RLock()


def _batch_member_cache_key(tier: str, sql: str, scope: str) -> tuple:
    return (id(_BATCH_FETCHERS.get(tier)), tier, sql, scope)


def _batch_member_cache_get(tier: str, sql: str, scope: str) -> tuple[pd.DataFrame, bool] | None:
    global _BATCH_MEMBER_CACHE_BYTES
    key = _batch_member_cache_key(tier, sql, scope)
    now = time.monotonic()
    with _BATCH_MEMBER_CACHE_LOCK:
        entry = _BATCH_MEMBER_CACHE.get(key)
        if entry is None:
            return None
        frame, truncated, expires_at, size = entry
        if expires_at <= now:
            _BATCH_MEMBER_CACHE.pop(key, None)
            _BATCH_MEMBER_CACHE_BYTES = max(0, _BATCH_MEMBER_CACHE_BYTES - size)
            return None
        _BATCH_MEMBER_CACHE.move_to_end(key)
        return frame.copy(deep=True), truncated


def _batch_member_cache_put(
    tier: str,
    sql: str,
    scope: str,
    frame: pd.DataFrame,
    truncated: bool,
) -> None:
    global _BATCH_MEMBER_CACHE_BYTES
    try:
        size = int(frame.memory_usage(index=True, deep=True).sum())
    except Exception:  # Cache admission must never fail a read.
        return
    if size > _BATCH_MEMBER_CACHE_MAX_BYTES // 2:
        return
    key = _batch_member_cache_key(tier, sql, scope)
    stored = frame.copy(deep=True)
    entry: _BatchMemberEntry = (
        stored,
        bool(truncated),
        time.monotonic() + CACHE_TTLS[tier],
        size,
    )
    with _BATCH_MEMBER_CACHE_LOCK:
        old = _BATCH_MEMBER_CACHE.pop(key, None)
        if old is not None:
            _BATCH_MEMBER_CACHE_BYTES = max(0, _BATCH_MEMBER_CACHE_BYTES - old[3])
        _BATCH_MEMBER_CACHE[key] = entry
        _BATCH_MEMBER_CACHE_BYTES += size
        while (
            len(_BATCH_MEMBER_CACHE) > _BATCH_MEMBER_CACHE_MAX_ENTRIES
            or _BATCH_MEMBER_CACHE_BYTES > _BATCH_MEMBER_CACHE_MAX_BYTES
        ):
            _, evicted = _BATCH_MEMBER_CACHE.popitem(last=False)
            _BATCH_MEMBER_CACHE_BYTES = max(0, _BATCH_MEMBER_CACHE_BYTES - evicted[3])


def _batch_member_cache_clear() -> None:
    """Test/hot-reload hook; refresh uses scoped keys and needs no global flush."""
    global _BATCH_MEMBER_CACHE_BYTES
    with _BATCH_MEMBER_CACHE_LOCK:
        _BATCH_MEMBER_CACHE.clear()
        _BATCH_MEMBER_CACHE_BYTES = 0


def _execute(sql: str, tier: str, page: str) -> pd.DataFrame:
    _FETCH_MISS.set(True)
    session = get_session()
    apply_query_tag(session, build_query_tag(page=page, tier=tier))
    apply_statement_timeout(session, STATEMENT_TIMEOUTS.get(tier, 120))
    # Async submit purely to get the job handle: it carries the Snowflake
    # QUERY_ID (v4.51, Codex #16) — the sync path never exposed it.
    try:
        job = session.sql(sql).to_pandas(block=False)
        _LAST_QUERY_ID.set(str(getattr(job, "query_id", "") or ""))
        return _normalize(job.result())
    except AttributeError:
        _LAST_QUERY_ID.set("")
        return _normalize(session.sql(sql).to_pandas())


# One cached function per tier: st.cache_data TTL is fixed at decoration time.
# ``scope`` is part of the key on purpose — see module docstring.

@st.cache_data(ttl=CACHE_TTLS["live"], show_spinner=False, max_entries=256)
def _fetch_live(sql: str, scope: str, _page: str = "") -> pd.DataFrame:
    return _execute(sql, "live", _page)


@st.cache_data(ttl=CACHE_TTLS["recent"], show_spinner=False, max_entries=512)
def _fetch_recent(sql: str, scope: str, _page: str = "") -> pd.DataFrame:
    return _execute(sql, "recent", _page)


@st.cache_data(ttl=CACHE_TTLS["historical"], show_spinner=False, max_entries=512)
def _fetch_historical(sql: str, scope: str, _page: str = "") -> pd.DataFrame:
    return _execute(sql, "historical", _page)


@st.cache_data(ttl=CACHE_TTLS["metadata"], show_spinner=False, max_entries=128)
def _fetch_metadata(sql: str, scope: str, _page: str = "") -> pd.DataFrame:
    return _execute(sql, "metadata", _page)


@st.cache_data(ttl=CACHE_TTLS["hourly"], show_spinner=False, max_entries=512)
def _fetch_hourly(sql: str, scope: str, _page: str = "") -> pd.DataFrame:
    return _execute(sql, "hourly", _page)


_FETCHERS = {
    "live": _fetch_live,
    "recent": _fetch_recent,
    "hourly": _fetch_hourly,
    "historical": _fetch_historical,
    "metadata": _fetch_metadata,
}


# r27 #14: writes to app tables invalidate only their domain; anything we
# can't classify (ALTER WAREHOUSE, CALLs into loader procs) still bumps the
# global salt — conservative, never stale.
_DOMAIN_TOKENS = {
    "alerts": ("ALERT_EVENTS", "ALERT_AUDIT", "ALERT_CONFIG", "ALERT_ROUTES"),
    "settings": ("OVERWATCH.SETTINGS",),
    "prefs": ("USER_PREFS",),
    "budgets": ("DEPT_BUDGETS",),
    "ledger": ("SAVINGS_LEDGER",),
    "queue": ("ACTION_QUEUE", "ACTION_ACTIVITY"),
    "remediation": ("REMEDIATION_LOG",),
    "incidents": ("OVERWATCH.INCIDENTS", "INCIDENT_MEMBERS"),
    "mappings": ("DEPARTMENT_MAP", "ENTITY_CATALOG"),   # codex#45: the table is DEPARTMENT_MAP (was 'DEPT_MAPPING', which matched nothing -> every mapping write fell through to a global cache bump)
    "evidence": ("EVIDENCE_LINKS",),
    "watchlist": ("USER_WATCHLIST",),
    "experiments": ("OPTIMIZATION_EXPERIMENTS",),
    "objectives": ("SLO_OBJECTIVES",),
}


def _domains_in(sql: str) -> list[str]:
    up = str(sql or "").upper()
    return [d for d, toks in _DOMAIN_TOKENS.items() if any(tok in up for tok in toks)]


def _bump_refresh(sql: str) -> None:
    """Post-write invalidation: domain-scoped when the target is known."""
    stamp = datetime.now().isoformat()
    doms = _domains_in(sql)
    if doms:
        salts = st.session_state.setdefault("_ow_domain_salts", {})
        for d in doms:
            salts[d] = stamp
    else:
        st.session_state["_ow_refresh_salt"] = stamp


# r27 #10 (light): operator writes are app-constructed and confirmation-gated,
# but the executor itself now refuses anything outside the action surface —
# one statement, aimed at OVERWATCH objects or a warehouse lever.
_WRITE_PREFIXES = (
    "ALTER WAREHOUSE ",
    # Bug round 2 B1: the Emergency-tab levers (Operations) build these exact
    # shapes; every one interpolates only _ident-validated identifiers / a
    # regex-validated value (app/logic/remediation.py), so the builder is the
    # injection defense and the allow-list is defense-in-depth. Without them
    # every non-warehouse lever was refused and logged a FAILED audit row.
    # ALTER ACCOUNT SET (not the broader ALTER ACCOUNT) + the multi-statement
    # guard (interior ';' rejected) keep the surface tight.
    "ALTER PIPE ",
    "ALTER TASK ",
    "ALTER USER ",
    "ALTER ACCOUNT SET ",
    "INSERT INTO DBA_MAINT_DB.OVERWATCH.",
    "UPDATE DBA_MAINT_DB.OVERWATCH.",
    "DELETE FROM DBA_MAINT_DB.OVERWATCH.",
    "MERGE INTO DBA_MAINT_DB.OVERWATCH.",
    "CALL DBA_MAINT_DB.OVERWATCH.",
)

_QUERY_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _statement_allowed(sql: str) -> tuple[bool, str]:
    body = str(sql or "").strip().rstrip(";").strip()
    # string literals may legitimately hold ';' (operator notes)
    if ";" in re.sub(r"'[^']*'", "''", body):
        return False, "multi-statement strings are not executed — one statement per call."
    if not body.upper().startswith(_WRITE_PREFIXES):
        return False, ("statement is outside the operator allow-list "
                       "(OVERWATCH tables, OVERWATCH procs, warehouse levers): "
                       + body[:80])
    return True, ""


def _cache_scope(sql: str = "") -> str:
    """Cache identity beyond the SQL text itself.

    The SQL string is a cache-key argument to every tier fetcher, and every
    filter a builder honors is baked into its SQL — so filters do NOT belong
    here. (They used to: the full filters signature cold-started every query
    on the page whenever ANY filter changed, even ones the query ignored.)
    Scope is what the SQL cannot express: who is asking (role decides row
    visibility under SiS; user isolates per-user reads) and the manual
    refresh generation. The caller's KEY is deliberately NOT here anymore:
    it made identical SQL fetched from different panels (alert rules from
    the sidebar jump box, the Rules section, and the drawer) cache-miss
    three times per TTL. Telemetry still records the key per call site.
    """
    role = str(st.session_state.get("_ow_current_role", "") or "")
    salt = str(st.session_state.get("_ow_refresh_salt", "") or "")
    dsalts = st.session_state.get("_ow_domain_salts", {}) or {}
    extra = "|".join(f"{d}={dsalts[d]}" for d in _domains_in(sql) if d in dsalts)
    # perf #9: account-wide reads (marts, ACCOUNT_USAGE) return the SAME rows for
    # every viewer under owner's-rights SiS — role governs row visibility, not the
    # person — so they key by ROLE only. Keying them by viewer made N concurrent
    # viewers each cold-miss the live fallback on a mart outage and re-run the same
    # account-wide (~50GB) scan on the one XS warehouse. The viewer is added ONLY
    # for user-specific reads: USER_PREFS is the sole one (identity_sql lives only
    # there), and its CURRENT_USER() fallback is not isolated by a literal in the
    # SQL — the SiS path already bakes the viewer name into the SQL text.
    user_part = ""
    up = str(sql or "").upper()
    if "USER_PREFS" in up or "CURRENT_USER()" in up:
        from app.core.identity import viewer_name
        user = viewer_name() or str(st.session_state.get("_ow_current_user", "") or "")
        user_part = f"user={user}|"
    return f"role={role}|{user_part}salt={salt}|{extra}"


def cache_scope(sql: str = "") -> str:
    """Public read of the cache identity string (role + refresh salt + the
    domain salts this SQL is invalidated by). For callers that memoize the
    PARSED shape of a read on top of run()'s own cache — main.py's health strip
    — so the Refresh button and post-write invalidation still reach them. Any
    such wrapper must pass this as a cache-key argument, or it goes stale for
    its whole TTL after an ack/resolve."""
    return _cache_scope(sql)


def _telemetry(page: str, tier: str, key: str, elapsed_ms: float, rows: int, ok: bool,
               cache_hit: bool | None = None, sql_hash: str | None = None,
               batch_size: int | None = None, truncated: bool | None = None,
               query_id: str | None = None) -> None:
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
            "cache_hit": cache_hit,
            "query_id": query_id or "",
        })
        del entries[:-_TELEMETRY_MAX]
    except Exception:
        pass
    _persist_telemetry(page, tier, key, elapsed_ms, rows, ok,
                       cache_hit=cache_hit, sql_hash=sql_hash,
                       batch_size=batch_size, truncated=truncated, query_id=query_id)


def query_telemetry() -> pd.DataFrame:
    return pd.DataFrame(st.session_state.get(_TELEMETRY_KEY, []))


def telemetry_dropped_counts() -> dict:
    """#28: telemetry rows the per-session budget declined to persist, so a viewer of the
    fleet board can distinguish a genuinely quiet page from one that is sampling-capped.
    ``total`` counts every declined row; ``failures`` the subset that were failed fetches
    (the loudest signal that _TELEMETRY_FAIL_CAP is too tight)."""
    return {
        "total": int(st.session_state.get("_ow_qtel_dropped", 0)),
        "failures": int(st.session_state.get("_ow_qtel_dropped_fail", 0)),
    }


def bump_refresh_salt() -> None:
    """Invalidate OVERWATCH's cached reads (the Refresh button)."""
    st.session_state["_ow_refresh_salt"] = datetime.now().isoformat()


class _BatchPartial(Exception):
    """Some batch members failed. Raising out of the cache_data-wrapped
    fetcher keeps the all-or-nothing cache invariant (a partial batch is
    never cached) while CARRYING the survivors — their frames were already
    computed server-side and paid for (Codex r9 #3: re-running them through
    the fallback duplicated scans and credits)."""

    def __init__(self, frames: dict, errors: dict, pending: set | None = None) -> None:
        super().__init__(f"{len(errors)} of {len(frames) + len(errors)} batch members failed")
        self.frames = frames
        self.errors = errors
        # r11 #4: indices whose submission never happened. Unsubmitted is NOT
        # failed — these rerun through the normal fallback and must never be
        # quarantined alongside the member that actually raised.
        self.pending = pending or set()


def _execute_batch(sqls: tuple, tier: str, page: str, *, timeout_s: int | None = None) -> tuple:
    """Submit every statement server-side async (collect on one connection is
    serialized; async jobs are not), then gather. Full success returns (and
    caches); any failure raises — _BatchPartial when there are survivors.

    ``timeout_s`` overrides the per-tier statement_timeout for a MIXED-tier batch
    (run_batch_mixed passes the MAX over its members' tiers, so no member gets
    LESS time than its solo run()); single-tier callers leave it None."""
    if len(sqls) > BATCH_MAX_CONCURRENCY:
        return _execute_batch_bounded(sqls, tier, page, timeout_s=timeout_s)
    session = get_session()
    apply_query_tag(session, build_query_tag(page=page, tier=tier))
    apply_statement_timeout(session,
                            timeout_s if timeout_s is not None else STATEMENT_TIMEOUTS.get(tier, 120))
    # P3: INCREMENTAL gather time per member, not the batch wall clock. Every
    # member used to be stamped with the whole batch's duration, so a 2-query
    # batch reported two identical inflated samples — batch:q and batch:p read
    # the same p50/p95, and any sum over telemetry (the Cost/Admin "pain"
    # boards) counted one batch's seconds N times. Incremental deltas are
    # SUM-PRESERVING: they add up to the gather wall time exactly once.
    # Caveat worth knowing: the jobs run in PARALLEL but are gathered in
    # submission order, so a fast member that finished behind a slow one reads
    # near-zero. That understates an individual member but never double-counts
    # the fleet total, which is the number the tuning boards rank on. The true
    # per-query duration lives in ACCOUNT_USAGE via the persisted QUERY_ID.
    member_ms: dict[int, float] = {}
    _BATCH_MEMBER_MS.set(member_ms)
    # #43: capture each async job's QUERY_ID as it is submitted (the handle carries it
    # immediately), so member telemetry can later join to QUERY_HISTORY for the true
    # per-query duration the submission-order gather time cannot give.
    member_qid: dict[int, str] = {}
    _BATCH_MEMBER_QID.set(member_qid)
    _mark = time.perf_counter()

    def _stamp(idx: int) -> None:
        nonlocal _mark
        now = time.perf_counter()
        member_ms[idx] = (now - _mark) * 1000
        _mark = now

    jobs: list = []
    try:
        for sql in sqls:
            # incremental on purpose (r10 #3): a comprehension loses the in-flight handles
            # when submission N fails, and #43 needs each handle's query_id at submit time.
            job = session.sql(sql).to_pandas(block=False)
            member_qid[len(jobs)] = str(getattr(job, "query_id", "") or "")
            jobs.append(job)
    except Exception as sub_exc:
        frames0: dict = {}        # those queries RUN server-side either way, and
        errors0: dict = {}        # dropping the handles re-paid them in fallback.
        _mark = time.perf_counter()   # submission cost is not any one member's
        for idx, job in enumerate(jobs):
            try:
                frames0[idx] = _normalize(job.result())
            except Exception as exc2:
                errors0[idx] = exc2
            finally:
                _stamp(idx)
        errors0[len(jobs)] = sub_exc          # the member whose submit raised
        pending0 = set(range(len(jobs) + 1, len(sqls)))
        raise _BatchPartial(frames0, errors0, pending0) from sub_exc
    frames: dict = {}
    errors: dict = {}
    _mark = time.perf_counter()               # clock the GATHER, not the submits
    for idx, job in enumerate(jobs):
        try:
            frames[idx] = _normalize(job.result())
        except Exception as exc:
            errors[idx] = exc
        finally:
            _stamp(idx)
    if errors:
        raise _BatchPartial(frames, errors)
    return tuple(frames[i] for i in range(len(jobs)))


def _execute_batch_bounded(sqls: tuple, tier: str, page: str, *, timeout_s: int | None = None) -> tuple:
    """Execute a large batch in bounded async waves.

    Four concurrent statements keep the XS warehouse responsive while
    preserving _BatchPartial's survivor/error/pending contract across waves.
    A failed member does not stop later waves; a submission failure marks only
    that wave's unsubmitted members pending, matching the small-batch path.
    ``timeout_s`` (mixed-tier) is forwarded to every wave so no wave silently
    reverts to a per-tier default.
    """
    frames: dict[int, pd.DataFrame] = {}
    errors: dict[int, Exception] = {}
    pending: set[int] = set()
    member_ms: dict[int, float] = {}
    member_qid: dict[int, str] = {}
    for start in range(0, len(sqls), BATCH_MAX_CONCURRENCY):
        chunk = sqls[start:start + BATCH_MAX_CONCURRENCY]
        try:
            returned = _execute_batch(chunk, tier, page, timeout_s=timeout_s)
            for local, frame in enumerate(returned):
                frames[start + local] = frame
        except _BatchPartial as partial:
            frames.update({start + local: frame for local, frame in partial.frames.items()})
            errors.update({start + local: exc for local, exc in partial.errors.items()})
            pending.update(start + local for local in partial.pending)
        chunk_ms = _BATCH_MEMBER_MS.get() or {}
        chunk_qid = _BATCH_MEMBER_QID.get() or {}
        member_ms.update({start + local: value for local, value in chunk_ms.items()})
        member_qid.update({start + local: value for local, value in chunk_qid.items()})
    _BATCH_MEMBER_MS.set(member_ms)
    _BATCH_MEMBER_QID.set(member_qid)
    if errors:
        raise _BatchPartial(frames, errors, pending)
    return tuple(frames[i] for i in range(len(sqls)))


@st.cache_data(ttl=CACHE_TTLS["recent"], show_spinner=False, max_entries=64)
def _fetch_recent_batch(sqls: tuple, scope: str, _page: str = "") -> tuple:
    return _execute_batch(sqls, "recent", _page)


@st.cache_data(ttl=CACHE_TTLS["historical"], show_spinner=False, max_entries=64)
def _fetch_historical_batch(sqls: tuple, scope: str, _page: str = "") -> tuple:
    return _execute_batch(sqls, "historical", _page)


@st.cache_data(ttl=CACHE_TTLS["live"], show_spinner=False, max_entries=64)
def _fetch_live_batch(sqls: tuple, scope: str, _page: str = "") -> tuple:
    return _execute_batch(sqls, "live", _page)


@st.cache_data(ttl=CACHE_TTLS["metadata"], show_spinner=False, max_entries=64)
def _fetch_metadata_batch(sqls: tuple, scope: str, _page: str = "") -> tuple:
    return _execute_batch(sqls, "metadata", _page)


@st.cache_data(ttl=CACHE_TTLS["hourly"], show_spinner=False, max_entries=64)
def _fetch_hourly_batch(sqls: tuple, scope: str, _page: str = "") -> tuple:
    return _execute_batch(sqls, "hourly", _page)


_BATCH_FETCHERS = {"recent": _fetch_recent_batch, "historical": _fetch_historical_batch,
                   "hourly": _fetch_hourly_batch,
                   "live": _fetch_live_batch, "metadata": _fetch_metadata_batch}


def _member_elapsed(idx: int, wall_ms: float) -> float:
    """This batch member's own gather time (P3), falling back to the batch wall
    clock. The fallback is what a CACHE HIT reports — nothing ran server-side,
    so wall_ms is ~0 and honest — and also covers a Snowpark build that never
    reached the timing loop."""
    return float((_BATCH_MEMBER_MS.get() or {}).get(idx, wall_ms))


def _member_qid(idx: int) -> str:
    """This batch member's Snowflake QUERY_ID (#43), captured off the async job handle
    so a later ACCOUNT_USAGE.QUERY_HISTORY join can recover the query's true duration.
    '' on a cache hit (no job ran) or a Snowpark build that never exposed the id — the
    same empty-dict-means-nothing-ran discipline _member_elapsed uses."""
    return str((_BATCH_MEMBER_QID.get() or {}).get(idx, "") or "")


def _batch_cache_hit() -> bool:
    """#29: was the batch answered from st.cache_data, i.e. did NO member run server-side?
    run_batch clears _BATCH_MEMBER_MS to None before the fetch; _execute_batch (a cache
    MISS) replaces it with a dict. So a value still None means the cached tuple was replayed
    — every member is a cache hit. Same sentinel discipline as _member_elapsed/_member_qid."""
    return _BATCH_MEMBER_MS.get() is None


def _sql_hash16(sql: str) -> str:
    """#29: sha1 prefix of a member's SQL, matching run()'s telemetry sql_hash grain, so
    batch members join to the same builder identity in APP_QUERY_TELEMETRY."""
    import hashlib
    return hashlib.sha1(str(sql).encode()).hexdigest()[:16]


def run_batch(specs: list[dict], *, page: str, tier: str = "recent") -> dict | None:
    """Parallel fetch for multi-query sections: [{key, sql, source, max_rows?}].

    ALWAYS returns {key: QueryResult} with every key present (v4.20, Codex
    r7 #1, owner-approved). The cached batch unit stays all-or-nothing —
    failures are never cached — but when the parallel path fails, the
    fallback now runs PER KEY through run(): successes cache individually
    and one bad query no longer drags its siblings back to serial-cold.
    Callers' `(_b or {}).get(k) or run(...)` pattern still works unchanged.
    """
    tier = tier if tier in _BATCH_FETCHERS else "recent"
    started = time.perf_counter()
    # r10 #2: a key that failed inside a batch this session is quarantined —
    # it runs individually (own cache, failures never cached) while the
    # healthy remainder re-batches SMALLER and caches normally. Cleared by
    # manual refresh (salt change) or by the key's next clean solo run
    # (r11 #5 rehab), so recovery needs no click at all.
    _salt = str(st.session_state.get("_ow_refresh_salt", "") or "")
    _q = st.session_state.get("_ow_batch_quarantine") or {}
    if _q.get("salt") != _salt:
        _q = {"salt": _salt, "keys": set()}
    out_direct: dict = {}
    bspecs = []
    for spec in specs:
        _qk = _quarantine_key(page, str(spec["key"]), str(spec["sql"]))
        if _qk in _q["keys"]:
            _solo = run(
                str(spec["sql"]), page=page, key=f"bfb:{spec['key']}", tier=tier,
                source=str(spec.get("source", "")),
                max_rows=spec.get("max_rows", DEFAULT_MAX_ROWS))
            if _solo.ok:
                _q["keys"].discard(_qk)
                st.session_state["_ow_batch_quarantine"] = _q
                _cap = int(spec.get("max_rows", DEFAULT_MAX_ROWS) or 0)
                _capped = _with_row_cap(str(spec["sql"]), _cap)
                _batch_member_cache_put(
                    tier, _capped, _cache_scope(_capped), _solo.df, _solo.truncated)
            out_direct[str(spec["key"])] = _solo
        else:
            bspecs.append(spec)
    if not bspecs:
        return out_direct
    uncached_specs, capped, caps, member_scopes = [], [], [], []
    for spec in bspecs:
        sql = str(spec["sql"])
        cap = int(spec.get("max_rows", DEFAULT_MAX_ROWS) or 0)
        capped_sql = _with_row_cap(sql, cap)
        member_scope = _cache_scope(capped_sql)
        cached = _batch_member_cache_get(tier, capped_sql, member_scope)
        if cached is not None:
            frame, truncated = cached
            key = str(spec["key"])
            _telemetry(page, tier, f"batch:{key}", 0.0, len(frame), ok=True,
                       batch_size=0, truncated=truncated,
                       sql_hash=_sql_hash16(sql), cache_hit=True)
            out_direct[key] = QueryResult(
                df=frame, ok=True, truncated=truncated,
                source=str(spec.get("source", "")), tier=tier,
                fetched_at=datetime.now(), elapsed_ms=0.0)
            continue
        uncached_specs.append(spec)
        capped.append(capped_sql)
        caps.append(cap)
        member_scopes.append(member_scope)
    bspecs = uncached_specs
    if not bspecs:
        return out_direct
    try:
        scope = "|members|" + "|".join(member_scopes)
        # P3: clear first — a stale dict from an EARLIER batch in this same
        # thread context would otherwise be read as this batch's timings when
        # the fetcher answers from cache (its body never runs). #43: the QUERY_ID
        # sidecar is cleared for the same reason (a cache hit ran no jobs -> blank).
        _BATCH_MEMBER_MS.set(None)
        _BATCH_MEMBER_QID.set(None)
        frames = _BATCH_FETCHERS[tier](tuple(capped), scope, page)
    except _BatchPartial as bp:
        elapsed = (time.perf_counter() - started) * 1000
        # r11 #4: only CONFIRMED failers are quarantined — bp.pending members
        # (never submitted) fall through to the run() fallback below untainted.
        _q["keys"] |= {_quarantine_key(page, str(bspecs[i]["key"]), str(bspecs[i]["sql"]))
                       for i in bp.errors}
        st.session_state["_ow_batch_quarantine"] = _q
        failed_keys = ",".join(str(bspecs[i].get("key")) for i in bp.errors)[:160]
        _telemetry(page, tier, f"batch_fallback:{tier}:n{len(bspecs)}",
                   elapsed, 0, ok=False)
        record_error(page, next(iter(bp.errors.values())),
                     context=f"run_batch partial tier={tier} failed=[{failed_keys}]")
        out: dict = {}
        for idx, spec in enumerate(bspecs):
            if idx in bp.frames:
                df = bp.frames[idx]
                truncated = bool(caps[idx]) and len(df) > caps[idx]
                if truncated:
                    df = df.head(caps[idx])
                member_ms = _member_elapsed(idx, elapsed)
                # #29: a _BatchPartial means the batch DID run server-side (survivors came
                # back from real jobs), so these members are cache MISSES, not hits.
                _telemetry(page, tier, f"batch:{spec['key']}", member_ms,
                           len(df), ok=True, batch_size=len(bspecs), truncated=truncated,
                           query_id=_member_qid(idx),
                           sql_hash=_sql_hash16(spec["sql"]), cache_hit=False)
                out[str(spec["key"])] = QueryResult(
                    df=df, ok=True, truncated=truncated,
                    source=str(spec.get("source", "")), tier=tier,
                    fetched_at=datetime.now(), elapsed_ms=member_ms)
                _batch_member_cache_put(tier, capped[idx], member_scopes[idx], df, truncated)
            else:
                solo = run(
                    str(spec["sql"]), page=page, key=f"bfb:{spec['key']}", tier=tier,
                    source=str(spec.get("source", "")),
                    max_rows=spec.get("max_rows", DEFAULT_MAX_ROWS))
                out[str(spec["key"])] = solo
                if solo.ok:
                    _batch_member_cache_put(
                        tier, capped[idx], member_scopes[idx], solo.df, solo.truncated)
        return {**out, **out_direct}
    except Exception as exc:
        _telemetry(page, tier, f"batch_fallback:{tier}:n{len(specs)}",
                   (time.perf_counter() - started) * 1000, 0, ok=False)
        keys = ",".join(str(s.get("key")) for s in bspecs)[:160]
        record_error(page, exc, context=(f"run_batch fallback tier={tier} n={len(specs)} "
                                         f"[{keys}] {type(exc).__name__}"))
        # Partial-success: retry each spec individually — run() brings its
        # own per-query cache, telemetry, and error isolation. Failed keys
        # come back as ok=False results (same surface the caller's own
        # serial fallback would produce).
        out: dict = {}
        for idx, spec in enumerate(bspecs):
            solo = run(
                str(spec["sql"]), page=page, key=f"bfb:{spec['key']}", tier=tier,
                source=str(spec.get("source", "")),
                max_rows=spec.get("max_rows", DEFAULT_MAX_ROWS))
            out[str(spec["key"])] = solo
            if solo.ok:
                _batch_member_cache_put(
                    tier, capped[idx], member_scopes[idx], solo.df, solo.truncated)
        return {**out, **out_direct}
    elapsed = (time.perf_counter() - started) * 1000
    out: dict = {}
    rows_total = 0
    cache_hit_batch = _batch_cache_hit()   # #29: whole tuple replayed from cache -> all hits
    for idx, (spec, df, cap) in enumerate(zip(bspecs, frames, caps, strict=True)):
        truncated = bool(cap) and len(df) > cap
        if truncated:
            df = df.head(cap)
        rows_total += len(df)
        member_ms = _member_elapsed(idx, elapsed)
        _telemetry(page, tier, f"batch:{spec['key']}", member_ms, len(df), ok=True,
                   batch_size=len(bspecs), truncated=truncated, query_id=_member_qid(idx),
                   sql_hash=_sql_hash16(spec["sql"]), cache_hit=cache_hit_batch)
        out[str(spec["key"])] = QueryResult(
            df=df, ok=True, truncated=truncated, source=str(spec.get("source", "")),
            tier=tier, fetched_at=datetime.now(), elapsed_ms=member_ms,
        )
        _batch_member_cache_put(tier, capped[idx], member_scopes[idx], df, truncated)
    # P3: the members now carry their own slices, so the batch's END-TO-END cost
    # (submits + gather + Snowpark overhead) would vanish from telemetry. Record
    # it ONCE under its own key. Aggregators that SUM elapsed must exclude
    # 'batch_wall:%' — it is a superset of its members, not extra work.
    _telemetry(page, tier, f"batch_wall:{tier}:n{len(bspecs)}", elapsed, rows_total,
               ok=True, batch_size=len(bspecs))
    return {**out, **out_direct}


def run_batch_mixed(specs: list[dict], *, page: str) -> dict:
    """Parallel fetch for panels whose members span DIFFERENT tiers (#58).

    specs: ``[{key, sql, tier, source?, max_rows?}]``. All members submit async on
    ONE session in ONE round trip (async job handles are tier-independent). The one
    ALTER SESSION statement_timeout is the MAX over the submitted members' tiers, so
    no member gets LESS time than its solo run(). Each member's telemetry AND its
    _BATCH_MEMBER_CACHE entry are stamped with ITS OWN tier, so the tier-keyed member
    cache + per-tier CACHE_TTLS stay exact and a member is cache-hit-reusable by the
    same member in any batch (mixed or single-tier) of that tier; a miss/fallback
    lands in the matching per-tier st.cache_data slot via run().

    Unlike single-tier run_batch there is NO tuple-level st.cache_data (a per-tier
    fetcher has one fixed TTL and cannot hold a mixed-TTL tuple), so the per-member
    _BATCH_MEMBER_CACHE above IS the caching layer. Same public contract: ALWAYS
    returns {key: QueryResult} with every key present; the parallel unit is
    all-or-nothing (failures never cached); a failed member is quarantined for the
    session and every non-survivor falls back through run() individually.
    """
    started = time.perf_counter()
    _salt = str(st.session_state.get("_ow_refresh_salt", "") or "")
    _q = st.session_state.get("_ow_batch_quarantine") or {}
    if _q.get("salt") != _salt:
        _q = {"salt": _salt, "keys": set()}
    out: dict = {}
    pending: list[tuple] = []   # (spec, tier, capped_sql, scope, cap) needing a server fetch
    for spec in specs:
        key = str(spec["key"])
        sql = str(spec["sql"])
        tier = str(spec.get("tier", "recent"))
        tier = tier if tier in _BATCH_FETCHERS else "recent"
        cap = int(spec.get("max_rows", DEFAULT_MAX_ROWS) or 0)
        capped_sql = _with_row_cap(sql, cap)
        scope = _cache_scope(capped_sql)
        qk = _quarantine_key(page, key, sql)
        if qk in _q["keys"]:                       # quarantined -> solo at its own tier
            solo = run(sql, page=page, key=f"bfb:{key}", tier=tier,
                       source=str(spec.get("source", "")),
                       max_rows=spec.get("max_rows", DEFAULT_MAX_ROWS))
            if solo.ok:
                _q["keys"].discard(qk)
                st.session_state["_ow_batch_quarantine"] = _q
                _batch_member_cache_put(tier, capped_sql, scope, solo.df, solo.truncated)
            out[key] = solo
            continue
        cached = _batch_member_cache_get(tier, capped_sql, scope)   # tier-keyed hit, own TTL
        if cached is not None:
            frame, truncated = cached
            _telemetry(page, tier, f"mixed:{key}", 0.0, len(frame), ok=True,
                       batch_size=0, truncated=truncated,
                       sql_hash=_sql_hash16(sql), cache_hit=True)
            out[key] = QueryResult(df=frame, ok=True, truncated=truncated,
                                   source=str(spec.get("source", "")), tier=tier,
                                   fetched_at=datetime.now(), elapsed_ms=0.0)
            continue
        pending.append((spec, tier, capped_sql, scope, cap))
    if not pending:
        return out
    sqls = tuple(p[2] for p in pending)
    timeout_s = max(STATEMENT_TIMEOUTS.get(p[1], 120) for p in pending)
    # Clear the timing / QUERY_ID sidecars (a stale dict would be misread as this batch's).
    _BATCH_MEMBER_MS.set(None)
    _BATCH_MEMBER_QID.set(None)
    try:
        # Direct execute, NO tuple-level st.cache_data (it can't hold a mixed-TTL tuple);
        # the per-member _BATCH_MEMBER_CACHE above is the caching layer.
        frames = _execute_batch(sqls, "mixed", page, timeout_s=timeout_s)
    except _BatchPartial as bp:
        elapsed = (time.perf_counter() - started) * 1000
        _q["keys"] |= {_quarantine_key(page, str(pending[i][0]["key"]), str(pending[i][0]["sql"]))
                       for i in bp.errors}          # only CONFIRMED failers; pending stay clean
        st.session_state["_ow_batch_quarantine"] = _q
        failed = ",".join(str(pending[i][0].get("key")) for i in bp.errors)[:160]
        _telemetry(page, "mixed", f"batch_fallback:mixed:n{len(pending)}", elapsed, 0, ok=False)
        record_error(page, next(iter(bp.errors.values())),
                     context=f"run_batch_mixed partial failed=[{failed}]")
        for idx, (spec, tier, capped_sql, scope, cap) in enumerate(pending):
            key = str(spec["key"])
            if idx in bp.frames:                    # survivor: ran server-side -> cache miss, cache it
                df = bp.frames[idx]
                truncated = bool(cap) and len(df) > cap
                if truncated:
                    df = df.head(cap)
                member_ms = _member_elapsed(idx, elapsed)
                _telemetry(page, tier, f"mixed:{key}", member_ms, len(df), ok=True,
                           batch_size=len(pending), truncated=truncated,
                           query_id=_member_qid(idx), sql_hash=_sql_hash16(spec["sql"]),
                           cache_hit=False)
                out[key] = QueryResult(df=df, ok=True, truncated=truncated,
                                       source=str(spec.get("source", "")), tier=tier,
                                       fetched_at=datetime.now(), elapsed_ms=member_ms)
                _batch_member_cache_put(tier, capped_sql, scope, df, truncated)
            else:                                   # failer OR unsubmitted -> solo run() at its tier
                solo = run(str(spec["sql"]), page=page, key=f"bfb:{key}", tier=tier,
                           source=str(spec.get("source", "")),
                           max_rows=spec.get("max_rows", DEFAULT_MAX_ROWS))
                out[key] = solo
                if solo.ok:
                    _batch_member_cache_put(tier, capped_sql, scope, solo.df, solo.truncated)
        return out
    except Exception as exc:                        # whole submit failed before any survivor
        elapsed = (time.perf_counter() - started) * 1000
        keys = ",".join(str(p[0].get("key")) for p in pending)[:160]
        _telemetry(page, "mixed", f"batch_fallback:mixed:n{len(pending)}", elapsed, 0, ok=False)
        record_error(page, exc,
                     context=f"run_batch_mixed fallback n={len(pending)} [{keys}] {type(exc).__name__}")
        for spec, tier, capped_sql, scope, _cap in pending:
            key = str(spec["key"])
            solo = run(str(spec["sql"]), page=page, key=f"bfb:{key}", tier=tier,
                       source=str(spec.get("source", "")),
                       max_rows=spec.get("max_rows", DEFAULT_MAX_ROWS))
            out[key] = solo
            if solo.ok:
                _batch_member_cache_put(tier, capped_sql, scope, solo.df, solo.truncated)
        return out
    elapsed = (time.perf_counter() - started) * 1000
    rows_total = 0
    for idx, (spec, tier, capped_sql, scope, cap) in enumerate(pending):
        key = str(spec["key"])
        df = frames[idx]
        truncated = bool(cap) and len(df) > cap
        if truncated:
            df = df.head(cap)
        rows_total += len(df)
        member_ms = _member_elapsed(idx, elapsed)
        _telemetry(page, tier, f"mixed:{key}", member_ms, len(df), ok=True,
                   batch_size=len(pending), truncated=truncated, query_id=_member_qid(idx),
                   sql_hash=_sql_hash16(spec["sql"]), cache_hit=False)
        out[key] = QueryResult(df=df, ok=True, truncated=truncated,
                               source=str(spec.get("source", "")), tier=tier,
                               fetched_at=datetime.now(), elapsed_ms=member_ms)
        _batch_member_cache_put(tier, capped_sql, scope, df, truncated)
    # Wall row is a SUPERSET of its members — key it under the 'batch_wall:' prefix that every
    # fleet-seconds aggregator (mart_sql fleet/pain/fetch rollups) already excludes, so a mixed
    # batch never double-counts its wall time (the member 'mixed:%' rows are the real samples).
    _telemetry(page, "mixed", f"batch_wall:mixed:n{len(pending)}", elapsed, rows_total,
               ok=True, batch_size=len(pending))
    return out


def run(
    sql: str,
    *,
    page: str,
    key: str,
    tier: str = "recent",
    source: str = "",
    max_rows: int = DEFAULT_MAX_ROWS,
    probe: bool = False,
) -> QueryResult:
    """Execute through the tiered cache and return a typed QueryResult.

    Never raises: failures come back as ok=False with a friendly error string,
    and are recorded to the error buffer/sink. Failures are never cached.

    probe=True marks an optional-object read (e.g. the Flyway ledger before
    Flyway exists): an object-does-not-exist failure is the EXPECTED answer,
    so it is neither error-logged nor counted as a failed fetch — the panel's
    absent branch is the record. Every other failure still records normally.
    """
    tier = tier if tier in _FETCHERS else "recent"
    started = time.perf_counter()
    try:
        cap = int(max_rows) if max_rows else 0
        _FETCH_MISS.set(False)
        df = _FETCHERS[tier](_with_row_cap(sql, cap), _cache_scope(sql), page)
        cache_hit = not _FETCH_MISS.get()
        truncated = bool(cap) and len(df) > cap
        if truncated:
            df = df.head(cap)
        elapsed = (time.perf_counter() - started) * 1000
        import hashlib as _hashlib
        _telemetry(page, tier, key, elapsed, len(df), ok=True,
                   cache_hit=cache_hit,
                   sql_hash=_hashlib.sha1(sql.encode()).hexdigest()[:16],
                   truncated=truncated,
                   query_id=("" if cache_hit else _LAST_QUERY_ID.get()))
        return QueryResult(
            df=df, ok=True, truncated=truncated, source=source, tier=tier,
            fetched_at=datetime.now(), elapsed_ms=elapsed,
        )
    except Exception as exc:
        elapsed = (time.perf_counter() - started) * 1000
        kind = _classify_error(exc)
        _expected_absence = probe and kind in ("absent", "unknown_function", "missing_column")
        if not _expected_absence:
            _telemetry(page, tier, key, elapsed, 0, ok=False)
            record_error(page, exc, context=f"query key={key} tier={tier}")
        return QueryResult(
            df=pd.DataFrame(), ok=False, error=format_snowflake_error(exc),
            error_kind=kind, source=source, tier=tier,
            fetched_at=datetime.now(), elapsed_ms=elapsed,
        )


def execute_statement_async(sql: str, *, page: str) -> bool:
    """Fire-and-forget write for telemetry rows (usage analytics).

    Submits server-side async so the render path never waits on an INSERT
    round trip; falls back to a blocking collect where async is unavailable.
    Post-submission failures are not observed — acceptable for telemetry
    only. Operator actions must keep using execute_statement().
    """
    ok, why = _statement_allowed(sql)
    if not ok:
        record_error(page, RuntimeError(why), context=f"execute_statement_async blocked: {sql[:200]}")
        return False
    try:
        session = get_session()
        apply_query_tag(session, build_query_tag(page=page, tier="write"))
        statement = session.sql(sql)
        try:
            statement.collect_nowait()
        except AttributeError:  # older Snowpark: no async API
            statement.collect()
        return True
    except Exception as exc:
        record_error(page, exc, context=f"execute_statement_async: {sql[:200]}")
        return False


# ---------------------------------------------------------------------------
# N12: telemetry/usage write buffer. Every rerun used to fire one single-row
# INSERT per usage/telemetry event on the shared warehouse — a round trip each.
# Buffer row fragments per target-table+column-shape during a rerun and flush
# each group as ONE multi-row `INSERT ... SELECT ... UNION ALL SELECT ...` — still
# a single allow-listed statement, one round trip per table instead of per row.
# ---------------------------------------------------------------------------
_WRITE_BUFFER_KEY = "_ow_write_buffer"
_WRITE_BUFFER_FLUSH_N = 25


def _flush_group(prefix: str) -> None:
    """Flush one buffered target (``prefix`` identifies its table + column shape)."""
    try:
        buf = st.session_state.get(_WRITE_BUFFER_KEY) or {}
        group = buf.pop(prefix, None)
        if not group or not group.get("rows"):
            return
        stmt = prefix + " UNION ALL ".join(group["rows"])
        flag = group.get("downgrade") or group.get("off")
        _fk = f"_ow_qtel_fail:{flag}" if flag else ""
        if execute_statement_async(stmt, page="Telemetry"):
            if _fk:
                st.session_state.pop(_fk, None)      # success clears the transient counter
            return
        if flag:
            # r4: require TWO consecutive failures before stepping DOWN the shape ladder
            # (12->10->6->off). execute_statement_async returns False on ANY error, so a
            # single transient blip (network/session) would otherwise latch the downgrade
            # and permanently drop SAMPLE_PROB for the session; a real shape mismatch fails
            # every flush and trips on the 2nd.
            n = int(st.session_state.get(_fk, 0)) + 1
            st.session_state[_fk] = n
            if n >= 2:
                st.session_state[flag] = True        # give up this shape; drop the batch
                return
            # #30: the FIRST strike does NOT drop the rows — a submit failure is
            # indistinguishable from a transient blip here, so re-queue the popped batch and
            # let the next flush retry it (bounded: the 2nd consecutive strike drops + steps
            # down). Without this, one blip silently lost a whole rerun's telemetry.
            _requeue_group(prefix, group)
    except Exception:
        pass


def _requeue_group(prefix: str, group: dict) -> None:
    """Put a failed flush's rows back on the buffer so the next flush retries them (#30).
    Prepends ahead of anything enqueued since the pop, so ordering within the group holds."""
    try:
        buf = st.session_state.setdefault(_WRITE_BUFFER_KEY, {})
        existing = buf.get(prefix)
        if existing is None:
            buf[prefix] = group
        else:
            existing["rows"] = list(group.get("rows", [])) + list(existing.get("rows", []))
    except Exception:
        pass


def _buffer_write(insert_prefix: str, row_select: str, *,
                  off_flag: str | None = None, downgrade_flag: str | None = None) -> None:
    """Enqueue one row for a buffered write. ``insert_prefix`` is the full
    'INSERT INTO tbl (cols) ' string (its identity groups shape-compatible rows);
    ``row_select`` is a bare 'SELECT v1, v2, ...' — no leading INSERT, no trailing
    semicolon. Auto-flushes the group once it reaches ``_WRITE_BUFFER_FLUSH_N``."""
    try:
        buf = st.session_state.setdefault(_WRITE_BUFFER_KEY, {})
        group = buf.setdefault(insert_prefix, {"off": off_flag, "downgrade": downgrade_flag, "rows": []})
        group["off"], group["downgrade"] = off_flag, downgrade_flag
        group["rows"].append(row_select)
        if len(group["rows"]) >= _WRITE_BUFFER_FLUSH_N:
            _flush_group(insert_prefix)
    except Exception:
        pass


def flush_write_buffer() -> None:
    """Drain every buffered write group (one multi-row INSERT per table+shape).
    Called at the top and end of each rerun so nothing is lost when st.rerun()
    unwinds past the end-of-render flush (session_state is the durability seam)."""
    try:
        buf = st.session_state.get(_WRITE_BUFFER_KEY) or {}
        for prefix in list(buf.keys()):
            _flush_group(prefix)
    except Exception:
        pass


def execute_statement(sql: str, *, page: str) -> tuple[bool, str]:
    """Run a single state-changing statement (operator actions only).

    Callers gate this behind role + typed confirmation. Returns (ok, message).
    """
    ok, why = _statement_allowed(sql)
    if not ok:
        return False, why
    try:
        # C48: the in-flight state lives HERE, at the one seam every write
        # crosses — the initiating button freezes for the round-trip, and the
        # spinner is the honest "Executing…" the operator watches instead.
        with st.spinner("Executing write…"):
            session = get_session()
            apply_query_tag(session, build_query_tag(page=page, tier="write"))
            session.sql(sql).collect()
        # r24 #8 + r27 #14: a successful action invalidates cached reads —
        # domain-scoped when the write target is a known app table, global
        # otherwise — so post-action freshness never depends on live tiers.
        _bump_refresh(sql)
        return True, "Statement executed."
    except Exception as exc:
        record_error(page, exc, context=f"execute_statement: {sql[:200]}")
        return False, format_snowflake_error(exc)


def execute_cancel_query(query_id: str, *, page: str) -> tuple[bool, str]:
    """Cancel a running query by id — the Operations kill-switch (bug round 2 B2).

    SYSTEM$CANCEL_QUERY is a SELECT, outside the write-prefix allow-list, so it
    gets its own seam instead of a blanket SELECT allowance: the id is
    regex-validated and the exact statement is built here, so no operator text
    reaches the SQL beyond a validated query id. The role + typed-confirm gate is
    upstream; the caller still writes the REMEDIATION_LOG audit row.
    """
    qid = str(query_id or "").strip()
    if not _QUERY_ID_RE.match(qid):
        return False, f"Invalid query id: {query_id!r}"
    from app.core.sqlsafe import sql_literal
    try:
        with st.spinner("Cancelling query…"):   # C48 in-flight state
            session = get_session()
            apply_query_tag(session, build_query_tag(page=page, tier="write"))
            session.sql(f"SELECT SYSTEM$CANCEL_QUERY({sql_literal(qid)})").collect()
        return True, f"Cancel requested for {qid}."
    except Exception as exc:
        record_error(page, exc, context=f"execute_cancel_query: {qid}")
        return False, format_snowflake_error(exc)


# codex#6: 'invalid identifier' removed — a missing PROCEDURE raises "unknown user-defined
# function"/"does not exist", whereas "invalid identifier" is almost always a column/table
# error from INSIDE a deployed proc, which must NOT route to the legacy path.
_PROC_MISSING = ("does not exist", "unknown function", "unknown user-defined function")

_CALL_NAME_RE = re.compile(r"\bCALL\s+([A-Za-z0-9_.\"]+)\s*\(", re.IGNORECASE)


def _looks_like_missing_proc(err: str, call_sql: str) -> bool:
    """codex#6: treat an error as 'procedure not deployed' (-> legacy fallback) ONLY when it
    both carries a missing-object phrase AND names the CALLed procedure. This stops a generic
    identifier error from a bug inside a DEPLOYED proc from silently running legacy DML."""
    low = err.lower()
    if not any(m in low for m in _PROC_MISSING):
        return False
    m = _CALL_NAME_RE.search(call_sql or "")
    if not m:
        return False
    proc = m.group(1).split(".")[-1].strip('"').lower()
    return bool(proc) and proc in low


def _legacy_action(fallback: list[str], *, page: str) -> tuple[bool, str]:
    results: list[str] = []
    for stmt in fallback:
        s = stmt.strip()
        if not s:
            continue
        o, m = execute_statement(s if s.endswith(";") else s + ";", page=page)
        results.append(m)
        if not o:
            # codex#8: STOP at the first failure. The legacy path is non-transactional, so a
            # later mutation (or the audit row) must not run after an earlier statement failed
            # — that would half-apply the change or record a success that never happened.
            return False, "(pre-V051 legacy path, stopped at failure) " + " / ".join(results)
    return True, "(pre-V051 legacy path) " + " / ".join(results)


def execute_action(call_sql: str, fallback: list[str], *, page: str) -> tuple[bool, str]:
    """Transactional action layer (V051), proc-first with a legacy fallback.

    CALL the atomic proc and read its RETURN string; if the proc is not
    deployed yet (pre-V051), run the legacy statements exactly as v4.52 did and
    label the path. The proc's own verdicts are honoured: 'OK...'/'VERIFIED...'
    succeed, 'DUPLICATE...' is an already-done success (idempotency), 'BLOCKED...'
    is a refusal. This is the single seam every operator action routes through,
    so the V051 rollout is one migration — not a code-flag flip.
    """
    ok, why = _statement_allowed(call_sql)
    if not ok:
        return False, why
    try:
        with st.spinner("Executing action…"):   # C48 in-flight state
            session = get_session()
            apply_query_tag(session, build_query_tag(page=page, tier="write"))
            rows = session.sql(call_sql).collect()
        verdict = str(rows[0][0]) if rows and rows[0] else ""
        _bump_refresh(call_sql)
        # codex#7: ALLOWLIST explicit success verdicts. Only OK / VERIFIED / DUPLICATE
        # (already-done idempotency) pass — a proc returning FAILED/ERROR, an unknown future
        # verdict, or NO row must not read as a silent success (the old code failed only on
        # BLOCKED, so everything else — including FAILED — returned True).
        if verdict.strip().upper().startswith(("OK", "VERIFIED", "DUPLICATE")):
            return True, verdict
        return False, verdict or "Procedure returned no verdict."
    except Exception as exc:
        if _looks_like_missing_proc(format_snowflake_error(exc), call_sql):
            return _legacy_action(fallback, page=page)   # pre-V051 deployment
        record_error(page, exc, context=f"execute_action: {call_sql[:200]}")
        return False, format_snowflake_error(exc)
