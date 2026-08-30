"""Action-queue ranking and the savings ledger state machine.

The savings ledger is the app's differentiator: estimated savings and verified
savings are separate states, and nothing reaches VERIFIED without a proof
query and a verified dollar amount.
"""

from __future__ import annotations

import pandas as pd

from app.logic.formulas import account_now, safe_float

SEVERITY_RANK = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3, "INFO": 4}
OPEN_STATUSES = ("OPEN", "IN_PROGRESS")

#: C2: rules whose server-side events DUPLICATE a signal the app computes itself.
#: SP_ANOMALY_SWEEP writes COST_ANOMALY_SWEEP events from the same
#: FACT_WAREHOUSE_DAILY series the app scores in-process, so the morning queue
#: showed each spend break twice — and with DISAGREEING severities, because the
#: sweep escalates at z >= 2*threshold while the app escalated at z >= 5. They
#: still belong on the Alerts page (that is the alert surface); they are dropped
#: only from this merged triage feed, where the app's own row is the richer twin.
DUPLICATE_ALERT_RULE_IDS = ("COST_ANOMALY_SWEEP",)

#: D6/C2: escalate a spend anomaly to HIGH on SHAPE **or** on MONEY.
#: The z gate matches SP_ANOMALY_SWEEP's ``IFF(z >= zthr * 2, 'HIGH', 'MEDIUM')``
#: ONLY at the sweep's DEFAULT threshold of 3.5 (2 x 3.5 = 7) — the app used to
#: say HIGH from z >= 5, so the same warehouse-day could arrive as HIGH (app) and
#: MEDIUM (server) in one list. This constant is FIXED, while the sweep reads the
#: configurable ALERT_CONFIG.THRESHOLD_NUM, so once that threshold is tuned the
#: two diverge; the server's COST_ANOMALY_SWEEP events (on Alerts) are then the
#: authoritative escalation and this in-app row is a best-effort morning-triage
#: twin (#37). Raising the z gate alone would DEMOTE real money, so the dollar arm
#: escalates any break whose excess over its own baseline clears the floor,
#: however tame its z. The floor is an uncalibrated starting point: roughly
#: "a five-figure annualized surprise", tune it against real incidents.
ANOMALY_HIGH_Z = 7.0
ANOMALY_HIGH_EXCESS_USD = 500.0

LEDGER_ESTIMATED = "ESTIMATED"
LEDGER_VERIFIED = "VERIFIED"
LEDGER_REJECTED = "REJECTED"
LEDGER_STATES = (LEDGER_ESTIMATED, LEDGER_VERIFIED, LEDGER_REJECTED)


def _datetime_col(view: pd.DataFrame, column: str) -> pd.Series:
    """Coerce ``column`` to a datetime Series, or an all-NaT one when absent.

    ``view.get(missing)`` returns None, and ``pd.to_datetime(None)`` is the NaT
    SCALAR — which has no ``.dt``/``.notna()`` — so a frame without DUE_DATE or
    CREATED_AT would blow up in ranking rather than degrade to "unknown".
    """
    if column not in view.columns:
        return pd.Series(pd.NaT, index=view.index, dtype="datetime64[ns]")
    return pd.to_datetime(view[column], errors="coerce")


