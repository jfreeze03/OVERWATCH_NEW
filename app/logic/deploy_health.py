"""#26 Phase 1: is OVERWATCH deployed and running? Pure — no Streamlit, no Snowflake reads.

Three questions Admin answers from reads it already makes (or one toggle-gated pair):

* **Schema vs build** — ``schema_drift`` compares SCHEMA_VERSION with the migrations this build
  expects. *Missing* means the deployment is behind (apply them); *ahead* means the database holds
  migrations this build does not know, i.e. the deployed app trails the schema (redeploy the app;
  the migrations are fine). The old tab only ever looked for missing rows, so an app left on an
  older build after a newer migration applied still read "All N migrations applied. App X expects
  exactly these."
* **Runtime** — ``runtime_versions`` reads the package versions actually running (local metadata,
  zero queries), so pinning the SiS environment follows observation instead of guesswork.
* **Task health** — ``task_health`` grades SHOW TASKS plus a TASK_HISTORY run summary against the
  task set the migrations leave live. It never claims healthy from a read that failed, came back
  empty, or was truncated, and it never flags "no run in 24h": weekly and monthly crons exist.
"""

from __future__ import annotations

import math
import platform
import warnings
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from importlib import metadata
from typing import Any

import pandas as pd

from app.logic.task_graph import canonical_task_name, parse_task_predecessors

# --------------------------------------------------------------------------- #
# Schema vs build
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class SchemaDrift:
    """SCHEMA_VERSION against the build's expected migrations.

    ``missing``: expected but not applied. ``ahead``: applied but unknown to this build (newer).
    ``build_tip``: the newest migration this build expects. ``db_tip``: the newest applied (None
    when nothing is applied). ``expected_n``: how many migrations the build expects."""

    missing: tuple[int, ...]
    ahead: tuple[int, ...]
    build_tip: int
    db_tip: int | None
    expected_n: int = 0


def applied_versions(values: Iterable[object]) -> set[int]:
    """SCHEMA_VERSION.VERSION values -> ints. Skips None / NaN / non-numeric / non-integral junk
    (a shaped harness hands floats; a hand-inserted row can hold anything)."""
    out: set[int] = set()
    for value in values:
        try:
            number = float(str(value).strip())
        except (TypeError, ValueError):
            continue
        if math.isfinite(number) and number.is_integer() and number > 0:
            out.add(int(number))
    return out


def schema_drift(applied: Iterable[int], expected: Iterable[int]) -> SchemaDrift:
    """``expected`` may be the ``_EXPECTED_MIGRATIONS`` dict itself (its keys are the versions)."""
    have = {int(v) for v in applied}
    want = {int(v) for v in expected}
    return SchemaDrift(
        missing=tuple(sorted(want - have)),
        ahead=tuple(sorted(have - want)),
        build_tip=max(want) if want else 0,
        db_tip=max(have) if have else None,
        expected_n=len(want),
    )


def version_span(versions: Sequence[int], limit: int = 6) -> str:
    """'V162' | 'V162–V163' (a contiguous run) | 'V162, V170' | 'V162, V170, …' (past ``limit``)."""
    vs = sorted({int(v) for v in versions})
    if not vs:
        return ""
    if len(vs) > 1 and vs[-1] - vs[0] == len(vs) - 1:
        return f"V{vs[0]:03d}–V{vs[-1]:03d}"
    limit = max(1, int(limit))
    shown = ", ".join(f"V{v:03d}" for v in vs[:limit])
    return shown + (", …" if len(vs) > limit else "")


def ahead_warning(drift: SchemaDrift, app_version: str) -> str:
    """The redeploy warning when the database is ahead of this build; '' when it is not."""
    if not drift.ahead:
        return ""
    return (f"The database has {len(drift.ahead)} migration(s) this app build does not know "
            f"({version_span(drift.ahead)}). The deployed app ({app_version}, built for "
            f"V001–V{drift.build_tip:03d}) is older than the schema. Redeploy it from the revision "
            "those migrations came from (`snow streamlit deploy --replace`). Until then, pages touched "
            "by those migrations run on this build's older assumptions. The migrations themselves are "
            "not the problem.")


