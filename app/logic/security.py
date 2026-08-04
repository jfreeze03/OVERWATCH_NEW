"""Pure Security policy, scoring, and write-statement rules."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta

import pandas as pd

from app.config import core_object
from app.core.sqlsafe import sql_literal, sql_number
from app.logic.formulas import account_today, safe_float

SECURITY_DOMAINS = (
    "IDENTITY",
    "PRIVILEGE",
    "CHANGE RISK",
    "DATA MOVEMENT",
    "TRUST CENTER",
    "ACCESS REVIEW",
)
IDENTITY_TYPES = ("HUMAN", "SERVICE", "EMERGENCY")
EGRESS_TARGET_KINDS = ("REGION", "STAGE")
ACCESS_DECISIONS = ("KEEP", "REVOKE", "EXCEPTION")
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


def version_key(value: object) -> tuple[int, ...]:
    """Comparable numeric version key; nonnumeric suffixes are ignored."""
    parts = re.findall(r"\d+", str(value or ""))
    return tuple(int(part) for part in parts[:8]) or (0,)


def apply_client_policies(frame: pd.DataFrame, policies: pd.DataFrame) -> pd.DataFrame:
    out = frame.copy()
    if out.empty or "DRIVER" not in out.columns:
        return out
    policy_map: dict[str, pd.Series] = {}
    if policies is not None and not policies.empty and "DRIVER" in policies.columns:
        policy_map = {
            str(row["DRIVER"]).upper(): row
            for _, row in policies.iterrows()
        }
    statuses: list[str] = []
    minimums: list[str] = []
    owners: list[str] = []
    for _, row in out.iterrows():
        policy = policy_map.get(str(row.get("DRIVER", "")).upper())
        if policy is None:
            statuses.append("UNKNOWN")
            minimums.append("")
            owners.append("")
            continue
        minimum = str(policy.get("MIN_APPROVED_VERSION", "") or "")
        warn_after = pd.to_datetime(policy.get("WARN_AFTER"), errors="coerce")
        observed_text = str(row.get("VERSION", "") or "")
        observed = version_key(observed_text)
        required = version_key(minimum)
        if not re.search(r"\d", observed_text) or not re.search(r"\d", minimum):
            status = "UNKNOWN"
        elif observed < required:
            status = "UNSUPPORTED"
        elif not pd.isna(warn_after) and warn_after.date() <= account_today():
            status = "NEAR_EOL"
        else:
            status = "SUPPORTED"
        statuses.append(status)
        minimums.append(minimum)
        owners.append(str(policy.get("OWNER_NAME", "") or ""))
    out["POLICY_STATUS"] = statuses
    out["MIN_APPROVED_VERSION"] = minimums
    out["POLICY_OWNER"] = owners
    return out


def _like_pattern(value: object) -> re.Pattern[str]:
    text = re.escape(str(value or ""))
    text = text.replace("%", ".*").replace("_", ".")
    return re.compile(f"^{text}$", re.IGNORECASE)


def apply_egress_policies(
    frame: pd.DataFrame,
    policies: pd.DataFrame,
    *,
    target_col: str,
    target_kind: str,
    company: str,
) -> pd.DataFrame:
    out = frame.copy()
    if out.empty or target_col not in out.columns:
        return out
    active: list[tuple[re.Pattern[str], str]] = []
    if policies is not None and not policies.empty:
        today = account_today()
        for _, row in policies.iterrows():
            if str(row.get("TARGET_KIND", "")).upper() != target_kind.upper():
                continue
            if str(row.get("STATUS", "APPROVED") or "APPROVED").upper() != "APPROVED":
                continue
            policy_company = str(row.get("COMPANY", "ALL") or "ALL").upper()
            if policy_company not in ("ALL", str(company or "ALL").upper()):
                continue
            expires = pd.to_datetime(row.get("EXPIRES_ON"), errors="coerce")
            if not pd.isna(expires) and expires.date() < today:
                continue
            active.append((_like_pattern(row.get("TARGET_PATTERN")), str(row.get("OWNER_NAME", "") or "")))
    states: list[str] = []
    owners: list[str] = []
    for target in out[target_col].astype(str):
        matched = next(((pattern, owner) for pattern, owner in active if pattern.match(target)), None)
        states.append("APPROVED" if matched else "UNAPPROVED")
        owners.append(matched[1] if matched else "")
    out["POLICY_STATUS"] = states
    out["POLICY_OWNER"] = owners
    return out


def enrich_identity_policy(frame: pd.DataFrame, policies: pd.DataFrame, user_col: str = "USER_NAME") -> pd.DataFrame:
    out = frame.copy()
    if out.empty or user_col not in out.columns:
        return out
    if policies is None or policies.empty or "USER_NAME" not in policies.columns:
        out["IDENTITY_TYPE"] = "UNKNOWN"
        out["IDENTITY_OWNER"] = ""
        out["EXPECTED_AUTH_METHOD"] = ""
        return out
    keep = [
        col for col in ("USER_NAME", "IDENTITY_TYPE", "OWNER_NAME", "EXPECTED_AUTH_METHOD",
                        "EXPECTED_NETWORK", "EXCEPTION_UNTIL")
        if col in policies.columns
    ]
    right = policies[keep].drop_duplicates("USER_NAME")
    merged = out.merge(right, how="left", left_on=user_col, right_on="USER_NAME", suffixes=("", "_POLICY"))
    identity_type = merged["IDENTITY_TYPE"] if "IDENTITY_TYPE" in merged else pd.Series(
        "UNKNOWN", index=merged.index, dtype="object"
    )
    owner_name = merged["OWNER_NAME"] if "OWNER_NAME" in merged else pd.Series(
        "", index=merged.index, dtype="object"
    )
    merged["IDENTITY_TYPE"] = identity_type.fillna("UNKNOWN")
    merged["IDENTITY_OWNER"] = owner_name.fillna("")
    return merged


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


def _date_sql(value: date | str | None) -> str:
    if value is None or not str(value).strip():
        return "NULL"
    text = value.isoformat() if isinstance(value, date) else str(value).strip()[:10]
    date.fromisoformat(text)
    return f"TO_DATE({sql_literal(text, 10)})"


def upsert_identity_policy_sql(
    user_name: str,
    *,
    identity_type: str,
    owner: str,
    auth_method: str,
    network: str,
    rotation_days: int | None,
    exception_until: date | str | None,
    notes: str,
    actor: str,
) -> str:
    user = str(user_name or "").strip().upper()
    kind = str(identity_type or "").strip().upper()
    if not user:
        raise ValueError("User name is required")
    if kind not in IDENTITY_TYPES:
        raise ValueError(f"Unsupported identity type: {kind}")
    rotation = "NULL" if rotation_days is None else sql_number(max(1, min(int(rotation_days), 3650)))
    return f"""
