"""Per-query optimization advisor (pure, tested, zero AI cost).

From one query's ACCOUNT_USAGE.QUERY_HISTORY stats (the row the Operations query
drill already loads via insights_sql.query_detail), emit concrete plain-English
fixes plus a composite 0-100 "optimize me first" badness score — deterministic,
no Cortex. An OPTIONAL per-query Cortex rewrite is wired separately in the UI and
never runs unless the operator clicks it.

Thresholds mirror ops_sql.query_optimization_triage / poor_pruning_queries EXACTLY
(remote spill > 0; PARTITIONS_TOTAL >= 100 AND scan-ratio > 0.8; > 50 GB scanned)
so the drill's findings never contradict the triage table that links here. The
score is a capped weighted sum (scoring._cap pattern): no single driver saturates
it, so a query bad on three axes always outranks one bad on one.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from .formulas import safe_div, safe_float

# --- thresholds (kept identical to ops_sql.query_optimization_triage) -------
REMOTE_SPILL_MIN_GB = 0.0        # any remote spill is memory exhaustion
LOCAL_SPILL_MIN_GB = 1.0         # local-only spill: milder pressure
POOR_PRUNE_MIN_PARTITIONS = 100  # below this, a high scan-ratio isn't meaningful
POOR_PRUNE_SCAN_RATIO = 0.8      # scanned/total above this = poor pruning
COLD_SCAN_MIN_GB = 50.0          # a "large" scan (mirrors triage's ELSE branch: size alone)
COMPILE_FRACTION = 0.5           # compile time this share of elapsed = compile-bound
COMPILE_MIN_ELAPSED_SEC = 1.0    # ignore trivially short queries
QUEUE_FRACTION = 0.5             # queued this share of elapsed = concurrency/resume
QUEUE_MIN_SEC = 1.0
ZERO_RESULT_MIN_GB = 10.0        # scanned a lot and produced nothing

# --- per-driver score weights + caps (a query maxes at 100) -----------------
# base points + a size-scaled bonus, each capped so one axis can't dominate.
_CAP = {"remote_spill": 55, "poor_pruning": 30, "cold_scan": 25,
        "compile_bound": 20, "queued": 15, "local_spill": 12, "zero_result": 12}


@dataclass(frozen=True)
class Finding:
    """One deterministic optimization finding for a query."""

    code: str
    severity: str   # "bad" (material) | "warn" (softer)
    title: str      # short chip label
    detail: str     # plain-English fix, grounded in this query's numbers
    points: int     # contribution to the 0-100 badness score


def _f(row: Mapping[str, object], col: str, default: float = 0.0) -> float:
    return safe_float(row.get(col), default)


def _cap(value: float, cap: float) -> int:
    return int(min(max(value, 0.0), cap))


def _size(row: Mapping[str, object]) -> str:
    s = str(row.get("WAREHOUSE_SIZE") or "").strip()
    return s or "current"


def advise(row: Mapping[str, object]) -> tuple[list[Finding], int]:
    """Return (findings, score). `row` is one query_detail row (Series or dict).

    score is 0-100 ("optimize me first"): the capped sum of fired findings.
    An empty list + score 0 means nothing actionable was detected.
    """
    findings: list[Finding] = []

    elapsed = _f(row, "ELAPSED_SEC")
    remote_spill = _f(row, "REMOTE_SPILL_GB")
    local_spill = _f(row, "LOCAL_SPILL_GB")
    gb_scanned = _f(row, "GB_SCANNED")
    cache_pct = _f(row, "CACHE_PCT")
    compile_sec = _f(row, "COMPILE_SEC")
    queued_sec = _f(row, "QUEUED_SEC")
    parts_scanned = _f(row, "PARTITIONS_SCANNED")
    parts_total = _f(row, "PARTITIONS_TOTAL")
    rows_produced = _f(row, "ROWS_PRODUCED", -1.0)  # -1 = column absent/unknown

    # 1) remote spill — the query ran out of memory (worst signal)
    if remote_spill > REMOTE_SPILL_MIN_GB:
        pts = _cap(40 + remote_spill * 5, _CAP["remote_spill"])
        findings.append(Finding(
            "remote_spill", "bad", "Remote spill",
            f"Spilled {remote_spill:.1f} GB to remote storage — the {_size(row)} "
            "warehouse ran out of memory. Size up one step, or shrink the working "
            "set (select fewer columns, filter earlier, avoid a wide DISTINCT/"
            "ORDER BY over the full table).",
            pts))
    # 2) local-only spill — milder memory pressure
    elif local_spill > LOCAL_SPILL_MIN_GB:
        pts = _cap(6 + local_spill * 2, _CAP["local_spill"])
        findings.append(Finding(
            "local_spill", "warn", "Local spill",
            f"Spilled {local_spill:.1f} GB to local disk — some memory pressure, "
            "not yet remote. Watch it; if it grows to remote spill, size up or "
            "trim the working set.",
            pts))

    # 3) poor partition pruning
    scan_ratio = safe_div(parts_scanned, parts_total)
    if parts_total >= POOR_PRUNE_MIN_PARTITIONS and scan_ratio > POOR_PRUNE_SCAN_RATIO:
        pts = _cap(18 + (scan_ratio - POOR_PRUNE_SCAN_RATIO) * 60, _CAP["poor_pruning"])
        findings.append(Finding(
            "poor_pruning", "bad", "Poor pruning",
            f"Scanned {scan_ratio * 100:.0f}% of {int(parts_total):,} micro-"
            "partitions — almost no pruning. Filter on the clustering key, add or "
            "repair a cluster key, or remove a function wrapping the filter column "
            "(it defeats pruning).",
            pts))

    # 4) large scan — size alone, exactly like the triage table's ELSE branch
    #    (no cache gate, so the two surfaces never disagree for the same query).
    if gb_scanned > COLD_SCAN_MIN_GB:
        pts = _cap(12 + gb_scanned / 50.0 * 8, _CAP["cold_scan"])
        findings.append(Finding(
            "cold_scan", "bad", "Large scan",
            f"Read {gb_scanned:.0f} GB ({cache_pct:.0f}% from cache). Select just the "
            "columns you need, filter earlier, or materialize the hot subset so repeat "
            "reads stay warm.",
            pts))

    # 5) compile-bound
    if elapsed >= COMPILE_MIN_ELAPSED_SEC and safe_div(compile_sec, elapsed) > COMPILE_FRACTION:
        frac = safe_div(compile_sec, elapsed) * 100
        pts = _cap(10 + (frac - 50) / 5, _CAP["compile_bound"])
        findings.append(Finding(
            "compile_bound", "warn", "Compile-bound",
            f"Compilation was {frac:.0f}% of the {elapsed:.1f}s runtime — usually a "
            "huge IN-list or a very wide/heavily-joined statement. Parameterize the "
            "IN-list (bind or a temp table) or simplify the statement.",
            pts))

    # 6) queued
    if queued_sec >= QUEUE_MIN_SEC and elapsed > 0 and safe_div(queued_sec, elapsed) > QUEUE_FRACTION:
        pts = _cap(8 + queued_sec, _CAP["queued"])
        findings.append(Finding(
            "queued", "warn", "Queued",
            f"Spent {queued_sec:.0f}s queued (of {elapsed:.1f}s total) — either "
            "concurrency (add a cluster or size up for parallelism) or warehouse "
            "resume overhead (lengthen AUTO_SUSPEND / keep it warm).",
            pts))

    # 7) zero-result-expensive
    if gb_scanned > ZERO_RESULT_MIN_GB and rows_produced == 0.0:
        pts = _cap(8 + gb_scanned / 50.0 * 4, _CAP["zero_result"])
        findings.append(Finding(
            "zero_result", "warn", "Expensive empty result",
            f"Scanned {gb_scanned:.0f} GB and returned 0 rows. Add an earlier "
            "filter or an EXISTS/LIMIT existence check so it stops reading before "
            "the full scan.",
            pts))

    findings.sort(key=lambda f: f.points, reverse=True)   # most impactful fix first
    score = min(100, sum(f.points for f in findings))
    return findings, score
