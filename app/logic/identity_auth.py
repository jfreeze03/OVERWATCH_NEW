"""Pure identity auth-readiness rules for Snowflake's single-factor password deprecation.

Read-only: classifies users from ``security_sql.user_auth_inventory`` rows and formats
review-then-run ALTER USER stubs. The app never executes the stubs."""

from __future__ import annotations

from collections.abc import Mapping

import pandas as pd

from app.logic.formulas import safe_float

#: USERS.TYPE values that are non-human. NULL / '' is documented to "function the same as PERSON".
SERVICE_TYPES: frozenset[str] = frozenset({"SERVICE", "LEGACY_SERVICE", "SNOWFLAKE_SERVICE", "SERVICE_AGENT"})
#: Service types on which Snowflake already refuses password sign-in.
_PASSWORDLESS_TYPES: frozenset[str] = frozenset({"SERVICE", "SNOWFLAKE_SERVICE", "SERVICE_AGENT"})

VERDICT_WILL_BREAK = "WILL BREAK"
VERDICT_MIGRATE = "MIGRATE"
VERDICT_READY = "READY"
_VERDICT_RANK = {VERDICT_WILL_BREAK: 0, VERDICT_MIGRATE: 1, VERDICT_READY: 2}

#: One place for the dated copy (verified on docs.snowflake.com 2026-09-24,
#: "Planning for the deprecation of single-factor password sign-ins").
ROLLOUT_NOTE = (
    "Snowflake is retiring single-factor password sign-in. Per Snowflake's published rollout, the "
    "final phase (rolling enforcement, Aug–Oct 2026) requires MFA for every PERSON password sign-in, "
    "blocks passwords for SERVICE users, and converts LEGACY_SERVICE users to SERVICE. Your account's "
    "exact date can differ — check the behavior-change bundle status before scheduling."
)


def _is_missing(value: object) -> bool:
    return value is None or (isinstance(value, float) and pd.isna(value))


def _norm_type(value: object) -> str:
    return "" if _is_missing(value) else str(value).strip().upper()


def _flag(value: object) -> bool:
    if _is_missing(value):
        return False
    if isinstance(value, str):
        return value.strip().upper() in ("TRUE", "1", "Y", "YES")
    return bool(value)


def type_label(user_type: object) -> str:
    """Display label: NULL/'' TYPE reads as PERSON (Snowflake treats it so), marked unset."""
    t = _norm_type(user_type)
    return t if t else "PERSON (unset)"


def account_band(user_type: object) -> str:
    """'Service' for any non-human TYPE, else 'Person' (incl. NULL)."""
    return "Service" if _norm_type(user_type) in SERVICE_TYPES else "Person"


def classify_auth_readiness(row: Mapping[str, object], *, evidence_ok: bool) -> tuple[str, str]:
    """(verdict, reason) for one user row.

    ``evidence_ok`` = FACT_LOGIN_DAILY fully covers the trailing 30 days; when False a
    missing/zero password-login count is UNKNOWN, never proof of disuse."""
    t = _norm_type(row.get("USER_TYPE"))
    has_pw = _flag(row.get("HAS_PASSWORD"))
    has_mfa = _flag(row.get("HAS_MFA"))
    raw = row.get("PASSWORD_LOGINS_30D")
    logins = 0.0 if _is_missing(raw) else safe_float(raw)
    used = logins > 0
    unknown = (not used) and (not evidence_ok)
    used_txt = f" ({int(logins)} password sign-ins in 30d)" if used else ""

    if t in _PASSWORDLESS_TYPES:
        return VERDICT_READY, f"{t}: password sign-in is already refused for this type."
    if t == "LEGACY_SERVICE":
        if has_pw and (used or unknown):
            return (VERDICT_WILL_BREAK,
                    "LEGACY_SERVICE with a password" + used_txt + " — password sign-in stops when "
                    "Snowflake converts LEGACY_SERVICE to SERVICE. Move it to key-pair first.")
        if has_pw:
            return (VERDICT_MIGRATE,
                    "LEGACY_SERVICE with an unused password — unset it and set TYPE = SERVICE.")
        return (VERDICT_MIGRATE,
                "LEGACY_SERVICE without a password — set TYPE = SERVICE (confirm it does not "
                "sign in with SAML SSO, which SERVICE also refuses).")
    # PERSON, unset (NULL), or any unrecognized type -> person rules
    if not has_pw:
        return VERDICT_READY, "No password — SSO, key-pair or OAuth only."
    if has_mfa:
        return VERDICT_READY, "Password with MFA enrolled."
    if t == "" and (used or unknown):
        return (VERDICT_WILL_BREAK,
                "TYPE unset (treated as PERSON) signing in by password without MFA"
                + (used_txt if used else " (disuse unproven — the login fact doesn't cover the window)") +
                " — if this is an integration, those sign-ins fail once MFA is enforced. "
                "Convert it to SERVICE + key-pair, or set TYPE = PERSON and enroll MFA.")
    return (VERDICT_MIGRATE,
            "Password without MFA" + used_txt + " — enroll MFA (a person) or convert to "
            "SERVICE + key-pair (an integration).")