def migrations_clean_message(drift: SchemaDrift) -> str:
    n = drift.expected_n or drift.build_tip
    return (f"All {n} migrations this build expects are applied (V001–V{drift.build_tip:03d}), "
            "and none newer.")


# --------------------------------------------------------------------------- #
# Runtime
# --------------------------------------------------------------------------- #

_RUNTIME_PACKAGES = (
    ("Streamlit", "streamlit"),
    ("pandas", "pandas"),
    ("Altair", "altair"),
    ("Snowpark", "snowflake-snowpark-python"),
)


def runtime_versions() -> dict[str, str]:
    """Python + the four packages the app leans on, as actually installed; '—' when unreadable
    (Snowpark is absent on the floor-compat CI leg and in local dev without it)."""
    out = {"Python": platform.python_version()}
    for label, dist in _RUNTIME_PACKAGES:
        try:
            out[label] = str(metadata.version(dist))
        except (metadata.PackageNotFoundError, ValueError):
            out[label] = "—"
    return out


def runtime_caption(versions: Mapping[str, str]) -> str:
    return "Runtime: " + " · ".join(f"{label} {ver}" for label, ver in versions.items())


# --------------------------------------------------------------------------- #
# Task health
# --------------------------------------------------------------------------- #

TASK_HEALTH_COLUMNS = (
    "TASK_NAME", "STATUS", "STATE", "RUNS_ON", "SUCCEEDED_N", "FAILED_N", "SKIPPED_N", "CANCELLED_N",
    "LAST_SUCCESS_AT", "LAST_FAILURE_AT", "LAST_ERROR_MESSAGE", "NOTE",
)
_SEVERITY_ORDER = {"bad": 0, "warn": 1, "info": 2, "ok": 3}
_UNVERIFIABLE = ("Task health: unverifiable — SHOW TASKS failed for the app's owner role. "
                 "This is NOT evidence the tasks are down.")
_NOT_VISIBLE = ("Task health: SHOW TASKS lists no OVERWATCH tasks for the app's owner role — either "
                "none are deployed or the role holds no privilege on them. This is NOT evidence the "
                "tasks are down.")
NOTE_RUNS_UNVERIFIABLE = ("Run history unverifiable (the TASK_HISTORY read failed); states above "
                          "are current.")
NOTE_NO_RUN_HISTORY = ("No run history visible — TASK_HISTORY returns rows only for tasks the role "
                       "owns or monitors. Not evidence the tasks did not run.")
NOTE_HISTORY_TRUNCATED = ("Run history hit its {limit:,}-row limit (it spans every task the role can "
                          "see); failures may be missing.")


@dataclass(frozen=True)
class TaskHealth:
    """``state``: RUNNING | DEGRADED | FAILING | NOT_VISIBLE | UNVERIFIABLE.
    ``severity``: ok | info | warn | bad (drives the headline's colour)."""

    state: str
    severity: str
    headline: str
    rows: pd.DataFrame = field(compare=False)
    notes: tuple[str, ...] = ()


def _empty_rows() -> pd.DataFrame:
    return pd.DataFrame(columns=list(TASK_HEALTH_COLUMNS))


def _bare(value: object) -> str:
    return canonical_task_name(value).split(".")[-1]


def _text(value: object) -> str:
    """A display string for a cell; '' for None / NaN / NaT."""
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except (TypeError, ValueError):
        pass                                   # list-like cells: pd.isna is elementwise
    return str(value).strip()


def _lower_columns(df: pd.DataFrame) -> dict[str, object]:
    """lower-cased column name -> the frame's own column label (SHOW columns arrive as
    lower-case quoted names off SiS and upper-case in some drivers)."""
    out: dict[str, object] = {}
    for col in df.columns:
        out.setdefault(str(col).strip().lower(), col)
    return out


