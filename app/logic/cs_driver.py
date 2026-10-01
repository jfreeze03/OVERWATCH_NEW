"""Cloud-services driver intelligence — classify a compile-heavy / metadata query
family and decide whether a warehouse resize could plausibly help.

Phase 0 (app-only, no migration): works on the columns the existing compile-heavy
family builders already return (SAMPLE_TEXT, COMPILE_PCT, AVG_TOTAL_S, RUNS, and
QUERY_TYPE/WAREHOUSE_NAME when present). It turns "cloud services spiked" into
"WHICH behavior spiked, WHO owns the fix, and is resizing even relevant".

Two deliberate Phase-0 boundaries, both documented so a later phase can lift them:

* Classification is multi-signal but leans on the query-text SIGNATURE (SAMPLE_TEXT)
  plus the compile-vs-total shape. The stronger ``IS_CLIENT_GENERATED_STATEMENT``
  flag, per-session APP-INIT correlation, and connection-churn detection need columns
  that are not in the mart/extract today (Phase 1 adds them), so the SYSTEM GENERATED
  and HIGH FREQUENCY classes are approximated here, not fully realized.
* The resize verdict is two-state (RESIZE NOT INDICATED / INSUFFICIENT EVIDENCE). The
  third state, RESIZE MAY HELP, requires spill/queue evidence that this frame does not
  carry; asserting it without that evidence would be a false positive, so Phase 0 never
  claims it. RESIZE NOT INDICATED — the genuinely new, high-value verdict for
  compile/metadata-dominated chatter — is fully supported.

Gross cloud-services credits are USAGE: the ~10% free-allowance adjustment is an account+day (UTC)
computation against a shared pool, so it is never ALLOCATED to a warehouse or a family, and a gross
figure is never dollarized. v4.595 prices one thing only — the MARGINAL billed credit of a family
(``billed_family_view``): per day, the smaller of the family's credits and the account's billed
cloud services that day (used + adjustment), i.e. what the bill drops by if that family alone stopped,
compute held fixed. It is unique (no allocation choice): it equals the family's credits 1:1 on days
whose billed cloud services are at least that family's credits (every complete day on this account in
the 2026-09-26 DIAG), is capped at the day's billed total below that, and is zero on a day under the
allowance. Per-family values add only while each day's billed cloud services cover their combined
credits, so a GROUP total (the sleep-polling KPI) is capped per day as a group, in the SQL.

Pure pandas — no Streamlit, no Snowflake. Reuses ``query_advisor.COMPILE_FRACTION`` so the
"compile-dominated" threshold matches the per-query advisor.
"""

from __future__ import annotations

import re
from collections import Counter

import pandas as pd

from app.config import APP_SIS_QUERY_TAG_FRAGMENT
from app.logic.formulas import credits_to_usd, format_credits, safe_float
from app.logic.query_advisor import COMPILE_FRACTION
from app.logic.system_wait import is_sleep_statement, polls_with_system_wait, wait_seconds

# --- driver classes (a Phase-0 subset of the 12-class taxonomy) --------------
SYSTEM_GENERATED = "System generated"
GOVERNANCE_DISCOVERY = "Governance / Cortex discovery"
JDBC_ODBC_DISCOVERY = "JDBC / ODBC discovery"
INFORMATION_SCHEMA = "INFORMATION_SCHEMA discovery"
STAGE_FILE = "Stage / file metadata"
COMPILE_HEAVY = "Compile heavy"
METADATA_CHATTER = "Metadata chatter"
NORMAL = "Normal workload"
UNKNOWN = "Unknown"
# v4.595: a SYSTEM$WAIT poll. It barely compiles and then sleeps in cloud services, so no
# compile-ranked view ever showed it (owner DIAG 2026-09-26: 26% of the account's CS credits).
SLEEP_POLLING = "Sleep polling"

DRIVER_CLASSES = (
    SYSTEM_GENERATED, SLEEP_POLLING, GOVERNANCE_DISCOVERY, JDBC_ODBC_DISCOVERY, INFORMATION_SCHEMA,
    STAGE_FILE, COMPILE_HEAVY, METADATA_CHATTER, NORMAL, UNKNOWN,
)

# --- resize verdicts ---------------------------------------------------------
RESIZE_NOT_INDICATED = "Resize not indicated"
RESIZE_INSUFFICIENT = "Insufficient evidence"

