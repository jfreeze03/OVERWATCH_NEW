"""Headless user-usage simulator (Option A) — a queries-per-interaction profiler.

Drives the OVERWATCH Streamlit-in-Snowflake app through streamlit.testing.v1.AppTest
exactly as a user would — boot the shell, navigate each page, permute the scope bar
(company x window x database), tweak a filter — with the read layer replaced by RECORDING
stubs. Each stub returns the SAME correctly-shaped frame the shaped-render harness uses
(so every page renders its POPULATED branches, not just the empty ones) and, on the way,
logs every query the interaction would issue.

Why this is the right metric for SiS: the app reruns the whole script on every widget
interaction and re-issues queries unless a cache absorbs them. Because run()/run_batch are
STUBBED here, the app's tiered st.cache_data is bypassed — so the counts are the *cold
logical* queries a page fires per interaction: the "how chatty is this page" number that
drives Snowflake round-trip cost. It classifies each query's data source
(SNOWFLAKE.ACCOUNT_USAGE = slow, DBA_MAINT_DB.OVERWATCH mart = fast, INFORMATION_SCHEMA /
SHOW = metadata) so you can see where the expensive scans land, and flags duplicate SQL
issued more than once in a single interaction.

It needs NO Snowflake connection and runs in CI (see tests/test_usage_sim.py). For real
round-trip / latency / credits you want the LIVE variant instead: run this same AppTest
driver but do NOT stub run(); fall app.core.session._connect back to st.connection("snowflake")
(secrets) and wrap the physical app.core.query._execute / _execute_batch seams, then read
the app's own query_telemetry() and join QUERY_ID -> ACCOUNT_USAGE.QUERY_HISTORY.

Run:
    python tests/usage_sim.py                 # full sweep (all DBA pages x a few scopes), text report
    python tests/usage_sim.py --json          # machine-readable
    python tests/usage_sim.py --pages Overview Operations Security
    python tests/usage_sim.py --scope default # single scope only

Shaping + navigation are REUSED from tests/test_pages_shaped.py (single source of truth for
the shaped-frame convention), so this tool and the render-contract test never drift.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import logging
import os
import re
import sys
from collections import Counter
from unittest import mock

# The stubbed reads never touch Snowflake, but streamlit/snowpark still log connection
# probes; silence them so the report is the only thing on stdout/stderr.
for _noisy in ("snowflake", "snowflake.connector", "streamlit"):
    logging.getLogger(_noisy).setLevel(logging.ERROR)

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for _p in (_ROOT, _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# Reuse the shaped-frame builder + navigation + app entry from the render-contract harness.
from test_pages_shaped import (  # noqa: E402
    _entry,
    _nav_to,
    _shaped_from_sql,
)

from app.config import PAGES_BY_PROFILE  # noqa: E402
from app.core.state import FILTER_DEFAULTS  # noqa: E402

DEFAULT_PAGES = list(PAGES_BY_PROFILE["DBA"])
# A small, meaningful scope matrix: the landing default, the bounded 'Last month' window,
# and the all-company view. Widen with --scope / by editing SCOPES.
SCOPES = {
    "default": {},
    "last_month": {"flt_days": "LAST_MONTH"},
    "company_all": {"flt_company": "ALL"},
}

# Company-scope UDFs live in DBA_MAINT_DB.OVERWATCH but are NOT marts — a QUERY_HISTORY
# scan that merely calls COMPANY_FOR_WAREHOUSE(col) must still classify as account_usage.
_COMPANY_UDFS = {
    "COMPANY_FOR_WAREHOUSE", "COMPANY_FOR_DATABASE", "COMPANY_FOR_USER", "COMPANY_FOR_ROLE",
}
_MART_REF = re.compile(r"DBA_MAINT_DB\.OVERWATCH\.([A-Z0-9_]+)")


def classify_source(sql: str) -> str:
    """Coarsely classify a query by its most expensive data source."""
    s = str(sql or "").upper()
    if "SNOWFLAKE.ACCOUNT_USAGE." in s or "SNOWFLAKE.ORGANIZATION_USAGE." in s:
        return "account_usage"                       # slow / metered ACCOUNT_USAGE scan
    if s.lstrip().startswith("SHOW ") or "INFORMATION_SCHEMA." in s:
        return "metadata"                            # SHOW / INFORMATION_SCHEMA
    if any(ref not in _COMPANY_UDFS for ref in _MART_REF.findall(s)):
        return "mart"                                # fast pre-aggregated mart/fact/view
    return "other"                                   # session tables, literals, etc.


# --------------------------------------------------------------------------- #
# Recording layer: the same shaped frames the render harness returns, plus a ledger.
# --------------------------------------------------------------------------- #
_LEDGER: list[dict] = []
_CURRENT = {"page": "(boot)", "scope": "(boot)"}


def _record(kind: str, sql, read_page, key, tier) -> None:
    text = str(sql or "")
    _LEDGER.append({
        "flow_page": _CURRENT["page"],
        "scope": _CURRENT["scope"],
        "kind": kind,
        "read_page": read_page,
        "key": key,
        "tier": tier or "",
        "source": classify_source(text),
        "sql_hash": hashlib.md5(text.encode("utf-8")).hexdigest()[:10],
        "sql_len": len(text),
    })


def _rec_run(*args, **kwargs):
    sql = args[0] if args else kwargs.get("sql", "")
    _record("single", sql, kwargs.get("page"), kwargs.get("key"), kwargs.get("tier"))
    return _shaped_from_sql(sql, kwargs.get("source", "stub"))


def _rec_batch(specs, **kwargs):
    for s in (specs or []):
        _record("batch", s.get("sql", ""), kwargs.get("page") or s.get("page"),
                s.get("key"), s.get("tier") or kwargs.get("tier"))
    return {s.get("key"): _shaped_from_sql(s.get("sql", ""), s.get("source", "stub"))
            for s in (specs or [])}


def _rec_mart_first(mart_sql, live_sql="", **kwargs):
    # Hot path: the mart answers (a shaped frame is non-empty), so exactly ONE mart read.
    # A real mart MISS would additionally issue the live ACCOUNT_USAGE leg — noted in the report.
    _record("mart_first", mart_sql, kwargs.get("page"), kwargs.get("key"), "hourly")
    res = _shaped_from_sql(mart_sql, kwargs.get("mart_source", "stub"))
    if res.df.empty:
        res = _shaped_from_sql(live_sql, kwargs.get("live_source", "stub"))
    return res


def _fake_execute(*_args, **_kwargs):
    return True, "stubbed"


_READ_STUBS = {
    "run": _rec_run,
    "run_batch": _rec_batch,
    "run_batch_mixed": _rec_batch,
    "run_mart_first": _rec_mart_first,
    "execute_statement": _fake_execute,
}


def _patched_modules():
    """The UI modules whose directly-imported read names must be stubbed. Mirrors the
    tests/test_pages_shaped.py::_stub_shaped list; kept honest by test_usage_sim.py."""
    import app.main as main_mod
    from app.ui import ai_panel, components, security_center, workbench
    from app.ui import decision_studio as ds_render
    from app.ui.pages import (
        admin,
        alerts,
        ask,
        brief,
        control_room,
        cost,
        decision_studio,
        operations,
        overview,
        security,
    )
    from app.ui.pages.cost_parts import ai_chargeback, compare, contract, optimize, spend, unit_costs
    return main_mod, [
        main_mod, components, ai_panel, ds_render, security_center, workbench,
        overview, control_room, cost, operations, alerts, security, admin, brief,
        ask, decision_studio, ai_chargeback, compare, contract, optimize, spend, unit_costs,
    ]


@contextlib.contextmanager
def recording_stubs():
    """Install the recording read-stubs + the base render bypasses across every UI module."""
    from app.config import DEFAULT_SETTINGS
    from app.ui import ai_panel, components
    main_mod, modules = _patched_modules()
    settings = dict(DEFAULT_SETTINGS)
    settings["_source"] = "stub"
    with contextlib.ExitStack() as stack:
        def patch(target, name, value):
            stack.enter_context(mock.patch.object(target, name, value))

        patch(main_mod, "connection_available", lambda: True)
        patch(main_mod, "current_role", lambda: "SNOW_SYSADMINS")
        patch(main_mod, "_schema_floor_breach", lambda: None)  # bypass startup schema gate
        for module in modules:
            for fname, fstub in _READ_STUBS.items():
                if hasattr(module, fname):
                    patch(module, fname, fstub)
            if hasattr(module, "current_role"):
                patch(module, "current_role", lambda: "SNOW_SYSADMINS")
            if hasattr(module, "load_settings"):
                patch(module, "load_settings", lambda _page: dict(settings))
        patch(components, "load_settings", lambda _page: dict(settings))
        patch(ai_panel, "cortex_complete", lambda *a, **k: (True, "stub"))
        yield


# --------------------------------------------------------------------------- #
# Driver
# --------------------------------------------------------------------------- #
def _set_scope(at, overrides: dict) -> None:
    base = dict(FILTER_DEFAULTS)
    base.update(overrides or {})
    for key, value in base.items():
        at.session_state[key] = value


def _summarize(page: str, scope: str, ledger: list[dict], error: str) -> dict:
    by_source = Counter(r["source"] for r in ledger)
    hashes = Counter(r["sql_hash"] for r in ledger)
    return {
        "page": page,
        "scope": scope,
        "total": len(ledger),
        "account_usage": by_source.get("account_usage", 0),
        "mart": by_source.get("mart", 0),
        "metadata": by_source.get("metadata", 0),
        "other": by_source.get("other", 0),
        "by_tier": dict(Counter(r["tier"] for r in ledger)),
        "by_kind": dict(Counter(r["kind"] for r in ledger)),
        "distinct_keys": len({r["key"] for r in ledger if r["key"]}),
        "duplicates": {h: c for h, c in hashes.items() if c > 1},
        "error": error,
    }


def _new_apptest(timeout: float):
    from streamlit.testing.v1 import AppTest
    at = AppTest.from_function(_entry, default_timeout=timeout)
    _set_scope(at, {})
    at.run()
    return at


def simulate(pages=None, scopes=None, *, timeout: float = 30.0,
             measure_rerun: bool = True) -> dict:
    """Run the flow matrix and return a structured report. One AppTest per scope (pages
    share it, as a real session would); rebooted if a flow raises."""
    pages = list(pages or DEFAULT_PAGES)
    scope_items = list((scopes or SCOPES).items())
    flows: list[dict] = []
    rerun = None
    with recording_stubs():
        for scope_label, overrides in scope_items:
            at = _new_apptest(timeout)
            for page in pages:
                _set_scope(at, overrides)
                _CURRENT["page"], _CURRENT["scope"] = page, scope_label
                _LEDGER.clear()
                error = ""
                try:
                    _nav_to(at, page)
                    at.run()
                    if at.exception:
                        error = str(at.exception)
                except Exception as exc:  # noqa: BLE001 - a flow failure is data, not fatal
                    error = repr(exc)
                flows.append(_summarize(page, scope_label, list(_LEDGER), error))
                if error:                                   # bad state -> fresh shell for the next page
                    at = _new_apptest(timeout)

        if measure_rerun:
            rerun = _measure_filter_rerun(timeout)
    return {"pages": pages, "scopes": [s for s, _ in scope_items], "flows": flows, "rerun": rerun}


def _measure_filter_rerun(timeout: float) -> dict | None:
    """Cold nav to a heavy page, then the re-query cost of tweaking one filter WITHOUT
    navigating — the core Streamlit whole-script-rerun concern."""
    page = "Operations" if "Operations" in DEFAULT_PAGES else DEFAULT_PAGES[-1]
    try:
        at = _new_apptest(timeout)
        _set_scope(at, {})
        _CURRENT["page"], _CURRENT["scope"] = page, "rerun:nav"
        _LEDGER.clear()
        _nav_to(at, page)
        at.run()
        nav = _summarize(page, "rerun:nav", list(_LEDGER), str(at.exception or ""))
        _CURRENT["scope"] = "rerun:filter"
        _LEDGER.clear()
        at.session_state["flt_schema_contains"] = "PUBLIC"
        at.run()
        tweak = _summarize(page, "rerun:filter", list(_LEDGER), str(at.exception or ""))
        return {"page": page, "on_nav": nav, "on_filter_tweak": tweak}
    except Exception as exc:  # noqa: BLE001
        return {"page": page, "error": repr(exc)}


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #
def format_report(report: dict) -> str:
    flows = sorted(report["flows"], key=lambda f: (-f["total"], f["page"]))
    lines: list[str] = []
    lines.append("OVERWATCH usage simulation — COLD logical queries per interaction")
    lines.append("(run()/run_batch stubbed => cache bypassed; counts are per-interaction reads)")
    lines.append("")
    header = f"{'PAGE':<16}{'SCOPE':<13}{'TOTAL':>6}{'AU':>5}{'MART':>6}{'META':>6}{'DUP':>5}  NOTE"
    lines.append(header)
    lines.append("-" * len(header))
    for f in flows:
        dup = sum(c - 1 for c in f["duplicates"].values())
        note = "ERROR: " + f["error"][:48] if f["error"] else ""
        lines.append(
            f"{f['page']:<16}{f['scope']:<13}{f['total']:>6}{f['account_usage']:>5}"
            f"{f['mart']:>6}{f['metadata']:>6}{dup:>5}  {note}"
        )
    lines.append("")
    lines.append("AU = SNOWFLAKE.ACCOUNT_USAGE scans (slow/metered) — the cost centers to watch.")

    # Per-page ACCOUNT_USAGE hotspots (max over scopes)
    au_by_page: dict[str, int] = {}
    for f in report["flows"]:
        au_by_page[f["page"]] = max(au_by_page.get(f["page"], 0), f["account_usage"])
    hot = sorted((v, k) for k, v in au_by_page.items() if v)
    if hot:
        lines.append("")
        lines.append("Heaviest ACCOUNT_USAGE pages (max across scopes):")
        for v, k in reversed(hot):
            lines.append(f"  {k:<18} {v} AU scan(s)")

    # Duplicate SQL within a single interaction (same query issued 2+ times)
    dup_flows = [f for f in report["flows"] if f["duplicates"]]
    if dup_flows:
        lines.append("")
        lines.append("Redundant identical SQL within one interaction (same query issued 2+ times).")
        lines.append("  Same-mechanism dups are cache-absorbed at runtime; run()-vs-batch dups can be real:")
        for f in sorted(dup_flows, key=lambda x: -sum(x["duplicates"].values())):
            worst = sorted(f["duplicates"].items(), key=lambda kv: -kv[1])[:3]
            frag = ", ".join(f"{h}x{c}" for h, c in worst)
            lines.append(f"  {f['page']:<16} [{f['scope']}]  {frag}")

    rr = report.get("rerun")
    if rr and "error" not in rr:
        nav_n = rr["on_nav"]["total"]
        tw_n = rr["on_filter_tweak"]["total"]
        lines.append("")
        lines.append(f"Filter-tweak rerun ({rr['page']}): cold nav issues {nav_n} queries; "
                     f"changing one filter re-issues {tw_n} (whole-script rerun).")
    errs = [f for f in report["flows"] if f["error"]]
    if errs:
        lines.append("")
        lines.append(f"!! {len(errs)} flow(s) errored — see NOTE column above.")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="OVERWATCH headless usage simulator")
    ap.add_argument("--pages", nargs="*", help="subset of pages (default: all DBA pages)")
    ap.add_argument("--scope", choices=sorted(SCOPES), help="a single scope (default: all)")
    ap.add_argument("--json", action="store_true", help="emit JSON instead of a text report")
    ap.add_argument("--no-rerun", action="store_true", help="skip the filter-tweak rerun probe")
    ap.add_argument("--timeout", type=float, default=30.0)
    args = ap.parse_args(argv)

    scopes = {args.scope: SCOPES[args.scope]} if args.scope else SCOPES
    report = simulate(pages=args.pages, scopes=scopes, timeout=args.timeout,
                      measure_rerun=not args.no_rerun)
    print(json.dumps(report, indent=2, default=str) if args.json else format_report(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