def auth_readiness(frame: pd.DataFrame | None, *, evidence_ok: bool) -> pd.DataFrame:
    """Add TYPE_LABEL / ACCOUNT_BAND / VERDICT / REASON; worst-first, admins first within a
    verdict, then most password sign-ins. When evidence is complete a NULL
    PASSWORD_LOGINS_30D (no sign-in rows) becomes 0; otherwise it stays NULL so the table
    shows an em-dash, not a false 0. Empty/None in -> empty out; never raises."""
    if frame is None or frame.empty:
        return pd.DataFrame()
    out = frame.copy()
    if "PASSWORD_LOGINS_30D" in out.columns:
        out["PASSWORD_LOGINS_30D"] = pd.to_numeric(out["PASSWORD_LOGINS_30D"], errors="coerce")
        if evidence_ok:
            out["PASSWORD_LOGINS_30D"] = out["PASSWORD_LOGINS_30D"].fillna(0)
    types = (out["USER_TYPE"] if "USER_TYPE" in out.columns
             else pd.Series([None] * len(out), index=out.index, dtype=object))
    out["TYPE_LABEL"] = types.map(type_label)
    out["ACCOUNT_BAND"] = types.map(account_band)
    verdicts = [classify_auth_readiness(r, evidence_ok=evidence_ok) for r in out.to_dict("records")]
    out["VERDICT"] = [v for v, _ in verdicts]
    out["REASON"] = [why for _, why in verdicts]
    admin = (out["IS_ADMIN"].map(_flag) if "IS_ADMIN" in out.columns
             else pd.Series(False, index=out.index))
    logins = (out["PASSWORD_LOGINS_30D"].fillna(0) if "PASSWORD_LOGINS_30D" in out.columns
              else pd.Series(0, index=out.index))
    return (out.assign(_o=out["VERDICT"].map(_VERDICT_RANK), _a=~admin.astype(bool), _l=logins)
            .sort_values(["_o", "_a", "_l"], ascending=[True, True, False], kind="stable")
            .drop(columns=["_o", "_a", "_l"]).reset_index(drop=True))


def readiness_counts(ready: pd.DataFrame | None) -> dict[str, int]:
    """Rows per verdict (0 for an absent verdict)."""
    if ready is None or ready.empty or "VERDICT" not in ready.columns:
        return dict.fromkeys(_VERDICT_RANK, 0)
    vc = ready["VERDICT"].value_counts()
    return {v: int(vc.get(v, 0)) for v in _VERDICT_RANK}


def quote_user(name: object) -> str:
    """Double-quoted identifier (embedded quote doubled). USERS.NAME is the stored,
    exact-case name, so quoting preserves it. ValueError on blank / control characters."""
    raw = "" if _is_missing(name) else str(name)
    if not raw.strip() or any(ord(ch) < 32 for ch in raw):
        raise ValueError(f"unsafe user name: {raw!r}")
    return '"' + raw.replace('"', '""') + '"'