def _series(df: pd.DataFrame, cols: Mapping[str, object], name: str) -> pd.Series:
    col = cols.get(name)
    if col is None:
        return pd.Series([None] * len(df), index=df.index, dtype=object)
    picked = df[col]
    if isinstance(picked, pd.DataFrame):       # duplicate labels: first wins
        picked = picked.iloc[:, 0]
    return picked


def _numbers(series: pd.Series) -> pd.Series:
    try:
        return pd.to_numeric(series, errors="coerce")
    except (TypeError, ValueError):
        return pd.Series([math.nan] * len(series), index=series.index)


def _times(series: pd.Series) -> pd.Series:
    """Naive timestamps (NaT for junk). Mixed-offset input falls back to UTC-normalised naive."""
    nat = pd.Series([pd.NaT] * len(series), index=series.index, dtype="datetime64[ns]")
    try:
        with warnings.catch_warnings():     # junk cells: pandas warns per-element parsing; NaT is the answer
            warnings.simplefilter("ignore")
            out = pd.to_datetime(series, errors="coerce")
            if not pd.api.types.is_datetime64_any_dtype(out):
                out = pd.to_datetime(series, errors="coerce", utc=True)
        if not pd.api.types.is_datetime64_any_dtype(out):
            return nat
        if getattr(out.dt, "tz", None) is not None:
            out = out.dt.tz_localize(None)
    except (TypeError, ValueError, OverflowError, AttributeError):
        return nat
    return out


def _live_tasks(tasks: pd.DataFrame) -> dict[str, dict]:
    cols = _lower_columns(tasks)
    names = _series(tasks, cols, "name")
    states = _series(tasks, cols, "state")
    schedules = _series(tasks, cols, "schedule")
    preds = _series(tasks, cols, "predecessors")
    reasons = _series(tasks, cols, "last_suspended_reason")
    live: dict[str, dict] = {}
    for i in range(len(tasks)):
        name = _bare(_text(names.iloc[i]))
        if not name or name in live:
            continue
        try:
            parents = [_bare(p) for p in parse_task_predecessors(preds.iloc[i])]
        except (TypeError, ValueError):
            parents = []
        live[name] = {
            "state": _text(states.iloc[i]).lower(),
            "schedule": _text(schedules.iloc[i]),
            "after": [p for p in parents if p],
            "reason": _text(reasons.iloc[i]),
        }
    return live


def _run_summary(runs: pd.DataFrame) -> tuple[dict[str, dict], float | None, bool]:
    """-> ({TASK: counts/timestamps}, max HISTORY_ROWS or None, any named row?)."""
    cols = _lower_columns(runs)
    names = _series(runs, cols, "task_name")
    succeeded = _numbers(_series(runs, cols, "succeeded_n"))
    failed = _numbers(_series(runs, cols, "failed_n"))
    skipped = _numbers(_series(runs, cols, "skipped_n"))
    cancelled = _numbers(_series(runs, cols, "cancelled_n"))
    last_ok = _times(_series(runs, cols, "last_success_at"))
    last_fail = _times(_series(runs, cols, "last_failure_at"))
    errors = _series(runs, cols, "last_error_message")
    hist = _numbers(_series(runs, cols, "history_rows"))
    hist_max = float(hist.max()) if hist.notna().any() else None
    out: dict[str, dict] = {}
    for i in range(len(runs)):
        name = _bare(_text(names.iloc[i]))
        if not name or name in out:
            continue                                  # the NULL-name row only carries HISTORY_ROWS
        out[name] = {
            "succeeded": succeeded.iloc[i], "failed": failed.iloc[i], "skipped": skipped.iloc[i],
            "cancelled": cancelled.iloc[i],
            "last_ok": last_ok.iloc[i], "last_fail": last_fail.iloc[i],
            "error": _text(errors.iloc[i]) or None,
        }
    return out, hist_max, bool(out)


