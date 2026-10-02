"""Warehouse right-sizing advisor (pure, tested).

Transparent scenario model, not a promise: Snowflake size steps double/halve
the credit rate, so the table shows current monthly spend next to the
mechanical x0.5 / x2.0 scenarios and a rules-based recommendation. Runtime
effects are workload-dependent; the rationale says why, the DBA decides.
"""

from __future__ import annotations

from datetime import date

import pandas as pd

from .formulas import humanize_duration, safe_float

QUEUE_UP_MIN_PER_DAY = 30.0    # sustained OVERLOAD queueing -> add a cluster (scale out) [#38]
# D2 (audit 2026-07-31): every load signal is now PER DAY. The spill threshold
# used to be a WINDOW TOTAL sitting next to a per-day queue threshold, so the
# same warehouse crossed it at 90d and not at 7d — the advice moved with the
# window picker. 1 GB/day of REMOTE spill is real memory pressure; a tenth of
# that is the noise floor a size-DOWN must stay under.
SPILL_UP_GB_PER_DAY = 1.0
SPILL_DOWN_MAX_GB_PER_DAY = 0.1
DOWN_P95_SEC = 10.0            # fast p95 and calm queue -> down candidate
DOWN_IDLE_PCT = 30.0           # meaningful idle share strengthens down case
SUSPEND_FIRST_IDLE_PCT = 50.0  # mostly idle -> fix auto-suspend before resizing
MIN_ACTIVE_DAYS = 2
MIN_ACTIVE_DAYS_PER_30D = 3.0
AUTO_SUSPEND_TARGET_SEC = 60

# Next-Fifty #38: the merged "Size up / add cluster" verdict is SPLIT by the kind of pressure.
# Overload queueing with no remote spill is CONCURRENCY -> more clusters, not a bigger size.
# Remote spill is PER-QUERY memory -> a bigger size (more memory per cluster); a cluster does not
# help one spilling query. Both at once -> size up FIRST (spilling queries hold slots longer).
RECOMMEND_SCALE_OUT = "Add a cluster (scale out)"
RECOMMEND_SIZE_UP = "Size up"
# Next-Fifty #38 cluster-cap gate: queueing on a multi-cluster warehouse whose queries never reached its
# current MAX_CLUSTER_COUNT is not cluster-cap-bound (cluster start-up, a few long queries), so a higher
# maximum would not help. Capacity pressure all the same: actionable, ranked with the up verdicts, $0.
RECOMMEND_BELOW_CAP = "Size up or split (cluster cap not reached)"
# Legacy merged verdict (<= v4.600.0). NEVER emitted any more; kept so an old import or an
# externally built frame still resolves and still counts as capacity pressure.
RECOMMEND_UP = "Size up / add cluster"
UP_VERDICTS = frozenset({RECOMMEND_SCALE_OUT, RECOMMEND_SIZE_UP, RECOMMEND_UP, RECOMMEND_BELOW_CAP})
# A long peak-day p95 is context for a scale-out row, never a routing signal (on the mart path it
# is the PEAK daily p95, so it would over-route to size-up). Mirrors wh_health.LONG_P95_SEC.
LONG_P95_SEC = 120.0
# remediation.cluster_range_fix clamps MAX_CLUSTER_COUNT to 10; the prefill never promises more.
CLUSTER_RANGE_CAP = 10
# Next-Fifty #38 cluster-cap gate. Months are <= 31 days, so any trailing 35-day window holds a month-end.
CLUSTER_CHECK_MIN_DAYS = 35
CLUSTER_CHECK_MAX_DAYS = 90          # the live QUERY_HISTORY clamp (data.common.bounded_days)
CLUSTER_CHECK_MAX_WAREHOUSES = 100   # == the sizing profile's LIMIT 100, so no profile row is ever left out
CAP_REACHED, CAP_NOT_REACHED, CAP_NO_QUERIES, CAP_NOT_CHECKED = "reached", "not_reached", "no_queries", "not_checked"
# Where the cluster-cap check lives (review r1 R1-5 / R1-11): every surface that says "add a cluster" without
# running the check points here (through CLUSTER_CAP_QUALIFIER), so a higher MAX_CLUSTER_COUNT is not advised on a
# cap nobody checked. tests/test_cluster_cap_gate.py sweeps app/ for add-a-cluster text without the qualifier
# (review r2 R2-4); its allowlist names the exceptions: labels, the gated verdicts, and one unqualified text, the
# query advisor's fallback for a row WITHOUT the overload/provisioning split (the older row shape), which Next-Fifty
# #17 byte-locks in tests/test_cold_start_split.py. The advisor's split-known, no-dominant-cause text carries the
# qualifier (review r3 R3-2 / R3-7).
CLUSTER_CAP_CHECK_PATH = "Cost Intelligence ▸ Optimization & Savings ▸ Idle & sizing ▸ Check cluster use"
# The qualifier those surfaces carry (ETL evidence, Operations), inside their own parentheses: no read,
# just the rule.
CLUSTER_CAP_QUALIFIER = ("on a multi-cluster warehouse, raise MAX_CLUSTER_COUNT only if its queries reach the "
                         f"current maximum — {CLUSTER_CAP_CHECK_PATH} checks it")
RECOMMEND_DOWN = "Size down candidate"
RECOMMEND_SUSPEND = "Tune auto-suspend first"
RECOMMEND_CADENCE = "Review cadence / consolidation"
RECOMMEND_OBSERVE = "Collect more evidence"
RECOMMEND_KEEP = "Keep"


