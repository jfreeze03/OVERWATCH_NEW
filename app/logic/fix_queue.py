"""Operations > Optimize fix queue: per-family diagnosis, and the ONE shared Track write path (Operations >
Optimize, Control Room triage and, since v4.605, Cost > Chargeback & AI exceptions). Pure module.

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

from app import companies
from app.config import core_object
from app.core.sqlsafe import sql_literal, sql_number
from app.logic import query_advisor, query_opt
from app.logic.actions import SEVERITY_RANK
from app.logic.formulas import format_usd, humanize_duration, humanize_gb, safe_float

TRACK_ALL_CAP = 25            # rows one "Track all ACT NOW" click may insert
TRACK_COOLDOWN_DAYS = 90      # a family dismissed (DROPPED) or marked DONE this recently is never bulk re-tracked
TRACK_SOURCE = "Operations > Optimize"
TRACK_ENTITY_TYPE = "QUERY_FINGERPRINT"
SPECIFIC_SOURCES = frozenset({"live", "mart", "heuristic"})
_OPEN_STATUSES = frozenset({"OPEN", "IN_PROGRESS"})
# Next-Fifty #15: Control Room triage tracks task-failure and warehouse-spend rows through the SAME write
# path. Alerts are never tracked: an ACTION_QUEUE item has no lifecycle link to ALERT_EVENTS, so resolving
# the alert would never close the work item (they are owned through Acknowledge and the incident flow).
TRACK_OPEN_STATUS = "Tracked (open)"
TRACK_UNKNOWN_STATUS = "Unknown"
TRIAGE_TRACK_SOURCE = "Control Room > Triage"
TRIAGE_TRACK_TYPES = ("TASK", "WAREHOUSE")
# Optimize + triage Track items land at MEDIUM or LOW only: HIGH/CRITICAL items feed the Overview score.
TRACK_SEVERITIES = ("MEDIUM", "LOW")
# v4.605: Cost > Chargeback & AI exceptions track through the SAME write. One item per user (USER, keyed on the
# exception's USER_NAME, every signal of that user in its detail) plus the all-users budget breach, keyed on the
# Company scope (AI_BUDGET -- a scope key, NOT an Entity 360 type: the drill guards in workbench.py and
# decision_studio.py never open it). The source is the legacy writer's SOURCE, byte-identical, so its still-open
# rows are recognised (see _LEGACY_TITLE_ARMS). Severity is preserved (the legacy writer's behaviour).
AI_TRACK_SOURCE = "Cost Intelligence > Chargeback & AI > AI users"
AI_USER_ENTITY_TYPE = "USER"
AI_SCOPE_ENTITY_TYPE = "AI_BUDGET"
AI_TRACK_CAP = 10             # exception ROWS read per click (the legacy writer's head(10))
AI_TRACK_SEVERITIES = ("CRITICAL", "HIGH", "MEDIUM", "LOW")
_AI_SCOPE_ROW = "(all users)"
_TRACKABLE_TYPES = frozenset({TRACK_ENTITY_TYPE, *TRIAGE_TRACK_TYPES, AI_USER_ENTITY_TYPE, AI_SCOPE_ENTITY_TYPE})
# Pre-v4.605 Chargeback & AI rows carry no SOURCE_ENTITY_TYPE/KEY; their TITLE is 'Cortex <signal>: <user>
# (<source>)', and the scope row's is 'Cortex AI budget breach (all users): (all users) ((all sources))' under
# COMPANY = the scope. A still-open legacy row blocks the matching entity-keyed item (source_scoped only). CONTAINS
# is an exact substring (no LIKE wildcards); ': ' and ' (' bracket the name, so JDOE never matches XJDOE.
_LEGACY_TITLE_ARMS = {
    "USER": "CONTAINS(UPPER(q.TITLE), ': ' || UPPER(v.ENTITY_KEY) || ' (')",
    "AI_BUDGET": ("CONTAINS(UPPER(q.TITLE), ': (ALL USERS) (') "
                  "AND UPPER(COALESCE(q.COMPANY, '')) = UPPER(v.ENTITY_KEY)"),
}

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


def resolve_deep_link(fingerprint: object, fingerprints: Iterable[object]) -> str:
    """#28: the queue's own spelling of a deep-linked fingerprint, or "" when it is not queued.
    Case-insensitive like every join in this module; master_detail binds the detail pane by EXACT id."""
    want = _text(fingerprint).upper()
    if not want:
        return ""
    for fp in fingerprints:
        text = _text(fp)
        if text.upper() == want:
            return text
    return ""


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
    # the families the live profile actually examined (incl. its clean 'None' rows): only those may be
    # told "the live profile found nothing" (review r1: an out-of-filter family was never seen)
    live_seen: set[str] = set()
    if live_scored is not None and "FINGERPRINT" in live_scored.columns:
        live_seen = {_text(fp).upper() for fp in live_scored["FINGERPRINT"]}
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
                if fp and fp.upper() in live_seen:
                    first_fix += ("The live profile found no actionable inefficiency in a typical "
                                  "successful run either; open its Entity 360 and read a slow run's "
                                  "query profile.")
                elif live_scored is not None:
                    first_fix += ("The live profile did not cover this family (outside its filters, or "
                                  "beyond its successful-run, window or top-500 scope); open its Entity "
                                  "360 and read a slow run's query profile.")
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


def _tracked_sets(tracked: pd.DataFrame | None) -> tuple[dict[str, str], set[str], set[str], set[str]]:
    """From ``workbench_sql.tracked_actions`` rows: (latest status by key, open keys, keys
    dismissed within the read's cooldown window, keys marked DONE within it). Keys are upper-cased."""
    latest: dict[str, str] = {}
    open_keys: set[str] = set()
    dropped: set[str] = set()
    done: set[str] = set()
    if tracked is None or tracked.empty or "ENTITY_KEY_U" not in tracked.columns:
        return latest, open_keys, dropped, done
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
        if safe_float(rec.get("DONE_N")) > 0 or status == "DONE":
            done.add(key)
    return latest, open_keys, dropped, done


def with_track_status(df: pd.DataFrame, tracked: pd.DataFrame | None, *,
                      key_col: str = "FINGERPRINT") -> pd.DataFrame:
    """Add TRACK_STATUS (Tracked (open) / Dismissed / Done / Untracked), TRACKED_ACTION_ID (the
    newest OPEN item for a tracked-open family, the one Action Center lists by default, else the
    latest closed item; '' when untracked) and TRACKED_COMPANY (that open item's COMPANY, '' when
    none): Action Center filters by company, so the doorway needs it (review r2). ``key_col`` names
    the column matched against the tracked rows' ENTITY_KEY_U (Control Room triage passes a
    composite TYPE|KEY column)."""
    out = df.copy()
    latest, open_keys, dropped, done = _tracked_sets(tracked)
    ids: dict[str, str] = {}
    open_cos: dict[str, str] = {}
    if tracked is not None and not tracked.empty and "ENTITY_KEY_U" in tracked.columns:
        for rec in tracked.to_dict("records"):
            key_u = _text(rec.get("ENTITY_KEY_U")).upper()
            open_id = _text(rec.get("OPEN_ACTION_ID"))
            ids[key_u] = open_id if key_u in open_keys and open_id else _text(rec.get("LATEST_ACTION_ID"))
            open_cos[key_u] = _text(rec.get("OPEN_ACTION_COMPANY"))
    statuses: list[str] = []
    action_ids: list[str] = []
    companies: list[str] = []
    keys = out[key_col] if key_col in out.columns else pd.Series([""] * len(out))
    for fp in keys:
        key = _text(fp).upper()
        if key in open_keys:
            statuses.append(TRACK_OPEN_STATUS)
        elif key in dropped:
            statuses.append("Dismissed")
        elif key in done:
            statuses.append("Done")
        else:
            statuses.append("Untracked")
        action_ids.append(ids.get(key, "") if key in latest else "")
        companies.append(open_cos.get(key, "") if key in open_keys else "")
    out["TRACK_STATUS"] = statuses
    out["TRACKED_ACTION_ID"] = action_ids
    out["TRACKED_COMPANY"] = companies
    return out


def track_all_static_mask(df: pd.DataFrame) -> pd.Series:
    """The row-level half of Track all's rule: LANE 'ACT NOW' AND a specific diagnosis AND not OVERWATCH's
    own traffic AND a non-blank fingerprint. The other half (no open item, no cooldown) needs the Action
    Center read; track_all_eligible applies both. ``df`` must carry FINGERPRINT, LANE and SPECIFIC."""
    keys = df["FINGERPRINT"].map(lambda v: _text(v).upper())
    return (df["LANE"].astype(str).eq("ACT NOW")
            & df["SPECIFIC"].map(_is_true)
            & ~own_traffic(df)
            & keys.ne(""))


def track_all_takes(row: Mapping[str, object] | pd.Series) -> bool:
    """Whether Track all ACT NOW can take this family at all (track_all_static_mask on one row): the
    Optimize detail pane says 'Track all includes it again' for a re-broke / not-fixed family ONLY then --
    its lifted DONE cooldown re-admits nothing outside ACT NOW, a vague diagnosis or own traffic (C23)."""
    rec = dict(row.items()) if isinstance(row, pd.Series) else dict(row)
    if not {"FINGERPRINT", "LANE", "SPECIFIC"}.issubset(rec):
        return False
    return bool(track_all_static_mask(pd.DataFrame([rec])).iloc[0])


def track_all_eligible(df: pd.DataFrame | None, tracked: pd.DataFrame | None, *,
                       rebroke: Iterable[str] = ()) -> pd.DataFrame:
    """The rows one "Track all ACT NOW" click takes: ACT NOW lane AND a specific diagnosis AND
    not OVERWATCH's own traffic AND not open-tracked AND not dismissed or marked DONE within the
    cooldown, in PRIORITY_SCORE order, capped at TRACK_ALL_CAP. Done is cooled down too (review r2):
    the trailing-window mart still carries the pre-fix runs, so a just-fixed family stays ACT NOW
    and would otherwise be re-queued with the fix the team already applied.

    Next-Fifty #46: ``rebroke`` names families whose MEASURED outcome since done says the fix
    re-broke or never held (outcomes.OVERRIDES_COOLDOWN); their DONE cooldown is lifted, because a
    measured non-improvement is exactly the case where re-tracking is right. A dismissal (DROPPED)
    is deliberate and is never lifted."""
    if df is None or df.empty or not {"FINGERPRINT", "LANE", "SPECIFIC"}.issubset(df.columns):
        return pd.DataFrame(columns=list(df.columns) if df is not None else [])
    _latest, open_keys, dropped, done = _tracked_sets(tracked)
    done = done - {_text(k).upper() for k in rebroke}
    keys = df["FINGERPRINT"].map(lambda v: _text(v).upper())
    mask = (track_all_static_mask(df)
            & ~keys.isin(open_keys)
            & ~keys.isin(dropped)
            & ~keys.isin(done))
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


def track_entities_sql(items: list[dict], *, entity_type: str, source: str, actor_sql: str, bulk: bool,
                       cooldown_days: int = TRACK_COOLDOWN_DAYS, rebroke_keys: Iterable[str] = (),
                       severities: Iterable[str] = TRACK_SEVERITIES, company_from_user: bool = False,
                       source_scoped: bool = False) -> str:
    """ONE idempotent INSERT for the given track items ('' when there are none) -- the ONE Track write
    path, shared by Operations > Optimize (query families), Control Room triage (tasks, warehouses) and
    Cost > Chargeback & AI (users + the all-users budget scope, v4.605).

    Keyed on the ENTITY (SOURCE_ENTITY_TYPE + upper-cased SOURCE_ENTITY_KEY) plus open status,
    never the title or company, so an entity is tracked once whichever scope clicked it. ``bulk``
    adds the cooldown arm (an entity DROPPED or DONE within ``cooldown_days`` is skipped); a
    single, deliberate Track leaves it out so a human may re-track on purpose. Every value goes
    through sql_literal / sql_number, and the statement holds no ';' outside literals (one
    statement, so it passes the executor allow-list).

    ``entity_type`` must be QUERY_FINGERPRINT, TASK, WAREHOUSE, USER or AI_BUDGET (ValueError otherwise):
    an ALERT or INCIDENT has no lifecycle link from ACTION_QUEUE, so a tracked alert's work item would
    drift from the alert's own state. ``rebroke_keys`` (bulk only, Next-Fifty #46) lifts the DONE -- never
    the DROPPED -- cooldown for keys whose measured outcome re-broke or never held; with none the
    statement is byte-identical to the pre-#46 builder.

    v4.605 options (the defaults leave the statement byte-identical to v4.604): ``severities`` is the
    allowed SEVERITY set (anything else writes LOW); ``company_from_user`` (USER only, ValueError
    otherwise) resolves COMPANY in SQL as COALESCE(NULLIF(COMPANY_FOR_USER(key), 'UNKNOWN'), 'ALL') --
    the V163 [28] owner rule, on a plain column (V030 shape law); ``source_scoped`` keys the NOT EXISTS
    on ``source`` too (a Security work item on the same user never blocks an AI-spend item) and, for a
    type in _LEGACY_TITLE_ARMS, also lets a still-open pre-v4.605 row of that source (no entity key)
    block by its TITLE."""
    kind = str(entity_type or "").strip().upper()
    if kind not in _TRACKABLE_TYPES:
        raise ValueError(f"Track does not cover entity type {kind or '(blank)'}: only "
                         f"{', '.join(sorted(_TRACKABLE_TYPES))}")
    if company_from_user and kind != AI_USER_ENTITY_TYPE:
        raise ValueError(f"company_from_user resolves a USER's company; not {kind or '(blank)'}")
    allowed = {_text(s).upper() for s in severities}
    values: list[str] = []
    for item in items:
        key = _text(item.get("ENTITY_KEY"))
        if not key:
            continue
        sev = _text(item.get("SEVERITY")).upper()
        sev = sev if sev in allowed else "LOW"
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
    # ENTITY_KEY allows 500 characters; in_list clips at 300, so the IN-list is built here.
    keys_sql = ", ".join(sql_literal(k, 500) for k in sorted(
        {_text(k).upper() for k in rebroke_keys if _text(k)})) if bulk else ""
    rebroke = (f"\n               AND NOT (UPPER(q.STATUS) = 'DONE' AND UPPER(v.ENTITY_KEY) IN ({keys_sql}))"
               if keys_sql else "")
    cooldown = ((f"\n           OR (UPPER(q.STATUS) IN ('DROPPED', 'DONE')\n"
                 f"               AND COALESCE(q.COMPLETED_AT, q.UPDATED_AT) >= "
                 f"DATEADD('day', -{days}, CURRENT_TIMESTAMP())" + rebroke + ")") if bulk else "")
    rows_sql = ",\n    ".join(values)
    company_col = (f"COALESCE(NULLIF({companies.COMPANY_FOR_USER_FN}(v.ENTITY_KEY), 'UNKNOWN'), 'ALL')"
                   if company_from_user else "v.COMPANY")
    entity_match = (f"UPPER(q.SOURCE_ENTITY_TYPE) = {sql_literal(kind, 40)}\n"
                    f"      AND UPPER(q.SOURCE_ENTITY_KEY) = UPPER(v.ENTITY_KEY)")
    legacy_arm = _LEGACY_TITLE_ARMS.get(kind, "")
    if source_scoped and legacy_arm:
        match = (f"q.SOURCE = {sql_literal(source, 120)}\n"
                 f"      AND ((UPPER(q.SOURCE_ENTITY_TYPE) = {sql_literal(kind, 40)}\n"
                 f"            AND UPPER(q.SOURCE_ENTITY_KEY) = UPPER(v.ENTITY_KEY))\n"
                 f"           OR (q.SOURCE_ENTITY_TYPE IS NULL\n"
                 f"               AND {legacy_arm}))")
    elif source_scoped:
        match = f"q.SOURCE = {sql_literal(source, 120)}\n      AND {entity_match}"
    else:
        match = entity_match
    return f"""
INSERT INTO {core_object('ACTION_QUEUE')}
    (COMPANY, SEVERITY, TITLE, DETAIL, OWNER, STATUS, SOURCE, SOURCE_ENTITY_TYPE,
     SOURCE_ENTITY_KEY, CONFIDENCE, ESTIMATED_USD, PERIOD, UPDATED_BY)
SELECT {company_col}, v.SEVERITY, v.TITLE, v.DETAIL, 'UNASSIGNED', 'OPEN', {sql_literal(source, 120)},
       {sql_literal(kind, 40)}, v.ENTITY_KEY, v.CONF::FLOAT, v.USD::NUMBER(18,2),
       NULLIF(v.PER, ''), {actor_sql}
FROM (VALUES
    {rows_sql}
) AS v (COMPANY, SEVERITY, TITLE, DETAIL, ENTITY_KEY, CONF, USD, PER)
WHERE NOT EXISTS (
    SELECT 1 FROM {core_object('ACTION_QUEUE')} q
    WHERE {match}
      AND (UPPER(q.STATUS) IN ('OPEN', 'IN_PROGRESS'){cooldown}))
""".strip()


def track_fingerprints_sql(items: list[dict], *, actor_sql: str, bulk: bool,
                           cooldown_days: int = TRACK_COOLDOWN_DAYS,
                           rebroke_keys: Iterable[str] = ()) -> str:
    """Operations > Optimize's Track: ``track_entities_sql`` for query families (a thin wrapper, so the
    statement is byte-identical to the pre-#15 builder whenever no ``rebroke_keys`` are passed)."""
    return track_entities_sql(items, entity_type=TRACK_ENTITY_TYPE, source=TRACK_SOURCE, actor_sql=actor_sql,
                              bulk=bulk, cooldown_days=cooldown_days, rebroke_keys=rebroke_keys)


# --------------------------------------------------------------------------------------------
# Next-Fifty #15: Control Room triage glue (task-failure and warehouse-spend rows only)
# --------------------------------------------------------------------------------------------

def _triage_option(etype: object, ekey: object) -> str:
    kind, key = _text(etype).upper(), _text(ekey)
    return f"{kind}|{key}" if kind in TRIAGE_TRACK_TYPES and key else ""


def triage_track_options(queue: pd.DataFrame | None) -> list[str]:
    """'TYPE|KEY' identity strings of the trackable triage rows (TASK / WAREHOUSE with a key), in queue
    order, de-duplicated. Alert rows carry no entity and are never offered. [] without the columns."""
    if queue is None or queue.empty or not {"ENTITY_TYPE", "ENTITY_KEY"}.issubset(queue.columns):
        return []
    out: list[str] = []
    for etype, ekey in zip(queue["ENTITY_TYPE"], queue["ENTITY_KEY"], strict=False):
        opt = _triage_option(etype, ekey)
        if opt and opt not in out:
            out.append(opt)
    return out


def triage_track_label(option: str) -> str:
    """The picker label: a pure function of the option. On Streamlit 1.52.2 a selectbox's identity
    includes its format_func labels, so a label carrying a count, z-score or dollar figure would reset
    the pick whenever that number moved."""
    kind, _, key = str(option or "").partition("|")
    return f"{'Task' if kind == 'TASK' else 'Warehouse'} · {key}"


def triage_track_row(queue: pd.DataFrame | None, option: str) -> pd.Series | None:
    """The first queue row whose identity is ``option`` (bind by identity, never by position)."""
    if queue is None or queue.empty or not {"ENTITY_TYPE", "ENTITY_KEY"}.issubset(queue.columns):
        return None
    for i, (etype, ekey) in enumerate(zip(queue["ENTITY_TYPE"], queue["ENTITY_KEY"], strict=False)):
        if _triage_option(etype, ekey) == option:
            return queue.iloc[i]
    return None


def triage_track_item(row: Mapping[str, object], company: str) -> dict | None:
    """One ACTION_QUEUE item for a trackable triage row (None for an alert or a keyless row).

    COMPANY = the selected company, or under ALL the entity's own company (a task by its database, a
    warehouse by its name), UNKNOWN reading 'ALL'. SEVERITY = MEDIUM for a CRITICAL/HIGH row, else
    LOW: never HIGH, the Optimize rule (HIGH/CRITICAL items feed the Overview score). Unpriced: a
    triage row carries no savings estimate. DETAIL freezes the triage text at the moment of tracking."""
    kind = _text(row.get("ENTITY_TYPE")).upper()
    key = _text(row.get("ENTITY_KEY"))
    if kind not in TRIAGE_TRACK_TYPES or not key:
        return None
    scope = _text(company)
    if scope and scope.upper() != "ALL":
        item_company = scope
    else:
        found = (companies.classify_database(key.split(".", 1)[0]) if kind == "TASK"
                 else companies.classify_warehouse(key))
        item_company = found if found in ("ALFA", "Trexis") else "ALL"
    sev = _text(row.get("SEVERITY")).upper()
    title = f"{_text(row.get('KIND')) or kind.title()}: {key}"
    raised = _text(row.get("RAISED_AT"))
    tail = (f" Triage severity {sev or 'unset'}" + (f", raised {raised}" if raised else "")
            + ". Tracked from Control Room triage; unpriced (a triage row carries no savings estimate).")
    head = ". ".join(p for p in (_text(row.get("TITLE")), _text(row.get("DETAIL"))) if p)
    head = head + "." if head and not head.endswith(".") else head
    detail = head[:max(0, 1000 - len(tail))] + tail
    return {
        "ENTITY_TYPE": kind,
        "COMPANY": item_company[:40],
        "SEVERITY": "MEDIUM" if sev in ("CRITICAL", "HIGH") else "LOW",
        "TITLE": title[:300],
        "DETAIL": detail[:1000],
        "ENTITY_KEY": key[:500],
        "CONFIDENCE": None,
        "ESTIMATED_USD": None,
        "PERIOD": "",
    }


def with_triage_track_status(queue: pd.DataFrame, tracked: pd.DataFrame | None, *,
                             read_ok: bool) -> pd.DataFrame:
    """The triage queue with TRACKED (Action Center status of a TASK / WAREHOUSE row; None -> '—' for
    alerts; 'Unknown' when the tracked read FAILED), TRACKED_ACTION_ID / TRACKED_COMPANY (for the doorway;
    '' unless trackable and read), re-sorted UNOWNED FIRST within each severity.

    Owned = an acknowledged alert, or a row with an open Action Center item. Dismissed, Done and Unknown
    rows sort as unowned, so a failed read never demotes a row. The sort is a stable single key
    (severity rank x 2 + owned), so triage_queue's dollars-at-risk order holds inside each group, and the
    index is reset: Control Room maps a clicked row back by POSITION, so the frame it displays and the
    frame it indexes must be this same one."""
    if queue is None or queue.empty:
        return pd.DataFrame() if queue is None else queue.copy()
    out = queue.copy()
    n = len(out)
    etype = (out["ENTITY_TYPE"] if "ENTITY_TYPE" in out.columns
             else pd.Series([""] * n, index=out.index)).map(lambda v: _text(v).upper())
    ekey = (out["ENTITY_KEY"] if "ENTITY_KEY" in out.columns
            else pd.Series([""] * n, index=out.index)).map(_text)
    trackable = (etype.isin(TRIAGE_TRACK_TYPES) & ekey.ne("")).tolist()
    composite = None
    if tracked is not None and not tracked.empty and {"ENTITY_TYPE_U", "ENTITY_KEY_U"}.issubset(tracked.columns):
        composite = tracked.assign(ENTITY_KEY_U=tracked["ENTITY_TYPE_U"].map(lambda v: _text(v).upper())
                                   + "|" + tracked["ENTITY_KEY_U"].map(lambda v: _text(v).upper()))
    status = with_track_status(out.assign(_TRACK_KEY=(etype + "|" + ekey.str.upper()).tolist()),
                               composite, key_col="_TRACK_KEY")
    read = bool(read_ok)
    out["TRACKED"] = pd.Series(
        [(s if read else TRACK_UNKNOWN_STATUS) if t else None
         for s, t in zip(status["TRACK_STATUS"], trackable, strict=True)], index=out.index, dtype=object)
    out["TRACKED_ACTION_ID"] = [a if (t and read) else ""
                                for a, t in zip(status["TRACKED_ACTION_ID"], trackable, strict=True)]
    out["TRACKED_COMPANY"] = [c if (t and read) else ""
                              for c, t in zip(status["TRACKED_COMPANY"], trackable, strict=True)]
    kind_col = (out["KIND"] if "KIND" in out.columns else pd.Series([""] * n, index=out.index)).astype(str)
    alert_status = (out["STATUS"] if "STATUS" in out.columns
                    else pd.Series([""] * n, index=out.index)).map(lambda v: _text(v).upper())
    owned = (kind_col.eq("Alert") & alert_status.eq("ACK")) | out["TRACKED"].eq(TRACK_OPEN_STATUS)
    sev = (out["SEVERITY"] if "SEVERITY" in out.columns
           else pd.Series([""] * n, index=out.index)).map(lambda v: SEVERITY_RANK.get(_text(v).upper(), 9))
    return (out.assign(_K=sev.astype(float) * 2 + owned.astype(int))
            .sort_values("_K", kind="stable").drop(columns="_K").reset_index(drop=True))


def tracked_elsewhere(item_company: object, scope: object) -> bool:
    """An open item tracked under ANOTHER company is not in this scope's Action Center list (it filters
    COMPANY IN (scope, 'ALL')), so the doorway names it instead of linking to nothing (Optimize r2)."""
    co = _text(item_company)
    sc = _text(scope).upper() or "ALL"
    return bool(co) and co.upper() != "ALL" and sc not in ("ALL", co.upper())


# --------------------------------------------------------------------------------------------
# v4.605: Cost > Chargeback & AI exceptions glue (one item per user + the all-users budget scope)
# --------------------------------------------------------------------------------------------

def _ai_signal_segment(rec: Mapping[str, object]) -> str:
    return (f"{_text(rec.get('SIGNAL'))} ({_text(rec.get('SOURCE'))}): "
            f"{int(safe_float(rec.get('TOTAL_REQUESTS'))):,} requests, projected 30d "
            f"{format_usd(safe_float(rec.get('PROJECTED_30D_USD')))}, "
            f"cr/request {safe_float(rec.get('CREDITS_PER_REQUEST')):.4f}")


def ai_exception_track_items(exceptions: pd.DataFrame | None, company: str, *,
                             cap: int = AI_TRACK_CAP) -> dict[str, list[dict]]:
    """The Chargeback & AI Exceptions table as Track items: {"USER": [...], "AI_BUDGET": [...]} (both keys,
    always). Reads exceptions.head(cap) in table order -- the '(all users)' scope row first, then strongest
    first -- so the cap counts exception ROWS, as the legacy writer's head(10) did.

    USER: one item per upper-cased USER_NAME (first-seen order; a blank name is skipped). SEVERITY and TITLE
    come from the user's first (strongest) row; TITLE keeps the legacy 'Cortex <signal>: <user> (<source>)'
    format, plus ' + N more signal(s)'. DETAIL lists every signal of that user, frozen at tracking time.
    ESTIMATED_USD = the '(all sources)' budget row's PROJECTED_30D_USD when the user has one (it already sums
    every source), else the sum over the user's distinct-source rows; PERIOD 'MONTHLY' when priced. COMPANY is
    a placeholder: track_entities_sql(company_from_user=True) resolves it per user in SQL.

    AI_BUDGET: the '(all users)' row (at most one), keyed on the Company scope ('ALL' when blank). Its
    ESTIMATED_USD is the scope exposure BEYOND the user items tracked with it (max 0, None at 0), so the queued
    set sums to the scope total once."""
    out: dict[str, list[dict]] = {AI_USER_ENTITY_TYPE: [], AI_SCOPE_ENTITY_TYPE: []}
    if exceptions is None or exceptions.empty:
        return out
    scope = _text(company) or "ALL"
    by_user: dict[str, list[dict]] = {}
    scope_rec: dict | None = None
    for rec in exceptions.head(max(0, int(cap))).to_dict("records"):
        user = _text(rec.get("USER_NAME"))
        if not user:
            continue
        if user == _AI_SCOPE_ROW:
            scope_rec = scope_rec if scope_rec is not None else rec
            continue
        by_user.setdefault(user.upper(), []).append(rec)      # dicts keep first-seen order
    tail = ". Projected at the time of tracking."
    for recs in by_user.values():
        first = recs[0]
        user = _text(first.get("USER_NAME"))
        title = f"Cortex {_text(first.get('SIGNAL'))}: {user} ({_text(first.get('SOURCE'))})"
        if len(recs) > 1:
            title += f" + {len(recs) - 1} more signal" + ("s" if len(recs) - 1 > 1 else "")
        head = "; ".join(_ai_signal_segment(r) for r in recs)
        budget = [r for r in recs if _text(r.get("SOURCE")).lower() == "(all sources)"]
        if budget:
            est = safe_float(budget[0].get("PROJECTED_30D_USD"))
        else:
            per_source: dict[str, float] = {}
            for r in recs:
                per_source.setdefault(_text(r.get("SOURCE")).upper(), safe_float(r.get("PROJECTED_30D_USD")))
            est = sum(per_source.values())
        priced = est > 0
        out[AI_USER_ENTITY_TYPE].append({
            "COMPANY": scope[:40],
            "SEVERITY": _text(first.get("SEVERITY")).upper(),
            "TITLE": title[:300],
            "DETAIL": head[:max(0, 1000 - len(tail))] + tail,
            "ENTITY_KEY": user[:500],
            "CONFIDENCE": None,
            "ESTIMATED_USD": (round(est, 2) if priced else None),
            "PERIOD": ("MONTHLY" if priced else ""),
        })
    if scope_rec is not None:
        proj = safe_float(scope_rec.get("PROJECTED_30D_USD"))
        tracked = sum(safe_float(i["ESTIMATED_USD"]) for i in out[AI_USER_ENTITY_TYPE])
        est = max(0.0, proj - tracked)
        priced = round(est, 2) > 0
        out[AI_SCOPE_ENTITY_TYPE].append({
            "COMPANY": scope[:40],
            "SEVERITY": _text(scope_rec.get("SEVERITY")).upper(),
            "TITLE": (f"Cortex {_text(scope_rec.get('SIGNAL'))}: {_text(scope_rec.get('USER_NAME'))} "
                      f"({_text(scope_rec.get('SOURCE'))})")[:300],
            "DETAIL": (f"{int(safe_float(scope_rec.get('TOTAL_REQUESTS'))):,} requests, projected 30d "
                       f"{format_usd(proj)}, cr/request {safe_float(scope_rec.get('CREDITS_PER_REQUEST')):.4f}. "
                       "Its estimate is the scope exposure beyond the user items tracked with it (avoids "
                       "double-counting).")[:1000],
            "ENTITY_KEY": scope[:500],
            "CONFIDENCE": None,
            "ESTIMATED_USD": (round(est, 2) if priced else None),
            "PERIOD": ("MONTHLY" if priced else ""),
        })
    return out