# --- remediation owner per class --------------------------------------------
_OWNER = {
    SYSTEM_GENERATED: "Platform / Snowsight (usually benign)",
    SLEEP_POLLING: "Job scheduler / task owner",
    GOVERNANCE_DISCOVERY: "Security / Governance",
    JDBC_ODBC_DISCOVERY: "BI / IDE tool owner",
    INFORMATION_SCHEMA: "BI / IDE or Data Engineering",
    STAGE_FILE: "Data Engineering",
    COMPILE_HEAVY: "Application / SQL author",
    METADATA_CHATTER: "Application / BI tool owner",
    NORMAL: "",
    UNKNOWN: "Investigate (attribution needed)",
}

# The compile-dominated classes: their cost lives in the cloud-services/compile
# layer, so a virtual-warehouse resize (which scales only compute credits) cannot
# reduce them. Everything here => RESIZE NOT INDICATED.
_COMPILE_LAYER_CLASSES = frozenset({
    SYSTEM_GENERATED, GOVERNANCE_DISCOVERY, JDBC_ODBC_DISCOVERY,
    INFORMATION_SCHEMA, STAGE_FILE, METADATA_CHATTER, COMPILE_HEAVY,
})
# Classes a warehouse resize cannot fix. A sleep is not compile-layer work (it SLEEPS in cloud
# services), so it joins here rather than _COMPILE_LAYER_CLASSES: same verdict, honest reason.
_NOT_A_SIZING_PROBLEM = _COMPILE_LAYER_CLASSES | {SLEEP_POLLING}

# A family with fewer runs than this is a weak basis for a confident verdict
# (the builders already floor at 5/20; this only softens confidence, never excludes).
_THIN_RUNS = 20
# Compile share (0-100) at/above which the compile phase dominates. Matches
# query_advisor.COMPILE_FRACTION (a 0-1 fraction) so the two agree.
_COMPILE_DOMINANT_PCT = COMPILE_FRACTION * 100.0
# A compile-dominated family whose whole statement is this short is metadata-shaped
# (near-zero warehouse execution), not a heavy plan on a warehouse.
_METADATA_MAX_TOTAL_S = 2.0
# Output columns this module stamps onto a family frame.
DRIVER_COLS = ("DRIVER_CLASS", "DRIVER_CONFIDENCE", "RESIZE_VERDICT", "REMEDIATION_OWNER")
# v4.595: the per-row owner + fix, stamped only on request (classify_families(with_action=True)) so
# the existing compile-heavy and chatter panels keep exactly DRIVER_COLS.
ACTION_COLS = ("OWNER_HINT", "NEXT_STEP")
# MART_CLOUD_SVC_DAILY's COALESCE(QUERY_PARAMETERIZED_HASH, 'n/a'): mixed statements, no family.
_NO_HASH = "N/A"
# A system function with no specific rule — but never SYSTEM$WAIT( itself: a statement that only
# mentions the call (an ILIKE search, a comment) is not platform-issued (review r2).
_GENERIC_SYSTEM_FN_RE = re.compile(r"SYSTEM\$(?!WAIT\s*\()")


def _text(row: pd.Series | dict, col: str) -> str:
    val = row.get(col) if isinstance(row, dict) else row.get(col, None)
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return ""
    return str(val).upper()


def _num(row: pd.Series | dict, col: str) -> float:
    val = row.get(col) if isinstance(row, dict) else row.get(col, None)
    try:
        f = float(val)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0
    return 0.0 if pd.isna(f) else f