def _count(value: object) -> float:
    try:
        number = float(str(value).strip())
    except (TypeError, ValueError):
        return 0.0
    return number if math.isfinite(number) else 0.0


def _recovered(run: Mapping[str, Any]) -> bool:
    """The newest failure in the window is followed by a success."""
    last_ok, last_fail = run.get("last_ok", pd.NaT), run.get("last_fail", pd.NaT)
    return bool(pd.notna(last_ok) and pd.notna(last_fail) and last_ok > last_fail)


def _grade_runs(run: Mapping[str, Any]) -> tuple[str, str, str]:
    """(status, severity, note) for a STARTED task from its 24h run counts: Failing (bad) when the newest
    failure is not followed by a success, Recovered / Skipped runs / Cancelled runs (warn), else Running."""
    n_failed, n_skipped = _count(run.get("failed")), _count(run.get("skipped"))
    n_cancelled = _count(run.get("cancelled"))
    if n_failed > 0:
        if _recovered(run):
            return "Recovered", "warn", "failed in the window, succeeded since"
        return "Failing", "bad", ""
    if n_skipped > 0:
        return "Skipped runs", "warn", "a run was skipped (the previous run was still going, or a condition was false)"
    if n_cancelled > 0:
        return "Cancelled runs", "warn", "a run was cancelled (by an operator, or its graph run was cancelled)"
    return "Running", "ok", ""


