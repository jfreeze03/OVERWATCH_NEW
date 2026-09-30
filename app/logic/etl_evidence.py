"""Next-Fifty #14 Phase 1: "why did this ETL task fail or slow down?" — the verdict over
``etl_control_sql.run_task_evidence_scan``. Pure module: no Streamlit, no reads, no server clock.

The scan returns one row per matched Snowflake CALL of the task (newest first), or a single row with a
NULL CALL_QUERY_ID when none matched, plus uncapped window totals (MATCHED_CALLS, FAILED_CALLS,
ALL_CALLS_ELAPSED_MS). ``task_evidence_lines`` turns that into ordered lines — the failure verdict first,
then where the newest CALL's Snowflake time went — and never claims more than QUERY_HISTORY shows: inside
its ~45-minute lag an absent or clean CALL is "not caught up yet", never "the Informatica side".
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from datetime import date, datetime

import pandas as pd

from app.logic.formulas import account_today, humanize_bytes, humanize_duration, safe_float

QH_LAG_MIN = 45                  # ACCOUNT_USAGE.QUERY_HISTORY latency: absence inside it is not evidence
EVIDENCE_QUEUE_PCT = 20.0        # queued share of the CALL's statement time that reads as "slow: queued"
EVIDENCE_COMPILE_PCT = 30.0      # compile share of the statement time that reads as "compilation-heavy"
EVIDENCE_OUTSIDE_PCT = 50.0      # Snowflake CALL time below this share of the task window = time outside it
EVIDENCE_LOCAL_SPILL_GB = 1.0    # local spill worth naming (any remote spill always is)
INFORMATICA_SIDE_SENTENCE = (
    "No failed Snowflake CALL found in the window — the failure was likely on the Informatica side "
    "(the Snowflake statements it ran succeeded).")
_TAG_ASK_DOC = "docs/design/INFORMATICA_QUERY_TAG_ASK.md"
_GIB = 1024 ** 3
_DISPLAY_COLUMNS = (
    "CALL_START_TIME", "EXECUTION_STATUS", "ERROR_CODE", "ERROR_MESSAGE", "CALL_DATABASE", "WAREHOUSE_NAME",
    "CALL_ELAPSED_MS", "CALL_QUEUED_MS", "CHILD_STATEMENTS", "FAILED_CHILD_STATEMENTS", "QUEUED_PCT",
    "QUEUE_WAREHOUSE", "QUEUED_OVERLOAD_MS", "QUEUED_PROVISIONING_MS", "COMPILE_MS", "EXEC_MS",
    "SPILL_LOCAL_GB", "SPILL_REMOTE_GB", "CHILD_ERROR_MESSAGE", "FAILED_CHILD_QUERY_ID", "CALL_QUERY_ID",
)


@dataclass(frozen=True)
class EvidenceLine:
    """One rendered finding. ``level`` is 'error' | 'warn' | 'clean' | 'no_data_yet' (the page maps the
    first two to st.error / st.warning and the last two to the shared empty_state vocabulary)."""

    level: str
    text: str
    hint: str = ""


def _txt(value: object, default: str = "—") -> str:
    """A cell as display text; NULL / NaN / 'nan' / 'None' render as the em-dash (or ``default``)."""
    if value is None:
        return default
    try:
        if bool(pd.isna(value)):
            return default
    except (TypeError, ValueError):   # a list-like cell: not a scalar NULL
        pass
    text = str(value).strip()
    return text if text and text.lower() not in ("nan", "none", "<na>", "nat") else default


def _as_date(value: object) -> date | None:
    """A task start as a calendar date: Timestamp / datetime / date / ISO string. None for NULL / NaT
    (a datetime SUBCLASS, so checked first), a number (the shaped harness types the column as float)
    or anything unparseable."""
    if value is None:
        return None
    try:
        if bool(pd.isna(value)):
            return None
    except (TypeError, ValueError):
        return None
    if isinstance(value, datetime):          # pandas Timestamp is a datetime subclass
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return date.fromisoformat(value.strip()[:10])
        except ValueError:
            return None
    return None


def evidence_floor_days(task_start: object, *, today: date | None = None) -> int:
    """The literal QUERY_HISTORY pruning floor (whole days back from now) that still covers a task that
    started on ``task_start``: its age in days + 2, clamped to [2, 366]. 0 = no floor (correct, just a
    slower scan) when the start is missing or unparseable. ``today`` defaults to the ACCOUNT's today."""
    start = _as_date(task_start)
    if start is None:
        return 0
    ref = today if today is not None else account_today()
    return max(2, min(366, (ref - start).days + 2))