MERGE INTO {core_object('SECURITY_IDENTITY_POLICY')} t
USING (SELECT {sql_literal(user, 200)} AS USER_NAME) s ON UPPER(t.USER_NAME) = UPPER(s.USER_NAME)
WHEN MATCHED THEN UPDATE SET
  IDENTITY_TYPE = {sql_literal(kind, 20)}, OWNER_NAME = {sql_literal(owner, 200)},
  EXPECTED_AUTH_METHOD = {sql_literal(auth_method, 80)},
  EXPECTED_NETWORK = {sql_literal(network, 500)}, ROTATION_DAYS = {rotation},
  EXCEPTION_UNTIL = {_date_sql(exception_until)}, NOTES = {sql_literal(notes, 4000)},
  UPDATED_AT = CURRENT_TIMESTAMP(), UPDATED_BY = {sql_literal(actor, 200)}
WHEN NOT MATCHED THEN INSERT
  (USER_NAME, IDENTITY_TYPE, OWNER_NAME, EXPECTED_AUTH_METHOD, EXPECTED_NETWORK,
   ROTATION_DAYS, EXCEPTION_UNTIL, NOTES, UPDATED_BY)
VALUES
  (s.USER_NAME, {sql_literal(kind, 20)}, {sql_literal(owner, 200)},
   {sql_literal(auth_method, 80)}, {sql_literal(network, 500)}, {rotation},
   {_date_sql(exception_until)}, {sql_literal(notes, 4000)}, {sql_literal(actor, 200)})
""".strip()


def insert_egress_policy_sql(
    *,
    company: str,
    target_kind: str,
    pattern: str,
    owner: str,
    expires_on: date | str | None,
    notes: str,
    actor: str,
) -> str:
    kind = str(target_kind or "").strip().upper()
    target = str(pattern or "").strip()
    if kind not in EGRESS_TARGET_KINDS:
        raise ValueError(f"Unsupported egress target kind: {kind}")
    if not target:
        raise ValueError("Target pattern is required")
    company_name = str(company or "ALL").upper()
    return f"""