def rank_actions(df: pd.DataFrame, limit: int = 25) -> pd.DataFrame:
    """Rank open actions: severity, then overdue-ness, then dollars, then age.

    Expects columns SEVERITY, STATUS, DUE_DATE, CREATED_AT, ESTIMATED_USD (extra
    columns pass through untouched). Non-open rows are dropped.
    """
    if df is None or df.empty:
        return pd.DataFrame()
    view = df.copy()
    view["STATUS"] = view.get("STATUS", pd.Series(dtype=str)).astype(str).str.upper()
    view = view[view["STATUS"].isin(OPEN_STATUSES)].copy()
    if view.empty:
        return view
    # Same reason as _datetime_col: `view.get("SEVERITY", "")` hands back a bare str
    # when the column is absent, and str has no .astype.
    _sev_raw = (view["SEVERITY"] if "SEVERITY" in view.columns
                else pd.Series("", index=view.index)).astype(str).str.upper().str.strip()
    view["_SEV"] = _sev_raw.map(SEVERITY_RANK)
    # D1: an unrecognized or blank SEVERITY used to fall through to rank 9 and sort
    # BELOW INFO, so one loader typo or a severity token we don't know yet silently
    # dropped a possibly-critical row off the bottom of a top-5 list. Rank unknowns
    # at the MEDIUM tier instead — the same neutral default triage_queue gives a
    # severity-less alert — and mark the cell so the bad label gets FIXED rather
    # than quietly tolerated. A blank renders as "UNSET?", not as an empty cell.
    _unknown = view["_SEV"].isna()
    view["_SEV"] = view["_SEV"].fillna(float(SEVERITY_RANK["MEDIUM"]))
    if bool(_unknown.any()):
        view.loc[_unknown, "SEVERITY"] = _sev_raw[_unknown].replace("", "UNSET") + "?"
    due = _datetime_col(view, "DUE_DATE")
    created = _datetime_col(view, "CREATED_AT")
    # Account time: DUE_DATE and CREATED_AT are mart columns and the marts store
    # account time. A server-clock now() runs 5-6 hours ahead under SiS and would
    # flag rows overdue before they are.
    now = pd.Timestamp(account_now())
    # D1: compare DAYS, not instants. DUE_DATE is a date and lands at midnight, so
    # `due < now` called an action due TODAY overdue from 00:00:01 of its own due
    # day — the owner lost the whole day they were given. Normalizing both sides
    # makes an item overdue only once its due date is genuinely in the past.
    view["_OVERDUE"] = (due.notna() & (due.dt.normalize() < now.normalize())).astype(int)
    # D1: the queue was $-BLIND — a $40k idle-warehouse action and a $40 one at the
    # same severity were separated only by age, so the cheap-but-old row led the
    # executive list. Dollars break the tie within a severity band (after the hard
    # overdue signal, which is a commitment, not an estimate).
    view["_USD"] = (pd.to_numeric(view["ESTIMATED_USD"], errors="coerce").fillna(0.0)
                    if "ESTIMATED_USD" in view.columns else 0.0)
    view["_AGE_H"] = ((now - created).dt.total_seconds() / 3600).fillna(0).clip(lower=0)
    view = view.sort_values(["_SEV", "_OVERDUE", "_USD", "_AGE_H"],
                            ascending=[True, False, False, False])
    return view.drop(columns=["_SEV", "_OVERDUE", "_USD", "_AGE_H"]).head(limit)


def can_verify(row: dict) -> tuple[bool, str]:
    """Gate for ESTIMATED -> VERIFIED: needs proof SQL and a verified amount."""
    state = str(row.get("STATE", "")).upper()
    if state != LEDGER_ESTIMATED:
        return False, f"Only {LEDGER_ESTIMATED} items can be verified (state={state or 'missing'})."
    proof = str(row.get("PROOF_SQL", "") or "").strip()
    if not proof:
        return False, "A proof query is required before verification."
    try:
        verified = float(str(row.get("VERIFIED_USD")))
    except (TypeError, ValueError):
        return False, "A numeric verified USD amount is required."
    if verified < 0:
        return False, "Verified USD cannot be negative."
    return True, ""


