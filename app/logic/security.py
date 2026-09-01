"""Pure Security scoring and coverage rules (read-only posture)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

import pandas as pd

from app.core.sqlsafe import sql_literal, sql_number
from app.logic.formulas import account_today, safe_float

# MART_SECURITY_POSTURE_DAILY metrics an operator can monitor (Sec35). All are counts
# of problems (higher = worse), so a monitor fires on VALUE >= threshold.
POSTURE_METRICS = (
    "MFA_GAP_USERS",
    "EXPIRED_CRED",
    "EXPIRING_CRED_10D",
    "ADMIN_STMTS_24H",
    "GRANT_CHANGES_24H",
    "UNUSED_ROLES_90D",
    "BREAKGLASS_GRANTS_30D",
    "WH_NO_MONITOR",
    "WH_NO_AUTOSUSPEND",
)

SECURITY_DOMAINS = (
    "IDENTITY",
    "PRIVILEGE",
    "CHANGE RISK",
    "DATA MOVEMENT",
    "TRUST CENTER",
)
_SEVERITY_PENALTY = {"CRITICAL": 25, "HIGH": 12, "MEDIUM": 5, "LOW": 2, "INFO": 0}


@dataclass(frozen=True)
class DomainPosture:
    domain: str
    score: int | None
    state: str
    findings: int
    coverage: str
    newest: object = None


def fact_coverage_complete(result: object, days: int, *, lag_days: int = 1) -> bool:
    """True only when a fact result proves both span and recent freshness."""
    if result is None or not bool(getattr(result, "usable", lambda: False)()):
        return False
    frame = getattr(result, "df", pd.DataFrame())
    if frame.empty:
        return False
    row = frame.iloc[0]
    coverage = int(safe_float(row.get("COVERAGE_DAYS")))
    last_day = pd.to_datetime(row.get("LAST_DAY"), errors="coerce")
    required = max(1, min(int(days or 1), 3650))
    return (
        coverage >= required
        and not pd.isna(last_day)
        and last_day.date() >= account_today() - timedelta(days=max(0, int(lag_days)))
    )


def domain_posture(exceptions: pd.DataFrame, coverage: pd.DataFrame) -> tuple[DomainPosture, ...]:
    """Score only domains whose evidence source explicitly reports coverage."""
    exc = exceptions.copy() if exceptions is not None else pd.DataFrame()
    cov = coverage.copy() if coverage is not None else pd.DataFrame()
    rows: list[DomainPosture] = []
    for domain in SECURITY_DOMAINS:
        c = cov[cov.get("DOMAIN", pd.Series(dtype=str)).astype(str).str.upper() == domain]
        status = str(c.iloc[0].get("COVERAGE", "UNKNOWN") if not c.empty else "UNKNOWN").upper()
        newest = c.iloc[0].get("NEWEST") if not c.empty else None
        one = exc[exc.get("DOMAIN", pd.Series(dtype=str)).astype(str).str.upper() == domain]
        if "IMPACT_COUNT" in one:
            impacts = pd.to_numeric(one["IMPACT_COUNT"], errors="coerce").fillna(1).clip(lower=1)
            findings = int(impacts.sum())
        else:
            impacts = pd.Series(1, index=one.index, dtype="float64")
            findings = len(one)
        if status != "COMPLETE":
            state = {
                "ON_DEMAND": "On demand",
                "NOT_CONFIGURED": "Not configured",
            }.get(status, "Unknown")
            rows.append(DomainPosture(domain, None, state, findings, status, newest))
            continue
        penalty = sum(
            _SEVERITY_PENALTY.get(str(severity or "").upper(), 5)
            * min(3, max(1, int(safe_float(impact))))
            for severity, impact in zip(
                one.get("SEVERITY", pd.Series(dtype=str)), impacts, strict=True
            )
        )
        score = max(0, 100 - min(100, penalty))
        state = "Healthy" if score >= 90 else ("Watch" if score >= 70 else "Act")
        rows.append(DomainPosture(domain, score, state, findings, status, newest))
    return tuple(rows)


def posture_alert_rule_sql(
    metric: str, threshold: float, severity: str = "HIGH", owner: str = "DBA"
) -> str:
    """Generate an ALERT_CONFIG upsert that monitors a posture metric (Sec35).

    The rule is raised by SP_ALERT_SCAN's generic posture arm (V087) whenever the
    metric's newest MART_SECURITY_POSTURE_DAILY reading is at or over ``threshold``
    (posture counts are higher = worse). This is an explicit operator action, so it is
    an UPSERT: re-running with a new threshold/severity for the same metric UPDATES the
    existing rule (and re-enables it) rather than silently no-op-ing — a WHEN-NOT-MATCHED
    -only MERGE would report success while changing nothing. Raises ValueError on an
    unknown metric or a non-positive threshold rather than emitting a malformed or
    never-firing rule.
    """
    metric = str(metric or "").strip().upper()
    if metric not in POSTURE_METRICS:
        raise ValueError(f"unknown posture metric: {metric!r}")
    thr = float(threshold)
    if thr <= 0:
        raise ValueError("threshold must be positive (posture metrics are counts)")
    sev = str(severity or "HIGH").strip().upper()
    if sev not in ("CRITICAL", "HIGH", "MEDIUM", "LOW", "INFO"):
        sev = "HIGH"
    rule_id = f"SEC_POSTURE_{metric}"
    name = f"Posture monitor: {metric} at/over {thr:g}"
    return (
        "MERGE INTO DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG t\n"
        f"USING (SELECT {sql_literal(rule_id)} AS RULE_ID, 'SECURITY' AS FAMILY, "
        f"{sql_literal(name)} AS NAME, TRUE AS ENABLED, {sql_literal(sev)} AS SEVERITY, "
        f"{sql_number(thr)} AS THRESHOLD_NUM, 24 AS WINDOW_HOURS, "
        f"{sql_literal(owner)} AS OWNER, {sql_literal(metric)} AS METRIC_NAME) s\n"
        "ON t.RULE_ID = s.RULE_ID\n"
        "WHEN MATCHED THEN UPDATE SET THRESHOLD_NUM = s.THRESHOLD_NUM, "
        "SEVERITY = s.SEVERITY, NAME = s.NAME, ENABLED = TRUE, UPDATED_AT = CURRENT_TIMESTAMP()\n"
        "WHEN NOT MATCHED THEN INSERT (RULE_ID, FAMILY, NAME, ENABLED, SEVERITY, "
        "THRESHOLD_NUM, WINDOW_HOURS, OWNER, METRIC_NAME)\n"
        "VALUES (s.RULE_ID, s.FAMILY, s.NAME, s.ENABLED, s.SEVERITY, s.THRESHOLD_NUM, "
        "s.WINDOW_HOURS, s.OWNER, s.METRIC_NAME);"
    )


def escalation_flags(
    frame: pd.DataFrame,
    *,
    depth_weight: float = 4.0,
    admin_bump: float = 25.0,
    self_escalate_bump: float = 40.0,
) -> pd.DataFrame:
    """Sharpen object-privilege exposure into an escalation risk (Sec2).

    The base is the OBJECT-privilege exposure (ownership + sensitive grants), which
    is blind to admin-role membership and to the manage-grants surface — so a path
    that inherits ACCOUNTADMIN, or holds MANAGE GRANTS, while holding few listed
    object grants scores low on the base despite being god-mode. (The base is
    recomputed here from the object components rather than reusing the RISK_SCORE
    column, because RISK_SCORE ALSO weights MANAGE_GRANTS — reusing it would
    double-count manage against the self-escalation bump below.) Two facts correct
    the base:
      * ``REACHES_ADMIN`` — the path inherits an admin role, a standing exposure.
      * ``MANAGE_GRANTS`` — the effective role holds the MANAGE GRANTS privilege,
        which lets it grant *any* role (including admin) to anyone. That is the
        textbook self-escalation surface: the holder can grant themselves admin.

    ``SELF_ESCALATION`` flags any path whose effective role can manage grants —
    evaluated per row (not AND-ed with ``REACHES_ADMIN``), because the privilege
    that lets you climb and the role that already sits at the top can be reached
    by different paths. ``ESCALATION_SCORE`` folds in path depth (a long inherited
    chain is harder to audit than a direct grant) plus admin-reach and
    self-escalation bumps. Returns a copy with ``REACHES_ADMIN``,
    ``SELF_ESCALATION`` (bool) and ``ESCALATION_SCORE`` (0-100 int) added; safe on
    empty or absent columns.
    """
    df = frame.copy() if frame is not None else pd.DataFrame()
    if df.empty:
        if "REACHES_ADMIN" not in df:
            df["REACHES_ADMIN"] = pd.Series(dtype=bool)
        df["SELF_ESCALATION"] = pd.Series(dtype=bool)
        df["ESCALATION_SCORE"] = pd.Series(dtype="int64")
        return df
    depth = pd.to_numeric(df.get("DEPTH"), errors="coerce").fillna(0.0).clip(lower=0.0)
    manage = pd.to_numeric(df.get("MANAGE_GRANTS"), errors="coerce").fillna(0.0)
    reaches_raw = df.get("REACHES_ADMIN")
    reaches = (
        reaches_raw.fillna(False).astype(bool)
        if reaches_raw is not None
        else pd.Series(False, index=df.index)
    )
    self_escalation = manage > 0
    # Object-privilege base EXCLUDING manage grants — the self-escalation bump below
    # weights manage, and effective_access's RISK_SCORE column ALREADY weights it
    # (MANAGE_GRANTS*25), so summing RISK_SCORE with the bump double-counted manage
    # (bug-hunt 2026-08-29; the docstring's "RISK_SCORE is blind to MANAGE_GRANTS" was
    # wrong). Recompute the base from the object components so manage counts once.
    # NOTE: mirrors the ownership*10 + sensitive*20 weights in security_sql.effective_access.
    _zero = pd.Series(0.0, index=df.index)   # absent-column safe (docstring contract)
    ownership = pd.to_numeric(df.get("OWNERSHIP_GRANTS", _zero), errors="coerce").fillna(0.0)
    sensitive = pd.to_numeric(df.get("SENSITIVE_PRIVILEGES", _zero), errors="coerce").fillna(0.0)
    object_risk = (ownership * 10.0 + sensitive * 20.0).clip(upper=100.0)
    score = (
        object_risk
        + depth * float(depth_weight)
        + reaches.astype(float) * float(admin_bump)
        + self_escalation.astype(float) * float(self_escalate_bump)
    ).clip(lower=0.0, upper=100.0)
    df["REACHES_ADMIN"] = reaches
    df["SELF_ESCALATION"] = self_escalation
    df["ESCALATION_SCORE"] = score.round().astype(int)
    return df


def sensitive_privileges_by_user(frame: pd.DataFrame) -> pd.DataFrame:
    """Per-user count of reachable sensitive-privilege grants, DE-DUPLICATED by role.

    SENSITIVE_PRIVILEGES is a per-EFFECTIVE_ROLE attribute in effective_access
    (privilege_rollup is joined on EFFECTIVE_ROLE), so it repeats on every access-PATH
    row that reaches a role. A role reached via two direct roles — or diamond
    inheritance (A->B->D and A->C->D) — appears on multiple rows, so summing the raw
    path rows multi-counts it and overstates a user's real sensitive reach. Collapse to
    distinct (USER_NAME, EFFECTIVE_ROLE) first — mirroring how the summary counts
    EFFECTIVE_ROLES with nunique — so the total reflects reachable ROLES, not paths.

    Returns a two-column frame [USER_NAME, SENSITIVE_PRIVILEGES] (int); safe on an empty
    frame or absent columns.
    """
    df = frame if frame is not None else pd.DataFrame()
    if df.empty or "USER_NAME" not in df.columns:
        return pd.DataFrame({"USER_NAME": pd.Series(dtype=object),
                             "SENSITIVE_PRIVILEGES": pd.Series(dtype="int64")})
    work = df.copy()
    work["SENSITIVE_PRIVILEGES"] = pd.to_numeric(
        work.get("SENSITIVE_PRIVILEGES"), errors="coerce").fillna(0.0)
    # dedup key is the role; without an EFFECTIVE_ROLE column, fall back to one row/user
    role_col = "EFFECTIVE_ROLE" if "EFFECTIVE_ROLE" in work.columns else "USER_NAME"
    deduped = work.drop_duplicates(["USER_NAME", role_col])
    out = deduped.groupby("USER_NAME", as_index=False)["SENSITIVE_PRIVILEGES"].sum()
    out["SENSITIVE_PRIVILEGES"] = out["SENSITIVE_PRIVILEGES"].round().astype("int64")
    return out


def _grant_reason(first_time: bool, off_hours: bool, weekend: bool) -> str:
    parts: list[str] = []
    if first_time:
        parts.append("first admin grant on record for this user")
    if off_hours:
        parts.append("weekend grant" if weekend else "off-hours grant")
    return "; ".join(parts) if parts else "within normal pattern"


def grant_anomaly_flags(
    frame: pd.DataFrame,
    *,
    business_start_hour: int = 7,
    business_end_hour: int = 19,
) -> pd.DataFrame:
    """Flag admin-role grants whose *timing* is anomalous (Sec2 time-context).

    Two deltas turn a bare 'X gained ACCOUNTADMIN' event into a triage signal:
      * ``FIRST_TIME`` — the grantee has no prior grant of this role on record,
        so this is a first-ever elevation, not a renewal of standing access.
      * ``OFF_HOURS`` — the grant landed on a weekend or outside business hours
        (account-local), i.e. outside a normal change/deploy window.

    Adds ``FIRST_TIME``, ``OFF_HOURS``, ``ANOMALY`` (either), ``SEVERITY``
    (HIGH if first-time, else MEDIUM if off-hours, else INFO) and a human
    ``REASON``. Returns a copy; safe on empty or absent columns.
    """
    df = frame.copy() if frame is not None else pd.DataFrame()
    if df.empty:
        for col in ("FIRST_TIME", "OFF_HOURS", "ANOMALY"):
            df[col] = pd.Series(dtype=bool)
        df["SEVERITY"] = pd.Series(dtype=object)
        df["REASON"] = pd.Series(dtype=object)
        return df
    prior = pd.to_numeric(df.get("PRIOR_GRANTS"), errors="coerce").fillna(0.0)
    dow = pd.to_numeric(df.get("DOW_ISO"), errors="coerce").fillna(0.0)
    hour = pd.to_numeric(df.get("HOUR_OF_DAY"), errors="coerce").fillna(12.0)
    start = int(business_start_hour)
    end = int(business_end_hour)
    first_time = prior <= 0
    weekend = dow.isin([6, 7])
    off_hours = weekend | (hour < start) | (hour >= end)
    df["FIRST_TIME"] = first_time
    df["OFF_HOURS"] = off_hours
    df["ANOMALY"] = first_time | off_hours
    df["SEVERITY"] = [
        "HIGH" if ft else ("MEDIUM" if oh else "INFO")
        for ft, oh in zip(first_time, off_hours, strict=True)
    ]
    df["REASON"] = [
        _grant_reason(bool(ft), bool(oh), bool(wk))
        for ft, oh, wk in zip(first_time, off_hours, weekend, strict=True)
    ]
    return df