def classify_row(row: pd.Series | dict) -> tuple[str, str]:
    """Return (driver_class, confidence) for one family row.

    Confidence is HIGH when an unambiguous text signature matched, MEDIUM for a
    shape-only call, LOW when the sample is thin or the text is missing/truncated.
    """
    text = _text(row, "SAMPLE_TEXT") or _text(row, "QUERY_TEXT")
    qtype = _text(row, "QUERY_TYPE")
    runs = _num(row, "RUNS")
    compile_pct = _num(row, "COMPILE_PCT")
    total_s = _num(row, "AVG_TOTAL_S")
    thin = 0 < runs < _THIN_RUNS
    # the unhashed bucket mixes unrelated statements under one ANY_VALUE sample: never confident
    mixed = _text(row, "QUERY_PARAMETERIZED_HASH") == _NO_HASH

    def conf(base: str) -> str:
        if mixed:
            return "LOW"
        # a text-signature HIGH decays to MEDIUM on a thin sample; a shape MEDIUM to LOW
        if thin:
            return {"HIGH": "MEDIUM", "MEDIUM": "LOW"}.get(base, base)
        return base

    # --- text-signature classes (most specific first) ------------------------
    # upper-cased. SiS stamps its app tag on every app statement, but today's family builders
    # (compile_heavy_families, the mart twin, chatter families) do not select QUERY_TAG, so this arm only
    # fires for a frame that carries it; it keeps the classifier correct if one ever does.
    _tag = _text(row, "QUERY_TAG")
    if ("SYSTEM$FBE" in text or "EXECUTE STREAMLIT" in text or _tag.startswith("OVERWATCH")
            or APP_SIS_QUERY_TAG_FRAGMENT.upper() in _tag):
        return SYSTEM_GENERATED, conf("HIGH")
    # v4.595 SLEEP POLLING: a SYSTEM$WAIT call (SELECT or CALL). Its cloud-services cost is the sleep
    # itself (~0.07 s compile on a 10-1200 s statement), so it must precede the generic "SYSTEM$"
    # platform arm below, which would call a Control-M / task poll benign platform noise. Exact call
    # signature (system_wait): SYSTEM$WAIT_FOR_SERVICES and other SYSTEM$ calls still fall through, and
    # a CREATE/ALTER that merely CONTAINS the call (task/proc DDL) is no sleep. A scripting block that
    # polls with the call is LABELLED here too (MEDIUM) so its owner gets the fix, but sleep_estimate
    # and the SQL SLEEP_FLAG count only the sleep statement itself (its child row), never the wrapper.
    if polls_with_system_wait(text, qtype):
        return SLEEP_POLLING, conf("HIGH" if is_sleep_statement(text, qtype) else "MEDIUM")
    if ("SYSTEM$GET_CLASSIFICATION" in text or "SYSTEM$CLASSIFY" in text
            or "SYSTEM$CORTEX_MODEL_ACCESSIBLE" in text or "SHOW CORTEX" in text):
        return GOVERNANCE_DISCOVERY, conf("HIGH")
    if ("JDBC:DATABASEMETADATA" in text or "/* JDBC" in text or "/* ODBC" in text
            or "GETPRIMARYKEYS" in text or "GETIMPORTEDKEYS" in text
            or "GETTABLES" in text or "GETCOLUMNS" in text):
        return JDBC_ODBC_DISCOVERY, conf("HIGH")
    if "INFORMATION_SCHEMA" in text:
        return INFORMATION_SCHEMA, conf("HIGH")
    if ("FROM @" in text or "LIST @" in text or text.startswith(("GET ", "PUT "))
            or qtype in ("GET_FILES", "LIST_FILES", "PUT_FILES")):
        return STAGE_FILE, conf("HIGH")
    # a system function we do not have a specific rule for: still platform-issued
    if _GENERIC_SYSTEM_FN_RE.search(text):
        return SYSTEM_GENERATED, conf("MEDIUM")

    # --- shape-based classes (no decisive text signature) --------------------
    if compile_pct >= _COMPILE_DOMINANT_PCT:
        # compile phase dominates. Sub-second total => metadata-only (no real
        # warehouse execution); otherwise a genuinely compile-heavy plan.
        # R1-090: "no warehouse" is a NULL / 'NONE' WAREHOUSE_NAME VALUE, never an absent column -- the
        # Cost ▸ Spend compile-heavy builders (live + mart, and the per-warehouse drill) select none, so
        # every compile-dominated family there read Metadata chatter and COMPILE_HEAVY could never appear.
        # ("in" checks a dict's keys and a Series' index.)
        no_warehouse = "WAREHOUSE_NAME" in row and _text(row, "WAREHOUSE_NAME") in ("", "NONE", "NULL")
        if 0 < total_s <= _METADATA_MAX_TOTAL_S or no_warehouse:
            return METADATA_CHATTER, conf("MEDIUM")
        return COMPILE_HEAVY, conf("MEDIUM")
    if compile_pct > 0 and total_s > 0:
        return NORMAL, conf("MEDIUM")
    return UNKNOWN, "LOW"