def size_recommendations(df: pd.DataFrame, credit_rate_usd: float, window_days: int) -> pd.DataFrame:
    """Score each warehouse row and attach scenario dollars + recommendation.

    Expects columns: WAREHOUSE_NAME, CREDITS_TOTAL, QUERY_COUNT, ACTIVE_QUERY_DAYS,
    P95_ELAPSED_SEC, QUEUED_SEC, SPILL_REMOTE_GB, IDLE_PCT (optional),
    QUEUED_PROVISIONING_SEC (optional).

    ``window_days`` must be the window actually SERVED (components.served_days),
    not the requested one: every per-day rate and the x30 monthly figure below
    divide by it, and a live builder clamped to 90d fed a 365d divisor reads ~4x
    low.

    D2: QUEUED_SEC is OVERLOAD queueing only where the reader can split it.
    Provisioning time is a warehouse waking up — an auto-suspend/scheduling
    signal, not concurrency pressure — so sizing up to "fix" it buys nothing.
    """
    if df is None or df.empty:
        return pd.DataFrame()
    rate = safe_float(credit_rate_usd, 3.68)
    days = max(int(window_days or 1), 1)
    out = df.copy()
    for col in ("CREDITS_TOTAL", "QUERY_COUNT", "ACTIVE_QUERY_DAYS", "P95_ELAPSED_SEC", "QUEUED_SEC",
                "QUEUED_PROVISIONING_SEC", "SPILL_REMOTE_GB", "IDLE_PCT"):
        if col in out.columns:
            out[col] = out[col].map(safe_float)
    if "IDLE_PCT" not in out.columns:
        out["IDLE_PCT"] = 0.0
    if "ACTIVE_QUERY_DAYS" not in out.columns:
        out["ACTIVE_QUERY_DAYS"] = 0.0
    has_prov = "QUEUED_PROVISIONING_SEC" in out.columns
    settings_supplied = "AUTO_SUSPEND" in df.columns
    if settings_supplied:
        current_suspend = pd.to_numeric(out["AUTO_SUSPEND"], errors="coerce")
        suspend_known = current_suspend.notna()
        if "AUTO_SUSPEND_KNOWN" in out.columns:
            suspend_known &= out["AUTO_SUSPEND_KNOWN"].fillna(False).astype(bool)
        out["AUTO_SUSPEND"] = current_suspend
        out["AUTO_SUSPEND_KNOWN"] = suspend_known

    out["QUEUED_MIN_PER_DAY"] = (out["QUEUED_SEC"] / 60.0 / days).round(1)
    out["SPILL_GB_PER_DAY"] = (out["SPILL_REMOTE_GB"] / days).round(2)
    out["ACTIVE_DAYS_PER_30D"] = (out["ACTIVE_QUERY_DAYS"] / days * 30).round(1)
    out["EVIDENCE_SUFFICIENT"] = (
        (out["ACTIVE_QUERY_DAYS"] >= MIN_ACTIVE_DAYS)
        & (out["ACTIVE_DAYS_PER_30D"] >= MIN_ACTIVE_DAYS_PER_30D)
    )
    out["CONFIDENCE"] = out["EVIDENCE_SUFFICIENT"].map({True: "MEDIUM", False: "LOW"})
    out.loc[
        (out["ACTIVE_QUERY_DAYS"] >= 7) & (out["ACTIVE_DAYS_PER_30D"] >= 7),
        "CONFIDENCE",
    ] = "HIGH"
    if has_prov:
        out["PROVISION_MIN_PER_DAY"] = (out["QUEUED_PROVISIONING_SEC"] / 60.0 / days).round(1)
    out["MONTHLY_USD_NOW"] = (out["CREDITS_TOTAL"] * rate / days * 30).round(0)
    out["SCENARIO_DOWN_USD"] = (out["MONTHLY_USD_NOW"] * 0.5).round(0)
    out["SCENARIO_UP_USD"] = (out["MONTHLY_USD_NOW"] * 2.0).round(0)
    # D2: the idle share of today's bill — the money a SUSPEND row is really
    # about. Unlike the x0.5 scenario this is measured, not speculative.
    out["IDLE_MONTHLY_USD"] = (out["MONTHLY_USD_NOW"] * out["IDLE_PCT"].clip(0, 100) / 100).round(0)
    # rec #13: a size-down saving is a RANGE, not a flat half-the-bill point.
    # Halving the per-hour rate reliably halves only the IDLE portion; a
    # compute-bound query on a smaller warehouse runs ~2x longer, so its BUSY
    # credits stay roughly flat (worst case). Honest bounds:
    #   LOW  = 0.5 * idle          (busy credits unchanged — the safe floor)
    #   HIGH = 0.5 * monthly       (busy also halves — the old optimistic point)
    _busy_monthly = (out["MONTHLY_USD_NOW"] - out["IDLE_MONTHLY_USD"]).clip(lower=0)
    out["SAVING_LOW_USD"] = (out["IDLE_MONTHLY_USD"] * 0.5).round(0)
    out["SAVING_HIGH_USD"] = ((out["IDLE_MONTHLY_USD"] + _busy_monthly) * 0.5).round(0)

    def _recommend(row) -> tuple[str, str]:
        queued = row["QUEUED_MIN_PER_DAY"]
        spill = row["SPILL_GB_PER_DAY"]
        idle = row["IDLE_PCT"]
        p95 = row["P95_ELAPSED_SEC"]
        pressure = queued >= QUEUE_UP_MIN_PER_DAY or spill >= SPILL_UP_GB_PER_DAY
        enough = bool(row["EVIDENCE_SUFFICIENT"])
        if pressure and enough:
            return _pressure_verdict(row, queued, spill, p95)
        if idle >= SUSPEND_FIRST_IDLE_PCT:
            if settings_supplied:
                if not bool(row.get("AUTO_SUSPEND_KNOWN", False)):
                    return RECOMMEND_OBSERVE, (
                        f"{idle:.0f}% idle, but current AUTO_SUSPEND is unknown; verify the setting "
                        "before treating idle spend as a timer opportunity.")
                current = safe_float(row.get("AUTO_SUSPEND"), 0.0)
                if 0 < current <= AUTO_SUSPEND_TARGET_SEC:
                    return RECOMMEND_CADENCE, (
                        f"{idle:.0f}% idle despite AUTO_SUSPEND={current:.0f}s; review workload cadence, "
                        "warehouse consolidation, or retirement instead of lengthening the timer.")
            prov = ""
            if has_prov and row.get("PROVISION_MIN_PER_DAY", 0) >= 1:
                prov = (f" ({humanize_duration(row['PROVISION_MIN_PER_DAY'], 'min')}/day provisioning — "
                        "resume overhead, not concurrency)")
            return RECOMMEND_SUSPEND, (
                f"{idle:.0f}% of credits are idle-hours{prov} - shorten AUTO_SUSPEND before resizing.")
        if not enough:
            return RECOMMEND_OBSERVE, (
                f"Only {row['ACTIVE_QUERY_DAYS']:.0f} active query day(s) "
                f"({row['ACTIVE_DAYS_PER_30D']:.1f}/30d); do not resize from episodic evidence.")
        if (queued < 1 and spill < SPILL_DOWN_MAX_GB_PER_DAY
                and p95 <= DOWN_P95_SEC and idle >= DOWN_IDLE_PCT):
            # r34: never recommend a size-DOWN (or book its saving) for a warehouse already at
            # the smallest size — there is no target below XSMALL, so "one size down at half the
            # rate" is impossible advice and POTENTIAL_MONTHLY_SAVING_USD would be unrealizable.
            # When the current size is supplied (CURRENT_SIZE, from SHOW WAREHOUSES) and IS the
            # floor, route to cadence/consolidation instead. Size unknown -> unchanged behavior
            # (we can't prove it's the floor, so the fail-open path keeps the DOWN candidate).
            if normalize_size(row.get("CURRENT_SIZE")) == SIZE_ORDER[0]:
                return RECOMMEND_CADENCE, (
                    f"Already at the smallest size (X-Small), {idle:.0f}% idle - no size-down "
                    "target; reduce idle via AUTO_SUSPEND cadence or consolidation.")
            return RECOMMEND_DOWN, (
                f"No queueing, no spill, p95 {humanize_duration(p95, 's')}, {idle:.0f}% idle - "
                "one size down likely holds SLAs at half the rate.")
        return RECOMMEND_KEEP, "Load and capacity look matched for this window."

    verdicts = out.apply(_recommend, axis=1, result_type="expand")
    out["RECOMMENDATION"] = verdicts[0]
    out["RATIONALE"] = verdicts[1]
    out["ACTIONABLE"] = out["RECOMMENDATION"].isin(
        UP_VERDICTS | {RECOMMEND_SUSPEND, RECOMMEND_DOWN}
    )
    # rec #13: the HEADLINE saving is the conservative floor (only idle reliably
    # shrinks) — not the old MONTHLY - 0.5*MONTHLY = 0.5*MONTHLY that assumed every
    # busy credit halves too. SAVING_LOW_USD / SAVING_HIGH_USD carry the full range.
    out["POTENTIAL_MONTHLY_SAVING_USD"] = out.apply(
        lambda r: r["SAVING_LOW_USD"] if r["RECOMMENDATION"] == RECOMMEND_DOWN else 0.0,
        axis=1,
    )
    # D2: SUSPEND outranks DOWN. Tuning a timer is reversible, costs nothing, and
    # its saving is measured idle; a size-down is a speculative SLA bet whose
    # "saving" is the mechanical half-rate scenario. The old order sold the bet
    # first.
    order = {**dict.fromkeys(UP_VERDICTS, 0), RECOMMEND_SUSPEND: 1, RECOMMEND_DOWN: 2,
             RECOMMEND_CADENCE: 3, RECOMMEND_OBSERVE: 4, RECOMMEND_KEEP: 5}
    out["_O"] = out["RECOMMENDATION"].map(order).fillna(9)
    return (out.sort_values(["_O", "MONTHLY_USD_NOW"], ascending=[True, False])
            .drop(columns="_O").reset_index(drop=True))


