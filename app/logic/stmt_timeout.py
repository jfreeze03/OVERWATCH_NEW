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
guard for the alert drawer's one-click "Statement timeout 1h" lever.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Mapping

import pandas as pd

from .formulas import humanize_duration, safe_float
from .remediation import statement_timeout_fix

SNOWFLAKE_DEFAULT_STMT_TIMEOUT_S = 172_800      # == admin._SNOWFLAKE_DEFAULT_STMT_TIMEOUT_S (parity-tested)
SNOWFLAKE_MAX_STMT_TIMEOUT_S = 604_800          # what 0 (and anything above it) enforces
CAP_LADDER_S = (300, 600, 900, 1800, 3600, 7200, 14400, 28800, 43200, 86400)
PREFILL_MULTIPLIER = 3
MIN_RUNS_FOR_P99 = 100
TAIL_MIN_DAYS, TAIL_MAX_DAYS = 30, 90
# Review C21: each warehouse's SHOW PARAMETERS is its own entry in the process-wide metadata-tier cache
# (query._fetch_metadata, max_entries=128, LRU), shared with SHOW DATABASES, the user directory, the
# schema-version gates and the Admin probes. 40 reads + the account SHOW + SHOW WAREHOUSES stay within a
# third of that store (tests/test_stmt_timeout.py pins the ratio), so one toggle cannot evict the rest.
MAX_WAREHOUSES_READ = 40
STATUS_UNCAPPED, STATUS_CAPPED, STATUS_UNREAD, STATUS_NOT_VISIBLE = "Uncapped", "Capped", "Unread", "Not visible"
POSTURE_COLUMNS = ["WAREHOUSE_NAME", "STATUS", "EFFECTIVE_TIMEOUT_SEC", "CAP_SOURCE", "COMPLETED_RUNS",
                   "P99_ELAPSED_SEC", "MAX_ELAPSED_SEC", "TIMEOUT_CANCELLED_RUNS", "SUGGESTED_TIMEOUT_SEC",
                   "WOULD_CANCEL_RUNS", "WAREHOUSE_LEVEL", "WAREHOUSE_TIMEOUT_SEC"]

_STATUS_ORDER = {STATUS_UNCAPPED: 0, STATUS_CAPPED: 1, STATUS_UNREAD: 2, STATUS_NOT_VISIBLE: 3}
_SOURCE_BY_LEVEL = {"WAREHOUSE": "Warehouse", "ACCOUNT": "Account"}
_UNQUOTED = re.compile(r"^[A-Z_][A-Z0-9_$]*$")
_NUMERIC_COLS = ("EFFECTIVE_TIMEOUT_SEC", "COMPLETED_RUNS", "P99_ELAPSED_SEC", "MAX_ELAPSED_SEC",
                 "TIMEOUT_CANCELLED_RUNS", "SUGGESTED_TIMEOUT_SEC", "WOULD_CANCEL_RUNS",
                 "WAREHOUSE_TIMEOUT_SEC")
_TAIL_COLS = ("COMPLETED_RUNS", "P99_ELAPSED_SEC", "MAX_ELAPSED_SEC", "TIMEOUT_CANCELLED_RUNS")


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


def empty_universe_state(company: str, *, tail_ok: bool, show_ok: bool, active: int = 0) -> tuple[str, str, str]:
    """(empty_state kind, message, which read's error to show: 'tail' | 'show' | '') when
    ``warehouse_universe`` returned nothing to read (review C16, house rule 8: 'unavailable' = the read failed).

    A company scope lists only the runtime tail's warehouses, so a FAILED tail is 'unavailable', never a
    verified-empty scope. ALL scope lists SHOW WAREHOUSES (the tail is only its fallback), so a failed SHOW
    is 'unavailable'. ``active``: tail warehouses SHOW does not list (the company's warehouses all Not
    visible)."""
    if str(company or "ALL").strip().upper() != "ALL":
        if not tail_ok:
            return ("unavailable", "The completed-runtime tail could not be read, so this company's warehouses "
                    "are unknown (a company scope lists only the warehouses active in the window).", "tail")
        if active:
            return ("no_data_yet", f"The {active:,} warehouse(s) active for this company in the window are not "
                    "listed by SHOW WAREHOUSES (dropped, renamed, or not visible to the app role), so there is "
                    "no timeout to read.", "")
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


