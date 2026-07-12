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
from datetime import datetime

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
_TELEMETRY_PERSIST_CAP = 60  # rows per session: a broken page can't spam


def should_persist_telemetry(elapsed_ms: float, ok: bool, persisted: int,
                             threshold_ms: float = TELEMETRY_PERSIST_MS,
                             cap: int = _TELEMETRY_PERSIST_CAP,
                             sample_roll: float | None = None,
                             sample_rate: float = 0.02) -> bool:
    """Pure gate: failed always qualifies, slow qualifies, capped per session.

    ``sample_roll`` (caller passes random()) additionally persists ~2% of ALL
    fetches so the fleet view sees the healthy baseline, not just the tail —
    without it, p50 by page is invisible in APP_QUERY_TELEMETRY (Codex #19).
    """
    if persisted >= cap:
        return False
    if (not ok) or float(elapsed_ms) >= float(threshold_ms):
        return True
    return sample_roll is not None and float(sample_roll) < float(sample_rate)


def _persist_telemetry(page: str, tier: str, key: str, elapsed_ms: float,
                       rows: int, ok: bool, cache_hit: bool | None = None,
                       sql_hash: str | None = None, batch_size: int | None = None,
                       truncated: bool | None = None) -> None:
    def _b(v):
        return "NULL" if v is None else ("TRUE" if v else "FALSE")

    try:
        if st.session_state.get("_ow_qtel_off"):
            return
        done = int(st.session_state.get("_ow_qtel_n", 0))
        import random as _random
        if not should_persist_telemetry(elapsed_ms, ok, done, sample_roll=_random.random()):
            return
        st.session_state["_ow_qtel_n"] = done + 1
        session = get_session()
        base = (
            f"{sql_literal(str(page)[:80])}, {sql_literal(str(tier)[:20])}, "
            f"{sql_literal(str(key)[:120])}, {round(float(elapsed_ms), 1)}, "
            f"{int(rows)}, {'TRUE' if ok else 'FALSE'}"
        )
        if not st.session_state.get("_ow_qtel_oldshape"):
            # V027 shape; one failure (pre-V027 live) drops to the old shape.
            statement = session.sql(
                f"INSERT INTO {core_object('APP_QUERY_TELEMETRY')} "
                "(PAGE, TIER, QUERY_KEY, ELAPSED_MS, ROWS_RETURNED, OK, "
                "CACHE_HIT, SQL_HASH, BATCH_SIZE, TRUNCATED) VALUES ("
                + base + f", {_b(cache_hit)}, "
                f"{sql_literal(str(sql_hash)[:64]) if sql_hash else 'NULL'}, "
                f"{int(batch_size) if batch_size is not None else 'NULL'}, {_b(truncated)})"
            )
            try:
                try:
                    statement.collect_nowait()
                except AttributeError:
                    statement.collect()
                return
            except Exception:
                st.session_state["_ow_qtel_oldshape"] = True
        statement = session.sql(
            f"INSERT INTO {core_object('APP_QUERY_TELEMETRY')} "
            "(PAGE, TIER, QUERY_KEY, ELAPSED_MS, ROWS_RETURNED, OK) VALUES (" + base + ")"
        )
        try:
            statement.collect_nowait()
        except AttributeError:
            statement.collect()
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
    if "statement reached its statement or warehouse timeout" in s or "timeout" in s:
        return "timeout"
    return "other"


def _with_row_cap(sql: str, cap: int) -> str:
    """Append ``LIMIT cap+1`` unless the SQL already carries a LIMIT clause.

    Fetching cap+1 lets the caller detect truncation honestly (n+1 rows back
    means the cap was hit) — see run()/run_batch().
    """
    if cap <= 0 or _TAIL_LIMIT_RE.search(sql.rstrip()):
        return sql
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


def _execute(sql: str, tier: str, page: str) -> pd.DataFrame:
    _FETCH_MISS.set(True)
    session = get_session()
    apply_query_tag(session, build_query_tag(page=page, tier=tier))
    apply_statement_timeout(session, STATEMENT_TIMEOUTS.get(tier, 120))
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


def _cache_scope() -> str:
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
    user = str(st.session_state.get("_ow_current_user", "") or "")
    salt = str(st.session_state.get("_ow_refresh_salt", "") or "")
    return f"role={role}|user={user}|salt={salt}"


def _telemetry(page: str, tier: str, key: str, elapsed_ms: float, rows: int, ok: bool,
               cache_hit: bool | None = None, sql_hash: str | None = None,
               batch_size: int | None = None, truncated: bool | None = None) -> None:
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
        })
        del entries[:-_TELEMETRY_MAX]
    except Exception:
        pass
    _persist_telemetry(page, tier, key, elapsed_ms, rows, ok,
                       cache_hit=cache_hit, sql_hash=sql_hash,
                       batch_size=batch_size, truncated=truncated)


def query_telemetry() -> pd.DataFrame:
    return pd.DataFrame(st.session_state.get(_TELEMETRY_KEY, []))


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