def _num(value: object) -> float:
    """NaN for absent/unparseable (with_warehouse_settings leaves NaN on an unmatched warehouse)."""
    return safe_float(value, default=float("nan"))


def _policy(value: object) -> str:
    return value.strip().upper() if isinstance(value, str) else ""


def _queue_follow_up(row) -> str:
    """The Size up (spill + queueing) rationale's follow-up (review r2 R2-4 / R2-11): add a cluster if the queue
    persists, gated like the scale-out verdict. On a multi-cluster non-ECONOMY warehouse, in this order:
      - the cluster-cap check ran and no query reached the cap -> split instead, with the evidence (a higher maximum
        would not help);
      - already at the generator's cap of CLUSTER_RANGE_CAP (10) clusters or more -> split the workload across
        warehouses, whether or not the cap was checked (reached, not checked or no clustered query), as the
        scale-out verdict does (review r3 R3-3 / R3-6);
      - below that cap and the check shows it reached -> add one, with the hours.
    Everything else (single-cluster, ECONOMY, unknown range, and below the cap not checked or no clustered query)
    gets the rule itself, CLUSTER_CAP_QUALIFIER."""
    lead = "if queueing persists after the resize, "
    mx = _num(row.get("MAX_CLUSTER_COUNT"))
    if mx == mx and mx > 1 and _policy(row.get("SCALING_POLICY")) != "ECONOMY":
        n, cap = int(mx), cluster_cap_state(row)
        d = int(_num(row.get("CLUSTER_CHECK_DAYS"))) if cap in (CAP_REACHED, CAP_NOT_REACHED) else 0
        if cap == CAP_NOT_REACHED:
            return (lead + f"split the workload: in the last {d} days no query ran above cluster "
                    f"{int(_num(row.get('PEAK_CLUSTERS')))} of {n}, so a higher MAX_CLUSTER_COUNT would not help.")
        if n >= CLUSTER_RANGE_CAP:
            return lead + f"split the workload across warehouses (already at {n} clusters)."
        if cap == CAP_REACHED:
            return (lead + f"add a cluster: queries reached cluster {n} of {n} in "
                    f"{_hour_count_txt(int(_num(row.get('AT_CAP_HOUR_COUNT'))))} of the last {d} days.")
    return lead + "add a cluster (" + CLUSTER_CAP_QUALIFIER + ")."