def ledger_totals(df: pd.DataFrame) -> dict:
    """Estimated vs verified totals (never mixed), plus the realization story — the
    verified dollars as a share of what those verified items were originally estimated
    to save (Cost #9), how much was verified THIS QUARTER, and the average time from
    booking to verification (DS #40/#31/#19, the ROI/track-record the ledger already
    holds). realization_pct / avg_days_to_verify are None until something is verified."""
    empty = {"estimated_usd": 0.0, "verified_usd": 0.0, "estimated_count": 0,
             "verified_count": 0, "verified_estimated_usd": 0.0, "realization_pct": None,
             "verified_qtd_usd": 0.0, "avg_days_to_verify": None}
    if df is None or df.empty or "STATE" not in df.columns:
        return empty
    view = df.copy()
    view["STATE"] = view["STATE"].astype(str).str.upper()
    est = view[view["STATE"] == LEDGER_ESTIMATED]
    ver = view[view["STATE"] == LEDGER_VERIFIED]
    est_usd = pd.to_numeric(est.get("ESTIMATED_USD"), errors="coerce").fillna(0).sum()
    ver_usd_col = pd.to_numeric(ver.get("VERIFIED_USD"), errors="coerce").fillna(0)
    ver_usd = ver_usd_col.sum()
    # Track record: of what VERIFIED items were originally estimated to save, how much
    # actually measured out — a fair estimate-vs-actual on verified items only. Restrict
    # the ratio to items that carried a positive estimate: a verified item with a zero or
    # absent estimate would otherwise add its verified $ to the numerator with nothing in
    # the denominator, inflating realization past a meaningful figure.
    ver_est_col = pd.to_numeric(ver.get("ESTIMATED_USD"), errors="coerce").fillna(0)
    ver_est_usd = ver_est_col.sum()
    _est_pos = ver_est_col > 0
    _real_den = float(ver_est_col[_est_pos].sum())
    realization = (round(float(ver_usd_col[_est_pos].sum()) / _real_den * 100, 1)
                   if _real_den > 0 else None)
    # This-quarter capture + booking->verify latency, both from VERIFIED_AT. Default to an
    # index-aligned NaT Series so a ledger read missing these columns stays a Series (not a
    # scalar NaT) and the comparisons below never raise.
    _nat = pd.Series(pd.NaT, index=ver.index)
    verified_at = pd.to_datetime(ver.get("VERIFIED_AT", _nat), errors="coerce")
    created_at = pd.to_datetime(ver.get("CREATED_AT", _nat), errors="coerce")
    now = pd.Timestamp(account_now())
    q_start = pd.Timestamp(year=now.year, month=((now.month - 1) // 3) * 3 + 1, day=1)
    qtd = float(ver_usd_col[verified_at >= q_start].sum())
    days = (verified_at - created_at).dt.total_seconds() / 86400.0
    days = days[days.notna() & (days >= 0)]
    avg_days = round(float(days.mean()), 1) if not days.empty else None
    return {
        "estimated_usd": round(float(est_usd), 2),
        "verified_usd": round(float(ver_usd), 2),
        "estimated_count": len(est),
        "verified_count": len(ver),
        "verified_estimated_usd": round(float(ver_est_usd), 2),
        # The numerator/denominator the realization_pct is ACTUALLY computed from —
        # restricted to verified items that carried a positive estimate (_est_pos). The
        # KPI delta must render THESE, not the all-verified totals, or the "$X of $Y
        # estimated" line contradicts the % beside it.
        "realized_verified_usd": round(float(ver_usd_col[_est_pos].sum()), 2),
        "realized_estimated_usd": round(_real_den, 2),
        "realization_pct": realization,
        "verified_qtd_usd": round(qtd, 2),
        "avg_days_to_verify": avg_days,
    }


def savings_by_month(df: pd.DataFrame, months: int = 12) -> pd.DataFrame:
    """Verified savings per calendar month (the run-rate the ROI story needs) —
    VERIFIED items only, bucketed by VERIFIED_AT, most recent ``months`` months,
    oldest-first for a time-ordered chart. Columns MONTH (YYYY-MM), VERIFIED_USD.
    Empty in, empty out."""
    cols = ["MONTH", "VERIFIED_USD"]
    if df is None or df.empty or "STATE" not in df.columns:
        return pd.DataFrame(columns=cols)
    ver = df[df["STATE"].astype(str).str.upper() == LEDGER_VERIFIED].copy()
    if ver.empty:
        return pd.DataFrame(columns=cols)
    ver["_AT"] = pd.to_datetime(ver.get("VERIFIED_AT"), errors="coerce")
    ver = ver.dropna(subset=["_AT"])
    if ver.empty:
        return pd.DataFrame(columns=cols)
    ver["MONTH"] = ver["_AT"].dt.strftime("%Y-%m")
    ver["VERIFIED_USD"] = pd.to_numeric(ver.get("VERIFIED_USD"), errors="coerce").fillna(0.0)
    out = (ver.groupby("MONTH", as_index=False)["VERIFIED_USD"].sum()
           .sort_values("MONTH"))
    return out.tail(max(1, int(months))).reset_index(drop=True)[cols]


def savings_by_lever(df: pd.DataFrame) -> pd.DataFrame:
    """Verified savings by lever (FINDING_TYPE) — where the realized money comes
    from, most-valuable first. Columns LEVER, VERIFIED_USD, ITEMS, REALIZATION_PCT
    (verified vs what those items were estimated to save). Empty in, empty out."""
    cols = ["LEVER", "VERIFIED_USD", "ITEMS", "REALIZATION_PCT"]
    if df is None or df.empty or "STATE" not in df.columns:
        return pd.DataFrame(columns=cols)
    ver = df[df["STATE"].astype(str).str.upper() == LEDGER_VERIFIED].copy()
    if ver.empty:
        return pd.DataFrame(columns=cols)
    ver["LEVER"] = ver.get("FINDING_TYPE", "unclassified").astype(str)
    ver["_V"] = pd.to_numeric(ver.get("VERIFIED_USD"), errors="coerce").fillna(0.0)
    ver["_E"] = pd.to_numeric(ver.get("ESTIMATED_USD"), errors="coerce").fillna(0.0)
    # Realization % compares like-for-like: only verified items that carried a positive
    # estimate count toward the numerator too, else a zero/absent-estimate verified item
    # adds its $ to the numerator with nothing in the denominator and inflates the ratio
    # past 100% — the exact guard ledger_totals uses (_est_pos). VERIFIED_USD/ITEMS stay
    # the full-lever totals for the $ column (bug-hunt 2026-08-30).
    ver["_VPOS"] = ver["_V"].where(ver["_E"] > 0, 0.0)
    grp = ver.groupby("LEVER").agg(VERIFIED_USD=("_V", "sum"), ITEMS=("_V", "size"),
                                   _VPOS=("_VPOS", "sum"), _EST=("_E", "sum")).reset_index()
    grp["REALIZATION_PCT"] = (grp["_VPOS"] / grp["_EST"].where(grp["_EST"] > 0)
                              * 100).round(0)
    grp["VERIFIED_USD"] = grp["VERIFIED_USD"].round(2)
    return grp.sort_values("VERIFIED_USD", ascending=False).reset_index(drop=True)[cols]


def since_last_visit_summary(new_alerts: object, new_crit: object,
                             new_high: object, new_actions: object) -> dict:
    """One-line 'what changed since your last visit' text + severity (Cost3).

    ``quiet`` is True when nothing alert- or action-worthy landed while the
    viewer was away, so the caller renders a calm note rather than an alarm.
    Severity escalates to ``bad`` on a new critical, ``warn`` on a new high or
    any new alert, else ``ok``."""
    na, nc = int(safe_float(new_alerts)), int(safe_float(new_crit))
    nh, nac = int(safe_float(new_high)), int(safe_float(new_actions))
    if na <= 0 and nac <= 0:
        return {"severity": "ok", "quiet": True, "text": "nothing new while you were away"}
    parts: list[str] = []
    if na > 0:
        extra = f" ({nc} critical)" if nc > 0 else (f" ({nh} high)" if nh > 0 else "")
        parts.append(f"{na} new alert{'' if na == 1 else 's'}{extra}")
    if nac > 0:
        parts.append(f"{nac} new action{'' if nac == 1 else 's'}")
    severity = "bad" if nc > 0 else "warn" if (nh > 0 or na > 0) else "ok"
    return {"severity": severity, "quiet": False, "text": "; ".join(parts)}


def _dedupe_alert_feed(alerts: pd.DataFrame) -> pd.DataFrame:
    """C2: drop the server-sweep events the app recomputes itself (see
    DUPLICATE_ALERT_RULE_IDS). Frames without a RULE_ID column pass through —
    an older reader that cannot tell the rules apart keeps every row rather
    than guessing which ones are twins."""
    if "RULE_ID" not in alerts.columns:
        return alerts
    rule = alerts["RULE_ID"].astype(str).str.upper().str.strip()
    return alerts[~rule.isin(DUPLICATE_ALERT_RULE_IDS)]


def _collapse_task_failures(task_failures: pd.DataFrame) -> pd.DataFrame:
    """D6: one row per TASK, not one per task-DAY.

    The Control Room reads FACT_TASK_DAILY over a 2-day window, which is DAY
    grain — a task failing on both days produced TWO queue rows that then ranked
    independently, so a single broken task could take two of the top slots and
    push a different fire off the bottom of the morning list. Worse, severity
    keyed off ONE day's count: two days of 2 failures each read as MEDIUM twice
    instead of the HIGH that 4 failures deserves. Sum FAILED across the window
    and keep the newest day's state/error, then rank once.
    """
    keys = [c for c in ("DATABASE_NAME", "SCHEMA_NAME", "TASK_NAME") if c in task_failures.columns]
    if not keys:
        return task_failures
    view = task_failures.copy()
    if "FAILED" in view.columns:
        view["FAILED"] = pd.to_numeric(view["FAILED"], errors="coerce").fillna(0.0)
    else:
        view["FAILED"] = 0.0
    view["FAILED"] = view.groupby(keys, dropna=False)["FAILED"].transform("sum")
    # Sort oldest-first so keep="last" retains the most recent day's LAST_ERROR /
    # LAST_STATE; frames without a DAY column (the live TASK_HISTORY fallback is
    # already one row per task) sort as all-NaT and collapse to a no-op.
    view["_ORDER"] = (pd.to_datetime(view["DAY"], errors="coerce") if "DAY" in view.columns
                      else pd.Series(pd.NaT, index=view.index, dtype="datetime64[ns]"))
    view = view.sort_values("_ORDER", na_position="first")
    return view.drop_duplicates(subset=keys, keep="last").drop(columns=["_ORDER"])


def triage_queue(
    alerts: pd.DataFrame | None,
    task_failures: pd.DataFrame | None,
    anomalies: list[dict] | None,
    high_excess_usd: float = ANOMALY_HIGH_EXCESS_USD,
) -> pd.DataFrame:
    """Merge alert events, task failures, and spend anomalies into one ranked
    morning-triage queue for the Control Room. (Restored 2026-07-13 — the
    owner meant resource monitors, not task monitoring.)

    ``anomalies`` rows may carry ``excess_usd`` (spend over that series' own
    robust baseline); when present it drives both escalation and ranking (D6).
    """
    rows: list[dict] = []
    # N3: carry EVENT_ID / RULE_ID (empty for non-alert kinds) so the Control Room
    # queue can link a row back to its origin instead of being a read-only wall.
    if alerts is not None and not alerts.empty:
        for _, r in _dedupe_alert_feed(alerts).iterrows():
            rows.append({
                "SEVERITY": str(r.get("SEVERITY", "MEDIUM")).upper(),
                "KIND": "Alert",
                "DATABASE": "",
                "TITLE": str(r.get("TITLE", "Alert event")),
                "DETAIL": str(r.get("DETAIL", ""))[:220],
                "SOURCE": "ALERT_EVENTS",
                "RAISED_AT": r.get("RAISED_AT"),
                "EVENT_ID": str(r.get("EVENT_ID", "") or ""),
                "RULE_ID": str(r.get("RULE_ID", "") or ""),
                "WAREHOUSE": "",
                # Alerts carry no comparable dollar figure; 0.0 keeps them in the
                # severity band but behind any priced row (see the sort below).
                "_USD": 0.0,
            })
    if task_failures is not None and not task_failures.empty:
        for _, r in _collapse_task_failures(task_failures).iterrows():
            failed = int(float(r.get("FAILED", 0) or 0))
            if failed <= 0:
                continue
            database = str(r.get("DATABASE_NAME", "") or "")
            schema = str(r.get("SCHEMA_NAME", "") or "")
            qualified = ".".join(p for p in (database, schema) if p)
            rows.append({
                "SEVERITY": "HIGH" if failed >= 3 else "MEDIUM",
                "KIND": "Task failure",
                "DATABASE": database,
                "TITLE": f"{qualified + '.' if qualified else ''}{r.get('TASK_NAME', 'task')} failed {failed}x",
                "DETAIL": str(r.get("LAST_ERROR", "") or "")[:220],
                "SOURCE": "FACT_TASK_DAILY",
                "RAISED_AT": r.get("DAY"),
                "EVENT_ID": "",
                "RULE_ID": "",
                "WAREHOUSE": "",
                # A failed loader has a real cost, but not one this row can price.
                "_USD": 0.0,
            })
    for a in anomalies or []:
        z = float(a.get("z", 0) or 0.0)
        label = a.get("label", "warehouse")
        value = a.get("value", 0)
        # D6: dollars over the series' own robust baseline. Callers that predate
        # the field (or cannot compute a baseline) leave it out and get 0.0, which
        # degrades this row to the pure-z behavior — never to a false escalation.
        excess = abs(float(a.get("excess_usd", 0.0) or 0.0))
        _excess_note = f" ${excess:,.0f} off baseline." if excess > 0 else ""
        # N10: a spend COLLAPSE (z << 0) is usually a stalled loader / dead
        # pipeline — a real outage signal — not the same thing as a spend SPIKE.
        # Give it a distinct identity so a DBA doesn't triage it like an overspend.
        if z < 0:
            kind = "Spend collapse"
            title = f"{label} daily spend collapsed z={z:+.1f}"
            detail = (f"Daily spend ${value:,.0f} far below robust baseline — "
                      "possible stalled workload / dead pipeline." + _excess_note)
        else:
            kind = "Spend anomaly"
            title = f"{label} daily spend z={z:+.1f}"
            detail = f"Daily spend ${value:,.0f} vs robust baseline." + _excess_note
        rows.append({
            # D6/C2: shape OR money. A z-score alone is $-blind — z=8 on a $12/day
            # sandbox warehouse outranked z=4 on a $9k/day production one. See
            # ANOMALY_HIGH_Z / ANOMALY_HIGH_EXCESS_USD for why the z gate moved 5 -> 7.
            "SEVERITY": "HIGH" if (abs(z) >= ANOMALY_HIGH_Z or excess >= high_excess_usd) else "MEDIUM",
            "KIND": kind,
            "DATABASE": "",
            "TITLE": title,
            "DETAIL": detail,
            "SOURCE": "FACT_WAREHOUSE_DAILY",
            # r6-bug5: stamp the anomaly's own day (was hardcoded None -> a dateless HIGH
            # indistinguishable from an overnight break). Normalized to text below.
            "RAISED_AT": a.get("day"),
            "EVENT_ID": "",
            "RULE_ID": "",
            # rec22: carry the offending warehouse so triage can route the spend
            # spike to that warehouse instead of restarting account-wide.
            "WAREHOUSE": str(label),
            "_USD": excess,
        })
    if not rows:
        return pd.DataFrame()
    queue = pd.DataFrame(rows)
    # Sources mix timestamps, dates, and None here; Arrow serialization in
    # st.dataframe requires one type, so render RAISED_AT as text.
    queue["RAISED_AT"] = queue["RAISED_AT"].map(
        lambda v: "" if v is None or pd.isna(v) else str(v)
    )
    queue["_SEV"] = queue["SEVERITY"].map(SEVERITY_RANK).fillna(9)
    # D6: within a severity band, rank by DOLLARS at risk — the old tiebreak was
    # alphabetical on KIND, so "Alert" always beat "Spend anomaly" and a $9k/day
    # spend break sat below a cosmetic alert purely because 'A' sorts before 'S'.
    # Unpriced rows carry 0.0 and keep KIND as the final, stable tiebreak so the
    # order is deterministic across reruns.
    queue = (queue.sort_values(["_SEV", "_USD", "KIND"], ascending=[True, False, True])
             .drop(columns=["_SEV", "_USD"]).reset_index(drop=True))
    return queue
