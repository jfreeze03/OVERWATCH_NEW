"""Prove-it scorecard — does OVERWATCH work, and does it pay for itself?

The owner's gate before autonomy: hard numbers that the advising features are correct
and valuable. Nearly every input already exists as a pure function or mart builder
scattered across four pages (savings realization on Proof, formerly Decision Studio ▸ ROI;
remediation acceptance in Admin; per-rule alert precision in Alerts; evidence coverage from the
query-family portfolio, now Operations ▸ Optimize). This module adds only the three
aggregates none of them provided — an account-wide alert precision roll-up, an
action-acceptance rate, and the ROI multiple — and a one-line verdict that composes the
five proof signals. Pure pandas; no Streamlit,
no Snowflake. Tested in tests/test_proof.py.

A "None" ratio means NO DATA (nothing decided/resolved yet), never 0% — an honest blank
so an empty ledger doesn't read as "0% precise".
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pandas as pd

from app.logic.actions import (
    LEDGER_ESTIMATED,
    LEDGER_REJECTED,
    LEDGER_VERIFIED,
    split_reverted,
    split_superseded,
)
from app.logic.formulas import ACCOUNT_TIMEZONE, account_today, format_usd, safe_float


def account_precision(rule_precision_df: pd.DataFrame | None) -> dict:
    """Roll per-rule precision up to ONE fleet number: when any rule fires, how often
    is it real. ``rule_precision_df`` is mart_sql.rule_precision output (ACTIONED, NOISE,
    EXPECTED, UNTAGGED, RESOLVED_EVENTS). Precision = ACTIONED/(ACTIONED+NOISE), EXPECTED
    excluded from the denominator (a known-maintenance close is neither a hit nor noise).
    UNTAGGED_SHARE is the trust caveat — a high untagged share means the number isn't yet
    reliable (events closed without a kind)."""
    empty = {"PRECISION_PCT": None, "ACTIONED": 0, "NOISE": 0, "EXPECTED": 0,
             "UNTAGGED": 0, "RESOLVED": 0, "UNTAGGED_SHARE_PCT": 0.0, "RULES": 0}
    if rule_precision_df is None or rule_precision_df.empty:
        return empty

    def _col(name: str) -> float:
        if name not in rule_precision_df.columns:
            return 0.0
        return float(pd.to_numeric(rule_precision_df[name], errors="coerce").fillna(0).sum())

    actioned, noise = _col("ACTIONED"), _col("NOISE")
    expected, untagged = _col("EXPECTED"), _col("UNTAGGED")
    resolved = _col("RESOLVED_EVENTS") or (actioned + noise + expected + untagged)
    denom = actioned + noise
    return {
        "PRECISION_PCT": round(100.0 * actioned / denom, 1) if denom > 0 else None,
        "ACTIONED": int(actioned), "NOISE": int(noise), "EXPECTED": int(expected),
        "UNTAGGED": int(untagged), "RESOLVED": int(resolved),
        "UNTAGGED_SHARE_PCT": round(100.0 * untagged / resolved, 1) if resolved > 0 else 0.0,
        "RULES": len(rule_precision_df),
    }


def acceptance_summary(row: pd.DataFrame | pd.Series | dict | None) -> dict:
    """Of the recommendations the team DECIDED on, how many did they act on vs dismiss.
    ``row`` is the one-row mart_sql.action_acceptance output (DONE_N, DROPPED_N, OPEN_N,
    DONE_USD). Acceptance = DONE/(DONE+DROPPED); OPEN is the still-undecided backlog (not
    counted for/against). None acceptance when nothing has been decided yet."""
    if isinstance(row, pd.DataFrame):
        row = row.iloc[0].to_dict() if not row.empty else {}
    elif isinstance(row, pd.Series):
        row = row.to_dict()
    row = row or {}
    done = int(safe_float(row.get("DONE_N")))
    dropped = int(safe_float(row.get("DROPPED_N")))
    decided = done + dropped
    return {
        "DONE_N": done, "DROPPED_N": dropped, "OPEN_N": int(safe_float(row.get("OPEN_N"))),
        "DECIDED": decided,
        "ACCEPTANCE_PCT": round(100.0 * done / decided, 1) if decided > 0 else None,
        "DONE_USD": round(safe_float(row.get("DONE_USD")), 2),
    }


def roi_multiple(verified_usd: float, run_cost_usd: float) -> dict:
    """Does OVERWATCH pay for itself: verified savings as a multiple of its own run cost.
    RATIO None when run cost is unknown/zero (can't divide). PAYS = ratio >= 1.

    ``verified_usd`` is the ACTIVE verified monthly run-rate (mart_sql.savings_summary_quarter
    VERIFIED_ACTIVE_MONTHLY_USD), never a quarter-to-date sum — a QTD numerator reset to 0x on the
    first day of every quarter (Next-Fifty #3)."""
    verified = safe_float(verified_usd)
    run_cost = safe_float(run_cost_usd)
    ratio = round(verified / run_cost, 1) if run_cost > 0 else None
    return {
        "VERIFIED_USD": round(verified, 2), "RUN_COST_USD": round(run_cost, 2),
        "NET_USD": round(verified - run_cost, 2),
        "RATIO": ratio, "PAYS": bool(ratio is not None and ratio >= 1.0),
    }


# Trust bands for the headline verdict (uncalibrated starting points).
_MIN_PRECISION = 70.0        # below this, alerts are crying wolf too often
_MIN_REALIZATION = 60.0      # below this, estimates are not holding up
_MIN_ACCEPTANCE = 40.0       # below this, the team is ignoring the advice
_MAX_UNTAGGED_SHARE = 40.0   # above this, precision isn't trustworthy yet


def proof_verdict(roi: dict, realization_pct: float | None, acceptance_pct: float | None,
                  precision: dict) -> dict:
    """One-line headline: is OVERWATCH earning its keep? Composes the five signals into a
    level (good | watch | unproven) and the worst-first reasons. 'unproven' means not
    enough labeled evidence yet (the honest state early on), not failure."""
    reasons: list[str] = []
    level = "good"

    # Unproven: the tool can't be judged without labeled outcomes.
    if roi.get("RATIO") is None and realization_pct is None and precision.get("PRECISION_PCT") is None:
        return {"level": "unproven",
                "headline": "Not enough verified outcomes yet to prove value — "
                            "resolve alerts with a kind and verify savings to build the record.",
                "reasons": []}

    if roi.get("RATIO") is not None and not roi.get("PAYS"):
        reasons.append(f"run cost not yet covered ({roi['RATIO']:.1f}x)")
        level = "watch"
    if realization_pct is not None and realization_pct < _MIN_REALIZATION:
        reasons.append(f"low realization ({realization_pct:.0f}%)")
        level = "watch"
    prec = precision.get("PRECISION_PCT")
    if prec is not None and prec < _MIN_PRECISION:
        reasons.append(f"alert precision {prec:.0f}%")
        level = "watch"
    if acceptance_pct is not None and acceptance_pct < _MIN_ACCEPTANCE:
        reasons.append(f"team acts on only {acceptance_pct:.0f}%")
        level = "watch"
    if precision.get("UNTAGGED_SHARE_PCT", 0) > _MAX_UNTAGGED_SHARE:
        reasons.append(f"{precision['UNTAGGED_SHARE_PCT']:.0f}% of alerts unlabeled — precision not yet trustworthy")
        # Downgrade to watch like every other signal: the 'good' headline is composed from `bits`
        # (which cites the precision number) and never renders `reasons`, so without this the caveat
        # that precision itself isn't trustworthy would be silently dropped and the flagship verdict
        # would headline that untrustworthy precision as proof (ds-hunt 2026-08-30).
        level = "watch"

    if level == "good":
        bits = []
        if roi.get("RATIO") is not None:
            bits.append(f"pays for itself {roi['RATIO']:.1f}x")
        if prec is not None:
            bits.append(f"{prec:.0f}% alert precision")
        if realization_pct is not None:
            bits.append(f"{realization_pct:.0f}% realization")
        if acceptance_pct is not None:
            bits.append(f"the team acts on {acceptance_pct:.0f}%")
        # name what is NOT measured yet, so a 'good' verdict never implies proof it does not have
        missing = [name for name, v in (("ROI multiple (run cost)", roi.get("RATIO")), ("realization", realization_pct),
                                        ("alert precision", prec), ("team follow-through", acceptance_pct))
                   if v is None]
        headline = ("OVERWATCH is earning its keep" + (" — " + ", ".join(bits) if bits else "")
                    + (f" (not yet measured: {', '.join(missing)})" if missing else "") + ".")
    else:
        headline = "OVERWATCH is providing value, but watch: " + "; ".join(reasons) + "."
    return {"level": level, "headline": headline, "reasons": reasons}


# ---------------------------------------------------------------------------
# v4.597 (Option C) — Proof ▸ per-item evidence. Pure helpers over
# mart_sql.savings_ledger (the per-row ledger + its linked change) and
# mart_sql.ledger_attribution (who gets credit + the uncapped window totals).
# ---------------------------------------------------------------------------

# mart_sql.ledger_attribution ATTRIBUTION class -> sentence-case label.
ATTRIBUTION_LABELS: dict[str, str] = {
    "OVERWATCH_EXECUTED": "Executed by OVERWATCH",
    "OVERWATCH_RECOMMENDED": "Recommended by OVERWATCH, executed elsewhere",
    "OVERWATCH_BOOKED": "Booked in OVERWATCH",
    "DETECTED_ELSEWHERE": "Detected elsewhere",
    "EXPERIMENT": "Experiment (verified by hand)",
}

EVIDENCE_COLUMNS: tuple[str, ...] = (
    "STATE", "VERIFIED_USD", "LEVER", "TARGET", "CHANGE", "VERDICT", "MEASURED_AFTER_DAYS",
    "WINDOW", "ATTRIBUTION", "FLAGS", "VERIFIED_AT",
)

_UNJUDGED_VERDICTS = ("NO_BASELINE", "INSUFFICIENT_AFTER")
_FULL_WINDOW_NOTE = "full window"          # V153 settle note: "measured on the full window: ..."
# the owner's O-8 re-settle (snowflake/resettle_autobook_14d.sql grid 2) restates a pre-V153 row on its closed
# 14-day window and appends this sentinel -- a full window too (review r1)
_RESETTLED_NOTE = "re-settled on the full 14-day window"
_NOT_MEASURABLE_NOTE = "not measurable"    # V153 close-out note (no metered credits after the change)
_CO_ATTRIBUTED_NOTE = "LBA-1 co-attributed"


def _naive_ts(values: pd.Series) -> pd.Series:
    """Timestamps as tz-NAIVE account (Central) wall time, element by element. TIMESTAMP_LTZ columns
    come back tz-aware while the ledger's NTZ columns are naive; a vectorised to_datetime over a mix
    silently coerces one kind to NaT, and comparing/sorting the two kinds raises."""
    def _one(value: object) -> pd.Timestamp | None:
        if value is None:
            return None
        try:
            if bool(pd.isna(value)):
                return None
        except (TypeError, ValueError):
            pass
        try:
            ts = pd.Timestamp(value)
        except (TypeError, ValueError):
            return None
        if ts.tzinfo is not None:
            ts = ts.tz_convert(ACCOUNT_TIMEZONE).tz_localize(None)
        return ts

    return pd.to_datetime(pd.Series([_one(v) for v in values], index=values.index, dtype="object"),
                          errors="coerce")


def _col(frame: pd.DataFrame, name: str, default: object = None) -> pd.Series:
    return frame[name] if name in frame.columns else pd.Series(default, index=frame.index, dtype="object")


def _truthy(value: object) -> bool:
    """A Snowflake BOOLEAN cell as read back (True / 'TRUE' / 1) -> True; NULL / NaN / other -> False."""
    if isinstance(value, str):
        return value.strip().upper() in ("TRUE", "1", "YES")
    try:
        return False if value is None or bool(pd.isna(value)) else bool(value)
    except (TypeError, ValueError):
        return False


def _text(value: object) -> str:
    """A cell as stripped text; NULL / NaN / NaT -> ''."""
    if value is None:
        return ""
    try:
        if bool(pd.isna(value)):
            return ""
    except (TypeError, ValueError):
        pass
    return str(value).strip()


def _is_auto(frame: pd.DataFrame) -> pd.Series:
    """A change-scan row: SOURCE_CHANGE_ID set, or (older reads) the derived SOURCE == 'auto'."""
    cid = _col(frame, "SOURCE_CHANGE_ID").map(_text)
    return cid.ne("") | _col(frame, "SOURCE", "").map(_text).str.lower().eq("auto")


def ledger_with_attribution(ledger_df: pd.DataFrame | None,
                            attribution_df: pd.DataFrame | None) -> pd.DataFrame:
    """LEFT-merge mart_sql.ledger_attribution onto the savings_ledger frame on ITEM_ID (ledger row order
    kept). Only columns the ledger lacks are taken, so ledger values (e.g. CHANGE_VERDICT) always win.
    A missing / failed attribution read returns the ledger unchanged."""
    if ledger_df is None:
        return pd.DataFrame()
    base = ledger_df.copy()
    if (attribution_df is None or attribution_df.empty or "ITEM_ID" not in attribution_df.columns
            or "ITEM_ID" not in base.columns):
        return base
    extra = ["ITEM_ID"] + [c for c in attribution_df.columns if c != "ITEM_ID" and c not in base.columns]
    attr = attribution_df[extra].drop_duplicates("ITEM_ID")
    return base.merge(attr, on="ITEM_ID", how="left")


def _short_date(value: date) -> str:
    return f"{value:%b} {value.day}"


def _window_state(row: pd.Series, auto: bool, today: date) -> str | None:
    """WINDOW for one row — change-scan rows only (app-booked / experiment rows have no measured window)."""
    if not auto:
        return None
    state = _text(row.get("STATE")).upper()
    notes = _text(row.get("NOTES"))
    if state == LEDGER_ESTIMATED:
        until = row.get("_TRACKING_UNTIL")
        if until is None or pd.isna(until):
            return "settles after its 14-day window"
        until_day = pd.Timestamp(until).date()
        settle = until_day + timedelta(days=1)       # the first daily scan after TRACKING_UNTIL settles it
        if settle >= today:
            return f"settles ~{_short_date(settle)}"
        return f"awaiting settle (window closed {_short_date(until_day)})"
    if _FULL_WINDOW_NOTE in notes or _RESETTLED_NOTE in notes:
        return "full 14-day window"
    if _NOT_MEASURABLE_NOTE in notes:
        return "not measurable"
    return "short window (pre-V153)"


def _undone_value(value: object, setting: object) -> str:
    """A registry value as shown in a booked change or a revert. AUTO_SUSPEND blank OR <= 0 is 'never' —
    the SQL cost rank's own rule (mart_sql._setting_cost_rank_sql ranks NULL / <= 0 as never suspends;
    V109 stores TRY_TO_NUMBER(auto_suspend), so SHOW's 'never' reads NULL and a 0 reads '0'); any other
    blank is '?'. Text only — the shaped harness feeds floats."""
    text = _text(value)
    if _text(setting).upper() == "AUTO_SUSPEND" and (not text or safe_float(text, default=1.0) <= 0):
        return "never"
    return text or "?"


def _revert_setting(row: pd.Series) -> tuple[str, bool]:
    """(the setting the undoing change touched, inherited?) — REVERT_SETTING, else the row's own
    CHANGE_SETTING. Inherited (review r1 F1): a co-attributed partner's change was undone (one measured
    window), so the carried revert names ANOTHER setting than the booked one."""
    own = _text(row.get("CHANGE_SETTING")).upper()
    by = _text(row.get("REVERT_SETTING")).upper()
    return (by or own), bool(by and own and by != own)


def _flags(row: pd.Series) -> str | None:
    out: list[str] = []
    # Next-Fifty #31: the revert reads FIRST — the saving no longer counts toward the run-rate
    _rv_at = row.get("_REVERTED_AT")
    if _rv_at is not None and not pd.isna(_rv_at):
        _by, _inherited = _revert_setting(row)
        _to = _undone_value(row.get("REVERT_NEW_VALUE"), _by)
        _kind = "partly reverted" if _text(row.get("REVERT_KIND")).lower() == "partial" else "reverted"
        _when = _short_date(pd.Timestamp(_rv_at).date())
        out.append(f"{_kind} {_when} (co-attributed {_by} → {_to})" if _inherited else f"{_kind} {_when} → {_to}")
    if _truthy(row.get("VOLUME_CONFOUNDED")):
        out.append("volume-confounded")
    verdict = _text(row.get("CHANGE_VERDICT")).upper()
    state = _text(row.get("STATE")).upper()
    if verdict == "REGRESSED":
        out.append("cheaper but slower" if state == LEDGER_VERIFIED else "performance regressed")
    elif verdict in _UNJUDGED_VERDICTS:
        out.append("performance unjudged")
    if _CO_ATTRIBUTED_NOTE in _text(row.get("NOTES")):
        out.append("co-attributed $0")
    return " · ".join(out) if out else None


def _change_text(old: pd.Series, new: pd.Series, setting: pd.Series) -> list[str | None]:
    """'old → new' per row (an AUTO_SUSPEND blank or <= 0 reads 'never', other blanks '?'); None when both
    are blank."""
    out: list[str | None] = []
    for o, n, s in zip(old, new, setting, strict=True):
        if not _text(o) and not _text(n):
            out.append(None)
        else:
            out.append(f"{_undone_value(o, s)} → {_undone_value(n, s)}")
    return out


def evidence_rows(ledger_df: pd.DataFrame | None, attribution_df: pd.DataFrame | None,
                  today: date | None = None) -> pd.DataFrame:
    """What each ledger saving rests on — one row per LIVE ledger item (superseded manual twins are
    excluded, as from every total), sorted by VERIFIED_USD desc (NULL last). Columns EVIDENCE_COLUMNS:
      STATE, VERIFIED_USD (monthly run-rate; NULL -> "—"), LEVER (FINDING_TYPE), TARGET (TARGET_OBJECT,
      else the linked change's warehouse), CHANGE ("old → new" from the linked registry row), VERDICT
      (the change scan's verdict), MEASURED_AFTER_DAYS,
      WINDOW — change-scan rows only: "full 14-day window" (V153 settle), "settles ~<TRACKING_UNTIL+1>"
        (still measuring), "short window (pre-V153)" (settled on ~3 days, never rewritten) or
        "not measurable" (closed with no metered credits after the change); NULL on app-booked rows,
      ATTRIBUTION — the ledger_attribution class as a sentence-case label (NULL when that read failed),
      FLAGS — first "reverted <Mon d> → <value>" / "partly reverted <Mon d> → <value>" (Next-Fifty #31:
        the daily scan saw the booked change undone on that day, so the saving left the run-rate; the
        row is KEPT here, flagged; "partly reverted <Mon d> (co-attributed <SETTING> → <value>)" when a
        change the same scan saw with it was undone — one measured window), then "volume-confounded",
        "cheaper but slower" (REGRESSED yet saved), "performance unjudged" (NO_BASELINE /
        INSUFFICIENT_AFTER), "co-attributed $0" (LBA-1), joined " · "; NULL when none,
      VERIFIED_AT — tz-naive account time.
    Row-level display only: headline totals come from the SQL window columns (evidence_split)."""
    if ledger_df is None or ledger_df.empty or "STATE" not in ledger_df.columns:
        return pd.DataFrame(columns=list(EVIDENCE_COLUMNS))
    today = today or account_today()
    live, _ = split_superseded(ledger_with_attribution(ledger_df, attribution_df))
    if live.empty:
        return pd.DataFrame(columns=list(EVIDENCE_COLUMNS))
    view = live.reset_index(drop=True).copy()
    view["_TRACKING_UNTIL"] = _naive_ts(_col(view, "TRACKING_UNTIL")).to_numpy()
    view["_REVERTED_AT"] = _naive_ts(_col(view, "REVERTED_AT")).to_numpy()
    auto = _is_auto(view)
    target = _col(view, "TARGET_OBJECT").map(_text)
    target = target.where(target.ne(""), _col(view, "CHANGE_WAREHOUSE").map(_text))
    # the same 'old → new' text as the Reverted savings list (an AUTO_SUSPEND blank / <= 0 reads 'never')
    change = _change_text(_col(view, "CHANGE_OLD_VALUE"), _col(view, "CHANGE_NEW_VALUE"),
                          _col(view, "CHANGE_SETTING"))
    verdict = _col(view, "CHANGE_VERDICT").map(_text).str.upper()
    attribution = _col(view, "ATTRIBUTION").map(_text).str.upper().map(ATTRIBUTION_LABELS)
    lever = _col(view, "FINDING_TYPE").map(_text)
    out = pd.DataFrame({
        "STATE": _col(view, "STATE").map(_text).str.upper(),
        "VERIFIED_USD": pd.to_numeric(_col(view, "VERIFIED_USD"), errors="coerce"),
        "LEVER": lever.where(lever.ne(""), None),
        "TARGET": target.where(target.ne(""), None),
        "CHANGE": change,
        "VERDICT": verdict.where(verdict.ne(""), None),
        "MEASURED_AFTER_DAYS": pd.to_numeric(_col(view, "MEASURED_AFTER_DAYS"), errors="coerce"),
        "WINDOW": [_window_state(view.iloc[i], bool(auto.iloc[i]), today) for i in range(len(view))],
        "ATTRIBUTION": attribution.where(attribution.notna(), None),
        "FLAGS": [_flags(view.iloc[i]) for i in range(len(view))],
        "VERIFIED_AT": _naive_ts(_col(view, "VERIFIED_AT")).to_numpy(),
    })
    return out.sort_values("VERIFIED_USD", ascending=False, na_position="last",
                           kind="stable").reset_index(drop=True)


REVERTED_COLUMNS: tuple[str, ...] = ("TARGET", "LEVER", "CHANGE", "REVERTED_AT", "REVERTED_TO", "REVERT",
                                     "STATE", "VERIFIED_USD")


def reverted_rows(ledger_df: pd.DataFrame | None) -> pd.DataFrame:
    """Next-Fifty #31: the live (non-superseded) ledger rows whose booked change the daily scan later saw
    undone — the Proof 'Reverted savings' list. CHANGE = booked 'old → new'; REVERTED_TO = the undoing
    change 'old → new', prefixed '<SETTING>: ' when it was a co-attributed partner's change (review r1 F1:
    one measured window, so undoing the partner stales this row's saving too); REVERT = 'Full' / 'Partial';
    VERIFIED_USD = the $/mo that left the run-rate (NULL -> '—'); REVERTED_AT tz-naive account time.
    Newest revert first. Display only — never a total."""
    if ledger_df is None or ledger_df.empty or "STATE" not in ledger_df.columns:
        return pd.DataFrame(columns=list(REVERTED_COLUMNS))
    live, _ = split_superseded(ledger_df)
    _, reverted = split_reverted(live)
    if reverted.empty:
        return pd.DataFrame(columns=list(REVERTED_COLUMNS))
    view = reverted.reset_index(drop=True)
    setting = _col(view, "CHANGE_SETTING")
    by = [_revert_setting(view.iloc[i]) for i in range(len(view))]
    undone = _change_text(_col(view, "REVERT_OLD_VALUE"), _col(view, "REVERT_NEW_VALUE"),
                          pd.Series([b for b, _ in by], index=view.index, dtype="object"))
    undone = [f"{b}: {t}" if inherited and t else t for t, (b, inherited) in zip(undone, by, strict=True)]
    target = _col(view, "TARGET_OBJECT").map(_text)
    target = target.where(target.ne(""), _col(view, "CHANGE_WAREHOUSE").map(_text))
    lever = _col(view, "FINDING_TYPE").map(_text)
    kind = _col(view, "REVERT_KIND").map(_text).str.lower()
    out = pd.DataFrame({
        "TARGET": target.where(target.ne(""), None),
        "LEVER": lever.where(lever.ne(""), None),
        "CHANGE": _change_text(_col(view, "CHANGE_OLD_VALUE"), _col(view, "CHANGE_NEW_VALUE"), setting),
        "REVERTED_AT": _naive_ts(_col(view, "REVERTED_AT")).to_numpy(),
        "REVERTED_TO": undone,
        "REVERT": kind.map({"full": "Full", "partial": "Partial"}),
        "STATE": _col(view, "STATE").map(_text).str.upper(),
        "VERIFIED_USD": pd.to_numeric(_col(view, "VERIFIED_USD"), errors="coerce"),
    })
    out["REVERT"] = out["REVERT"].where(out["REVERT"].notna(), None)
    return out.sort_values("REVERTED_AT", ascending=False, na_position="last",
                           kind="stable").reset_index(drop=True)[list(REVERTED_COLUMNS)]


# evidence_split key -> mart_sql.ledger_attribution window column (pivoted onto every row).
_SPLIT_COLUMNS: dict[str, str] = {
    "active_usd": "ACTIVE_USD",
    "executed_usd": "EXECUTED_ACTIVE_USD",
    "recommended_usd": "RECOMMENDED_ACTIVE_USD",
    "booked_usd": "BOOKED_ACTIVE_USD",
    "elsewhere_usd": "ELSEWHERE_ACTIVE_USD",
    "experiment_usd": "EXPERIMENT_ACTIVE_USD",
    "regressed_usd": "REGRESSED_ACTIVE_USD",
    "neutral_usd": "NEUTRAL_ACTIVE_USD",
    "short_window_usd": "SHORT_WINDOW_ACTIVE_USD",
}


def evidence_split(attribution_df: pd.DataFrame | None) -> dict:
    """The attribution split of the ACTIVE verified run-rate (the ROI numerator's rows), read straight
    from mart_sql.ledger_attribution's SQL window columns — NEVER summed in pandas, so it is whole-ledger
    even when run() row-caps the frame (every row carries the same pivoted totals). Returns {} when the
    read is missing/empty/older-shaped, else the _SPLIT_COLUMNS $ keys plus active_items, total_items
    and ``capped`` (the frame holds fewer rows than the ledger)."""
    if attribution_df is None or attribution_df.empty:
        return {}
    if not set(_SPLIT_COLUMNS.values()).issubset(attribution_df.columns):
        return {}
    first = attribution_df.iloc[0]
    out: dict = {key: round(safe_float(first.get(col)), 2) for key, col in _SPLIT_COLUMNS.items()}
    out["active_items"] = int(safe_float(first.get("ACTIVE_ITEMS")))
    total = int(safe_float(first.get("TOTAL_ITEMS")))
    out["total_items"] = total
    out["capped"] = bool(total > len(attribution_df))
    return out


SAVED_TO_DATE_COLUMNS: tuple[str, ...] = ("SAVED_TO_DATE_USD", "SAVED_MEASURED_USD",
                                          "SAVED_BEFORE_REVERT_USD", "SAVED_SINCE_DATE")


def _as_day(value: object) -> date | None:
    """A SQL DATE cell as a date: a date, datetime, Timestamp or ISO string. NULL / NaT, a float (the
    shaped harness) and anything unparseable -> None. NaT is a datetime subclass, so NULL is checked first."""
    if value is None or isinstance(value, bool | int | float):
        return None
    try:
        if bool(pd.isna(value)):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(value, datetime):              # pandas Timestamp is a datetime subclass
        return value.date()
    if isinstance(value, date):
        return value
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return pd.Timestamp(value.strip()).date()
    except (TypeError, ValueError):
        return None


def saved_to_date(summary_df: pd.DataFrame | None) -> dict | None:
    """Proof 'Saved to date' from mart_sql.savings_summary_quarter's row 0 — SQL aggregates over the WHOLE
    ledger, never a sum of the row-capped ledger frame. None when the frame is missing/empty/older-shaped
    (any SAVED_TO_DATE_COLUMNS absent). Else {total_usd, measured_usd (<= total), carried_usd = total -
    measured (>= 0), before_revert_usd, since: date | None}. Accrued dollars (each verified item's monthly
    saving / 30 x the whole days it has been in place), not a run-rate; it never feeds roi_multiple or
    proof_verdict."""
    if summary_df is None or summary_df.empty:
        return None
    if not set(SAVED_TO_DATE_COLUMNS).issubset(summary_df.columns):
        return None
    row = summary_df.iloc[0]
    total = round(safe_float(row.get("SAVED_TO_DATE_USD")), 2)
    # ROUND(SUM(..), 2) per column can leave measured a cent above the total: clamp, never a negative carry
    measured = round(min(safe_float(row.get("SAVED_MEASURED_USD")), total), 2)
    return {
        "total_usd": total,
        "measured_usd": measured,
        "carried_usd": round(max(0.0, total - measured), 2),
        "before_revert_usd": round(safe_float(row.get("SAVED_BEFORE_REVERT_USD")), 2),
        "since": _as_day(row.get("SAVED_SINCE_DATE")),
    }


SAVED_NOTHING_VERIFIED = "nothing verified yet"
SAVED_NOTHING_ACCRUED = ("nothing accrued yet — an item accrues from the day after it is verified or its "
                         "change is seen")


def saved_to_date_card(saved: dict | None, *, verified_any: bool) -> tuple[str, str]:
    """(value, delta) for the Proof 'Saved to date' card from saved_to_date(). DOLLARS, never '/mo' (the D4
    lesson: a cumulative figure must not read as a run-rate), and it always discloses the measured vs
    carried-forward split. None (the whole-ledger summary read failed) -> '—' and says so: there is no
    fallback to the row-capped ledger frame.

    ``verified_any`` — whether ANY verified item exists (the caller's count, reverted ones included). A $0
    total only reads "nothing verified yet" when none does (review r1 F2/F6/F21): an item hand-verified
    today has 0 whole days in effect, and an all-$0 record accrues nothing, while the run-rate card beside
    this one already counts them — so the zero state is neutral then, never "nothing verified"."""
    if saved is None:
        return "—", "whole-ledger summary unavailable"
    total = safe_float(saved.get("total_usd"))
    if total <= 0:
        return format_usd(total), (SAVED_NOTHING_ACCRUED if verified_any else SAVED_NOTHING_VERIFIED)
    since = saved.get("since")
    parts = [f"since {_short_date(since)}, {since.year}"] if isinstance(since, date) else []
    parts += [f"{format_usd(safe_float(saved.get('measured_usd')))} measured",
              f"{format_usd(safe_float(saved.get('carried_usd')))} carried forward at the verified rate"]
    before = safe_float(saved.get("before_revert_usd"))
    if before > 0:
        parts.append(f"incl. {format_usd(before)} saved before a change was undone")
    return format_usd(total), " · ".join(parts)


def carried_realization(rows: pd.DataFrame | None) -> dict | None:
    """Realization vs OVERWATCH's OWN up-front estimate, carried onto the auto-measured row (v4.597,
    open question 18). A SEPARATE, separately labelled figure — it never overwrites
    actions.ledger_totals' realization_pct (which only sees rows whose own ESTIMATED_USD > 0; autobook
    rows book $0 up front, so that ratio is usually None).

    ``rows`` is ledger_with_attribution(ledger, attribution). Per live row (superseded twins dropped):
      effective estimate = the first POSITIVE of ESTIMATED_USD (its own), TWIN_ESTIMATED_USD (the
      superseded manual twin's), REMEDIATION_EST_USD (the executed remediation's), REC_EST_USD (the idle
      alert's recommended $/mo) — all recorded BEFORE the change, no hindsight.
    Eligible: VERIFIED or REJECTED with a positive effective estimate, excluding LBA-1 co-attributed $0
    rows (their saving is booked once on the primary change; they would read as 0% realized). A REJECTED
    row with an estimate counts as 0 realized — a real miss, which realization_pct silently drops.
    Returns None when nothing is eligible, else carried_pct, realized_usd, estimated_usd, items,
    rejected_items, carried_items (estimate came from a twin / remediation / alert, not the row) and
    by_source counts. Keeps reverted rows — accuracy, not persistence (Next-Fifty #31): the measurement
    was real, and dropping undone items would bias the ratio toward the changes that survived."""
    if rows is None or rows.empty or "STATE" not in rows.columns:
        return None
    live, _ = split_superseded(rows)
    if live.empty:
        return None
    view = live.reset_index(drop=True)
    sources = (("own", "ESTIMATED_USD"), ("twin", "TWIN_ESTIMATED_USD"),
               ("remediation", "REMEDIATION_EST_USD"), ("recommendation", "REC_EST_USD"))
    est = pd.Series(float("nan"), index=view.index)
    src = pd.Series(None, index=view.index, dtype="object")
    for label, col in sources:
        vals = pd.to_numeric(_col(view, col), errors="coerce")
        take = est.isna() & vals.gt(0)
        est = est.where(~take, vals)
        src = src.where(~take, label)
    state = _col(view, "STATE").map(_text).str.upper()
    co_attributed = _col(view, "NOTES").map(_text).str.contains(_CO_ATTRIBUTED_NOTE, regex=False)
    eligible = state.isin((LEDGER_VERIFIED, LEDGER_REJECTED)) & est.gt(0) & ~co_attributed
    if not bool(eligible.any()):
        return None
    verified = pd.to_numeric(_col(view, "VERIFIED_USD"), errors="coerce").fillna(0.0)
    realized = verified.where(state.eq(LEDGER_VERIFIED), 0.0)
    est_total = float(est[eligible].sum())
    real_total = float(realized[eligible].sum())
    by_source = {label: int((eligible & src.eq(label)).sum()) for label, _ in sources}
    return {
        "carried_pct": round(real_total / est_total * 100.0, 1) if est_total > 0 else None,
        "realized_usd": round(real_total, 2),
        "estimated_usd": round(est_total, 2),
        "items": int(eligible.sum()),
        "rejected_items": int((eligible & state.eq(LEDGER_REJECTED)).sum()),
        "carried_items": int((eligible & src.ne("own")).sum()),
        "by_source": by_source,
    }


def settle_schedule(ledger_df: pd.DataFrame | None, today: date | None = None) -> dict:
    """When the change-scan rows still measuring will settle (v4.597, Proof ▸ Settling). Over LIVE
    ESTIMATED auto rows (superseded twins and, Next-Fifty #31, reverted rows dropped; auto =
    SOURCE_CHANGE_ID set or SOURCE 'auto'):
      pending — how many are still ESTIMATED,
      next    — the earliest settle day still ahead (TRACKING_UNTIL + 1; the first daily scan after the
                14-day window settles the row), or None,
      overdue — rows whose window closed before today but have not settled yet (awaiting the scan),
      undated — rows with no linked TRACKING_UNTIL (an unreadable registry link).
    Display-only counts over the frame passed in; never a $ total."""
    out: dict = {"pending": 0, "next": None, "overdue": 0, "undated": 0}
    if ledger_df is None or ledger_df.empty or "STATE" not in ledger_df.columns:
        return out
    today = today or account_today()
    live, _ = split_superseded(ledger_df)
    # Next-Fifty #31: a pending change the scan already saw undone is not "settling" toward the run-rate
    # (the same rule as actions.ledger_totals' auto_settle_pending_count, so the Settling card agrees)
    live, _ = split_reverted(live)
    if live.empty:
        return out
    view = live.reset_index(drop=True)
    pending = _col(view, "STATE").map(_text).str.upper().eq(LEDGER_ESTIMATED) & _is_auto(view)
    if not bool(pending.any()):
        return out
    until = _naive_ts(_col(view, "TRACKING_UNTIL"))[pending]
    settle = [(ts.date() + timedelta(days=1)) for ts in until.dropna()]
    ahead = [d for d in settle if d >= today]
    out.update({
        "pending": int(pending.sum()),
        "next": min(ahead) if ahead else None,
        "overdue": sum(1 for d in settle if d < today),
        "undated": int(until.isna().sum()),
    })
    return out
