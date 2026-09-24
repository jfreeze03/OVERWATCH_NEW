"""Health verdict for the opt-in EMAIL alert path (snowflake/native_alert_templates.sql) — pure.

Next-Fifty #4: the email path carries three out-of-band dead-man watchers, so a silent email path is
itself a risk. This reads SHOW ALERTS + INFORMATION_SCHEMA.ALERT_HISTORY + NOTIFICATION_HISTORY
(OVERWATCH_EMAIL) and says LIVE / FAILING / SUSPENDED / PARTIAL / NOT_VISIBLE / UNVERIFIABLE.

A frame of None means that READ failed. The verdict goes red ONLY on positive failure evidence (a send
failure, or an alert evaluation failure, newer than the last success); a privilege gap or an
uninstalled opt-in reads unverifiable / not visible — never a false red."""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd


@dataclass(frozen=True)
class EmailPathVerdict:
    state: str      # LIVE | FAILING | SUSPENDED | PARTIAL | NOT_VISIBLE | UNVERIFIABLE
    severity: str   # ok | warn | bad | info
    headline: str
    detail: str = ""


def _ts(value: object) -> pd.Timestamp | None:
    t = pd.to_datetime(value, errors="coerce")
    return None if t is None or pd.isna(t) else t


def _newer(fail: object, ok: object) -> bool:
    """True when a failure timestamp exists and no success is newer than it."""
    f, o = _ts(fail), _ts(ok)
    return f is not None and (o is None or f > o)


def _ts16(value: object) -> str:
    t = _ts(value)
    return "" if t is None else str(t)[:16]


def _col(df: pd.DataFrame, name: str) -> object:
    return df.iloc[0][name] if name in df.columns and not df.empty else None


def _failing(hist: pd.DataFrame | None, notif: pd.DataFrame | None) -> EmailPathVerdict | None:
    """FAILING on positive evidence only: the latest send failed with nothing succeeding since, or an
    alert's latest evaluation failed with no success since. None otherwise (incl. unreadable)."""
    if notif is not None and not notif.empty and _newer(_col(notif, "LAST_FAILED_AT"),
                                                        _col(notif, "LAST_SENT_AT")):
        return EmailPathVerdict(
            "FAILING", "bad",
            f"Email path FAILING — the latest OVERWATCH_EMAIL send failed at "
            f"{_ts16(_col(notif, 'LAST_FAILED_AT'))} and nothing has succeeded since",
            str(_col(notif, "LAST_ERROR") or "")[:300])
    if hist is not None and not hist.empty and {"NAME", "LAST_FAIL_AT", "LAST_OK_AT"} <= set(hist.columns):
        bad = [r for _, r in hist.iterrows() if _newer(r["LAST_FAIL_AT"], r["LAST_OK_AT"])]
        if bad:
            worst = max(bad, key=lambda r: _ts(r["LAST_FAIL_AT"]) or pd.Timestamp.min)
            return EmailPathVerdict(
                "FAILING", "bad",
                f"Email path FAILING — {worst['NAME']} failed its last evaluation at "
                f"{_ts16(worst['LAST_FAIL_AT'])} with no success since",
                str(worst.get("LAST_FAIL_ERROR") or "")[:300])
    return None


def email_path_verdict(alerts: pd.DataFrame | None, hist: pd.DataFrame | None,
                       notif: pd.DataFrame | None, expected: tuple[str, ...]) -> EmailPathVerdict:
    """alerts = SHOW ALERTS rows (NAME, STATE); hist = email_alert_history rows; notif =
    email_notification_history's one aggregate row. None = that read failed."""
    n_exp = len(expected)
    # RED first, on positive evidence only — even when SHOW ALERTS is unreadable or the alerts are owned
    # by another role, a readable send / evaluation failure newer than the last success is still red.
    red = _failing(hist, notif)
    if red is not None:
        return red
    if alerts is None:
        return EmailPathVerdict("UNVERIFIABLE", "info",
                                "Email path: unverifiable — the app role could not read SHOW ALERTS. "
                                "This is NOT evidence email is down.")
    name_col = next((c for c in alerts.columns if str(c).upper() == "NAME"), None)
    state_col = next((c for c in alerts.columns if str(c).upper() == "STATE"), None)
    if alerts.empty or name_col is None:
        return EmailPathVerdict("NOT_VISIBLE", "info",
                                "Email path: not installed, or its alerts are owned by a role the app "
                                "cannot see (opt-in — snowflake/native_alert_templates.sql).")
    visible = {str(r[name_col]).upper(): (str(r[state_col]).lower() if state_col else "")
               for _, r in alerts.iterrows()}
    wanted = [n.upper() for n in expected]
    started = [n for n in wanted if visible.get(n) == "started"]
    suspended = [n for n in wanted if n in visible and visible[n] != "started"]
    missing = [n for n in wanted if n not in visible]

    if not started:
        name = (suspended or wanted or ["<alert>"])[0]
        return EmailPathVerdict(
            "SUSPENDED", "warn",
            f"{len(suspended)} of {n_exp} email alerts suspended — "
            f"ALTER ALERT DBA_MAINT_DB.OVERWATCH.{name} RESUME")
    if suspended or missing:
        parts = []
        if suspended:
            parts.append("suspended: " + ", ".join(suspended))
        if missing:
            parts.append("not visible: " + ", ".join(missing))
        return EmailPathVerdict("PARTIAL", "warn",
                                f"{len(started)} of {n_exp} email alerts running ({'; '.join(parts)})")
    headline = f"Email path LIVE — {len(started)}/{n_exp} email alerts running"
    if notif is not None and not notif.empty:
        sent_n = int(pd.to_numeric(pd.Series([_col(notif, "SENT_N")]), errors="coerce").fillna(0).iloc[0])
        last = _ts16(_col(notif, "LAST_SENT_AT"))
        headline += (" · no email needed in 7d" if sent_n == 0 or not last else f" · last email {last}")
    if notif is None:
        headline += " (send history unverifiable)"
    if hist is None:
        headline += " (evaluation history unverifiable)"
    return EmailPathVerdict("LIVE", "ok", headline)