def resize_verdict(driver_class: str, compile_pct: float, total_s: float) -> str:
    """Two-state resize relevance (Phase 0). RESIZE NOT INDICATED for anything whose
    cost lives in the compile/cloud-services layer; INSUFFICIENT EVIDENCE otherwise
    (this frame has no spill/queue columns, so RESIZE MAY HELP is never asserted)."""
    if driver_class in _NOT_A_SIZING_PROBLEM or compile_pct >= _COMPILE_DOMINANT_PCT:
        return RESIZE_NOT_INDICATED
    return RESIZE_INSUFFICIENT


def remediation_owner(driver_class: str) -> str:
    return _OWNER.get(driver_class, "")


def _raw(row: pd.Series | dict, col: str) -> str:
    """A display value as recorded (not upper-cased); "" for missing / NaN."""
    val = row.get(col) if isinstance(row, dict) else row.get(col, None)
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return ""
    return str(val).strip()


def owner_hint(row: pd.Series | dict) -> str:
    """Who to talk to for THIS family: the task owner role for a task-issued statement (USER_NAME
    SYSTEM), else the user and the application behind most of their queries (USER_TOP_APP, a
    user-level hint). "" when the row names no user."""
    user = _raw(row, "USER_NAME")
    if user.upper() == "SYSTEM":
        role = _raw(row, "ROLE_NAME")
        return f"Task owner (role {role})" if role else "Task owner"
    app = _raw(row, "USER_TOP_APP")
    if user and app:
        return f"{app} · {user}"
    return f"User {user}" if user else ""


# The concrete next step per class. Each holds at most ONE "$" (a markdown sink pairs two into LaTeX).
_ACTION = {
    SYSTEM_GENERATED: "Platform-issued (Snowsight, Workspaces, a native feature): usually leave it; "
                      "confirm the feature is wanted.",
    GOVERNANCE_DISCOVERY: "Review the classification / Cortex probe schedule with Security and run it "
                          "less often.",
    JDBC_ODBC_DISCOVERY: "Turn off metadata prefetch / schema browsing in the BI or IDE driver; pool "
                         "connections.",
    INFORMATION_SCHEMA: "Cache these INFORMATION_SCHEMA lookups or run them less often; each call bills "
                        "cloud services.",
    STAGE_FILE: "Batch stage LIST / file reads, or move to event-driven loads.",
    COMPILE_HEAVY: "Simplify the statement or parameterize huge IN-lists; the compile cost repeats on "
                   "every run.",
    METADATA_CHATTER: "Cut the cadence: cache metadata, batch the calls, pool connections, or quiet the "
                      "tool issuing them.",
    NORMAL: "Cloud-services cost here follows run count: run it less often or batch the work into fewer "
            "statements.",
    UNKNOWN: "Attribute it first (owner hint, sample statement), then decide.",
}
_SLEEP_TASK_ACTION = (
    "Stop sleeping inside the task: run the dependent step AFTER its predecessor in a task graph, or "
    "trigger it when a stream has data, instead of a CALL SYSTEM$WAIT loop. A resize won't help.")
_SLEEP_CLIENT_ACTION = (
    "Move the wait out of Snowflake: let the scheduler own the interval (Control-M cyclic interval or "
    "file watcher, orchestrator sensor), or run one short readiness check per cycle instead of "
    "SYSTEM$WAIT; cloud-services credits accrue for the whole sleep. A resize won't help.")


def remediation_action(driver_class: str, row: pd.Series | dict | None = None) -> str:
    """The concrete fix for a class; for sleep polling it depends on who sleeps (a task vs a
    client/scheduler). An unhashed ('n/a') row is prefixed as mixed statements."""
    if driver_class == SLEEP_POLLING:
        is_task = row is not None and _raw(row, "USER_NAME").upper() == "SYSTEM"
        action = _SLEEP_TASK_ACTION if is_task else _SLEEP_CLIENT_ACTION
    else:
        action = _ACTION.get(driver_class, _ACTION[UNKNOWN])
    if row is not None and _text(row, "QUERY_PARAMETERIZED_HASH") == _NO_HASH:
        action = "Mixed statements (no family hash): " + action
    return action


