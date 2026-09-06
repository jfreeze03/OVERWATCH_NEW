"""Bug-hunt round 29b: cache-correctness re-run (the r29 cache-key finder had errored).

1 confirmed defect + a docstring accuracy fix; the cross-viewer-leak concern was RAISED and
REFUTED (verified directly: the only viewer-scoped builder is prefs_sql/USER_PREFS, which is
exactly what _cache_scope keys by `user_part`; every other read is account-wide/role-keyed or
bakes its company filter into the SQL text, which is itself a cache-key argument).

#1 (MED, batch-cache): run_batch's success loop re-stamped the process-global member cache with
   a FRESH now+TTL expiry even on a tuple-cache HIT, where the replayed `frames` are already up
   to CACHE_TTLS[tier] old. An evicted member re-served on a tuple hit thus got its freshness
   extended toward ~2x the tier TTL, and run_batch_mixed (no tuple layer) then served it stale.
   Fixed: only (re)put the member cache on a FRESH tuple fetch (`if not cache_hit_batch:`).

Docstring fix (LOW): the mart-fail backoff comment claimed it "clears the instant the mart
succeeds", but inside the 120s window the on-demand mart read is skipped, so it clears only on
window expiry or a healthy preloaded prefetch. Corrected the comment to say so.
"""

from __future__ import annotations

from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]


def _read(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


def test_batch_member_cache_not_restamped_on_tuple_hit():
    src = _read("app/core/query.py")
    body = src.split("def run_batch(", 1)[1].split("\ndef ", 1)[0]
    # the success loop (after cache_hit_batch is computed) guards the member re-put so a
    # tuple-cache HIT never extends a member entry's TTL with already-stale replayed data
    loop = body.split("cache_hit_batch = _batch_cache_hit()", 1)[1].split("batch_wall:", 1)[0]
    assert "if not cache_hit_batch:" in loop
    guard = loop.index("if not cache_hit_batch:")
    put = loop.index("_batch_member_cache_put(", guard)
    # the guard immediately precedes the success-loop put (no unguarded put after it)
    assert put > guard
    assert "_batch_member_cache_put(" not in loop[:guard], \
        "the success-loop member put must be guarded by `if not cache_hit_batch:`"


def test_mart_backoff_docstring_is_accurate_about_recovery():
    src = _read("app/ui/components.py")
    # the overstated claim is gone; the honest description (expiry OR a preloaded prefetch) is in
    assert "clears the instant the mart succeeds" not in src
    assert "_note_mart_health(ok=True)" in src and "on-demand mart read is skipped" in src
