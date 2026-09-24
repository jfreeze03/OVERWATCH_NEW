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
# metadata chatter / compile-dominated discovery: almost ALL compile with ~no warehouse
# execution (a SYSTEM$/SHOW/INFORMATION_SCHEMA/driver metadata call). Its cost is in the
# cloud-services layer, so a resize cannot touch it — distinct from compile_bound (a genuinely
# expensive compile on real warehouse work, where execution is non-trivial).
METADATA_COMPILE_FRACTION = 0.7  # compile this share of elapsed AND ...
METADATA_MAX_EXEC_SEC = 0.5      # ... execution at/below this = no real warehouse work
ZERO_RESULT_MIN_GB = 10.0        # scanned a lot and produced nothing
# R2/R3: on the fingerprint grain (AVG'd columns), the queued/compile gates fire only when the
# pathology is TYPICAL (>= this share of runs), not when one storm run inflated the AVG ratio.
FINGERPRINT_QUEUE_TYPICAL_SHARE = 0.5
FINGERPRINT_COMPILE_TYPICAL_SHARE = 0.5
# Next-Fifty #17: a queued fingerprint is named a cold start (or overload) only when that component
# dominated at least this share of its meaningfully-queued runs; a mixed fingerprint keeps the hedge.
FINGERPRINT_SPLIT_DOMINANT_SHARE = 0.6

# --- per-driver score weights + caps (a query maxes at 100) -----------------
# base points + a size-scaled bonus, each capped so one axis can't dominate.
_CAP = {"remote_spill": 55, "poor_pruning": 30, "cold_scan": 25,
        "compile_bound": 20, "metadata_chatter": 18, "queued": 15, "cold_start": 15,
        "local_spill": 12, "zero_result": 12}


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


