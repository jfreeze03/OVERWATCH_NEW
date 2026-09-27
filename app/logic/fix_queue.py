"""Operations > Optimize fix queue: per-family diagnosis and the Track write builder. Pure module.

Runs on top of ``decision.prioritize_workloads`` output (lanes, IMPACT_USD_30D, CONFIDENCE) and
never re-ranks it. For each measured query family it names ONE diagnosis and a first fix, from
the best evidence available, in this precedence:

1. **live**: the Operations > Queries optimization profile (``query_opt.score_opportunities``
   over a live QUERY_HISTORY scan) found an actionable pathology for the same fingerprint.
2. **mart**: ``query_advisor.advise`` over the family mart's per-run averages. The marts carry
   what ``cold_scan``, ``compile_bound``, ``metadata_chatter`` and ``sleep_polling`` need; spill,
   pruning, queueing and rows returned are invisible to them.
3. **heuristic**: the portfolio's own NEXT_MOVE when it is specific (cache or materialize,
   stabilize failures, reduce recurrence), with a canned first fix.
4. **none**: nothing specific; the first fix names what the marts cannot see and points at the
   live toggle.

IMPACT_USD_30D is OBSERVED cost, never a savings figure. The only priced diagnosis is
"Stabilize failures" (the failed-run share of observed cost); everything else tracks unpriced.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping

import pandas as pd

from app.config import core_object
from app.core.sqlsafe import sql_literal, sql_number
from app.logic import query_advisor, query_opt
from app.logic.formulas import format_usd, humanize_duration, humanize_gb, safe_float

TRACK_ALL_CAP = 25            # rows one "Track all ACT NOW" click may insert
TRACK_COOLDOWN_DAYS = 90      # a family dismissed (DROPPED) this recently is never bulk re-tracked
TRACK_SOURCE = "Operations > Optimize"
TRACK_ENTITY_TYPE = "QUERY_FINGERPRINT"
SPECIFIC_SOURCES = frozenset({"live", "mart", "heuristic"})
_OPEN_STATUSES = frozenset({"OPEN", "IN_PROGRESS"})

# The portfolio's specific NEXT_MOVE values and the first fix each one implies.
_HEURISTIC_FIX = {
    "Cache or materialize": (
        "This family runs often and rarely reads warm: materialize the hot subset (a table, dynamic "
        "table or materialized view refreshed on the load cadence) or cache the result, so repeat "
        "runs stop recomputing it."),
    "Stabilize failures": (
        "A share of its runs fail and bill compute before they fail: open the failures in "
        "Operations > Queries (error family and first failing statement) and fix the cause, or add "
        "a pre-check so a doomed run stops early."),
    "Reduce recurrence": (
        "Each run is fast, so the cost is the run count: cut the cadence (poll less often, batch "
        "the calls, or cache the answer) rather than tuning the SQL."),
}
_MART_BLIND = "spill, partition pruning, queueing and rows returned"


def _is_true(value: object) -> bool:
    """A flag cell from Snowflake (bool), pandas (numpy bool / float) or text, read as a bool."""
    if isinstance(value, str):
        return value.strip().upper() in ("TRUE", "T", "Y", "YES", "1")
    number = safe_float(value, default=float("nan"))
    return number == number and number != 0.0


def is_own_traffic(value: object) -> bool:
    """The OW_SELF flag (OVERWATCH's own loader/mart statement) read from any cell shape."""
    return _is_true(value)


def own_traffic(df: pd.DataFrame | None) -> pd.Series:
    """Boolean Series: which rows are OVERWATCH's own traffic (all-False without OW_SELF)."""
    if df is None:
        return pd.Series(dtype=bool)
    if "OW_SELF" not in df.columns:
        return pd.Series(False, index=df.index, dtype=bool)
    return df["OW_SELF"].map(_is_true).astype(bool)


def _present(value: object) -> bool:
    """True when a numeric cell carries a real measurement (not None/NaN/garbage)."""
    number = safe_float(value, default=float("nan"))
    return number == number


def _text(value: object) -> str:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return ""
    return str(value).strip()


def advisor_input(row: Mapping[str, object]) -> dict[str, object]:
    """Map one fix-queue row (family-mart totals) onto ``query_advisor.advise``'s per-run inputs.

    Per run: ELAPSED_SEC = TOTAL_ELAPSED_SEC / FAMILY_RUNS, EXECUTION_SEC = TOTAL_EXEC_SEC /
    FAMILY_RUNS, COMPILE_SEC = TOTAL_COMPILE_SEC / FAMILY_RUNS; GB_SCANNED is already per run;
    CACHE_PCT = AVG_CACHE_PCT; SAMPLE_TEXT = QUERY_PREVIEW; plus the two typical-run proxies.
    A missing (NaN) or zero FAMILY_RUNS leaves the per-run keys out, so advise falls back to its
    own defaults (elapsed 0, execution -1) and no timing finding can fire off a divide-by-zero."""
    preview = row.get("QUERY_PREVIEW")
    out: dict[str, object] = {
        "SAMPLE_TEXT": preview if isinstance(preview, str) else "",
        "QUERY_TYPE": "",
        "GB_SCANNED": row.get("GB_SCANNED"),
        "CACHE_PCT": row.get("AVG_CACHE_PCT"),
        "COMPILE_RUN_PCT": row.get("COMPILE_RUN_PCT"),
        "COMPILE_DOMINANT_RUN_PCT": row.get("COMPILE_DOMINANT_RUN_PCT"),
    }
    runs = safe_float(row.get("FAMILY_RUNS"))
    if runs > 0:
        for src, dst in (("TOTAL_ELAPSED_SEC", "ELAPSED_SEC"), ("TOTAL_EXEC_SEC", "EXECUTION_SEC"),
                         ("TOTAL_COMPILE_SEC", "COMPILE_SEC")):
            if _present(row.get(src)):
                out[dst] = safe_float(row.get(src)) / runs
    return out


def evidence_sentence(row: Mapping[str, object]) -> str:
    """The "Why" line: the measured behaviour behind a family, durations humanized. Only the
    signals actually present are named; a blind family says so instead of implying zeros."""
    bits: list[str] = []
    if _present(row.get("P95_SEC")):
        bits.append(f"P95 {humanize_duration(safe_float(row.get('P95_SEC')), 's')}")
    if _present(row.get("AVG_CACHE_PCT")):
        bits.append(f"{safe_float(row.get('AVG_CACHE_PCT')):.0f}% cache")
    runs = safe_float(row.get("FAMILY_RUNS"))
    if _present(row.get("GB_SCANNED")) and runs > 0:
        bits.append(f"{humanize_gb(safe_float(row.get('GB_SCANNED')))}/run")
    if _present(row.get("TOTAL_COMPILE_SEC")) and runs > 0:
        bits.append(f"compile {humanize_duration(safe_float(row.get('TOTAL_COMPILE_SEC')) / runs, 's')}")
    if _present(row.get("WAREHOUSES")):
        n_wh = int(safe_float(row.get("WAREHOUSES")))
        bits.append(f"{n_wh} warehouse{'' if n_wh == 1 else 's'}")
    if _present(row.get("FAILS")):
        _fail = safe_float(row.get("FAIL_PCT"))
        bits.append(f"{_fail:.1f}% fail" if _fail else "0% fail")
    text = " · ".join(bits) if bits else "No behaviour evidence in the family mart"
    # OVERWATCH's own loader/mart statements stay visible but carry a tag (never bulk-tracked).
    return f"OVERWATCH's own traffic · {text}" if is_own_traffic(row.get("OW_SELF")) else text


def _live_index(live_scored: pd.DataFrame | None, live_breakdowns: Mapping | None) -> dict:
    """FINGERPRINT (upper) -> (pathology, confidence 0-1, first fix, breakdown) for every live
    row that found something actionable. A live row whose pathology is "None" ran cleanly on its
    typical successful run, so it names nothing and the family falls through to the mart."""
    out: dict[str, tuple] = {}
    if live_scored is None or live_scored.empty or "FINGERPRINT" not in live_scored.columns:
        return out
    breakdowns = live_breakdowns or {}
    for rec in live_scored.to_dict("records"):
        fp = _text(rec.get("FINGERPRINT"))
        pathology = _text(rec.get("PATHOLOGY"))
        if not fp or pathology in ("", "None"):
            continue
        bd = list(breakdowns.get(fp) or [])
        first_fix = _text(bd[0][2]) if bd and len(bd[0]) >= 3 else _text(rec.get("FIRST_ACTION"))
        conf = max(0.0, min(safe_float(rec.get("CONFIDENCE")) / 100.0, 1.0))
        out.setdefault(fp.upper(), (pathology, round(conf, 2), first_fix, bd))
    return out


def diagnose_workloads(portfolio: pd.DataFrame | None, *, live_scored: pd.DataFrame | None = None,
                       live_breakdowns: Mapping | None = None) -> pd.DataFrame:
    """Add DIAGNOSIS, FIRST_FIX, DIAG_SOURCE, DIAG_CONFIDENCE, BREAKDOWN, SPECIFIC, EVIDENCE and
    PRICED_USD_MO to ``prioritize_workloads`` output, keeping its row order (no re-rank).

    DIAG_CONFIDENCE is 0-1: the live profile's confidence / 100 for a live diagnosis, else the
    portfolio's evidence score (CONFIDENCE). PRICED_USD_MO = IMPACT_USD_30D x FAIL_PCT / 100 for
    "Stabilize failures" only (the failed-run share of observed cost), NaN otherwise."""
    cols = ["DIAGNOSIS", "FIRST_FIX", "DIAG_SOURCE", "DIAG_CONFIDENCE", "BREAKDOWN", "SPECIFIC",
            "EVIDENCE", "PRICED_USD_MO"]
    if portfolio is None:
        return pd.DataFrame(columns=cols)
    out = portfolio.copy().reset_index(drop=True)
    if out.empty:
        for col in cols:
            out[col] = pd.Series(dtype=object)
        return out
    live = _live_index(live_scored, live_breakdowns)
    live_ran = live_scored is not None
    rows: dict[str, list] = {col: [] for col in cols}
    for rec in out.to_dict("records"):
        fp = _text(rec.get("FINGERPRINT"))
        evidence_conf = max(0.0, min(safe_float(rec.get("CONFIDENCE")), 1.0))
        next_move = _text(rec.get("NEXT_MOVE"))
        hit = live.get(fp.upper()) if fp else None
        breakdown: list = []
        if hit is not None:
            diagnosis, conf, first_fix, breakdown = hit
            source = "live"
        else:
            findings, _score = query_advisor.advise(
                advisor_input(rec), remote_spill_floor_gb=query_opt._FINGERPRINT_REMOTE_SPILL_FLOOR_GB)
            conf = evidence_conf
            if findings:
                lead = findings[0]
                diagnosis, first_fix, source = query_opt.pathology_label(lead.code), lead.detail, "mart"
                breakdown = [(f.title, f.points, f.detail) for f in findings]
            elif next_move in _HEURISTIC_FIX:
                diagnosis, first_fix, source = next_move, _HEURISTIC_FIX[next_move], "heuristic"
            else:
                source = "none"
                if next_move == "Validate evidence":
                    diagnosis = "Validate evidence"
                    first_fix = ("The family mart has no cache, latency or failure evidence for this "
                                 "family (a join miss, or below the mart's per-day top-2000 cut). ")
                else:
                    diagnosis = "Profile it"
                    first_fix = "Nothing specific shows in the daily marts. "
                if live_ran:
                    first_fix += ("The live profile found no actionable inefficiency in a typical "
                                  "successful run either; open its Entity 360 and read a slow run's "
                                  "query profile.")
                else:
                    first_fix += (f"The marts cannot see {_MART_BLIND}: turn on the live query "
                                  "profile above to diagnose it.")
        # FAIL_PCT = family-mart FAILS over pattern-mart RUNS (two populations), so it can read past
        # 100% on a thin join; the failed-run share of observed cost can never exceed that cost.
        fail_pct = max(0.0, min(safe_float(rec.get("FAIL_PCT")), 100.0))
        impact = safe_float(rec.get("IMPACT_USD_30D"))
        priced = (round(impact * fail_pct / 100.0, 2)
                  if diagnosis == "Stabilize failures" and impact > 0 and fail_pct > 0 else math.nan)
        rows["DIAGNOSIS"].append(diagnosis)
        rows["FIRST_FIX"].append(first_fix)
        rows["DIAG_SOURCE"].append(source)
        rows["DIAG_CONFIDENCE"].append(round(float(conf), 2))
        rows["BREAKDOWN"].append(breakdown)
        rows["SPECIFIC"].append(source in SPECIFIC_SOURCES)
        rows["EVIDENCE"].append(evidence_sentence(rec))
        rows["PRICED_USD_MO"].append(priced)
    for col in cols:
        out[col] = rows[col]
    return out


def _tracked_sets(tracked: pd.DataFrame | None) -> tuple[dict[str, str], set[str], set[str]]:
    """From ``workbench_sql.tracked_actions`` rows: (latest status by key, open keys, keys
    dismissed within the read's cooldown window). Keys are upper-cased."""
    latest: dict[str, str] = {}
    open_keys: set[str] = set()
    dropped: set[str] = set()
    if tracked is None or tracked.empty or "ENTITY_KEY_U" not in tracked.columns:
        return latest, open_keys, dropped
    for rec in tracked.to_dict("records"):
        key = _text(rec.get("ENTITY_KEY_U")).upper()
        if not key:
            continue
        status = _text(rec.get("ACTION_STATUS")).upper()
        latest[key] = status
        if safe_float(rec.get("OPEN_N")) > 0 or status in _OPEN_STATUSES:
            open_keys.add(key)
        if safe_float(rec.get("DROPPED_N")) > 0 or status == "DROPPED":
            dropped.add(key)
    return latest, open_keys, dropped


def with_track_status(df: pd.DataFrame, tracked: pd.DataFrame | None) -> pd.DataFrame:
    """Add TRACK_STATUS (Tracked (open) / Dismissed / Done / Untracked) and TRACKED_ACTION_ID
    (the latest Action Center item for the family, '' when untracked)."""
    out = df.copy()
    latest, open_keys, dropped = _tracked_sets(tracked)
    ids: dict[str, str] = {}
    if tracked is not None and not tracked.empty and "ENTITY_KEY_U" in tracked.columns:
        for rec in tracked.to_dict("records"):
            ids[_text(rec.get("ENTITY_KEY_U")).upper()] = _text(rec.get("LATEST_ACTION_ID"))
    statuses: list[str] = []
    action_ids: list[str] = []
    keys = out["FINGERPRINT"] if "FINGERPRINT" in out.columns else pd.Series([""] * len(out))
    for fp in keys:
        key = _text(fp).upper()
        if key in open_keys:
            statuses.append("Tracked (open)")
        elif key in dropped:
            statuses.append("Dismissed")
        elif latest.get(key) == "DONE":
            statuses.append("Done")
        else:
            statuses.append("Untracked")
        action_ids.append(ids.get(key, "") if key in latest else "")
    out["TRACK_STATUS"] = statuses
    out["TRACKED_ACTION_ID"] = action_ids
    return out


def track_all_eligible(df: pd.DataFrame | None, tracked: pd.DataFrame | None) -> pd.DataFrame:
    """The rows one "Track all ACT NOW" click takes: ACT NOW lane AND a specific diagnosis AND
    not OVERWATCH's own traffic AND not open-tracked AND not dismissed within the cooldown,
    in PRIORITY_SCORE order, capped at TRACK_ALL_CAP."""
    if df is None or df.empty or not {"FINGERPRINT", "LANE", "SPECIFIC"}.issubset(df.columns):
        return pd.DataFrame(columns=list(df.columns) if df is not None else [])
    _latest, open_keys, dropped = _tracked_sets(tracked)
    keys = df["FINGERPRINT"].map(lambda v: _text(v).upper())
    own = own_traffic(df)
    mask = (df["LANE"].astype(str).eq("ACT NOW")
            & df["SPECIFIC"].map(_is_true)
            & ~own
            & keys.ne("")
            & ~keys.isin(open_keys)
            & ~keys.isin(dropped))
    picked = df[mask]
    if "PRIORITY_SCORE" in picked.columns:
        picked = picked.assign(_P=pd.to_numeric(picked["PRIORITY_SCORE"], errors="coerce").fillna(0.0))
        picked = picked.sort_values("_P", ascending=False, kind="stable").drop(columns="_P")
    return picked.head(TRACK_ALL_CAP).reset_index(drop=True)


def track_items(rows: pd.DataFrame | Iterable[Mapping[str, object]], company: str) -> list[dict]:
    """One ACTION_QUEUE item per family, deduped on the (upper-cased) fingerprint.

    COMPANY = the selected company, or the family's dominant TOP_COMPANY under ALL (else 'ALL').
    SEVERITY = MEDIUM for ACT NOW, else LOW: never HIGH/CRITICAL, which feed the Overview score.
    CONFIDENCE = DIAG_CONFIDENCE (0-1). ESTIMATED_USD/PERIOD = (PRICED_USD_MO, 'MONTHLY') only when
    the diagnosis is priced; observed cost (IMPACT_USD_30D) is never written as an estimate."""
    records = rows.to_dict("records") if isinstance(rows, pd.DataFrame) else list(rows)
    scope = _text(company).upper()
    items: list[dict] = []
    seen: set[str] = set()
    for rec in records:
        fp = _text(rec.get("FINGERPRINT"))
        if not fp or fp.upper() in seen:
            continue
        seen.add(fp.upper())
        top_company = _text(rec.get("TOP_COMPANY"))
        item_company = _text(company) if scope and scope != "ALL" else (top_company or "ALL")
        diagnosis = _text(rec.get("DIAGNOSIS")) or "Profile it"
        database = _text(rec.get("TOP_DATABASE"))
        title = f"{diagnosis}: {fp[:12]}…" + (f" ({database})" if database else "")
        priced = safe_float(rec.get("PRICED_USD_MO"), default=float("nan"))
        is_priced = priced == priced and priced > 0
        tail = (f" Evidence: {_text(rec.get('EVIDENCE'))}. Observed cost "
                f"{format_usd(safe_float(rec.get('IMPACT_USD_30D')))}/mo (measured)"
                + (f"; priced at the failed-run share, {format_usd(priced)}/mo" if is_priced else "")
                + "; confidence is OVERWATCH's evidence score, not an authored belief.")
        head = f"First fix: {_text(rec.get('FIRST_FIX'))}"
        detail = head[:max(0, 1000 - len(tail))] + tail
        conf = safe_float(rec.get("DIAG_CONFIDENCE"), default=float("nan"))
        items.append({
            "COMPANY": item_company[:40],
            "SEVERITY": "MEDIUM" if _text(rec.get("LANE")) == "ACT NOW" else "LOW",
            "TITLE": title[:300],
            "DETAIL": detail[:1000],
            "ENTITY_KEY": fp[:500],
            "CONFIDENCE": (max(0.0, min(conf, 1.0)) if conf == conf else None),
            "ESTIMATED_USD": (round(priced, 2) if is_priced else None),
            "PERIOD": ("MONTHLY" if is_priced else ""),
        })
    return items


def track_fingerprints_sql(items: list[dict], *, actor_sql: str, bulk: bool,
                           cooldown_days: int = TRACK_COOLDOWN_DAYS) -> str:
    """ONE idempotent INSERT for the given track items ('' when there are none).

    Keyed on the ENTITY (SOURCE_ENTITY_TYPE + upper-cased SOURCE_ENTITY_KEY) plus open status,
    never the title or company, so a family is tracked once whichever scope clicked it. ``bulk``
    adds the dismissed-cooldown arm (a family DROPPED within ``cooldown_days`` is skipped); a
    single, deliberate Track leaves it out so a human may re-track on purpose. Every value goes
    through sql_literal / sql_number, and the statement holds no ';' outside literals (one
    statement, so it passes the executor allow-list)."""
    values: list[str] = []
    for item in items:
        key = _text(item.get("ENTITY_KEY"))
        if not key:
            continue
        sev = _text(item.get("SEVERITY")).upper()
        sev = sev if sev in ("MEDIUM", "LOW") else "LOW"
        conf = item.get("CONFIDENCE")
        conf_f = safe_float(conf, default=float("nan"))
        conf_sql = "NULL" if conf is None or conf_f != conf_f else sql_number(max(0.0, min(conf_f, 1.0)))
        usd = item.get("ESTIMATED_USD")
        usd_f = safe_float(usd, default=float("nan"))
        priced = usd is not None and usd_f == usd_f and usd_f > 0
        usd_sql = sql_number(round(usd_f, 2)) if priced else "NULL"
        per_sql = sql_literal("MONTHLY" if priced else "", 20)
        values.append(
            f"({sql_literal(_text(item.get('COMPANY')) or 'ALL', 40)}, {sql_literal(sev, 20)}, "
            f"{sql_literal(_text(item.get('TITLE')), 300)}, {sql_literal(_text(item.get('DETAIL')), 1000)}, "
            f"{sql_literal(key, 500)}, {conf_sql}, {usd_sql}, {per_sql})")
    if not values:
        return ""
    days = max(1, min(int(cooldown_days or TRACK_COOLDOWN_DAYS), 365))
    cooldown = (f"\n           OR (UPPER(q.STATUS) = 'DROPPED'\n"
                f"               AND COALESCE(q.COMPLETED_AT, q.UPDATED_AT) >= "
                f"DATEADD('day', -{days}, CURRENT_TIMESTAMP()))") if bulk else ""
    rows_sql = ",\n    ".join(values)
    return f"""
INSERT INTO {core_object('ACTION_QUEUE')}
    (COMPANY, SEVERITY, TITLE, DETAIL, OWNER, STATUS, SOURCE, SOURCE_ENTITY_TYPE,
     SOURCE_ENTITY_KEY, CONFIDENCE, ESTIMATED_USD, PERIOD, UPDATED_BY)
SELECT v.COMPANY, v.SEVERITY, v.TITLE, v.DETAIL, 'UNASSIGNED', 'OPEN', {sql_literal(TRACK_SOURCE, 120)},
       {sql_literal(TRACK_ENTITY_TYPE, 40)}, v.ENTITY_KEY, v.CONF::FLOAT, v.USD::NUMBER(18,2),
       NULLIF(v.PER, ''), {actor_sql}
FROM (VALUES
    {rows_sql}
) AS v (COMPANY, SEVERITY, TITLE, DETAIL, ENTITY_KEY, CONF, USD, PER)
WHERE NOT EXISTS (
    SELECT 1 FROM {core_object('ACTION_QUEUE')} q
    WHERE UPPER(q.SOURCE_ENTITY_TYPE) = {sql_literal(TRACK_ENTITY_TYPE, 40)}
      AND UPPER(q.SOURCE_ENTITY_KEY) = UPPER(v.ENTITY_KEY)
      AND (UPPER(q.STATUS) IN ('OPEN', 'IN_PROGRESS'){cooldown}))
""".strip()
