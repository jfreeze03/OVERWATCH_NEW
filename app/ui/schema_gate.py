"""Shared schema gate (Next-Fifty wave 4 section 2.1): has the connected database applied migration V<n>?

A deploy can land before the owner applies a migration, so every app read of a new column, and every
caption that claims a new behaviour, asks ``has_migration(n, page)`` first and keeps the pre-apply
behaviour until the version row exists.

ONE read per full script run, and normally none of its own:
- The startup floor gate (``app.main._schema_floor_breach``) reads SCHEMA_VERSION on every non-Admin
  page and hands its result to ``remember()``. Every later ``has_migration`` call in the same run
  answers from that stash, so the gate adds no statement and no telemetry row to any page.
- Without a stash for this run (the Admin page, which the floor gate skips, or a fragment that runs
  before main() has read), ``applied_versions`` issues the SAME SQL at the SAME tier as the startup
  gate: run() caches by (tier, capped SQL, scope) and page/key are telemetry-only, so it lands on the
  gate's st.cache_data entry. Its answer is stashed for the rest of the run, a failed read included
  (st.cache_data never caches a failure; the admin ``_read_fresh_applied`` r2 lesson).

Unreadable, empty or junk -> an empty set, so ``has_migration`` is False and the caller keeps its
pre-apply behaviour (the safe side). The metadata tier holds for 4 hours, so a freshly applied
migration shows up after the cache turns over or on Refresh; Admin > Migrations reads fresher.

``has_migration_fresh`` is for a WRITE whose statement changes shape at the apply (V170's 5-arg
SP_INCIDENT_DECLARE, which credits the declaring DBA instead of the app owner, for good): a 4-hour lag
there writes a wrong row that is never rewritten. It answers from the stash when the stash already holds
the version (the steady state: no statement). Only when the stash lacks it does it re-read SCHEMA_VERSION
on the live tier (30 s), once per full script run, stashed separately -- a failed read included -- so the
first write after the apply takes the new shape within 30 s. Holistic review #16, v4.609.0.

The stash is keyed by ``_ow_run_seq`` (bumped at the top of main(); fragment reruns keep it), the
same per-run pattern as ``admin._read_fresh_applied``.
"""

from __future__ import annotations

import streamlit as st

from app.core.query import run
from app.core.result import QueryResult
from app.data import mart_sql
from app.logic import deploy_health

STASH_KEY = "_ow_schema_versions"
_READ_KEY = "schema_gate"
# The startup gate's tier: the cache entry both reads share (tests/test_schema_gate.py pins the pair).
TIER = "metadata"
# has_migration_fresh: its own per-run stash, telemetry key and tier (30 s; query.CACHE_TTLS["live"]).
FRESH_STASH_KEY = "_ow_schema_versions_fresh"
_FRESH_READ_KEY = "schema_gate_fresh"
FRESH_TIER = "live"


def _parse(res: QueryResult | None) -> frozenset[int]:
    """SCHEMA_VERSION result -> applied versions; unreadable / empty / no VERSION column -> empty."""
    if res is None or not res.usable() or "VERSION" not in res.df.columns:
        return frozenset()
    return frozenset(deploy_health.applied_versions(res.df["VERSION"]))


def _run_state():
    """(session_state, run seq), or (None, None) when there is no run to key a stash on."""
    state = getattr(st, "session_state", None)          # absent under unit-test stubs
    seq = state.get("_ow_run_seq") if state is not None else None
    return (state, seq) if seq is not None else (None, None)


def _stash(versions: frozenset[int], key: str = STASH_KEY) -> None:
    state, seq = _run_state()
    if state is not None:
        state[key] = (seq, versions)


def _stashed(key: str = STASH_KEY) -> frozenset[int] | None:
    state, seq = _run_state()
    if state is None:
        return None
    stash = state.get(key)
    if isinstance(stash, tuple) and len(stash) == 2 and stash[0] == seq and isinstance(stash[1], frozenset):
        return stash[1]
    return None


def remember(res: QueryResult | None) -> frozenset[int]:
    """The startup gate's SCHEMA_VERSION result becomes this run's answer (no second statement)."""
    versions = _parse(res)
    _stash(versions)
    return versions


def applied_versions(page: str) -> set[int]:
    """Every applied migration version, for this full script run. Empty when unreadable."""
    versions = _stashed()
    if versions is None:
        res = run(mart_sql.schema_version(), page=page, key=_READ_KEY, tier=TIER,
                  source="SCHEMA_VERSION", probe=True)
        versions = remember(res)
    return set(versions)


def has_migration(v: int, page: str) -> bool:
    """True once V<v> is in the applied SCHEMA_VERSION set; False when it is not, or unreadable."""
    return int(v) in applied_versions(page)


def has_migration_fresh(v: int, page: str) -> bool:
    """``has_migration`` for a write whose SQL changes shape at the apply (see the module docstring).

    The stash answers when it already holds V<v> (no statement). Otherwise SCHEMA_VERSION is re-read on the
    live tier, once per full script run; False when that read is unreadable too (the pre-apply shape)."""
    if has_migration(v, page):
        return True
    versions = _stashed(FRESH_STASH_KEY)
    if versions is None:
        res = run(mart_sql.schema_version(), page=page, key=_FRESH_READ_KEY, tier=FRESH_TIER,
                  source="SCHEMA_VERSION", probe=True)
        versions = _parse(res)
        _stash(versions, FRESH_STASH_KEY)
    return int(v) in versions
