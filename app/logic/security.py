"""Pure Security scoring and coverage rules (read-only posture)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

import pandas as pd

from app.logic.formulas import account_today, safe_float

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


def security_change_risk(query_type: object, role: object = "", database: object = "") -> tuple[int, str, str]:
    kind = str(query_type or "").upper()
    if kind.startswith(("DROP", "TRUNCATE")):
        score, family = 90, "DESTRUCTIVE"
    elif kind in ("GRANT", "REVOKE"):
        score, family = 80, "PRIVILEGE"
    elif "POLICY" in kind or "USER" in kind:
        score, family = 85, "SECURITY POLICY"
    elif kind.startswith(("ALTER", "RENAME")):
        score, family = 55, "ALTER"
    else:
        score, family = 30, "CREATE"
    if str(role or "").upper() in ("ACCOUNTADMIN", "SNOW_ACCOUNTADMINS"):
        score += 10
    if "PROD" in str(database or "").upper():
        score += 10
    score = min(100, score)
    level = "CRITICAL" if score >= 90 else ("HIGH" if score >= 70 else ("MEDIUM" if score >= 45 else "LOW"))
    return score, level, family


def escalation_flags(
    frame: pd.DataFrame,
    *,
    depth_weight: float = 4.0,
    admin_bump: float = 25.0,
    self_escalate_bump: float = 40.0,
) -> pd.DataFrame:
    """Sharpen the raw privilege RISK_SCORE into an escalation risk (Sec2).

    ``RISK_SCORE`` counts *object* privileges (ownership, sensitive grants) but is
    blind to admin-role membership, so a path that inherits ACCOUNTADMIN while
    holding few listed object grants can score low despite being god-mode. Two
    facts correct that:
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
    risk = pd.to_numeric(df.get("RISK_SCORE"), errors="coerce").fillna(0.0)
    depth = pd.to_numeric(df.get("DEPTH"), errors="coerce").fillna(0.0).clip(lower=0.0)
    manage = pd.to_numeric(df.get("MANAGE_GRANTS"), errors="coerce").fillna(0.0)
    reaches_raw = df.get("REACHES_ADMIN")
    reaches = (
        reaches_raw.fillna(False).astype(bool)
        if reaches_raw is not None
        else pd.Series(False, index=df.index)
    )
    self_escalation = manage > 0
    score = (
        risk
        + depth * float(depth_weight)
        + reaches.astype(float) * float(admin_bump)
        + self_escalation.astype(float) * float(self_escalate_bump)
    ).clip(lower=0.0, upper=100.0)
    df["REACHES_ADMIN"] = reaches
    df["SELF_ESCALATION"] = self_escalation
    df["ESCALATION_SCORE"] = score.round().astype(int)
    return df