def _execute_batch(sqls: tuple, tier: str, page: str) -> tuple:
    """Submit every statement server-side async (collect on one connection is
    serialized; async jobs are not), then gather. Full success returns (and
    caches); any failure raises — _BatchPartial when there are survivors."""
    session = get_session()
    apply_query_tag(session, build_query_tag(page=page, tier=tier))
    apply_statement_timeout(session, STATEMENT_TIMEOUTS.get(tier, 120))
    jobs: list = []
    try:
        for sql in sqls:
            jobs.append(session.sql(sql).to_pandas(block=False))  # noqa: PERF401 — incremental on purpose: a comprehension loses the in-flight handles when submission N fails (r10 #3)
    except Exception as sub_exc:
        frames0: dict = {}        # those queries RUN server-side either way, and
        errors0: dict = {}        # dropping the handles re-paid them in fallback.
        for idx, job in enumerate(jobs):
            try:
                frames0[idx] = _normalize(job.result())
            except Exception as exc2:
                errors0[idx] = exc2
        errors0[len(jobs)] = sub_exc          # the member whose submit raised
        pending0 = set(range(len(jobs) + 1, len(sqls)))
        raise _BatchPartial(frames0, errors0, pending0) from sub_exc
    frames: dict = {}
    errors: dict = {}
    for idx, job in enumerate(jobs):
        try:
            frames[idx] = _normalize(job.result())
        except Exception as exc:
            errors[idx] = exc
    if errors:
        raise _BatchPartial(frames, errors)
    return tuple(frames[i] for i in range(len(jobs)))


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
        if str(spec["key"]) in _q["keys"]:
            _solo = run(
                str(spec["sql"]), page=page, key=f"bfb:{spec['key']}", tier=tier,
                source=str(spec.get("source", "")),
                max_rows=spec.get("max_rows", DEFAULT_MAX_ROWS))
            if _solo.ok:
                _q["keys"].discard(str(spec["key"]))
                st.session_state["_ow_batch_quarantine"] = _q
            out_direct[str(spec["key"])] = _solo
        else:
            bspecs.append(spec)
    if not bspecs:
        return out_direct
    capped, caps = [], []
    for spec in bspecs:
        sql = str(spec["sql"])
        cap = int(spec.get("max_rows", DEFAULT_MAX_ROWS) or 0)
        capped.append(_with_row_cap(sql, cap))
        caps.append(cap)
    try:
        scope = _cache_scope()
        frames = _BATCH_FETCHERS[tier](tuple(capped), scope, page)
    except _BatchPartial as bp:
        elapsed = (time.perf_counter() - started) * 1000
        # r11 #4: only CONFIRMED failers are quarantined — bp.pending members
        # (never submitted) fall through to the run() fallback below untainted.
        _q["keys"] |= {str(bspecs[i]["key"]) for i in bp.errors}
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
                _telemetry(page, tier, f"batch:{spec['key']}", elapsed / max(len(bspecs), 1),
                           len(df), ok=True, batch_size=len(bspecs), truncated=truncated)
                out[str(spec["key"])] = QueryResult(
                    df=df, ok=True, truncated=truncated,
                    source=str(spec.get("source", "")), tier=tier,
                    fetched_at=datetime.now(), elapsed_ms=elapsed)
            else:
                out[str(spec["key"])] = run(
                    str(spec["sql"]), page=page, key=f"bfb:{spec['key']}", tier=tier,
                    source=str(spec.get("source", "")),
                    max_rows=spec.get("max_rows", DEFAULT_MAX_ROWS))
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
        for spec in bspecs:
            out[str(spec["key"])] = run(
                str(spec["sql"]), page=page, key=f"bfb:{spec['key']}", tier=tier,
                source=str(spec.get("source", "")),
                max_rows=spec.get("max_rows", DEFAULT_MAX_ROWS))
        return {**out, **out_direct}
    elapsed = (time.perf_counter() - started) * 1000
    out: dict = {}
    for spec, df, cap in zip(bspecs, frames, caps, strict=True):
        truncated = bool(cap) and len(df) > cap
        if truncated:
            df = df.head(cap)
        _telemetry(page, tier, f"batch:{spec['key']}", elapsed / max(len(bspecs), 1), len(df), ok=True,
                   batch_size=len(bspecs), truncated=truncated)
        out[str(spec["key"])] = QueryResult(
            df=df, ok=True, truncated=truncated, source=str(spec.get("source", "")),
            tier=tier, fetched_at=datetime.now(), elapsed_ms=elapsed,
        )
    return {**out, **out_direct}


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
        df = _FETCHERS[tier](_with_row_cap(sql, cap), _cache_scope(), page)
        cache_hit = not _FETCH_MISS.get()
        truncated = bool(cap) and len(df) > cap
        if truncated:
            df = df.head(cap)
        elapsed = (time.perf_counter() - started) * 1000
        import hashlib as _hashlib
        _telemetry(page, tier, key, elapsed, len(df), ok=True,
                   cache_hit=cache_hit,
                   sql_hash=_hashlib.sha1(sql.encode()).hexdigest()[:16],
                   truncated=truncated)
        return QueryResult(
            df=df, ok=True, truncated=truncated, source=source, tier=tier,
            fetched_at=datetime.now(), elapsed_ms=elapsed,
        )
    except Exception as exc:
        elapsed = (time.perf_counter() - started) * 1000
        kind = _classify_error(exc)
        _expected_absence = probe and kind in ("absent", "unknown_function")
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