def task_health(tasks: pd.DataFrame | None, runs: pd.DataFrame | None, *, expected: Iterable[str],
                opt_in: Mapping[str, str], retired: Iterable[str], suspended_ok: Mapping[str, str],
                result_limit: int) -> TaskHealth:
    """Grade the OVERWATCH tasks. ``tasks`` is SHOW TASKS (None = the read failed); ``runs`` is
    ``ops_sql.overwatch_task_run_summary`` (None = the read failed). Never raises on odd frames:
    columns are matched case-insensitively and checked before use, numbers and timestamps are
    coerced, and a missing count stays NaN (renders '—')."""
    if tasks is None:
        return TaskHealth("UNVERIFIABLE", "info", _UNVERIFIABLE, _empty_rows())
    live = _live_tasks(tasks) if isinstance(tasks, pd.DataFrame) and not tasks.empty else {}
    if not live:
        # Never 32 false "Not visible" rows: an empty SHOW is one fact, stated once.
        return TaskHealth("NOT_VISIBLE", "info", _NOT_VISIBLE, _empty_rows())

    expected_set = {_bare(n) for n in expected}
    retired_set = {_bare(n) for n in retired}
    opt_in_map = {_bare(k): v for k, v in opt_in.items()}
    suspended_ok_map = {_bare(k): v for k, v in suspended_ok.items()}

    notes: list[str] = []
    run_map: dict[str, dict] = {}
    hist_max: float | None = None
    if runs is None:
        notes.append(NOTE_RUNS_UNVERIFIABLE)
    else:
        run_map, hist_max, any_named = (_run_summary(runs) if isinstance(runs, pd.DataFrame)
                                        else ({}, None, False))
        if not any_named:
            notes.append(NOTE_NO_RUN_HISTORY)
    truncated = hist_max is not None and hist_max >= int(result_limit)
    if truncated:
        notes.append(NOTE_HISTORY_TRUNCATED.format(limit=int(result_limit)))

    rows: list[dict] = []
    started = suspended = failing = opt_in_failing = 0
    for name in sorted(expected_set | set(live)):
        task = live.get(name)
        run = run_map.get(name, {})
        note = ""
        if task is None:
            status, sev = "Not visible", "warn"
            note = ("SHOW TASKS does not list it — not created, dropped, or the owner role holds "
                    "no privilege on it.")
        elif name in retired_set:
            status, sev, note = "Retired", "warn", "dropped by a migration not yet applied"
        elif name in opt_in_map:
            # review r1: an installed, STARTED opt-in task is a real job -- grade its runs (a failing one is
            # never green); suspended/stateless stays 'Opt-in'. Kept out of the expected-set counters.
            note = f"optional — installed by {opt_in_map[name]}"
            status, sev = "Opt-in", "ok"
            if task["state"] == "started":
                graded, g_sev, g_note = _grade_runs(run)
                if graded != "Running":
                    status, sev = graded, g_sev
                    note = f"{note}; {g_note}" if g_note else note
                    opt_in_failing += int(graded == "Failing")
            elif task["state"] == "suspended" and _count(run.get("failed")) > 0:
                if _recovered(run):
                    # review r3: an opt-in trial (EXECUTE TASK on a suspended task) that failed, then succeeded
                    note = f"{note}; failed in the window, succeeded since"
                else:
                    # review r2: FAILED_AND_AUTO_SUSPENDED leaves it suspended -- the terminal form of failing
                    status, sev = "Suspended after failures", "warn"
                    note = f"{note}; its newest run in the window failed and it is suspended"
                    opt_in_failing += 1
        elif name not in expected_set:
            status, sev = "Not in this build", "warn"
            note = ("live, but no migration in this build creates it — a newer migration's task "
                    "(redeploy the app) or one made by hand")
        elif task["state"] == "started":
            started += 1
            status, sev, note = _grade_runs(run)
            failing += int(status == "Failing")
        elif task["state"]:
            suspended += 1
            if name in suspended_ok_map:
                status, sev, note = "Suspended (expected)", "warn", suspended_ok_map[name]
            else:
                status, sev, note = "Suspended", "bad", task["reason"]
        else:
            status, sev, note = "State unknown", "warn", "SHOW TASKS returned no state for it"

        runs_on = ""
        if task is not None:
            runs_on = task["schedule"] or (f"after {', '.join(task['after'])}" if task["after"] else "")
        rows.append({
            "_SEV": _SEVERITY_ORDER[sev],
            "TASK_NAME": name,
            "STATUS": status,
            "STATE": (task["state"] or None) if task is not None else None,
            "RUNS_ON": runs_on or None,
            "SUCCEEDED_N": run.get("succeeded", math.nan),
            "FAILED_N": run.get("failed", math.nan),
            "SKIPPED_N": run.get("skipped", math.nan),
            "CANCELLED_N": run.get("cancelled", math.nan),
            "LAST_SUCCESS_AT": run.get("last_ok", pd.NaT),
            "LAST_FAILURE_AT": run.get("last_fail", pd.NaT),
            "LAST_ERROR_MESSAGE": run.get("error"),
            "NOTE": note or None,
        })

    frame = pd.DataFrame(rows).sort_values(["_SEV", "TASK_NAME"], kind="stable")
    sevs = set(frame["_SEV"])
    frame = frame.drop(columns=["_SEV"]).reset_index(drop=True)
    for col in ("SUCCEEDED_N", "FAILED_N", "SKIPPED_N", "CANCELLED_N"):
        frame[col] = _numbers(frame[col])
    for col in ("LAST_SUCCESS_AT", "LAST_FAILURE_AT"):
        frame[col] = _times(frame[col])

    if _SEVERITY_ORDER["bad"] in sevs:
        state, severity = "FAILING", "bad"
    elif _SEVERITY_ORDER["warn"] in sevs:
        state, severity = "DEGRADED", "warn"
    else:
        state, severity = "RUNNING", "ok"
    if severity == "ok" and notes:
        # A failed, invisible or truncated run history cannot prove health: never green.
        severity = "info"
    headline = (f"{started} of {len(expected_set)} OVERWATCH tasks started · {suspended} suspended · "
                f"{failing} failing (last 24h)"
                + (f" · {opt_in_failing} opt-in failing" if opt_in_failing else ""))
    return TaskHealth(state, severity, headline, frame[list(TASK_HEALTH_COLUMNS)], tuple(notes))