def task_first_start(df: pd.DataFrame | None, task: str) -> date | None:
    """The earliest parseable TASK_START_DTTM of ``task``'s rows (retry attempts included), or None."""
    if df is None or df.empty or "TASK_NAME" not in df.columns or "TASK_START_DTTM" not in df.columns:
        return None
    rows = df[df["TASK_NAME"].map(lambda v: _txt(v, "")) == str(task or "").strip()]
    starts = [d for d in (_as_date(v) for v in rows["TASK_START_DTTM"].tolist()) if d is not None]
    return min(starts) if starts else None


def evidence_run_id(df: pd.DataFrame | None) -> str:
    """The RUN_ID a task table shows (workflow_runtimes_scan projects one run on every row), as text for
    run_task_evidence_scan's ``run_id``; "" when the frame has none, so the drill falls back to the
    latest-run predicates."""
    if df is None or df.empty or "RUN_ID" not in df.columns:
        return ""
    return _txt(df["RUN_ID"].iloc[0], "")


def _task_frame(df: pd.DataFrame | None, failed_statuses: Collection[str]) -> pd.DataFrame:
    """One row per task: _N name, _F any attempt failed (0/1), _R slowest attempt (sec), _A attempts."""
    if df is None or df.empty or "TASK_NAME" not in df.columns:
        return pd.DataFrame(columns=["_N", "_F", "_R", "_A"])
    names = df["TASK_NAME"].map(lambda v: _txt(v, ""))
    failed = {str(s).strip().upper() for s in failed_statuses}
    status = (df["TASK_STATUS"].map(lambda v: _txt(v, "").upper()) if "TASK_STATUS" in df.columns
              else pd.Series("", index=df.index))
    runtime = (df["RUNTIME_SEC"].map(safe_float) if "RUNTIME_SEC" in df.columns
               else pd.Series(0.0, index=df.index))
    frame = pd.DataFrame({"_N": names, "_F": status.isin(failed).astype(int), "_R": runtime, "_A": 1})
    frame = frame[frame["_N"] != ""]
    if frame.empty:
        return pd.DataFrame(columns=["_N", "_F", "_R", "_A"])
    grouped = frame.groupby("_N", sort=False).agg(_F=("_F", "max"), _R=("_R", "max"), _A=("_A", "sum"))
    return grouped.reset_index().sort_values(["_F", "_R"], ascending=[False, False], kind="stable")


def evidence_task_options(df: pd.DataFrame | None, failed_statuses: Collection[str]) -> list[str]:
    """The run's task names for the picker, one per task (a retried task's attempt rows collapse): tasks
    with a failed attempt first, then slowest first. Tolerates a frame without TASK_STATUS or
    RUNTIME_SEC; [] without TASK_NAME."""
    return [str(n) for n in _task_frame(df, failed_statuses)["_N"].tolist()]


def evidence_task_labels(df: pd.DataFrame | None, failed_statuses: Collection[str]) -> dict[str, str]:
    """Picker labels keyed by task name: 'NAME · failed · 2 attempts' (parts only when true).

    No runtime (review F12/F20): a running task's RUNTIME_SEC is measured to now, so it changes on every
    5-minute cache refresh, and on SiS's streamlit 1.52 a changed option label re-creates the selectbox,
    which snaps the pick back to the first task. The runtime stays in the table above."""
    out: dict[str, str] = {}
    for rec in _task_frame(df, failed_statuses).itertuples(index=False):
        name, failed, attempts = str(rec[0]), int(rec[1]), int(rec[3])
        bits = [name]
        if failed:
            bits.append("failed")
        if attempts > 1:
            bits.append(f"{attempts} attempts")
        out[name] = " · ".join(bits)
    return out