def _pressure_verdict(row, queued: float, spill: float, p95: float) -> tuple[str, str]:
    """Next-Fifty #38: split capacity pressure into scale-out (concurrency) vs size-up (per-query).

    Cluster-cap gate (#38 remainder): on a multi-cluster non-ECONOMY warehouse a higher
    MAX_CLUSTER_COUNT is suggested ONLY when the cluster-cap check (with_cluster_use) shows queries
    reaching the current maximum (AT_CAP_HOUR_COUNT > 0). Checked and never at the cap ->
    RECOMMEND_BELOW_CAP (size up or split; no cluster statement). Not checked, or checked with no
    clustered query -> the rationale says so and suggests no raise. Single-cluster, ECONOMY and
    unknown-range rows are unchanged."""
    q_txt = f"{humanize_duration(queued, 'min')}/day overload queueing"
    if spill >= SPILL_UP_GB_PER_DAY:
        cur = normalize_size(row.get("CURRENT_SIZE"))
        step = ("" if not cur else
                " Already at the largest size — split the workload or fix the spilling queries."
                if cur == SIZE_ORDER[-1] else f" Next size: {shifted_size(cur, 1)}.")
        if queued >= QUEUE_UP_MIN_PER_DAY:
            return RECOMMEND_SIZE_UP, (
                f"Per-query memory pressure with queueing: {spill:.1f} GB/day remote spill and {q_txt}. "
                "Size up first — spilling queries hold slots longer, so the queue often clears too; "
                + _queue_follow_up(row) + step)
        return RECOMMEND_SIZE_UP, (
            f"Per-query memory pressure: {spill:.1f} GB/day remote spill. Size up one step for more "
            "memory per cluster — another cluster does not help a single spilling query." + step)
    lead = f"Concurrency pressure: {q_txt}, remote spill under {SPILL_UP_GB_PER_DAY:g} GB/day."
    long_tail = (f" Peak-day p95 is {humanize_duration(p95)} — long queries holding the slots point to a "
                 "size-up." if p95 >= LONG_P95_SEC else "")
    mx = _num(row.get("MAX_CLUSTER_COUNT"))
    gated = mx == mx and mx > 1 and _policy(row.get("SCALING_POLICY")) != "ECONOMY"
    cap = cluster_cap_state(row) if gated else ""
    if cap == CAP_NOT_REACHED:            # any n > 1, including n >= CLUSTER_RANGE_CAP
        n, d = int(mx), int(_num(row.get("CLUSTER_CHECK_DAYS")))
        peak = int(_num(row.get("PEAK_CLUSTERS")))
        return RECOMMEND_BELOW_CAP, (
            f"Concurrency pressure: {q_txt}, remote spill under {SPILL_UP_GB_PER_DAY:g} GB/day — but the "
            f"cluster cap is not what holds the queue: in the last {d} days no query ran above cluster "
            f"{peak} of {n}. Queueing below the cap usually comes from cluster start-up time or a few long "
            "queries holding the slots; size up so queries finish sooner, or split the workload onto its "
            "own warehouse. A higher MAX_CLUSTER_COUNT would not help." + long_tail)
    if gated and mx < CLUSTER_RANGE_CAP and cap in (CAP_NOT_CHECKED, CAP_NO_QUERIES):
        n = int(mx)
        why = (f"whether queries ever reach cluster {n} was not checked" if cap == CAP_NOT_CHECKED else
               "the cluster-cap check found no query with a cluster number on it in the last "
               f"{int(_num(row.get('CLUSTER_CHECK_DAYS')))} days, so whether queries reach cluster {n} "
               "is unknown")
        return RECOMMEND_SCALE_OUT, (
            f"{lead} Already multi-cluster (up to {n}); {why}, so no higher MAX_CLUSTER_COUNT is "
            "suggested. Raise it only if they do; otherwise size up or split the workload." + long_tail)
    if mx != mx:
        how = ("Multi-cluster needs Enterprise edition and MAX_CLUSTER_COUNT > 1 (the current setting "
               "is unknown); otherwise move the concurrent workload to its own warehouse.")
    elif mx <= 1:
        how = ("Single-cluster today (MAX_CLUSTER_COUNT = 1): raise MAX_CLUSTER_COUNT to 2 or more "
               "(multi-cluster needs Enterprise edition), or move the concurrent workload to its own "
               "warehouse.")
    elif _policy(row.get("SCALING_POLICY")) == "ECONOMY":
        how = (f"Already multi-cluster (up to {int(mx)}) on ECONOMY, which waits for sustained load "
               "before starting a cluster — try SCALING_POLICY = STANDARD before raising the maximum.")
    elif mx >= CLUSTER_RANGE_CAP:
        how = f"Already at {int(mx)} clusters — split the workload across warehouses."
    else:                                 # gated, below the generator cap, and the cap was reached
        n = int(mx)
        how = (f"Already multi-cluster (up to {n}), and queries reached cluster {n} of {n} in "
               f"{_hour_count_txt(int(_num(row.get('AT_CAP_HOUR_COUNT'))))} of the last "
               f"{int(_num(row.get('CLUSTER_CHECK_DAYS')))} days: raise MAX_CLUSTER_COUNT to {n + 1}, "
               "or split the workload.")
    tail = ""
    if p95 >= LONG_P95_SEC:
        tail = (f" Peak-day p95 is {humanize_duration(p95)} — if the queue sits behind a few long "
                "queries rather than many concurrent ones, size up instead.")
    return RECOMMEND_SCALE_OUT, f"{lead} Add a cluster rather than a bigger size. " + how + tail


def scale_out_plan(row, multi_cluster_seen: bool | None = None) -> dict:
    """Review-only scale-out prefill for ONE RECOMMEND_SCALE_OUT row (Next-Fifty #38). Pure.

    ``prefill`` is True only when a MAX_CLUSTER_COUNT statement is shown: a single-cluster warehouse
    (1 -> 2), or a multi-cluster STANDARD one whose cluster-cap check reached the current maximum
    (``cap`` == CAP_REACHED). Not checked / no queries / never reached -> no prefill, ``max`` stays the
    current value and the note says why (#38 remainder)."""
    mx = _num(row.get("MAX_CLUSTER_COUNT"))
    mn = _num(row.get("MIN_CLUSTER_COUNT"))
    edition = ("This account already runs multi-cluster warehouses, so the edition supports it."
               if multi_cluster_seen else
               "Multi-cluster needs Enterprise edition or higher — if the ALTER fails, move the "
               "concurrent workload to its own warehouse instead.")
    plan = {"known": mx == mx, "min": 1, "max": 1, "policy_to_standard": False, "at_cap": False,
            "prefill": False, "cap": "", "note": edition}
    if mx != mx:
        plan["note"] = "Current cluster range unknown (SHOW WAREHOUSES did not return it). " + edition
        return plan
    cur_max = max(1, int(mx))
    cur_min = max(1, int(mn)) if mn == mn else 1
    plan["min"] = min(cur_min, CLUSTER_RANGE_CAP)
    if cur_max > 1 and _policy(row.get("SCALING_POLICY")) == "ECONOMY":
        plan.update(policy_to_standard=True, max=cur_max,
                    note="ECONOMY waits for sustained load before starting a cluster; STANDARD starts "
                         "one as soon as queries queue. " + edition)
        return plan
    if cur_max >= CLUSTER_RANGE_CAP:
        plan.update(at_cap=True, max=cur_max,
                    note=f"Already at {cur_max} clusters (the generator's cap) — split the workload.")
        return plan
    if cur_max > 1:
        plan["cap"] = cluster_cap_state(row)
        if plan["cap"] != CAP_REACHED:
            plan["max"] = cur_max
            if plan["cap"] == CAP_NOT_CHECKED:
                plan["note"] = (f"Whether queries ever reach cluster {cur_max} of {cur_max} was not checked, "
                                "so no MAX_CLUSTER_COUNT change is prefilled.")
            elif plan["cap"] == CAP_NO_QUERIES:
                plan["note"] = ("The cluster-cap check found no query with a cluster number on this "
                                f"warehouse in the last {int(_num(row.get('CLUSTER_CHECK_DAYS')))} days, "
                                "so no MAX_CLUSTER_COUNT change is prefilled.")
            else:
                plan["note"] = (f"Queries never reached cluster {cur_max} of {cur_max} in the last "
                                f"{int(_num(row.get('CLUSTER_CHECK_DAYS')))} days, so no MAX_CLUSTER_COUNT "
                                "change is prefilled.")
            return plan
    plan["max"], plan["prefill"] = cur_max + 1, True
    if cur_max > 1:
        plan["note"] = (f"Raises MAX_CLUSTER_COUNT {cur_max} → {cur_max + 1}: queries reached cluster "
                        f"{cur_max} of {cur_max} in "
                        f"{_hour_count_txt(int(_num(row.get('AT_CAP_HOUR_COUNT'))))} of the last "
                        f"{int(_num(row.get('CLUSTER_CHECK_DAYS')))} days. MIN stays {plan['min']} so the "
                        "extra cluster runs only while queries queue. " + edition)
    else:
        plan["note"] = (f"Raises MAX_CLUSTER_COUNT {cur_max} → {cur_max + 1}; MIN stays {plan['min']} so the "
                        "extra cluster runs only while queries queue. " + edition)
    return plan