def classify_families(df: pd.DataFrame, *, with_action: bool = False) -> pd.DataFrame:
    """Return a copy of ``df`` with DRIVER_CLASS, DRIVER_CONFIDENCE, RESIZE_VERDICT
    and REMEDIATION_OWNER stamped on (plus OWNER_HINT / NEXT_STEP when ``with_action``).
    Column-tolerant: a frame missing SAMPLE_TEXT / COMPILE_PCT / AVG_TOTAL_S still classifies
    (as UNKNOWN where it must). Never mutates the caller's frame; an empty frame comes back with
    the columns present so downstream renders/tests see a stable schema. The default output is
    byte-identical to the pre-v4.595 contract (the compile-heavy + chatter panels rely on it)."""
    out = df.copy()
    cols = DRIVER_COLS + (ACTION_COLS if with_action else ())
    if out.empty:
        for col in cols:
            out[col] = pd.Series(dtype="object")
        return out

    classes, confs, verdicts, owners, hints, actions = [], [], [], [], [], []
    for _, row in out.iterrows():
        cls, confidence = classify_row(row)
        classes.append(cls)
        confs.append(confidence)
        verdicts.append(resize_verdict(cls, _num(row, "COMPILE_PCT"), _num(row, "AVG_TOTAL_S")))
        owners.append(remediation_owner(cls))
        if with_action:
            hints.append(owner_hint(row))
            actions.append(remediation_action(cls, row))
    out["DRIVER_CLASS"] = classes
    out["DRIVER_CONFIDENCE"] = confs
    out["RESIZE_VERDICT"] = verdicts
    out["REMEDIATION_OWNER"] = owners
    if with_action:
        out["OWNER_HINT"] = hints
        out["NEXT_STEP"] = actions
    return out


def driver_summary(df: pd.DataFrame) -> dict:
    """One-line rollup for a caption: how many families landed in each class, how many
    are RESIZE NOT INDICATED, and the owner that recurs most. ``df`` is assumed to have
    already passed through ``classify_families``."""
    if df.empty or "DRIVER_CLASS" not in df.columns:
        return {"total": 0, "not_indicated": 0, "by_class": {}, "top_owner": ""}
    classes = [str(c) for c in df["DRIVER_CLASS"].tolist()]
    owners = [str(o) for o in df.get("REMEDIATION_OWNER", pd.Series(dtype="object")).tolist() if o]
    not_indicated = int((df["RESIZE_VERDICT"].astype(str) == RESIZE_NOT_INDICATED).sum())
    top_owner = Counter(owners).most_common(1)[0][0] if owners else ""
    return {
        "total": len(classes),
        "not_indicated": not_indicated,
        "by_class": dict(Counter(classes)),
        "top_owner": top_owner,
    }


# ---------------------------------------------------------------------------
# v4.595: statement families ranked by the cloud-services credits they BILL
# (mart_sql.cloud_svc_billed_families -> Cost > Spend). Pure display shaping + prose.
# ---------------------------------------------------------------------------

# Display order == the CSV. Every duration ends in _S / _SEC so the table humanizes it to Hr/Min/Sec;
# CS_CREDITS_PER_SLEEP_HOUR is a rate, not a duration (its last token is HOUR, not HOURS / H).
# "#" is the family's true rank by CS credits (the SQL CS_RANK): a sleep family pulled in below the top 50
# keeps its real rank instead of the table's positional 1..N.
BILLED_VIEW_COLS = (
    "#", "DRIVER_CLASS", "SAMPLE_TEXT", "WAREHOUSE_NAME", "USER_NAME", "USER_TOP_APP", "ROLE_NAME",
    "QUERY_TYPE", "RUNS", "ACTIVE_DAYS", "AVG_COMPILE_S", "AVG_EXEC_S", "AVG_ELAPSED_S", "COMPILE_PCT",
    "CS_CREDITS", "CS_SHARE_PCT", "BILLED_CS_CREDITS", "BILLED_CS_USD", "WAIT_PER_RUN_SEC", "SLEEP_SEC",
    "SLEEP_BASIS", "CS_CREDITS_PER_SLEEP_HOUR", "OWNER_HINT", "REMEDIATION_OWNER", "NEXT_STEP",
    "RESIZE_VERDICT", "DRIVER_CONFIDENCE", "QUERY_PARAMETERIZED_HASH",
)
_NAN = float("nan")