MERGE INTO {core_object('SECURITY_EGRESS_POLICY')} t
USING (
  SELECT {sql_literal(company_name, 40)} AS COMPANY,
         {sql_literal(kind, 20)} AS TARGET_KIND,
         {sql_literal(target, 500)} AS TARGET_PATTERN
) s
ON UPPER(t.COMPANY) = UPPER(s.COMPANY)
 AND UPPER(t.TARGET_KIND) = UPPER(s.TARGET_KIND)
 AND UPPER(t.TARGET_PATTERN) = UPPER(s.TARGET_PATTERN)
WHEN MATCHED THEN UPDATE SET
  STATUS = 'APPROVED', OWNER_NAME = {sql_literal(owner, 200)},
  EXPIRES_ON = {_date_sql(expires_on)}, NOTES = {sql_literal(notes, 4000)},
  UPDATED_AT = CURRENT_TIMESTAMP(), UPDATED_BY = {sql_literal(actor, 200)}
WHEN NOT MATCHED THEN INSERT
  (COMPANY, TARGET_KIND, TARGET_PATTERN, OWNER_NAME, STATUS, EXPIRES_ON, NOTES, UPDATED_BY)
VALUES
  (s.COMPANY, s.TARGET_KIND, s.TARGET_PATTERN, {sql_literal(owner, 200)}, 'APPROVED',
   {_date_sql(expires_on)}, {sql_literal(notes, 4000)}, {sql_literal(actor, 200)})
""".strip()


def upsert_client_policy_sql(
    driver: str,
    *,
    minimum_version: str,
    warn_after: date | str | None,
    owner: str,
    notes: str,
    actor: str,
) -> str:
    name = str(driver or "").strip()
    minimum = str(minimum_version or "").strip()
    if not name or not minimum:
        raise ValueError("Driver and minimum version are required")
    return f"""
MERGE INTO {core_object('SECURITY_CLIENT_POLICY')} t
USING (SELECT {sql_literal(name, 200)} AS DRIVER) s ON UPPER(t.DRIVER) = UPPER(s.DRIVER)
WHEN MATCHED THEN UPDATE SET
  MIN_APPROVED_VERSION = {sql_literal(minimum, 80)}, WARN_AFTER = {_date_sql(warn_after)},
  OWNER_NAME = {sql_literal(owner, 200)}, NOTES = {sql_literal(notes, 4000)},
  UPDATED_AT = CURRENT_TIMESTAMP(), UPDATED_BY = {sql_literal(actor, 200)}
WHEN NOT MATCHED THEN INSERT
  (DRIVER, MIN_APPROVED_VERSION, WARN_AFTER, OWNER_NAME, NOTES, UPDATED_BY)
VALUES
  (s.DRIVER, {sql_literal(minimum, 80)}, {_date_sql(warn_after)},
   {sql_literal(owner, 200)}, {sql_literal(notes, 4000)}, {sql_literal(actor, 200)})
""".strip()


def create_access_review_sql(
    campaign_id: str,
    *,
    title: str,
    company: str,
    due_date: date | str | None,
    actor: str,
) -> str:
    cid = str(campaign_id or "").strip()
    name = str(title or "").strip()
    if not cid or not name:
        raise ValueError("Campaign ID and title are required")
    return (
        f"CALL {core_object('SP_CREATE_ACCESS_REVIEW')}("
        f"{sql_literal(cid, 80)}, {sql_literal(name, 300)}, "
        f"{sql_literal(str(company or 'ALL').upper(), 40)}, {_date_sql(due_date)}, "
        f"{sql_literal(actor, 200)})"
    )


def access_review_decision_sql(
    campaign_id: str,
    item_id: str,
    *,
    decision: str,
    reason: str,
    actor: str,
) -> str:
    choice = str(decision or "").strip().upper()
    if choice not in ACCESS_DECISIONS:
        raise ValueError(f"Unsupported access-review decision: {choice}")
    return (
        f"CALL {core_object('SP_ACCESS_REVIEW_DECIDE')}("
        f"{sql_literal(campaign_id, 80)}, {sql_literal(item_id, 80)}, "
        f"{sql_literal(choice, 20)}, {sql_literal(reason, 4000)}, {sql_literal(actor, 200)})"
    )