# ---------------------------------------------------------------------------
# Next-Fifty #38 remainder: the cluster-cap check (pure; the page supplies the histogram)
# ---------------------------------------------------------------------------

_CLUSTER_USE_COLUMNS = ["WAREHOUSE_NAME", "MIN_CLUSTER_COUNT", "MAX_CLUSTER_COUNT", "ACTIVE_HOUR_COUNT",
                        "PEAK_CLUSTERS", "P95_PEAK_CLUSTERS", "AT_CAP_HOUR_COUNT", "CLUSTER_CAP"]


def _hour_count_txt(n: int) -> str:
    return f"{n:,} hour" + ("" if n == 1 else "s")


def cluster_check_days(served_days: int, window_start: date | None, today: date) -> int:
    """The trailing window the cluster-cap check reads, in whole days before today. It is at least
    CLUSTER_CHECK_MIN_DAYS, so a month-end is inside. It reaches back to the sizing window's first day
    (served days on a trailing window, or today - bounds[0] on a calendar preset) — in full, because
    insights_sql.warehouse_cluster_use starts the read at MIDNIGHT (account time) that many days ago,
    not at now minus N days (review r1 R1-7). It is at most CLUSTER_CHECK_MAX_DAYS (the live
    QUERY_HISTORY clamp), so a longer window (Current year) is checked over its last 90 days only.
    Pure: today is passed in."""
    back = max(int(served_days or 0), (today - window_start).days if window_start is not None else 0)
    return max(CLUSTER_CHECK_MIN_DAYS, min(back, CLUSTER_CHECK_MAX_DAYS))


def cluster_check_targets(frame: pd.DataFrame | None) -> list[str]:
    """The multi-cluster warehouses of a sizing profile (after with_warehouse_settings): the upper-cased,
    stripped, de-duplicated, sorted WAREHOUSE_NAMEs whose SHOW MAX_CLUSTER_COUNT is above 1, capped at
    CLUSTER_CHECK_MAX_WAREHOUSES. [] when the frame is None/empty or lacks either column. Pure."""
    if (frame is None or frame.empty or "WAREHOUSE_NAME" not in frame.columns
            or "MAX_CLUSTER_COUNT" not in frame.columns):
        return []
    multi = pd.to_numeric(frame["MAX_CLUSTER_COUNT"], errors="coerce") > 1
    names = {str(n).strip().upper() for n in frame.loc[multi, "WAREHOUSE_NAME"] if str(n or "").strip()}
    return sorted(names)[:CLUSTER_CHECK_MAX_WAREHOUSES]


def cluster_range_coverage(frame: pd.DataFrame | None) -> tuple[int, int]:
    """(known, unknown): how many distinct warehouses of a sizing profile (after with_warehouse_settings)
    carry a SHOW MAX_CLUSTER_COUNT, and how many do not (review r1 R1-10). An empty SHOW WAREHOUSES result
    leaves no MAX_CLUSTER_COUNT column at all, and a SHOW that lists none of the profile's warehouses leaves
    it NaN on every row: both are (0, n) — the cluster ranges are UNKNOWN, which must never read as "no
    multi-cluster warehouse". (0, 0) for a None/empty frame or one without WAREHOUSE_NAME. Pure."""
    known, unknown = _cluster_range_split(frame)
    return len(known), len(unknown)


def _cluster_range_split(frame: pd.DataFrame | None) -> tuple[set[str], set[str]]:
    """(known, unknown) warehouse keys (upper-cased, stripped) of a sizing profile: with / without a SHOW
    MAX_CLUSTER_COUNT. One rule for cluster_range_coverage and cluster_range_unknown."""
    if frame is None or frame.empty or "WAREHOUSE_NAME" not in frame.columns:
        return set(), set()
    keys = frame["WAREHOUSE_NAME"].astype(str).str.strip().str.upper()
    if "MAX_CLUSTER_COUNT" not in frame.columns:
        return set(), set(keys)
    has = pd.to_numeric(frame["MAX_CLUSTER_COUNT"], errors="coerce").notna()
    known = set(keys[has])
    return known, set(keys) - known


# How many unlisted warehouses the partly-listed caption names before "and N more" (review r2 R2-3).
UNKNOWN_RANGE_NAMES_CAP = 10


def cluster_range_unknown(frame: pd.DataFrame | None) -> list[str]:
    """The profile warehouses with no SHOW MAX_CLUSTER_COUNT (cluster_range_coverage's ``unknown``, by name:
    upper-cased, stripped, sorted), so a partly-listed profile's caption names them (review r2 R2-3). Pure."""
    return sorted(_cluster_range_split(frame)[1])


def unknown_range_sentence(names: list[str]) -> str:
    """The partly-listed caption's clause (leading space) naming the profile warehouses SHOW WAREHOUSES did not
    list, capped at UNKNOWN_RANGE_NAMES_CAP with 'and N more'; '' when there are none. Pure."""
    if not names:
        return ""
    shown = list(names[:UNKNOWN_RANGE_NAMES_CAP])
    more = len(names) - len(shown)
    listed = ", ".join(shown) + (f" and {more:,} more" if more > 0 else "")
    return (f" {len(names):,} warehouse(s) in this profile are not in SHOW WAREHOUSES ({listed}), so their cluster "
            "range is unknown and they are not checked.")


def cluster_check_label(base: str, days: object) -> str:
    """A cluster-cap evidence column's header with its window (review r1 R1-9): "Hours at cap (last 35 days)"
    when the row carries CLUSTER_CHECK_DAYS, else ``base`` unchanged (the value is NaN there). The check
    window is not the sizing window, and the two sit side by side in the selected-row evidence. Pure."""
    d = _num(days)
    return f"{base} (last {int(d)} days)" if d == d and d > 0 else base