def _when(value: object) -> str:
    """A CALL start as 'Sep 27, 22:14' (account time as stored); '—' when missing."""
    if value is None:
        return "—"
    try:
        if bool(pd.isna(value)):
            return "—"
    except (TypeError, ValueError):
        pass
    if isinstance(value, datetime):
        return f"{value:%b} {value.day}, {value:%H:%M}"
    return _txt(value)


def _code(value: object) -> str:
    """An error code as text: a zero-padded string ('002043') is kept verbatim, a float artifact of a
    numeric cell (100132.0) loses its '.0'; '—' when missing."""
    if isinstance(value, float) and value == value and value.is_integer():
        return str(int(value))
    return _txt(value)


def _is_set(frame: pd.DataFrame, col: str) -> pd.Series:
    """Boolean mask: the numeric flag column is > 0 (a missing column is all False)."""
    if col not in frame.columns:
        return pd.Series(False, index=frame.index)
    return frame[col].map(safe_float) > 0


def task_evidence_lines(df: pd.DataFrame | None, *, task: str) -> list[EvidenceLine]:
    """The drill's findings for ``task``, most important first.

    Branches, in order: (1) no CONTROL_STATUS rows; (2) no matching CALL (lag-hedged inside
    QH_LAG_MIN, else the direct-SQL / QUERY_TAG hint); (3) a failed CALL — an error (naming the
    CALL's database) when the newest attempt failed, a warning when a later attempt succeeded, followed by the
    newest CALL's own reading (its caught statement failures, or, for a task CONTROL_STATUS still failed,
    the Informatica side — hedged inside QH_LAG_MIN and when none of its statements were visible);
    (4) a successful CALL whose statements failed; (5) CONTROL_STATUS says failed but no Snowflake CALL
    or statement did — the exact Informatica-side sentence only when every CALL is listed and every
    CALL's statements were seen, else a hedge. When no matched CALL ran in CONTROL_STATUS's own database
    (a same-named procedure, possibly another environment), the drill waits out the lag with one hedge,
    and after it leads with that warning and never reports the other database's failure as an error;
    (6) where the newest CALL's time went (queued, spill, compile, and — outside QH_LAG_MIN only — time
    outside Snowflake from the SQL window totals); (7) clean when nothing above warned and the task did
    not fail."""
    name = str(task or "").strip() or "this task"
    if df is None or df.empty:
        return [EvidenceLine("no_data_yet",
                             f"{name} has no CONTROL_STATUS rows for this run — nothing to explain.")]
    first = df.iloc[0]
    age = safe_float(first.get("END_AGE_MIN"), default=float("inf"))
    recent = age < QH_LAG_MIN
    _ago = (f"the task ended {int(age)} min ago" if age >= 1
            else "the task ended moments ago or is still running")
    has_call = (df["CALL_QUERY_ID"].map(lambda v: _txt(v, "") != "") if "CALL_QUERY_ID" in df.columns
                else pd.Series(False, index=df.index))
    calls = df[has_call]
    if calls.empty:
        if recent:
            return [EvidenceLine("no_data_yet",
                                 f"No CALL of {name} in QUERY_HISTORY yet — it lags up to ~{QH_LAG_MIN} min "
                                 f"and {_ago}. Re-check shortly.")]
        return [EvidenceLine(
            "no_data_yet", f"No Snowflake CALL of {name} ran in its window.",
            hint=("A task that sends its SQL directly instead of as a CALL (typically an M_* mapping), or "
                  "whose name differs from its procedure, can't be matched by name. Exact matching needs the "
                  f"Informatica QUERY_TAG ({_TAG_ASK_DOC})."))]

    out: list[EvidenceLine] = []
    # review F11 + r2: the SQL keeps only CONTROL_STATUS's own database when any CALL ran there; when none did,
    # every name match was kept -- possibly another environment's run of the same procedure. Inside the lag
    # the task's own CALL may simply not have landed, so wait; after it, say so FIRST and never headline
    # another database's failure as this task's error.
    ctl_db = _txt(first.get("CONTROL_DATABASE"), "").upper()
    dbs = (sorted({_txt(v, "").upper() for v in calls["CALL_DATABASE"].tolist()} - {""})
           if "CALL_DATABASE" in calls.columns else [])
    # review r3: only a KNOWN different database is another environment. A CALL whose database could not be
    # resolved (no current database in its session, an unqualified name) may be this task's own run.
    other_db = bool(ctl_db) and bool(dbs) and ctl_db not in dbs
    unknown_db = bool(ctl_db) and "CALL_DATABASE" in calls.columns and not dbs
    _where = ", ".join(dbs)
    if other_db and recent:
        return [EvidenceLine(
            "no_data_yet", f"No CALL of {name} in {ctl_db} (CONTROL_STATUS's database) in QUERY_HISTORY yet — "
                           f"it lags up to ~{QH_LAG_MIN} min and {_ago}. The same-named CALL(s) below ran in "
                           f"{_where}, likely another environment. Re-check shortly.")]
    if other_db:
        out.append(EvidenceLine(
            "warn", f"No CALL of {name} ran in {ctl_db} (CONTROL_STATUS's database) in this window; the CALL(s) "
                    f"shown match by procedure name in {_where} — check that is this environment's run before "
                    "acting on them."))
    if unknown_db:
        out.append(EvidenceLine(
            "warn", f"The CALL(s) of {name} shown ran with no resolvable database (no current database in their "
                    f"session and an unqualified name) — check they are {ctl_db}'s run before acting on them."))
    latest = calls.iloc[0]
    wh = _txt(latest.get("WAREHOUSE_NAME"))
    task_failed = safe_float(first.get("IS_TASK_FAILED")) > 0
    failed_calls = calls[_is_set(calls, "IS_CALL_FAILED")]
    n_calls = int(safe_float(first.get("MATCHED_CALLS"), default=float(len(calls))))
    n_failed = int(safe_float(first.get("FAILED_CALLS"), default=float(len(failed_calls))))
    kid_failed = calls[_is_set(calls, "FAILED_CHILD_STATEMENTS")]
    # a hedge below already says the CALL's statements were not visible (the separate
    # 'No statements were found' line would only repeat it)
    blind_hedged = False
    too_many = (f"{n_calls:,} CALLs of {name} matched, more than this drill lists — too many to rule out a "
                "failed statement; open them in Operations ▸ Queries.")
    if n_failed > 0 or not failed_calls.empty:
        f = failed_calls.iloc[0] if not failed_calls.empty else latest
        err = _txt(f.get("ERROR_MESSAGE"), "") or _txt(f.get("CHILD_ERROR_MESSAGE"), "") \
            or "no error text recorded"
        if safe_float(latest.get("IS_CALL_FAILED")) > 0:
            _db = _txt(f.get("CALL_DATABASE"), "")
            out.append(EvidenceLine(
                "warn" if other_db else "error",
                f"Failed: {err[:300]} (error {_code(f.get('ERROR_CODE'))}) — the Snowflake CALL of "
                f"{name}{f' in {_db}' if _db else ''} on {_txt(f.get('WAREHOUSE_NAME'))} at "
                f"{_when(f.get('CALL_START_TIME'))}. Open its query profile below."))
        else:
            out.append(EvidenceLine(
                "warn", f"Failed then retried: {max(n_failed, len(failed_calls)):,} of {max(n_calls, 1):,} "
                        f"CALL attempt(s) of {name} failed ({err[:200]}); the latest attempt succeeded."))
            # review F15: the retry succeeding is not the end of it — judge the newest CALL on its own
            n_kid_latest = int(safe_float(latest.get("FAILED_CHILD_STATEMENTS")))
            if n_kid_latest > 0:
                out.append(EvidenceLine(
                    "warn", f"The latest CALL returned success but {n_kid_latest:,} statement(s) inside it "
                            f"failed ({_txt(latest.get('CHILD_ERROR_MESSAGE'))[:200]}) — the procedure likely "
                            "caught the error; Informatica may have failed the task on its return value."))
            elif task_failed and recent:
                out.append(EvidenceLine(
                    "no_data_yet", f"CONTROL_STATUS failed the task, but QUERY_HISTORY lags up to "
                                   f"~{QH_LAG_MIN} min and {_ago}; re-check before concluding where the final "
                                   "failure was."))
            elif task_failed and n_calls > len(calls):
                out.append(EvidenceLine("no_data_yet", too_many))
            elif task_failed and safe_float(latest.get("CHILD_STATEMENTS")) <= 0:
                blind_hedged = True
                out.append(EvidenceLine(
                    "no_data_yet", "The latest CALL succeeded, but none of its statements were visible in "
                                   "QUERY_HISTORY, so a failure the procedure caught can't be ruled out; "
                                   "CONTROL_STATUS still failed the task, so the final failure may be on the "
                                   "Informatica side."))
            elif task_failed:
                out.append(EvidenceLine(
                    "warn", "The latest CALL succeeded, yet CONTROL_STATUS failed the task — the final "
                            "failure was likely on the Informatica side."))
    elif not kid_failed.empty:
        k = kid_failed.iloc[0]
        n_kid = int(safe_float(k.get("FAILED_CHILD_STATEMENTS")))
        out.append(EvidenceLine(
            "warn", f"The CALL returned success but {n_kid:,} statement(s) inside it failed "
                    f"({_txt(k.get('CHILD_ERROR_MESSAGE'))[:200]}) — the procedure likely caught the error; "
                    "Informatica may have failed the task on its return value."))
    elif task_failed:
        blind = calls[~_is_set(calls, "CHILD_STATEMENTS")]
        if recent:
            out.append(EvidenceLine(
                "no_data_yet", f"No failed Snowflake CALL yet — QUERY_HISTORY lags up to ~{QH_LAG_MIN} min "
                               f"and {_ago}; re-check before concluding the failure was on the Informatica "
                               "side."))
        elif n_calls > len(calls):
            # every CALL must be seen before 'they all succeeded' can be claimed (the rows are capped)
            out.append(EvidenceLine("no_data_yet", too_many))
        elif not blind.empty:
            # review F14/F19: 'the statements it ran succeeded' needs statements SEEN — with none linked, a
            # failed MERGE the procedure caught (branch 4's case) is indistinguishable from none at all
            blind_hedged = True
            lead = (f"The CALL of {name} succeeded, but none of its statements were visible in QUERY_HISTORY"
                    if len(calls) == 1 else
                    f"Every CALL of {name} succeeded, but {len(blind):,} of the {len(calls):,} showed no "
                    "statements in QUERY_HISTORY")
            out.append(EvidenceLine(
                "no_data_yet", lead + ", so a failure the procedure caught can't be ruled out; the task's "
                                      "failure may be on the Informatica side."))
        else:
            out.append(EvidenceLine("warn", INFORMATICA_SIDE_SENTENCE))


    # (6) where the newest CALL's Snowflake time went — always evaluated
    qpct = safe_float(latest.get("QUEUED_PCT"))
    if qpct >= EVIDENCE_QUEUE_PCT:
        qwh = _txt(latest.get("QUEUE_WAREHOUSE"), "") or wh
        prov = safe_float(latest.get("QUEUED_PROVISIONING_MS")) >= safe_float(latest.get("QUEUED_OVERLOAD_MS"))
        why = ("waiting for the warehouse to resume (provisioning) — keep it warm across the schedule; "
               "sizing up buys nothing here" if prov
               else "waiting behind other work (overload) — add a cluster or move this step off the busy "
                    "window")
        out.append(EvidenceLine("warn", f"Slow: {qpct:.0f}% of its statements' time was queued on {qwh} — "
                                        f"mostly {why}."))
    remote = safe_float(latest.get("SPILL_REMOTE_GB"))
    local = safe_float(latest.get("SPILL_LOCAL_GB"))
    if remote > 0:
        out.append(EvidenceLine("warn", f"Slow: its statements spilled {humanize_bytes(remote * _GIB)} to "
                                        "remote storage — the warehouse is undersized for this step."))
    elif local >= EVIDENCE_LOCAL_SPILL_GB:
        out.append(EvidenceLine("warn", f"Slow: its statements spilled {humanize_bytes(local * _GIB)} to "
                                        "local disk."))
    child_ms = safe_float(latest.get("CHILD_ELAPSED_MS"))
    compile_ms = safe_float(latest.get("COMPILE_MS"))
    if child_ms > 0 and 100.0 * compile_ms / child_ms >= EVIDENCE_COMPILE_PCT:
        out.append(EvidenceLine("warn", f"Slow: compilation took {100.0 * compile_ms / child_ms:.0f}% of its "
                                        f"statements' Snowflake time ({humanize_duration(compile_ms, 'ms')})."))
    window_sec = safe_float(first.get("TASK_WINDOW_SEC"))
    calls_sec = safe_float(first.get("ALL_CALLS_ELAPSED_MS")) / 1000.0
    # review F13: an Informatica-side attribution, so lag-gated like the verdict: inside QH_LAG_MIN (a
    # running task has END_AGE_MIN 0 and a window growing to now) a retry's CALL may not have landed yet
    if not recent and window_sec >= 60 and 100.0 * calls_sec / window_sec < EVIDENCE_OUTSIDE_PCT:
        out.append(EvidenceLine(
            "warn", f"Most of the task's time was outside Snowflake: its CALL(s) ran "
                    f"{humanize_duration(calls_sec, 's')} of the task's {humanize_duration(window_sec, 's')} "
                    f"window ({100.0 * calls_sec / window_sec:.0f}%) — the rest was Informatica-side (waits, "
                    "data transfer, scheduling)."))
    no_children = safe_float(latest.get("CHILD_STATEMENTS")) <= 0
    if no_children and not blind_hedged:
        out.append(EvidenceLine("no_data_yet", "No statements were found inside the CALL's session — the "
                                               "breakdown covers the CALL alone."))
    # (7) clean only when nothing warned AND CONTROL_STATUS did not fail the task (a failed task inside the
    # lag window is hedged above, never called healthy)
    if not task_failed and not any(line.level in ("error", "warn") for line in out):
        out.insert(0, EvidenceLine(
            "clean", f"Snowflake side looks healthy for {name}: " + (
                "its CALL succeeded." if no_children else
                f"the CALL succeeded with under {EVIDENCE_QUEUE_PCT:.0f}% queued and no remote spill.")))
    return out


def evidence_display_frame(df: pd.DataFrame | None) -> pd.DataFrame:
    """The per-CALL table: rows with a CALL only, in a fixed reading order (the columns present). The
    task-level helpers (TASK_*, ATTEMPTS, END_AGE_MIN, the window totals, IS_* flags, CONTROL_DATABASE)
    stay out — they feed the lines above, and a total must never be re-derived from these capped rows.
    CALL_DATABASE stays in: which environment's procedure each CALL was (review F11)."""
    if df is None or df.empty or "CALL_QUERY_ID" not in df.columns:
        return pd.DataFrame(columns=[c for c in _DISPLAY_COLUMNS if df is not None and c in df.columns])
    rows = df[df["CALL_QUERY_ID"].map(lambda v: _txt(v, "") != "")]
    return rows[[c for c in _DISPLAY_COLUMNS if c in rows.columns]].reset_index(drop=True)