def warehouse_universe(show_df: pd.DataFrame | None, tail_df: pd.DataFrame | None,
                       company: str) -> tuple[list[str], list[str]]:
    """(warehouses to read, warehouses NOT visible to SHOW).

    ALL scope: every warehouse SHOW WAREHOUSES lists (idle ones included), or the runtime-tail names when
    SHOW is unusable. A company scope: the tail's names (already company-scoped by COMPANY_FOR_WAREHOUSE,
    so only warehouses active in the window) that SHOW also lists. ``not_visible`` = tail names SHOW did
    not list (dropped, renamed, or not visible to the app role), only when SHOW was usable.

    At most MAX_WAREHOUSES_READ are read (one SHOW each): warehouses with the longest completed runs come
    first, then the rest by name. Both lists are returned sorted by name."""
    show = _names(show_df, "name")
    tail = _names(tail_df, "WAREHOUSE_NAME")
    show_ok = bool(show)
    show_set, tail_set = set(show), set(tail)
    if str(company or "ALL").strip().upper() == "ALL":
        pool = show_set if show_ok else tail_set
    else:
        pool = (tail_set & show_set) if show_ok else tail_set
    not_visible = sorted(tail_set - show_set) if show_ok else []
    rank = _tail_rank(tail_df)
    prioritised = sorted(pool, key=lambda n: (n not in rank, -rank.get(n, 0.0), n))
    return sorted(prioritised[:MAX_WAREHOUSES_READ]), not_visible


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


def timeout_posture(read_names: Iterable[str], params: Mapping[str, tuple[float | None, str]],
                    account_s: float | None, tail_df: pd.DataFrame | None,
                    not_visible: Iterable[str] = ()) -> pd.DataFrame:
    """One row per warehouse, exactly POSTURE_COLUMNS.

    STATUS: Uncapped (effective >= 48h) / Capped / Unread (its SHOW failed) / Not visible (ran in the
    window but SHOW WAREHOUSES does not list it). Tail values the runtime read did not return stay NaN
    (rendered as the dash), never 0. SUGGESTED_TIMEOUT_SEC and WOULD_CANCEL_RUNS (the completed statements
    in the window that cap would have cancelled) are set on Uncapped rows only. Sorted Uncapped, Capped,
    Unread, Not visible, then longest run first."""
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
        })
    for name in not_visible:
        t = tail.get(name)
        rows.append({
            "WAREHOUSE_NAME": name, "STATUS": STATUS_NOT_VISIBLE, "EFFECTIVE_TIMEOUT_SEC": float("nan"),
            "CAP_SOURCE": None, "COMPLETED_RUNS": _tail_value(t, "COMPLETED_RUNS"),
            "P99_ELAPSED_SEC": _tail_value(t, "P99_ELAPSED_SEC"),
            "MAX_ELAPSED_SEC": _tail_value(t, "MAX_ELAPSED_SEC"),
            "TIMEOUT_CANCELLED_RUNS": _tail_value(t, "TIMEOUT_CANCELLED_RUNS"),
            "SUGGESTED_TIMEOUT_SEC": float("nan"), "WOULD_CANCEL_RUNS": float("nan"),
            "WAREHOUSE_LEVEL": None, "WAREHOUSE_TIMEOUT_SEC": float("nan"),
        })
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
    """Counts by status; ``read`` = warehouses whose timeout was read (Uncapped + Capped)."""
    status = df["STATUS"] if df is not None and not df.empty and "STATUS" in df.columns else pd.Series([], dtype=str)
    counts = {s: int((status == s).sum()) for s in _STATUS_ORDER}
    return {"read": counts[STATUS_UNCAPPED] + counts[STATUS_CAPPED], "uncapped": counts[STATUS_UNCAPPED],
            "capped": counts[STATUS_CAPPED], "unread": counts[STATUS_UNREAD],
            "not_visible": counts[STATUS_NOT_VISIBLE]}


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


def tighten_timeout_plan(warehouse: str, current_s: float | None, current_level: str = "", *,
                         target: int = 3600) -> dict:
    """Tighten-only decision for a one-click 'SET STATEMENT_TIMEOUT_IN_SECONDS = target' on ``warehouse``.

    The A3 hazard for this lever: a blind SET = 3600 on a warehouse already capped tighter (e.g. 300 s)
    LOOSENS the cap. ``current_s`` is the warehouse's value from SHOW PARAMETERS ... IN WAREHOUSE (it
    already reflects an inherited account value). Mirrors remediation.tighten_suspend_plan: returns
    ``{stmt, undo, message, level}``; ``stmt`` is '' when the current value is unread (direction
    unprovable) or already at/below ``target``; ``level`` is 'warning' | 'info' | 'none'."""
    tgt = int(target)
    cur = enforced_s(current_s)
    if cur is None:
        return {"stmt": "", "undo": "", "level": "warning",
                "message": ("Current STATEMENT_TIMEOUT_IN_SECONDS could not be read. No executable ALTER or "
                            "savings entry is generated until SHOW PARAMETERS returns this warehouse's value.")}
    if cur <= tgt:
        return {"stmt": "", "undo": "", "level": "info",
                "message": (f"{warehouse} is already capped at {humanize_duration(cur)}. A "
                            f"{humanize_duration(tgt)} cap would loosen or keep it, so this engine will not "
                            "generate it.")}
    return {"stmt": statement_timeout_fix(warehouse, tgt),
            "undo": undo_sql(warehouse, current_s, current_level), "level": "none", "message": ""}