def unchecked_cap_note(n: int) -> str:
    """The VISIBLE disclosure for a page that shows add-a-cluster verdicts but never runs the cluster-cap
    check (Operations ▸ Warehouses ▸ Sizing & efficiency; review r1 R1-5): ``n`` = sizing_summary's
    cap_unchecked. '' when n <= 0. Pure."""
    n = int(n or 0)
    if n <= 0:
        return ""
    return (f"{n} \"{RECOMMEND_SCALE_OUT}\" row(s) above are on a multi-cluster warehouse whose cluster cap "
            "this page does not check: a higher MAX_CLUSTER_COUNT helps only if its queries reach the current "
            f"maximum. {CLUSTER_CAP_CHECK_PATH} checks it, and a warehouse that never reaches its cap reads "
            f"\"{RECOMMEND_BELOW_CAP}\" there.")


def cluster_use_summary(hist: pd.DataFrame | None, frame: pd.DataFrame, targets: list[str]) -> pd.DataFrame:
    """Judge each target warehouse's cluster use against its CURRENT SHOW MAX_CLUSTER_COUNT (from
    ``frame``, the sizing profile after with_warehouse_settings). ``hist`` is the
    insights_sql.warehouse_cluster_use histogram: (WAREHOUSE_NAME, PEAK_CLUSTER, HOUR_COUNT = clock
    hours whose highest CLUSTER_NUMBER was PEAK_CLUSTER). An hour is the hour a query STARTED in, so a long
    query counts only in its start hour and the hour counts are a floor (review r1 R1-8); the Reached / Not
    reached answer is unaffected (every clustered query has a start hour inside the window).

    One row per target: ACTIVE_HOUR_COUNT (hours in which a clustered query started), PEAK_CLUSTERS (the
    highest cluster any query ran on), P95_PEAK_CLUSTERS (nearest-rank p95 of the hourly peaks),
    AT_CAP_HOUR_COUNT (hours whose peak reached, or passed, the current maximum — ">=" so a since-lowered
    cap still counts) and CLUSTER_CAP (Reached / Not reached / No queries). An EMPTY frame (these
    columns) when ``hist`` is None or has rows without the expected columns: the page reads that as
    not checked. A zero-row ``hist`` is a valid answer: every target reads No queries. Sorted by hours
    at cap, then peak (unknown last), then name. Pure."""
    empty = pd.DataFrame(columns=_CLUSTER_USE_COLUMNS)
    need = {"WAREHOUSE_NAME", "PEAK_CLUSTER", "HOUR_COUNT"}
    if hist is None or (not hist.empty and not need.issubset(hist.columns)):
        return empty
    if hist.empty:
        h_all = pd.DataFrame({"KEY": pd.Series(dtype=object), "PEAK": pd.Series(dtype=float),
                              "HOURS": pd.Series(dtype=float)})
    else:
        h_all = pd.DataFrame({"KEY": hist["WAREHOUSE_NAME"].astype(str).str.strip().str.upper(),
                              "PEAK": pd.to_numeric(hist["PEAK_CLUSTER"], errors="coerce"),
                              "HOURS": pd.to_numeric(hist["HOUR_COUNT"], errors="coerce")})
        h_all = h_all[h_all["PEAK"].notna() & (h_all["HOURS"] > 0)]
    first: dict[str, pd.Series] = {}
    if frame is not None and not frame.empty and "WAREHOUSE_NAME" in frame.columns:
        for pos, key in enumerate(frame["WAREHOUSE_NAME"].astype(str).str.strip().str.upper()):
            first.setdefault(key, frame.iloc[pos])
    nan = float("nan")
    rows = []
    for target in targets:
        key = str(target).strip().upper()
        src = first.get(key)
        name = str(src["WAREHOUSE_NAME"]).strip() if src is not None else key
        mn = _num(src.get("MIN_CLUSTER_COUNT")) if src is not None else nan
        mx = _num(src.get("MAX_CLUSTER_COUNT")) if src is not None else nan
        h = h_all[h_all["KEY"] == key].sort_values("PEAK", kind="stable")
        active = int(h["HOURS"].sum()) if not h.empty else 0
        peak = p95 = nan
        at_cap, label = 0, "No queries"
        if active > 0:
            peak = float(h["PEAK"].max())
            p95 = float(h.loc[h["HOURS"].cumsum() >= 0.95 * active, "PEAK"].iloc[0])
            at_cap = int(h.loc[h["PEAK"] >= mx, "HOURS"].sum()) if mx == mx else 0
            label = "Reached" if at_cap > 0 else "Not reached"
        rows.append({"WAREHOUSE_NAME": name, "MIN_CLUSTER_COUNT": mn, "MAX_CLUSTER_COUNT": mx,
                     "ACTIVE_HOUR_COUNT": active, "PEAK_CLUSTERS": peak, "P95_PEAK_CLUSTERS": p95,
                     "AT_CAP_HOUR_COUNT": at_cap, "CLUSTER_CAP": label})
    if not rows:
        return empty
    out = pd.DataFrame(rows, columns=_CLUSTER_USE_COLUMNS)
    out["_KEY"] = out["WAREHOUSE_NAME"].astype(str).str.upper()
    return (out.sort_values(["AT_CAP_HOUR_COUNT", "PEAK_CLUSTERS", "_KEY"],
                            ascending=[False, False, True], na_position="last", kind="stable")
            .drop(columns="_KEY").reset_index(drop=True))


def with_cluster_use(frame: pd.DataFrame, summary: pd.DataFrame, days: int) -> pd.DataFrame:
    """Carry the cluster-cap check onto the sizing profile BEFORE size_recommendations (the verdicts read
    it). Case-insensitive on the warehouse name. Matched rows get CLUSTER_CHECK_DAYS, ACTIVE_HOUR_COUNT,
    PEAK_CLUSTERS, P95_PEAK_CLUSTERS and AT_CAP_HOUR_COUNT; every other row gets NaN (= not checked).
    An empty summary returns the frame unchanged. Works on a copy. Pure."""
    if (frame is None or frame.empty or summary is None or summary.empty
            or "WAREHOUSE_NAME" not in frame.columns or "WAREHOUSE_NAME" not in summary.columns):
        return frame
    out = frame.copy()
    keys = out["WAREHOUSE_NAME"].astype(str).str.strip().str.upper()
    s_keys = summary["WAREHOUSE_NAME"].astype(str).str.strip().str.upper()
    matched = keys.isin(set(s_keys))
    out["CLUSTER_CHECK_DAYS"] = [float(int(days)) if m else float("nan") for m in matched]
    for col in ("ACTIVE_HOUR_COUNT", "PEAK_CLUSTERS", "P95_PEAK_CLUSTERS", "AT_CAP_HOUR_COUNT"):
        vals = (pd.to_numeric(summary[col], errors="coerce") if col in summary.columns
                else pd.Series(float("nan"), index=summary.index))
        mapping = dict(zip(s_keys, vals, strict=False))
        out[col] = pd.to_numeric(keys.map(mapping), errors="coerce").astype(float)
    return out


