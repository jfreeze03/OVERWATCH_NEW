"""Statement-timeout posture per warehouse (Next-Fifty #33). Pure module.

What Snowflake enforces (docs: Parameters ▸ STATEMENT_TIMEOUT_IN_SECONDS):
  * the LOWEST NON-ZERO of the warehouse value and the session value, where 0 means the 7-day
    maximum (604800 s) and the built-in default is 172800 s (2 days);
  * the warehouse object inherits the ACCOUNT value unless it sets its own, and the session side
    inherits the account value unless a user or session sets its own.

``SHOW PARAMETERS LIKE 'STATEMENT_TIMEOUT_IN_SECONDS' IN WAREHOUSE <wh>`` reports the value in force
on the warehouse object with its LEVEL ('WAREHOUSE', 'ACCOUNT', or '' = set nowhere). So the effective
cap for a statement on that warehouse (absent a user/session override) is
``min(enforced(warehouse value), enforced(account value))``.

An effective cap of 48 hours or more is UNCAPPED: one runaway statement can bill for two days (or seven).
The posture panel flags those and writes a review-only script whose cap is the smallest ladder step at
or above p99 x 3 of the warehouse's completed statements. Capped warehouses never get an ALTER (the
tighten-only principle of remediation.tighten_suspend_plan). ``tighten_timeout_plan`` is the same
guard for the alert drawer's one-click "Statement timeout 1h" lever; since v4.603 it also weighs what
that cap would have cancelled (``TimeoutImpact``) and withholds the ALTER behind an explicit override
when that is anything but a verified zero.

A timeout cancel fires at whichever ceiling is lowest for THAT statement (warehouse, account, user,
session, client or task), so the per-warehouse 'Timed out' count is paired with the ceiling that fired
(TIMEOUT_FIRED_MIN_SEC / _MAX_SEC, parsed from the error text): one below the effective cap is a user,
session or client value (or an earlier warehouse value), not that cap firing.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

import pandas as pd

from .formulas import humanize_duration, safe_float
from .remediation import statement_timeout_fix

SNOWFLAKE_DEFAULT_STMT_TIMEOUT_S = 172_800      # == admin._SNOWFLAKE_DEFAULT_STMT_TIMEOUT_S (parity-tested)
SNOWFLAKE_MAX_STMT_TIMEOUT_S = 604_800          # what 0 (and anything above it) enforces
CAP_LADDER_S = (300, 600, 900, 1800, 3600, 7200, 14400, 28800, 43200, 86400)
PREFILL_MULTIPLIER = 3
MIN_RUNS_FOR_P99 = 100
TAIL_MIN_DAYS, TAIL_MAX_DAYS = 30, 90
# The alert drawer's impact read (ops_sql.warehouse_timeout_impact): a fixed trailing 30 days.
IMPACT_DAYS = 30
# Review C21: each warehouse's SHOW PARAMETERS is its own entry in the process-wide metadata-tier cache
# (query._fetch_metadata, max_entries=128, LRU), shared with SHOW DATABASES, the user directory, the
# schema-version gates and the Admin probes. 40 reads + the account SHOW + SHOW WAREHOUSES stay within a
# third of that store (tests/test_stmt_timeout.py pins the ratio), so one toggle cannot evict the rest.
MAX_WAREHOUSES_READ = 40
STATUS_UNCAPPED, STATUS_CAPPED, STATUS_UNREAD, STATUS_NOT_VISIBLE = "Uncapped", "Capped", "Unread", "Not visible"
# v4.603 (#33 D4): Snowflake-managed compute the runtime tail sees under ALL scope but SHOW WAREHOUSES never
# lists (serverless-task pools COMPUTE_SERVICE_WH_USER_TASKS_POOL_*, the native-app / SNOWFLAKEDB upgrade
# pools, COMPUTE_SERVICE_WH itself). No role can SHOW them and there is no warehouse object to set a timeout
# on, so they are not "Not visible" (which points a DBA at grants).
STATUS_MANAGED = "Managed compute"
MANAGED_COMPUTE_PREFIX = "COMPUTE_SERVICE_WH"
POSTURE_COLUMNS = ["WAREHOUSE_NAME", "STATUS", "EFFECTIVE_TIMEOUT_SEC", "CAP_SOURCE", "COMPLETED_RUNS",
                   "P99_ELAPSED_SEC", "MAX_ELAPSED_SEC", "TIMEOUT_CANCELLED_RUNS", "TIMEOUT_FIRED",
                   "SUGGESTED_TIMEOUT_SEC", "WOULD_CANCEL_RUNS", "WAREHOUSE_LEVEL", "WAREHOUSE_TIMEOUT_SEC",
                   "TIMEOUT_FIRED_MIN_SEC", "TIMEOUT_FIRED_MAX_SEC", "FIRED_BELOW_CAP"]
# The panel's table (and so its CSV): the reader-facing columns, TIMEOUT_FIRED ('Fired at') beside the count.
DISPLAY_COLUMNS = POSTURE_COLUMNS[:11]

_STATUS_ORDER = {STATUS_UNCAPPED: 0, STATUS_CAPPED: 1, STATUS_UNREAD: 2, STATUS_NOT_VISIBLE: 3, STATUS_MANAGED: 4}
_SOURCE_BY_LEVEL = {"WAREHOUSE": "Warehouse", "ACCOUNT": "Account"}
_UNQUOTED = re.compile(r"^[A-Z_][A-Z0-9_$]*$")
_NUMERIC_COLS = ("EFFECTIVE_TIMEOUT_SEC", "COMPLETED_RUNS", "P99_ELAPSED_SEC", "MAX_ELAPSED_SEC",
                 "TIMEOUT_CANCELLED_RUNS", "SUGGESTED_TIMEOUT_SEC", "WOULD_CANCEL_RUNS",
                 "WAREHOUSE_TIMEOUT_SEC", "TIMEOUT_FIRED_MIN_SEC", "TIMEOUT_FIRED_MAX_SEC")
_TAIL_COLS = ("COMPLETED_RUNS", "P99_ELAPSED_SEC", "MAX_ELAPSED_SEC", "TIMEOUT_CANCELLED_RUNS",
              "TIMEOUT_FIRED_MIN_SEC", "TIMEOUT_FIRED_MAX_SEC")
FIRED_BELOW_NOTE = "below cap"


def tail_window_days(days: object) -> int:
    """The runtime-tail window: the page Window, floored at 30 days and capped at 90 (the live cap)."""
    return int(min(max(safe_float(days, float(TAIL_MIN_DAYS)), TAIL_MIN_DAYS), TAIL_MAX_DAYS))


def fixable_name(name: object) -> bool:
    """True when ``name`` is an unquoted (upper-case) identifier the ALTER generator can emit verbatim.

    remediation._ident upper-cases what it is given, so a quoted mixed-case or spaced warehouse name would
    be ALTERed under the wrong identifier: those names get a write-it-by-hand line instead."""
    return bool(_UNQUOTED.match(str(name or "").strip()))


def _num(value: object) -> float | None:
    """A finite float, or None for None/NaN/garbage."""
    try:
        f = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) or math.isinf(f) else f


def _cols(df: pd.DataFrame) -> dict[str, object]:
    return {str(c).lower(): c for c in df.columns}


def parse_timeout_row(df: pd.DataFrame | None) -> tuple[float | None, str]:
    """(seconds, LEVEL) from a SHOW PARAMETERS frame (column names in any case).

    (None, '') when the frame is absent/empty, has no ``value`` column, or the value does not parse.
    LEVEL is upper-cased; a blank/NULL level (set nowhere) is ''."""
    if df is None or df.empty:
        return None, ""
    cols = _cols(df)
    if "value" not in cols:
        return None, ""
    row = df.iloc[0]
    value = _num(str(row[cols["value"]]).strip())
    if value is None:
        return None, ""
    level = row[cols["level"]] if "level" in cols else ""
    if level is None or (isinstance(level, float) and math.isnan(level)):
        level = ""
    return value, str(level).strip().upper()


def derive_account_timeout(rows: Iterable[tuple[float | None, str]]) -> float | None:
    """The account value when the account SHOW could not be read, from the warehouse rows.

    A row inherited from the account (LEVEL 'ACCOUNT') carries the account value; a row set nowhere
    (LEVEL '') proves the account is unset, i.e. the 172800 s default. Only WAREHOUSE-level rows -> None."""
    unset = False
    for value, level in rows:
        if value is None:
            continue
        lvl = str(level or "").strip().upper()
        if lvl == "ACCOUNT":
            return float(value)
        if lvl == "":
            unset = True
    return float(SNOWFLAKE_DEFAULT_STMT_TIMEOUT_S) if unset else None


def enforced_s(value: float | None) -> float | None:
    """What a parameter value enforces: 0 (or negative) is the 7-day maximum; nothing exceeds it."""
    if value is None:
        return None
    v = float(value)
    if math.isnan(v):
        return None
    return float(SNOWFLAKE_MAX_STMT_TIMEOUT_S) if v <= 0 else min(v, float(SNOWFLAKE_MAX_STMT_TIMEOUT_S))


def effective_timeout_s(warehouse_s: float | None, warehouse_level: str,
                        account_s: float | None) -> tuple[float | None, str]:
    """(effective seconds, cap source) for a statement on the warehouse, before user/session overrides.

    The source is 'Account' when the account value is the lower one; otherwise it follows the warehouse
    row's LEVEL ('Warehouse' / 'Account' / 'Snowflake default'). (None, '') when the warehouse value is
    unread: the account value alone cannot say whether the warehouse is tighter."""
    w = enforced_s(warehouse_s)
    if w is None:
        return None, ""
    a = enforced_s(account_s)
    if a is not None and a < w:
        return a, "Account"
    return w, _SOURCE_BY_LEVEL.get(str(warehouse_level or "").strip().upper(), "Snowflake default")


def account_value_kpi(account_s: float | None, how: str) -> dict:
    """The posture panel's 'Account value' tile (review C15/C20): the value the account ENFORCES, so a 0
    (Snowflake's 7-day maximum) reads 168h next to the uncapped caption and the table, never '0s'.
    ``how`` (read / derived from warehouse rows / unread) is the delta; the raw 0 is named in delta + help."""
    help_ = ("The account's STATEMENT_TIMEOUT_IN_SECONDS, which a warehouse (and every session) inherits "
             "unless it sets its own.")
    tile = {"label": "Account value", "value": "—", "delta": how, "delta_color": "off", "help": help_}
    enforced = enforced_s(account_s)
    if account_s is None or enforced is None:
        return tile
    tile["value"] = humanize_duration(enforced)
    if float(account_s) <= 0:
        tile["delta"] = f"{how}; 0 = 7-day max"
        tile["help"] = help_ + " It is set to 0, which Snowflake enforces as the 7-day maximum."
    return tile


def empty_universe_state(company: str, *, tail_ok: bool, show_ok: bool, active: int = 0,
                         managed: int = 0) -> tuple[str, str, str]:
    """(empty_state kind, message, which read's error to show: 'tail' | 'show' | '') when
    ``warehouse_universe`` returned nothing to read (review C16, house rule 8: 'unavailable' = the read failed).

    A company scope lists only the runtime tail's warehouses, so a FAILED tail is 'unavailable', never a
    verified-empty scope. ALL scope lists SHOW WAREHOUSES (the tail is only its fallback), so a failed SHOW
    is 'unavailable'. ``active``: tail warehouses SHOW does not list (the company's warehouses all Not
    visible); ``managed``: tail names that are Snowflake-managed compute (no warehouse timeout to set)."""
    if str(company or "ALL").strip().upper() != "ALL":
        if not tail_ok:
            return ("unavailable", "The completed-runtime tail could not be read, so this company's warehouses "
                    "are unknown (a company scope lists only the warehouses active in the window).", "tail")
        if active and managed:
            return ("no_data_yet", f"The {active + managed:,} warehouse(s) active for this company in the window "
                    f"are either not listed by SHOW WAREHOUSES ({active:,}: dropped, renamed, or not visible to "
                    f"the app role) or Snowflake-managed compute ({managed:,}: {MANAGED_COMPUTE_PREFIX}* pools "
                    "with no warehouse timeout to set), so there is no timeout to read.", "")
        if active:
            return ("no_data_yet", f"The {active:,} warehouse(s) active for this company in the window are not "
                    "listed by SHOW WAREHOUSES (dropped, renamed, or not visible to the app role), so there is "
                    "no timeout to read.", "")
        if managed:
            return ("no_data_yet", f"The {managed:,} warehouse(s) active for this company in the window are "
                    f"Snowflake-managed compute ({MANAGED_COMPUTE_PREFIX}* serverless-task and upgrade pools): "
                    "SHOW WAREHOUSES never lists them and there is no warehouse timeout to set.", "")
        return "no_data_yet", "No warehouse ran statements for this company in the window.", ""
    if not show_ok:
        tail_says = "neither could the runtime tail" if not tail_ok else "the runtime tail listed no warehouse"
        return ("unavailable", f"SHOW WAREHOUSES could not be read and {tail_says}, so the warehouses to check "
                "are unknown.", "show")
    return "no_data_yet", "SHOW WAREHOUSES lists no warehouse the app role can see.", ""


def is_uncapped(effective_s: float | None) -> bool:
    """48 hours or more: the Snowflake default, 0 (the 7-day maximum), or an explicit value that high."""
    return effective_s is not None and effective_s >= SNOWFLAKE_DEFAULT_STMT_TIMEOUT_S


def suggested_cap_s(p99_sec: object, runs: object) -> int | None:
    """The smallest ladder step at or above p99 x 3, in seconds.

    None when there are fewer than MIN_RUNS_FOR_P99 completed runs (a p99 of a handful of runs is the
    maximum, not a percentile), when the p99 is missing, or when p99 x 3 exceeds the top step (24 h)."""
    p, n = _num(p99_sec), _num(runs)
    if p is None or n is None or n < MIN_RUNS_FOR_P99:
        return None
    target = math.ceil(max(p, 0.0) * PREFILL_MULTIPLIER)
    return next((rung for rung in CAP_LADDER_S if rung >= target), None)


def _names(df: pd.DataFrame | None, col: str) -> list[str]:
    if df is None or df.empty:
        return []
    cols = _cols(df)
    if col.lower() not in cols:
        return []
    out = []
    for v in df[cols[col.lower()]].tolist():
        if v is None or (isinstance(v, float) and math.isnan(v)):
            continue
        s = str(v).strip()
        if s:
            out.append(s)
    return out


def _tail_rank(tail_df: pd.DataFrame | None) -> dict[str, float]:
    """name -> longest completed run (for the read-cap priority); absent names rank last."""
    if tail_df is None or tail_df.empty:
        return {}
    cols = _cols(tail_df)
    if "warehouse_name" not in cols:
        return {}
    mx = cols.get("max_elapsed_sec")
    out: dict[str, float] = {}
    for _, r in tail_df.iterrows():
        name = str(r[cols["warehouse_name"]] or "").strip()
        if name:
            longest = _num(r[mx]) if mx is not None else None
            out[name] = longest if longest is not None else 0.0
    return out


def is_managed_compute(name: object) -> bool:
    """True for a Snowflake-managed compute name (COMPUTE_SERVICE_WH*, any case). Only meaningful for a name
    SHOW WAREHOUSES does not list: ``warehouse_universe`` never applies it to a warehouse SHOW lists."""
    return str(name or "").strip().upper().startswith(MANAGED_COMPUTE_PREFIX)


def warehouse_universe(show_df: pd.DataFrame | None, tail_df: pd.DataFrame | None,
                       company: str) -> tuple[list[str], list[str], list[str]]:
    """(warehouses to read, warehouses NOT visible to SHOW, Snowflake-managed compute).

    ALL scope: every warehouse SHOW WAREHOUSES lists (idle ones included), or the runtime-tail names when
    SHOW is unusable. A company scope: the tail's names (already company-scoped by COMPANY_FOR_WAREHOUSE,
    so only warehouses active in the window) that SHOW also lists. Only when SHOW was usable, the tail names
    SHOW did not list split in two: ``managed`` = the COMPUTE_SERVICE_WH* names (Snowflake-managed
    serverless-task / upgrade pools: no role can SHOW them, no warehouse timeout to set) and
    ``not_visible`` = the rest (dropped, renamed, or not visible to the app role). A warehouse SHOW lists
    is always read, whatever its name.

    At most MAX_WAREHOUSES_READ are read (one SHOW each): warehouses with the longest completed runs come
    first, then the rest by name. All three lists are returned sorted by name."""
    show = _names(show_df, "name")
    tail = _names(tail_df, "WAREHOUSE_NAME")
    show_ok = bool(show)
    show_set, tail_set = set(show), set(tail)
    if str(company or "ALL").strip().upper() == "ALL":
        pool = show_set if show_ok else tail_set
    else:
        pool = (tail_set & show_set) if show_ok else tail_set
    unlisted = sorted(tail_set - show_set) if show_ok else []
    managed = [n for n in unlisted if is_managed_compute(n)]
    not_visible = [n for n in unlisted if not is_managed_compute(n)]
    rank = _tail_rank(tail_df)
    prioritised = sorted(pool, key=lambda n: (n not in rank, -rank.get(n, 0.0), n))
    return sorted(prioritised[:MAX_WAREHOUSES_READ]), not_visible, managed


def _tail_lookup(tail_df: pd.DataFrame | None) -> dict[str, Mapping[str, object]]:
    if tail_df is None or tail_df.empty:
        return {}
    cols = _cols(tail_df)
    if "warehouse_name" not in cols:
        return {}
    upper = {str(c).upper(): c for c in tail_df.columns}
    out: dict[str, Mapping[str, object]] = {}
    for _, r in tail_df.iterrows():
        name = str(r[cols["warehouse_name"]] or "").strip()
        if name:
            out[name] = {k: r[c] for k, c in upper.items()}
    return out


def _tail_value(t: Mapping[str, object] | None, col: str) -> float:
    v = _num(t.get(col)) if t is not None else None
    return float("nan") if v is None else v


def fired_below_cap(fired_min_s: object, effective_s: object) -> bool | None:
    """True when a timeout cancel fired BELOW the warehouse's effective cap: the ceiling that fired was a
    user, session or client value (or an earlier, lower warehouse value), not that cap. None when either
    side is unknown (no cancel with a parseable ceiling, or the cap is unread / not a warehouse)."""
    fired, eff = _num(fired_min_s), _num(effective_s)
    if fired is None or eff is None:
        return None
    return fired < eff


def fired_at_text(fired_min_s: object, fired_max_s: object, below: bool | None = None) -> str | None:
    """The 'Fired at' cell: the humanized ceiling(s) the window's timeout cancels fired at ('30m', or a
    range '2.0s–10s'), plus ' · below cap' when ``below``. None (the dash) when no cancel carried a
    parseable ceiling."""
    lo, hi = _num(fired_min_s), _num(fired_max_s)
    if lo is None and hi is None:
        return None
    lo = hi if lo is None else lo
    hi = lo if hi is None else hi
    text = humanize_duration(lo) if lo == hi else f"{humanize_duration(lo)}–{humanize_duration(hi)}"
    return f"{text} · {FIRED_BELOW_NOTE}" if below else text


def _fired_cols(t: Mapping[str, object] | None, effective_s: float | None) -> dict[str, object]:
    lo, hi = _tail_value(t, "TIMEOUT_FIRED_MIN_SEC"), _tail_value(t, "TIMEOUT_FIRED_MAX_SEC")
    below = fired_below_cap(lo, effective_s)
    return {"TIMEOUT_FIRED": fired_at_text(lo, hi, below), "TIMEOUT_FIRED_MIN_SEC": lo,
            "TIMEOUT_FIRED_MAX_SEC": hi, "FIRED_BELOW_CAP": below}


def _unlisted_row(name: str, status: str, t: Mapping[str, object] | None) -> dict[str, object]:
    return {
        "WAREHOUSE_NAME": name, "STATUS": status, "EFFECTIVE_TIMEOUT_SEC": float("nan"),
        "CAP_SOURCE": None, "COMPLETED_RUNS": _tail_value(t, "COMPLETED_RUNS"),
        "P99_ELAPSED_SEC": _tail_value(t, "P99_ELAPSED_SEC"),
        "MAX_ELAPSED_SEC": _tail_value(t, "MAX_ELAPSED_SEC"),
        "TIMEOUT_CANCELLED_RUNS": _tail_value(t, "TIMEOUT_CANCELLED_RUNS"),
        "SUGGESTED_TIMEOUT_SEC": float("nan"), "WOULD_CANCEL_RUNS": float("nan"),
        "WAREHOUSE_LEVEL": None, "WAREHOUSE_TIMEOUT_SEC": float("nan"),
        **_fired_cols(t, None),
    }


def timeout_posture(read_names: Iterable[str], params: Mapping[str, tuple[float | None, str]],
                    account_s: float | None, tail_df: pd.DataFrame | None,
                    not_visible: Iterable[str] = (), managed: Iterable[str] = ()) -> pd.DataFrame:
    """One row per warehouse, exactly POSTURE_COLUMNS.

    STATUS: Uncapped (effective >= 48h) / Capped / Unread (its SHOW failed) / Not visible (ran in the
    window but SHOW WAREHOUSES does not list it) / Managed compute (a COMPUTE_SERVICE_WH* pool SHOW does
    not list: no warehouse timeout to set). Tail values the runtime read did not return stay NaN
    (rendered as the dash), never 0. SUGGESTED_TIMEOUT_SEC and WOULD_CANCEL_RUNS (the completed statements
    in the window that cap would have cancelled) are set on Uncapped rows only. TIMEOUT_FIRED ('Fired at')
    is the humanized ceiling the window's timeout cancels fired at; FIRED_BELOW_CAP is True when the lowest
    of them is below the row's effective cap (a user, session or client value fired, not that cap). Sorted
    Uncapped, Capped, Unread, Not visible, Managed compute, then longest run first."""
    tail = _tail_lookup(tail_df)
    rows = []
    for name in read_names:
        w_val, w_lvl = params.get(name, (None, ""))
        eff, src = effective_timeout_s(w_val, w_lvl, account_s)
        status = STATUS_UNREAD if eff is None else STATUS_UNCAPPED if is_uncapped(eff) else STATUS_CAPPED
        t = tail.get(name)
        runs = _tail_value(t, "COMPLETED_RUNS")
        cap = suggested_cap_s(_tail_value(t, "P99_ELAPSED_SEC"), runs) if status == STATUS_UNCAPPED else None
        would = _tail_value(t, f"RUNS_OVER_{cap}") if cap is not None else float("nan")
        rows.append({
            "WAREHOUSE_NAME": name, "STATUS": status,
            "EFFECTIVE_TIMEOUT_SEC": float("nan") if eff is None else eff,
            "CAP_SOURCE": src or None,
            "COMPLETED_RUNS": runs, "P99_ELAPSED_SEC": _tail_value(t, "P99_ELAPSED_SEC"),
            "MAX_ELAPSED_SEC": _tail_value(t, "MAX_ELAPSED_SEC"),
            "TIMEOUT_CANCELLED_RUNS": _tail_value(t, "TIMEOUT_CANCELLED_RUNS"),
            "SUGGESTED_TIMEOUT_SEC": float("nan") if cap is None else float(cap),
            "WOULD_CANCEL_RUNS": would,
            "WAREHOUSE_LEVEL": w_lvl if w_val is not None else None,
            "WAREHOUSE_TIMEOUT_SEC": float("nan") if w_val is None else float(w_val),
            **_fired_cols(t, eff),
        })
    rows.extend(_unlisted_row(name, STATUS_NOT_VISIBLE, tail.get(name)) for name in not_visible)
    rows.extend(_unlisted_row(name, STATUS_MANAGED, tail.get(name)) for name in managed)
    out = pd.DataFrame(rows, columns=POSTURE_COLUMNS)
    for col in _NUMERIC_COLS:
        out[col] = pd.to_numeric(out[col], errors="coerce").astype(float)
    if out.empty:
        return out
    out["_O"] = out["STATUS"].map(_STATUS_ORDER)
    out = out.sort_values(["_O", "MAX_ELAPSED_SEC", "WAREHOUSE_NAME"], ascending=[True, False, True],
                          na_position="last")
    return out.drop(columns="_O").reset_index(drop=True)


def posture_summary(df: pd.DataFrame | None) -> dict:
    """Counts by status; ``read`` = warehouses whose timeout was read (Uncapped + Capped). ``fired_below`` =
    the warehouse rows (sorted names) whose timeout cancels fired below their effective cap."""
    status = df["STATUS"] if df is not None and not df.empty and "STATUS" in df.columns else pd.Series([], dtype=str)
    counts = {s: int((status == s).sum()) for s in _STATUS_ORDER}
    below: list[str] = []
    if df is not None and not df.empty and {"FIRED_BELOW_CAP", "WAREHOUSE_NAME"} <= set(df.columns):
        # a bool / numpy-bool True only: None and NaN (unknown) never count
        below = sorted(str(n) for n, b in zip(df["WAREHOUSE_NAME"], df["FIRED_BELOW_CAP"], strict=False)
                       if b is not None and _num(b) == 1.0)
    return {"read": counts[STATUS_UNCAPPED] + counts[STATUS_CAPPED], "uncapped": counts[STATUS_UNCAPPED],
            "capped": counts[STATUS_CAPPED], "unread": counts[STATUS_UNREAD],
            "not_visible": counts[STATUS_NOT_VISIBLE], "managed": counts[STATUS_MANAGED],
            "fired_below": below}


def status_notes(summary: Mapping[str, object]) -> list[str]:
    """The panel's one-line captions for the rows no timeout was read for, and for cancels that fired below
    the effective cap -- each names its real cause (v4.603, #33 D4/D5)."""
    notes = []
    nv = int(safe_float(summary.get("not_visible"), 0.0))
    if nv:
        notes.append(f"{nv:,} warehouse(s) ran statements in the window but SHOW WAREHOUSES does not list them "
                     "(dropped, renamed, or not visible to the app role): shown as Not visible, with no timeout "
                     "read.")
    mg = int(safe_float(summary.get("managed"), 0.0))
    if mg:
        notes.append(f"{mg:,} Snowflake-managed compute pool(s) ({MANAGED_COMPUTE_PREFIX}*: serverless-task and "
                     "upgrade pools) ran statements in the window. SHOW WAREHOUSES never lists them, for any "
                     "role, and there is no warehouse timeout to set; task statements there are capped by "
                     "USER_TASK_TIMEOUT_MS. Shown as Managed compute.")
    below = summary.get("fired_below") or []
    if isinstance(below, (list, tuple)) and below:
        shown = ", ".join(str(n) for n in below[:6]) + (f" and {len(below) - 6:,} more" if len(below) > 6 else "")
        notes.append(f"Timed out below the effective cap on {shown}: the ceiling that fired ('Fired at') is lower "
                     "than the warehouse's cap, so a user, session or client value fired below it (or an "
                     "earlier, lower warehouse value), not that cap.")
    return notes


def _comment_safe(text: object) -> str:
    """A name safe inside a one-line SQL comment: control characters (a newline would end the comment and
    turn the rest of a quoted identifier into a statement) become '?'."""
    return re.sub(r"[\x00-\x1f\x7f]", "?", str(text if text is not None else ""))[:255]


def undo_sql(warehouse: str, current_s: float | None, current_level: str) -> str:
    """The exact undo of a timeout ALTER: back to the warehouse's own value when it set one, otherwise
    UNSET so it inherits the account value (or Snowflake's default) again."""
    if str(current_level or "").strip().upper() == "WAREHOUSE" and current_s is not None:
        return statement_timeout_fix(warehouse, int(current_s))
    # Same identifier rule as the ALTER (statement_timeout_fix validates and upper-cases the name).
    ident = statement_timeout_fix(warehouse, 0).split(" SET ", 1)[0]
    return f"{ident} UNSET STATEMENT_TIMEOUT_IN_SECONDS;"


def _hand_reason(row: Mapping[str, object], tail_days: int, tail_ok: bool) -> str:
    if not tail_ok:
        return "the runtime tail could not be read, so there is no p99 to size a cap from"
    runs = _num(row.get("COMPLETED_RUNS"))
    if not runs:
        return f"no completed statements in the last {tail_days} days"
    if runs < MIN_RUNS_FOR_P99:
        return f"only {int(runs):,} completed statements in the last {tail_days} days (a p99 needs {MIN_RUNS_FOR_P99})"
    if _num(row.get("P99_ELAPSED_SEC")) is None:
        return "no p99 runtime was returned"
    return (f"p99 x {PREFILL_MULTIPLIER} ({humanize_duration(safe_float(row.get('P99_ELAPSED_SEC')) * PREFILL_MULTIPLIER)})"
            f" is above the {humanize_duration(CAP_LADDER_S[-1])} top step")


def fix_script(posture: pd.DataFrame | None, tail_days: int, *, tail_ok: bool = True) -> str:
    """The review-only tightening script for the Uncapped rows; '' when nothing is uncapped.

    Per Uncapped warehouse with a suggestion and an unquoted name: a comment (effective cap and its source,
    p99, longest run, how many completed statements the cap would have cancelled), the ALTER, and the exact
    undo as a COMMENT (running the whole script must not tighten and then immediately undo). Otherwise one
    '-- <warehouse>: uncapped; <reason> ...' line (``tail_ok=False``: the runtime read failed, so the
    reason says so instead of "no completed statements"). Capped warehouses never get an ALTER."""
    if posture is None or posture.empty or "STATUS" not in posture.columns:
        return ""
    unc = posture[posture["STATUS"] == STATUS_UNCAPPED]
    if unc.empty:
        return ""
    lines = [f"-- Review only: OVERWATCH never runs these. Each cap = the smallest step at or above p99 x "
             f"{PREFILL_MULTIPLIER} (completed statements, last {int(tail_days)} days). Delete lines for "
             "warehouses that legitimately run longer."]
    for _, r in unc.iterrows():
        name = str(r["WAREHOUSE_NAME"])
        shown = _comment_safe(name)
        cap = _num(r.get("SUGGESTED_TIMEOUT_SEC"))
        if cap is None:
            lines.append(f"-- {shown}: uncapped; {_hand_reason(r, int(tail_days), tail_ok)} — choose a cap by hand.")
            continue
        if not fixable_name(name):
            lines.append(f"-- {shown}: uncapped; its name needs a quoted identifier — write the ALTER by "
                         f"hand (suggested cap {humanize_duration(cap)}).")
            continue
        runs = safe_float(r.get("COMPLETED_RUNS"))
        would = _num(r.get("WOULD_CANCEL_RUNS"))
        would_txt = "—" if would is None else f"{int(would):,}"
        lines.append(
            f"-- {shown}: effective cap {humanize_duration(r.get('EFFECTIVE_TIMEOUT_SEC'))} "
            f"({r.get('CAP_SOURCE') or 'unknown source'}); p99 {humanize_duration(r.get('P99_ELAPSED_SEC'))}, "
            f"longest {humanize_duration(r.get('MAX_ELAPSED_SEC'))} over {int(runs):,} completed statements; "
            f"a {humanize_duration(cap)} cap would have cancelled {would_txt} of them.")
        lines.append(statement_timeout_fix(name, int(cap)))
        w_val = _num(r.get("WAREHOUSE_TIMEOUT_SEC"))
        lines.append("-- undo: " + undo_sql(name, w_val, str(r.get("WAREHOUSE_LEVEL") or "")))
    return "\n".join(lines)


@dataclass(frozen=True)
class TimeoutImpact:
    """What a cap of ``target`` seconds would have done on ONE warehouse over the last ``days`` days
    (ops_sql.warehouse_timeout_impact, completed statements only).

    ``ok`` False = the read failed or returned no usable row: the impact is UNKNOWN, never none.
    ``over_target`` counts on TOTAL_ELAPSED_TIME, which includes queue and compile time (an upper bound);
    ``exec_over_target`` counts on EXECUTION_TIME alone, so the queue share is visible. ``longest_s`` = the
    longest completed statement's elapsed seconds (None when none completed)."""
    ok: bool
    over_target: int | None = None
    exec_over_target: int | None = None
    longest_s: float | None = None
    days: int = IMPACT_DAYS
    error: str = ""


def parse_timeout_impact(df: pd.DataFrame | None, *, ok: bool, days: int = IMPACT_DAYS,
                         error: str = "") -> TimeoutImpact:
    """The one-row impact frame (columns in any case) -> TimeoutImpact. A failed read, an absent/empty frame
    or a NULL/garbage OVER_TARGET_RUNS is ``ok=False`` (unknown): an aggregate with no GROUP BY always
    returns one row, so anything else is not a verified zero."""
    if not ok or df is None or df.empty:
        return TimeoutImpact(ok=False, days=int(days), error=str(error or ""))
    cols = _cols(df)
    row = df.iloc[0]

    def _get(name: str) -> float | None:
        return _num(row[cols[name]]) if name in cols else None

    over = _get("over_target_runs")
    if over is None:
        return TimeoutImpact(ok=False, days=int(days), error=str(error or "") or "no OVER_TARGET_RUNS value")
    exec_over = _get("exec_over_target_runs")
    return TimeoutImpact(ok=True, over_target=int(over),
                         exec_over_target=None if exec_over is None else int(exec_over),
                         longest_s=_get("max_elapsed_sec"), days=int(days))


def timeout_would_tighten(current_s: float | None, target: int = 3600) -> bool:
    """True when SET = ``target`` would TIGHTEN the warehouse's current value (so the drawer reads the impact
    before generating it). False when the value is unread or already at/below the target."""
    cur = enforced_s(current_s)
    return cur is not None and cur > int(target)


def _plural(n: int, word: str) -> str:
    return f"{n:,} {word}" + ("" if n == 1 else "s")


def tighten_timeout_plan(warehouse: str, current_s: float | None, current_level: str = "", *,
                         target: int = 3600, impact: TimeoutImpact | None = None,
                         override: bool = False) -> dict:
    """Tighten-only decision for a one-click 'SET STATEMENT_TIMEOUT_IN_SECONDS = target' on ``warehouse``.

    The A3 hazard for this lever: a blind SET = 3600 on a warehouse already capped tighter (e.g. 300 s)
    LOOSENS the cap. ``current_s`` is the warehouse's value from SHOW PARAMETERS ... IN WAREHOUSE (it
    already reflects an inherited account value). Mirrors remediation.tighten_suspend_plan: returns
    ``{stmt, undo, message, level, override_needed, override_label}``; ``stmt`` is '' when the current
    value is unread (direction unprovable) or already at/below ``target``; ``level`` is 'warning' | 'info'.

    v4.603 (#33 D1): a tightening is weighed against ``impact`` (what the cap would have cancelled on this
    warehouse in the last ``impact.days`` days). A verified zero -> level 'info', the ALTER is generated.
    Anything above zero, or an impact that is unknown (``impact`` None = not read, or ``ok`` False = the
    read failed) -> level 'warning', ``override_needed`` True, and ``stmt`` / ``undo`` stay '' unless
    ``override`` (the operator ticked the explicit override). Never implies no impact when it is unknown."""
    tgt = int(target)
    cur = enforced_s(current_s)
    base = {"stmt": "", "undo": "", "override_needed": False, "override_label": ""}
    if cur is None:
        return {**base, "level": "warning",
                "message": ("Current STATEMENT_TIMEOUT_IN_SECONDS could not be read. No executable ALTER or "
                            "savings entry is generated until SHOW PARAMETERS returns this warehouse's value.")}
    if cur <= tgt:
        return {**base, "level": "info",
                "message": (f"{warehouse} is already capped at {humanize_duration(cur)}. A "
                            f"{humanize_duration(tgt)} cap would loosen or keep it, so this engine will not "
                            "generate it.")}
    tgt_h = humanize_duration(tgt)
    now_txt = (f"The warehouse value in force is {humanize_duration(cur)} "
               f"({_SOURCE_BY_LEVEL.get(str(current_level or '').strip().upper(), 'Snowflake default')}).")
    alter = {"stmt": statement_timeout_fix(warehouse, tgt), "undo": undo_sql(warehouse, current_s, current_level)}
    held = ("Override ticked: the ALTER below is generated anyway." if override
            else "The ALTER is withheld unless you tick the override.")
    if impact is None or not impact.ok or impact.over_target is None:
        days = impact.days if impact is not None else IMPACT_DAYS
        how = "was not read" if impact is None else "could not be read"
        msg = (f"Impact unknown: the last {days} days of completed statements on {warehouse} {how}, so how many "
               f"a {tgt_h} cap would cancel is not known (not zero). {now_txt} {held}")
        return {**base, **(alter if override else {}), "level": "warning", "message": msg,
                "override_needed": True,
                "override_label": f"Generate the {tgt_h} cap without knowing what it would cancel"}
    longest = "" if impact.longest_s is None else f" (longest {humanize_duration(impact.longest_s)})"
    if impact.over_target > 0:
        n = impact.over_target
        exec_txt = ""
        if impact.exec_over_target is not None:
            exec_txt = (f" Elapsed includes queue and compile time, so this is an upper bound: "
                        f"{impact.exec_over_target:,} of them ran over {tgt_h} on execution time alone.")
        msg = (f"A {tgt_h} cap on {warehouse} would have cancelled {_plural(n, 'completed statement')} in the "
               f"last {impact.days} days{longest}.{exec_txt} {now_txt} {held}")
        return {**base, **(alter if override else {}), "level": "warning", "message": msg,
                "override_needed": True,
                "override_label": (f"I accept that a {tgt_h} cap would have cancelled "
                                   f"{_plural(n, 'completed statement')}: generate it")}
    msg = (f"No completed statement on {warehouse} ran longer than {tgt_h} in the last {impact.days} days"
           f"{longest}, so the cap would have cancelled none of them. {now_txt}")
    return {**base, **alter, "level": "info", "message": msg}
