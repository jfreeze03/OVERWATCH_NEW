"""Typed result for every Snowflake read the app performs."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

import pandas as pd


@dataclass
class QueryResult:
    """What a page gets back from core.query.run().

    ``ok=False`` means the query failed and ``error`` says why — pages render
    a labeled error state, never a silent empty frame. ``truncated=True``
    means the row cap was hit and the UI must show a truncation banner.
    """

    df: pd.DataFrame = field(default_factory=pd.DataFrame)
    ok: bool = True
    error: str = ""
    # Classified from the RAW exception (Codex r10 #4): format_snowflake_error
    # rewrites messages for humans, which silently broke marker-string checks
    # downstream (canary GAP never matched). Kinds: absent | privilege | unknown_function
    # | missing_column | timeout | other | "" (no error).
    error_kind: str = ""
    truncated: bool = False
    source: str = ""
    tier: str = "recent"
    fetched_at: datetime | None = None
    # R12: fetched_at is stamped at RETURN, so on a cache hit it is the "served now"
    # time, NOT when Snowflake was actually hit (which was up to the tier TTL earlier).
    # cache_hit lets the provenance caption say "served … (cached)" instead of claiming
    # a fresh "fetched …", so an old result never carries a fresh-looking fetch time.
    cache_hit: bool = False
    elapsed_ms: float = 0.0
    # Next-Fifty #46(d): the Snowflake query id of the statement that produced ``df`` — run() sets it on a
    # cache MISS only (a hit replays an earlier statement); '' on a hit, a failure, or a batch result.
    query_id: str = ""

    @property
    def empty(self) -> bool:
        return self.df is None or self.df.empty

    def usable(self) -> bool:
        """True when the page can render data from this result."""
        return self.ok and not self.empty


# v4.605: a failed read renders by its KIND. needs_setup ("not installed / not readable by this app") is
# ONLY for a true absence: the object is missing or unauthorised ('absent', "does not exist or not
# authorized"), the role lacks a privilege on it ('privilege', "Insufficient privileges" -- the two errors
# format_snowflake_error rewrites to the setup advice guard() routes to needs_setup, so a panel and guard()
# agree), or the function is not available ('unknown_function'). A 'privilege' error proves the object
# exists, so it is never a legitimate zero: the literal-'absent' consumers (the health score's degraded
# sources, the canary GAP, run()'s unlogged probe absence), Admin > Setup progress and the Security
# coverage contract read it as a failed read (is_privilege_error). A missing column on an existing view ('missing_column') is schema
# drift, and a 'timeout' or any 'other' failure is a failed read: each renders
# empty_state("unavailable", <panel sentence>, detail=<res.error>), never needs_setup and never the
# clean state.
# Caveat: query.run(probe=True) still leaves 'missing_column' UNLOGGED (its expected-absence tuple is
# unchanged), so on a probe read the red 'unavailable' state is the only record of the drift -- an
# unavailable sentence for a probe read must not point at the Admin error log.
SETUP_ABSENCE_KINDS: frozenset[str] = frozenset({"absent", "privilege", "unknown_function"})


def is_setup_absence(error_kind: object) -> bool:
    """True when a failed read's kind is a true absence (needs_setup); False for drift, a timeout, any other
    failure, and '' / None (no classified absence)."""
    return str(error_kind or "").strip().lower() in SETUP_ABSENCE_KINDS


def is_privilege_error(error_kind: object) -> bool:
    """True for an "Insufficient privileges" failure ('privilege'): a setup absence for rendering (needs_setup, as
    guard() shows it), but the object exists, so a consumer that treats absence as a legitimate zero or as
    'nothing applied yet' must read it as a failed read instead."""
    return str(error_kind or "").strip().lower() == "privilege"


def is_schema_drift(error_kind: object) -> bool:
    """True for a missing column on an existing object ('missing_column'): schema drift, which a retry never
    clears (a SELECT with an explicit column list fails until a migration or a redeploy lines the two up)."""
    return str(error_kind or "").strip().lower() == "missing_column"