def cluster_cap_state(row) -> str:
    """CAP_NOT_CHECKED (no CLUSTER_CHECK_DAYS on the row), CAP_NO_QUERIES (checked, no clustered query),
    else CAP_REACHED when AT_CAP_HOUR_COUNT > 0 and CAP_NOT_REACHED otherwise. Pure."""
    d = _num(row.get("CLUSTER_CHECK_DAYS"))
    if d != d:
        return CAP_NOT_CHECKED
    if not (_num(row.get("ACTIVE_HOUR_COUNT")) > 0):
        return CAP_NO_QUERIES
    return CAP_REACHED if _num(row.get("AT_CAP_HOUR_COUNT")) > 0 else CAP_NOT_REACHED


def sizing_summary(out: pd.DataFrame) -> dict:
    """Counts plus the savings numbers, kept apart on purpose (D2):
    ``potential_saving_usd`` is the CONSERVATIVE size-down floor (rec #13: only
    the idle portion reliably shrinks when the rate halves), with
    ``potential_saving_high_usd`` the optimistic bound where busy credits halve
    too; ``idle_saving_usd`` is the measured idle spend on the rows that need an
    auto-suspend fix. Adding potential to idle would mix a model with a
    measurement, and the size-down floor stays honest about SLA risk.
    """
    if out is None or out.empty:
        return {"up": 0, "scale_out": 0, "size_up": 0, "below_cap": 0, "cap_unchecked": 0, "down": 0,
                "suspend": 0, "review": 0, "observe": 0,
                "potential_saving_usd": 0.0, "potential_saving_high_usd": 0.0,
                "idle_saving_usd": 0.0}
    rec = out["RECOMMENDATION"]
    # #38 remainder: add-a-cluster rows on a multi-cluster non-ECONOMY warehouse below the generator cap
    # whose cluster cap was NOT checked — exactly the rows whose rationale says "was not checked".
    cap_unchecked = 0
    if "MAX_CLUSTER_COUNT" in out.columns:
        mx = pd.to_numeric(out["MAX_CLUSTER_COUNT"], errors="coerce")
        cand = (rec == RECOMMEND_SCALE_OUT) & (mx > 1) & (mx < CLUSTER_RANGE_CAP)
        if "SCALING_POLICY" in out.columns:
            cand &= out["SCALING_POLICY"].map(_policy) != "ECONOMY"
        cap_unchecked = sum(cluster_cap_state(r) == CAP_NOT_CHECKED for _, r in out[cand].iterrows())
    idle_usd = 0.0
    if "IDLE_MONTHLY_USD" in out.columns:
        idle_usd = round(float(out.loc[rec == RECOMMEND_SUSPEND, "IDLE_MONTHLY_USD"].sum()), 0)
    high_usd = 0.0
    if "SAVING_HIGH_USD" in out.columns:
        high_usd = round(float(out.loc[rec == RECOMMEND_DOWN, "SAVING_HIGH_USD"].sum()), 0)
    return {
        "up": int(rec.isin(UP_VERDICTS).sum()),          # back-compat: every capacity-pressure row
        "scale_out": int((rec == RECOMMEND_SCALE_OUT).sum()),
        "size_up": int((rec == RECOMMEND_SIZE_UP).sum()),
        "below_cap": int((rec == RECOMMEND_BELOW_CAP).sum()),
        "cap_unchecked": int(cap_unchecked),
        "down": int((rec == RECOMMEND_DOWN).sum()),
        "suspend": int((rec == RECOMMEND_SUSPEND).sum()),
        "review": int((rec == RECOMMEND_CADENCE).sum()),
        "observe": int((rec == RECOMMEND_OBSERVE).sum()),
        "potential_saving_usd": round(float(out["POTENTIAL_MONTHLY_SAVING_USD"].sum()), 0),
        "potential_saving_high_usd": high_usd,
        "idle_saving_usd": idle_usd,
    }


# ---------------------------------------------------------------------------
# Interactive what-if simulator (pure; the UI supplies observed inputs)
# ---------------------------------------------------------------------------

# r34: Snowflake's warehouse ladder runs XS..6X-Large. The simulator stopped at 4X-Large,
# so normalize_size('5X-Large'/'6X-Large') fell through to '' (simulate refused these highest-
# rate warehouses) and shifted_size('4X-Large', +1) clamped to itself (a real upsize reported
# as 'unchanged'). Extend the ladder + aliases through 6X so the two priciest sizes simulate.
SIZE_ORDER = ("XSMALL", "SMALL", "MEDIUM", "LARGE", "XLARGE",
              "2XLARGE", "3XLARGE", "4XLARGE", "5XLARGE", "6XLARGE")
_SIZE_ALIASES = {"X-SMALL": "XSMALL", "XS": "XSMALL", "S": "SMALL", "M": "MEDIUM",
                 "L": "LARGE", "X-LARGE": "XLARGE", "XL": "XLARGE",
                 "2X-LARGE": "2XLARGE", "3X-LARGE": "3XLARGE", "4X-LARGE": "4XLARGE",
                 "5X-LARGE": "5XLARGE", "6X-LARGE": "6XLARGE", "5X": "5XLARGE", "6X": "6XLARGE",
                 # the Resize picker's spelling (remediation.RESIZE_SIZES); Snowflake accepts it for 2X-Large
                 "XXLARGE": "2XLARGE"}


def normalize_size(size: object) -> str:
    try:
        if pd.isna(size):          # None / NaN / pd.NA -> unknown (`pd.NA or ""` would raise)
            return ""
    except (TypeError, ValueError):   # list-likes etc. — fall through to the string path
        pass
    text = str(size or "").strip().upper().replace("_", "-")
    text = _SIZE_ALIASES.get(text, text.replace("-", ""))
    return text if text in SIZE_ORDER else ""


def shifted_size(size: str, delta: int) -> str:
    """Size N steps up/down the ladder, clamped at the ends ('' if unknown)."""
    current = normalize_size(size)
    if not current:
        return ""
    idx = max(0, min(SIZE_ORDER.index(current) + int(delta), len(SIZE_ORDER) - 1))
    return SIZE_ORDER[idx]


def picker_size_label(size: object, options: tuple[str, ...] | list[str]) -> str:
    """A warehouse size as the "Resize to" picker spells it (review r2 R2-2): the offered option whose ladder size
    matches (2X-Large -> XXLARGE, the option), else the ladder name (3XLARGE), '' if unknown. Pure."""
    cur = normalize_size(size)
    for opt in options:
        if cur and normalize_size(opt) == cur:
            return str(opt)
    return cur