def sleep_estimate(row: pd.Series | dict) -> tuple[float | None, float | None, str]:
    """(WAIT_PER_RUN_SEC, SLEEP_SEC, SLEEP_BASIS) for one family row; (None, None, "") when the row
    is not a sleep (or is the unhashed 'n/a' bucket).

    "requested": runs x the wait the statement asks for — trusted only when the family's two
    samples (MIN / MAX over its days) parse to the same wait. "measured": otherwise, runs x the
    family's average elapsed from the query-family mart. Neither available -> no hours."""
    text = _raw(row, "SAMPLE_TEXT")
    if (_text(row, "QUERY_PARAMETERIZED_HASH") == _NO_HASH
            or not is_sleep_statement(text, _raw(row, "QUERY_TYPE"))):
        return None, None, ""
    runs = _num(row, "RUNS")
    wait = wait_seconds(text)
    alt = _raw(row, "SAMPLE_TEXT_ALT")
    if wait is not None and (not alt or alt == text or wait_seconds(alt) == wait):
        return wait, runs * wait, "requested"
    elapsed = _num(row, "AVG_ELAPSED_S")
    if elapsed > 0:
        return None, runs * elapsed, "measured"
    return None, None, ""


def _numcol(df: pd.DataFrame, col: str) -> pd.Series:
    if col in df.columns:
        return pd.to_numeric(df[col], errors="coerce")
    return pd.Series(_NAN, index=df.index, dtype="float64")


def _empty_summary() -> dict:
    return {"rows": 0, "scope_cs": 0.0, "metered_cs": 0.0, "metered_days": 0, "under_allowance_days": 0,
            "last_metered_day": "", "unmetered_cs": 0.0, "low_compile_cs": 0.0, "low_compile_pct": 0.0,
            "sleep_families": 0, "sleep_cs": 0.0, "sleep_billed_cs": 0.0, "sleep_usd": _NAN,
            "sleep_share_pct": 0.0, "sleep_sec_shown": 0.0, "sleep_hours_complete": True}


def billed_family_view(df: pd.DataFrame | None, rate: float) -> tuple[pd.DataFrame, dict]:
    """Shape the billed-family frame for display: classify each family (with owner + next step),
    estimate hours slept, price the MARGINAL billed credits at the compute rate, and read the
    window totals the SQL computed over ALL families (never re-derived from the capped rows).

    Returns ``(view, summary)``; ``view`` has exactly BILLED_VIEW_COLS. BILLED_CS_USD is NaN (never
    $0) where daily metering has not loaded the day yet; ``summary['sleep_usd']`` is NaN when no
    metered day is in the window."""
    if df is None or df.empty:
        return pd.DataFrame(columns=list(BILLED_VIEW_COLS)), _empty_summary()
    out = df.copy()
    compile_s = _numcol(out, "AVG_COMPILE_S")
    exec_s = _numcol(out, "AVG_EXEC_S")
    elapsed = _numcol(out, "AVG_ELAPSED_S")
    # classifier shape inputs: measured elapsed when the family mart has it, else compile + execution
    total = elapsed.where(elapsed > 0, compile_s.fillna(0.0) + exec_s.fillna(0.0))
    out["AVG_TOTAL_S"] = total
    out["COMPILE_PCT"] = (compile_s.fillna(0.0) * 100.0 / total.where(total > 0)).clip(upper=100.0).fillna(0.0)
    out = classify_families(out, with_action=True)

    est = [sleep_estimate(r) for _, r in out.iterrows()]
    out["WAIT_PER_RUN_SEC"] = pd.Series([e[0] for e in est], index=out.index, dtype="float64")
    out["SLEEP_SEC"] = pd.Series([e[1] for e in est], index=out.index, dtype="float64")
    out["SLEEP_BASIS"] = [e[2] for e in est]
    billed = _numcol(out, "BILLED_CS_CREDITS")
    out["BILLED_CS_USD"] = billed.map(
        lambda c: credits_to_usd(c, rate, round_cents=False) if pd.notna(c) else _NAN).astype("float64")
    cs = _numcol(out, "CS_CREDITS")
    sleep_h = out["SLEEP_SEC"] / 3600.0
    out["CS_CREDITS_PER_SLEEP_HOUR"] = (cs / sleep_h.where(sleep_h > 0)).astype("float64")
    out["#"] = (_numcol(out, "CS_RANK") if "CS_RANK" in out.columns
                else pd.Series(range(1, len(out) + 1), index=out.index, dtype="float64"))
    view = out.reindex(columns=list(BILLED_VIEW_COLS))

    def _win(col: str) -> float:
        return safe_float(df[col].iloc[0]) if col in df.columns else 0.0

    scope_cs = _win("SCOPE_CS_CREDITS")
    metered_days = int(_win("METERED_DAYS"))
    sleep_families = int(_win("SLEEP_FAMILIES_ALL"))
    sleep_cs = _win("SLEEP_CS_CREDITS_ALL")
    # NULL when no sleep credit falls on a complete metered day: unpriced (NaN), never $0
    sleep_billed = (safe_float(df["SLEEP_BILLED_CS_CREDITS_ALL"].iloc[0], _NAN)
                    if "SLEEP_BILLED_CS_CREDITS_ALL" in df.columns else _NAN)
    low_compile = _win("LOW_COMPILE_CS_CREDITS_ALL")
    if "SLEEP_FLAG" in out.columns:
        sleep_mask = _numcol(out, "SLEEP_FLAG").fillna(0.0) > 0
    else:
        sleep_mask = out["DRIVER_CLASS"] == SLEEP_POLLING
    shown_sleep = out.loc[sleep_mask, "SLEEP_SEC"]
    last_day = df["LAST_METERED_DAY"].iloc[0] if "LAST_METERED_DAY" in df.columns else None
    summary = {
        "rows": len(view),
        "scope_cs": scope_cs,
        "metered_cs": _win("METERED_CS_CREDITS"),
        "metered_days": metered_days,
        "under_allowance_days": int(_win("UNDER_ALLOWANCE_DAYS")),
        "last_metered_day": "" if last_day is None or pd.isna(last_day) else str(last_day)[:10],
        "unmetered_cs": _win("SCOPE_UNMETERED_CS_CREDITS"),
        "low_compile_cs": low_compile,
        "low_compile_pct": low_compile / scope_cs * 100.0 if scope_cs > 0 else 0.0,
        "sleep_families": sleep_families,
        "sleep_cs": sleep_cs,
        "sleep_billed_cs": sleep_billed,
        "sleep_usd": (credits_to_usd(sleep_billed, rate, round_cents=False)
                      if metered_days and not pd.isna(sleep_billed) else _NAN),
        "sleep_share_pct": sleep_cs / scope_cs * 100.0 if scope_cs > 0 else 0.0,
        "sleep_sec_shown": float(shown_sleep.fillna(0.0).sum()),
        "sleep_hours_complete": bool(len(shown_sleep) == sleep_families and shown_sleep.notna().all()),
    }
    return view, summary