def alter_user_stubs(ready: pd.DataFrame | None) -> list[str]:
    """Review-then-run ALTER USER lines for WILL BREAK / MIGRATE rows, in frame order.

    Service path (LEGACY_SERVICE, or an unset-TYPE WILL BREAK): key-pair first, verify,
    then UNSET PASSWORD, then TYPE = SERVICE. Person path: MFA enrolment is a Snowsight
    step, so both TYPE choices are emitted commented. Rows whose name fails quote_user
    are skipped (never a malformed statement)."""
    if ready is None or ready.empty or "VERDICT" not in ready.columns:
        return []
    lines: list[str] = []
    for r in ready.to_dict("records"):
        verdict = str(r.get("VERDICT") or "")
        if verdict not in (VERDICT_WILL_BREAK, VERDICT_MIGRATE):
            continue
        try:
            u = quote_user(r.get("USER_NAME"))
        except ValueError:
            continue
        t = _norm_type(r.get("USER_TYPE"))
        service_path = t == "LEGACY_SERVICE" or (t == "" and verdict == VERDICT_WILL_BREAK)
        if lines:
            lines.append("")
        lines.append(f"-- {u}: {verdict}")
        if service_path:
            if not _flag(r.get("HAS_RSA_PUBLIC_KEY")):
                lines.append(f"ALTER USER {u} SET RSA_PUBLIC_KEY = '<paste the public key>';")
            lines.append("-- cut the client over to key-pair auth and confirm a KEY_PAIR sign-in first")
            if _flag(r.get("HAS_PASSWORD")):
                lines.append(f"ALTER USER {u} UNSET PASSWORD;")
            lines.append(f"ALTER USER {u} SET TYPE = SERVICE;")
            if t == "":
                lines.append(f"-- if this is a person instead: enroll MFA, then ALTER USER {u} SET TYPE = PERSON;")
        else:
            lines.append("-- a person: have them enroll MFA in Snowsight, then make the type explicit:")
            lines.append(f"-- ALTER USER {u} SET TYPE = PERSON;")
            lines.append("-- an integration: key-pair cutover + UNSET PASSWORD, then:")
            lines.append(f"-- ALTER USER {u} SET TYPE = SERVICE;")
    return lines


def with_account_band(frame: pd.DataFrame | None, service_names: set[str] | None) -> pd.DataFrame:
    """Dormant/reawakening scans: add ACCOUNT_BAND ('Service' when USER_NAME is in
    ``service_names``, case-insensitive) WITHOUT reordering. ``service_names=None`` (the
    TYPE read failed) bands everything 'Person' — the caller captions that the split is
    unavailable. None in -> empty frame."""
    if frame is None:
        return pd.DataFrame()
    out = frame.copy()
    if out.empty:
        out["ACCOUNT_BAND"] = pd.Series(dtype=object)
        return out
    if "USER_NAME" not in out.columns:   # no names to match: every row stays in the Person band
        out["ACCOUNT_BAND"] = "Person"
        return out
    names = {str(n).upper() for n in (service_names or set())}
    out["ACCOUNT_BAND"] = [("Service" if str(n).upper() in names else "Person")
                           for n in out["USER_NAME"]]
    return out


def admins_without_mfa(ready: pd.DataFrame | None) -> pd.DataFrame:
    """Admin holders (IS_ADMIN) with a password and no MFA, from an ``auth_readiness`` frame,
    regardless of login evidence. Flag-safe (NULL never reads as True). Order preserved."""
    if ready is None or ready.empty or not {"IS_ADMIN", "HAS_PASSWORD", "HAS_MFA"}.issubset(ready.columns):
        return pd.DataFrame()
    mask = [(_flag(a) and _flag(p) and not _flag(m))
            for a, p, m in zip(ready["IS_ADMIN"], ready["HAS_PASSWORD"], ready["HAS_MFA"], strict=True)]
    return ready[mask].reset_index(drop=True)