def advise(row: Mapping[str, object], *,
           remote_spill_floor_gb: float = REMOTE_SPILL_MIN_GB) -> tuple[list[Finding], int]:
    """Return (findings, score). `row` is one query_detail row (Series or dict).

    score is 0-100 ("optimize me first"): the capped sum of fired findings.
    An empty list + score 0 means nothing actionable was detected.

    ``remote_spill_floor_gb`` overrides the >0 remote-spill trip. It stays 0.0 for a
    single QUERY_HISTORY row (any real spill = memory exhaustion), but the fleet
    fingerprint path passes a small floor because it feeds AVG(remote spill): a rare
    1-in-N spill averages to ~0 and must NOT fire the "ran out of memory, size up"
    finding (that both misreads and recommends a cost increase — bug-hunt R1).

    More fingerprint-grain guards (R2/R3), read from ``row`` when the builder supplies them:
    ``QUEUED_RUN_PCT`` and ``COMPILE_RUN_PCT`` (queued / compile_bound fire only when the
    pathology is TYPICAL — a majority of runs — not one storm run inflating the AVG ratio) and
    ``MAX_ROWS_PRODUCED`` (zero_result fires only when NO run ever returned rows, since AVG(rows)
    can round to 0). All are absent on the per-QUERY grain, where the per-run gates are
    themselves correct, so they no-op there (sentinel -1).
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
    # R2 typical-run guards: present only on the fingerprint (AVG) grain; -1 = per-QUERY grain,
    # where the per-run gates below are themselves correct so the guard is a no-op.
    queued_run_pct = _f(row, "QUEUED_RUN_PCT", -1.0)
    compile_run_pct = _f(row, "COMPILE_RUN_PCT", -1.0)
    max_rows = _f(row, "MAX_ROWS_PRODUCED", -1.0)

    # 1) remote spill — the query ran out of memory (worst signal)
    if remote_spill > remote_spill_floor_gb:
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

    # 5a) metadata chatter / compile-dominated discovery — almost ALL compile with trivial
    #     warehouse execution. The cost lives in the cloud-services layer, so a resize can't touch
    #     it and the fix is behavioural (cadence, not SQL). Fires ahead of compile_bound and
    #     SUPPRESSES it (more specific label). EXECUTION_SEC when the builder supplies it, else
    #     derived as elapsed - compile - queued. Typical-run guarded on the fingerprint grain.
    execution_sec = _f(row, "EXECUTION_SEC", -1.0)
    if execution_sec < 0:
        execution_sec = max(0.0, elapsed - compile_sec - queued_sec)
    compile_frac = safe_div(compile_sec, elapsed)
    # dedicated typical-run guard (COMPILE_RUN_PCT gates on >=1s compile; chatter is sub-second):
    metadata_run_pct = _f(row, "COMPILE_DOMINANT_RUN_PCT", -1.0)
    metadata_chatter = (
        elapsed > 0 and compile_frac >= METADATA_COMPILE_FRACTION
        and execution_sec <= METADATA_MAX_EXEC_SEC
        and (metadata_run_pct < 0 or metadata_run_pct >= FINGERPRINT_COMPILE_TYPICAL_SHARE))
    if metadata_chatter:
        pts = _cap(10 + (compile_frac * 100 - 70) / 3, _CAP["metadata_chatter"])
        findings.append(Finding(
            "metadata_chatter", "warn", "Metadata chatter",
            f"Compilation was {compile_frac * 100:.0f}% of a {elapsed:.1f}s runtime with only "
            f"{execution_sec:.1f}s of execution — this is a metadata / discovery call (SHOW, "
            "INFORMATION_SCHEMA, a SYSTEM$ probe, or a driver's schema introspection), not "
            "warehouse work. A resize won't help. Reduce the CADENCE: cache the metadata, batch "
            "the calls, pool connections, or quiet the tool issuing it (Operations > Queries > "
            "cloud-services chatter by application shows who).",
            pts))

    # 5) compile-bound
    if (not metadata_chatter
            and elapsed >= COMPILE_MIN_ELAPSED_SEC and safe_div(compile_sec, elapsed) > COMPILE_FRACTION
            and (compile_run_pct < 0 or compile_run_pct >= FINGERPRINT_COMPILE_TYPICAL_SHARE)):
        frac = safe_div(compile_sec, elapsed) * 100
        pts = _cap(10 + (frac - 50) / 5, _CAP["compile_bound"])
        findings.append(Finding(
            "compile_bound", "warn", "Compile-bound",
            f"Compilation was {frac:.0f}% of the {elapsed:.1f}s runtime — usually a "
            "huge IN-list or a very wide/heavily-joined statement. Parameterize the "
            "IN-list (bind or a temp table) or simplify the statement.",
            pts))

    # 6) queued — on the fingerprint grain, only when queueing is TYPICAL (not one storm run
    #    inflating AVG(queued) past the ratio gate). queued_run_pct < 0 = per-query grain.
    #    Next-Fifty #17: when the overload/provisioning split is known, name the DOMINANT component —
    #    a resume (provisioning) wait is a cold start that a bigger warehouse does not fix. Points stay on
    #    the combined wait, so QOP/SQL_QOP/OOS and the ranking are byte-stable; only the label + fix move.
    over_sec = _f(row, "QUEUED_OVERLOAD_SEC", -1.0)
    prov_sec = _f(row, "QUEUED_PROVISIONING_SEC", -1.0)
    split_known = over_sec >= 0 and prov_sec >= 0
    # Which component to name. On the fingerprint grain the AVGs are time-weighted (one overload storm
    # outweighs ninety cold starts), so decide from the per-run dominance shares when the builder
    # supplies them, and name a cause only when it dominates most queued runs; else keep the hedged
    # wording. Per-QUERY grain (no shares): the two components are that run's real values.
    prov_runs = _f(row, "PROVISIONING_QUEUED_RUN_PCT", -1.0)
    over_runs = _f(row, "OVERLOAD_QUEUED_RUN_PCT", -1.0)
    wait_cause: str | None = None
    if split_known and prov_runs >= 0 and over_runs >= 0 and (prov_runs + over_runs) > 0:
        prov_share = prov_runs / (prov_runs + over_runs)
        if prov_share >= FINGERPRINT_SPLIT_DOMINANT_SHARE:
            wait_cause = "provisioning"
        elif 1.0 - prov_share >= FINGERPRINT_SPLIT_DOMINANT_SHARE:
            wait_cause = "overload"
    elif split_known:
        wait_cause = "provisioning" if prov_sec > over_sec else "overload"
    if (queued_sec >= QUEUE_MIN_SEC and elapsed > 0 and safe_div(queued_sec, elapsed) > QUEUE_FRACTION
            and (queued_run_pct < 0 or queued_run_pct >= FINGERPRINT_QUEUE_TYPICAL_SHARE)):
        pts = _cap(8 + queued_sec, _CAP["queued"])
        if wait_cause == "provisioning":
            findings.append(Finding(
                "cold_start", "warn", "Cold-start wait",
                f"Waited {queued_sec:.0f}s (of {elapsed:.1f}s total), {prov_sec:.0f}s of it PROVISIONING — "
                "the warehouse was resuming from suspend, not overloaded. A bigger warehouse won't help "
                "(each size step doubles the per-second rate). If the wait matters, keep it warm across "
                "this query's schedule (co-schedule it with other work on the warehouse, or lengthen "
                "AUTO_SUSPEND just enough to span the gap between runs — that costs idle credits); "
                "otherwise accept the resume latency.",
                pts))
        elif wait_cause == "overload":
            findings.append(Finding(
                "queued", "warn", "Queued",
                f"Spent {queued_sec:.0f}s queued (of {elapsed:.1f}s total), {over_sec:.0f}s of it OVERLOAD — "
                "the warehouse was saturated. A concurrency problem, not bad SQL: raise MAX_CLUSTER_COUNT "
                "(multi-cluster) or move this workload to its own warehouse; size up only if single "
                "queries are also slow or spilling.",
                pts))
        else:  # split unknown (older row shape) or no dominant cause: the original combined wording
            findings.append(Finding(
                "queued", "warn", "Queued",
                f"Spent {queued_sec:.0f}s queued (of {elapsed:.1f}s total) — either "
                "concurrency (add a cluster or size up for parallelism) or warehouse "
                "resume overhead (lengthen AUTO_SUSPEND / keep it warm).",
                pts))

    # 7) zero-result-expensive — on the fingerprint grain, only when NO run ever returned rows
    #    (AVG(rows) can round to 0 while a minority of runs do return rows). max_rows < 0 = per-query.
    if (gb_scanned > ZERO_RESULT_MIN_GB and rows_produced == 0.0
            and (max_rows < 0 or max_rows == 0.0)):
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