def billing_basis_note(summary: dict, rate: float) -> tuple[str, str]:
    """(headline, audit_detail) prose for the billed-family panel. Pure, so tests lock the copy.
    The headline holds dollar signs: render it through md_dollars."""
    md = int(summary.get("metered_days") or 0)
    under = int(summary.get("under_allowance_days") or 0)
    if md == 0:
        head = ("Billing basis: daily metering for this window isn't loaded yet, so these credits are "
                "usage and are not priced.")
    elif under == 0:
        days_txt = "the 1 complete metered day" if md == 1 else f"all {md} complete metered days"
        head = (f"Billing basis: the account's cloud services were above the free allowance (10% of daily "
                f"warehouse compute) on {days_txt}, so cutting these credits cuts the "
                f"bill, up to each day's billed cloud services. Billed $ = those credits × "
                f"${safe_float(rate):,.2f} (the compute rate).")
    else:
        head = (f"Billing basis: on {under} of {md} complete metered day{'' if md == 1 else 's'} the account "
                f"stayed under the free "
                f"allowance (10% of daily warehouse compute), where cloud services cost nothing extra; "
                f"Billed $ counts only credits above it, × ${safe_float(rate):,.2f}.")
    unmetered = safe_float(summary.get("unmetered_cs"))
    if md and unmetered > 0:
        head += (f" {format_credits(unmetered)} CS credits are from days daily metering has not closed yet "
                 "(today, and the day in progress when it last loaded) and are left unpriced.")
    detail = ("Per day, a family's billed credits are the smaller of its credits and the account's billed "
              "cloud services that day (used + adjustment): what the bill drops by if that family alone "
              "stopped, holding compute fixed. Per-family values add only while each day's billed cloud "
              "services cover their combined credits; the sleep-polling total is capped per day as a group. "
              "Statement days are Central and metering days UTC, so the daily check can shift by a few "
              "hours at the boundary. Statement credits come from query history, per statement, and are "
              "recorded separately from metering.")
    return head, detail