# Why the picker opens with nothing picked (review r2 R2-2), after the reason.
RESIZE_PICK_PROMPT = " The picker opens with no size picked: pick one to see the statement."


def resize_picker_default(recommendation: object, current_size: object,
                          options: tuple[str, ...] | list[str]) -> tuple[int | None, str]:
    """(index, note) for the Cost ▸ Idle & sizing "Resize to" picker (review r1 R1-4, r2 R2-2). Pure.

    A capacity-pressure verdict (UP_VERDICTS: add a cluster, size up, size up or split) opens on ONE SIZE UP
    from the current size where the picker offers it, since the pane under such a verdict calls the resize
    "the size-up route". Where it does not — the current size is the largest option (its next size is not
    offered), larger than every option (3XLARGE and up: every option is a downsize), or unknown — ``index``
    is None: the picker opens with NO size picked, so the pane shows no statement, no saving and no Execute
    until the operator picks one, and ``note`` says why (sizes spelled as the picker spells them). Any other
    verdict keeps the first option with no note (the v4.603 default)."""
    opts = [normalize_size(o) for o in options]
    if not opts or str(recommendation or "") not in UP_VERDICTS:
        return 0, ""
    cur = normalize_size(current_size)
    if not cur:
        return None, ("The current size of this warehouse is unknown (SHOW WAREHOUSES did not return it), so one "
                      "size up cannot be offered." + RESIZE_PICK_PROMPT)
    up = shifted_size(cur, 1)
    if up != cur and up in opts:
        return opts.index(up), ""
    label = picker_size_label(cur, options)
    if cur in opts:
        return None, (f"The next size up from {label} is not offered here, so every option is this size (no "
                      "change) or a downsize — none is the size-up route." + RESIZE_PICK_PROMPT)
    return None, (f"This warehouse ({label}) is larger than every size offered here, so every option is a "
                  "downsize — none is the size-up route." + RESIZE_PICK_PROMPT)


def simulate_scenario(
    *,
    size: str,
    credits_window: float,
    idle_credits_window: float,
    window_days: int,
    rate_usd: float,
    size_delta: int = 0,
    autosuspend_now_s: int = 600,
    autosuspend_new_s: int = 60,
) -> dict:
    """What-if for one warehouse: size step and/or auto-suspend change.

    Transparent replay of the observed window, not a promise:
    - Busy credits scale between two stated bounds. Sizing up (rate x2 per
      step): worst case queries run the SAME wall time (cost x2), best case
      they halve (cost-neutral). Sizing down mirrors that.
    - Idle credits scale with the new rate AND shrink/grow with the
      auto-suspend ratio (capped at 2x — longer suspends can't burn more
      than always-on).
    Returns monthly dollars: {ok, size_now, size_new, monthly_now_usd,
    monthly_low_usd, monthly_high_usd, assumptions: [...]}
    """
    from .formulas import safe_float as _sf

    size_now = normalize_size(size)
    size_new = shifted_size(size_now, size_delta)
    if not size_now:
        return {"ok": False, "reason": f"Unknown warehouse size {size!r}."}
    days = max(int(window_days or 1), 1)
    rate = _sf(rate_usd, 3.68)
    total = max(0.0, _sf(credits_window))
    idle = min(max(0.0, _sf(idle_credits_window)), total)
    busy = total - idle
    # Effective applied delta after clamping at the ladder ends.
    applied_delta = SIZE_ORDER.index(size_new) - SIZE_ORDER.index(size_now)
    factor = 2.0 ** applied_delta
    busy_bounds = sorted((busy * factor, busy * 1.0))
    # auto_suspend == 0 means NEVER suspend (an effectively unbounded idle window), NOT a
    # 0-second suspend. Modeling 0 literally pinned the ratio to the 2.0 cap, so turning
    # auto-suspend ON for a never-suspend warehouse (a real saving) read as a cost INCREASE
    # (round-4 regression from the round-3 fix that preserved a real 0). Map <=0 to a large
    # sentinel so new/now behaves: now=never -> ratio ~ 0 (idle collapses); new=never -> cap.
    _NEVER_S = 30 * 86400.0
    def _susp(v: object, default: float) -> float:
        s = _sf(v, default)
        return _NEVER_S if s <= 0 else s
    def _susp_label(v: object) -> str:
        return "never" if _sf(v, 0) <= 0 else f"{int(_sf(v, 0))}s"
    _now_s, _new_s = _susp(autosuspend_now_s, 600), _susp(autosuspend_new_s, 60)
    suspend_ratio = min(_new_s / _now_s, 2.0)
    idle_new = idle * factor * suspend_ratio
    to_month = 30.0 / days

    def _usd(credits: float) -> float:
        return round(credits * to_month * rate, 0)

    low = _usd(busy_bounds[0] + idle_new)
    high = _usd(busy_bounds[1] + idle_new)
    assumptions = [
        f"Observed window: {days}d, {total:,.1f} credits ({idle:,.1f} idle).",
        (f"Size {size_now} -> {size_new}: busy credits bounded between rate-scaled "
         f"(x{factor:g}) and cost-neutral (perfect runtime scaling)."
         if applied_delta else "Size unchanged: busy credits unchanged."),
        (f"Auto-suspend {_susp_label(autosuspend_now_s)} -> {_susp_label(autosuspend_new_s)}: idle "
         f"credits scaled x{suspend_ratio:.2f} (linear with suspend window, capped at 2x)."),
        "Idle burns at the NEW size's rate. Concurrency, caching, and queueing shifts are not modeled.",
    ]
    return {
        "ok": True,
        "size_now": size_now,
        "size_new": size_new,
        "monthly_now_usd": _usd(total),
        "monthly_low_usd": min(low, high),
        "monthly_high_usd": max(low, high),
        "assumptions": assumptions,
    }


def price_per_run_bounds(allocated_credits: float, runs: int, rate_usd: float,
                         size_delta: int = 0) -> dict:
    """$/run for a query pattern, now and at a size step, as honest bounds.

    Same assumption pair as simulate_scenario: a size step multiplies the
    rate by 2^delta; runtime lands between unchanged (rate-scaled cost) and
    perfectly scaled (cost-neutral).
    """
    from .formulas import safe_float as _sf

    runs_n = max(int(runs or 0), 1)
    per_run_now = _sf(allocated_credits) / runs_n * _sf(rate_usd, 3.68)
    factor = 2.0 ** int(size_delta)
    bounds = sorted((per_run_now * factor, per_run_now))
    return {
        "per_run_now_usd": round(per_run_now, 4),
        "per_run_low_usd": round(bounds[0], 4),
        "per_run_high_usd": round(bounds[1], 4),
    }
