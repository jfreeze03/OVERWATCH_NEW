"""Ask-OVERWATCH — a grounded answerer registry (ISOLATED, revertible feature).

WHY THIS EXISTS
    A narrow question-answering surface that does NOT do generic text-to-SQL over
    semantic views (the approach that produced plausible-but-wrong answers). Instead
    a free-text question is ROUTED to one of a small set of hand-built "answerers",
    each of which runs EXISTING, tested builders and returns a grounded result —
    every number in the answer is real query output, never invented. Unmapped
    questions get an HONEST refusal that lists what the app can actually answer.

DESIGN (four steps)
    1. Route   — deterministic keyword/phrase match picks one answerer + extracts
                 params (window). No AI, reproducible. No match -> honest refusal.
    2. Run     — the answerer's `needs()` yields QuerySpecs; the UI layer runs them.
    3. Analyze — the answerer's PURE `analyze(frames)` returns an AnswerResult.
    4. Narrate — the deterministic headline is authoritative; the UI may OPTIONALLY
                 ask Cortex to rephrase the already-grounded result (invents nothing).

REVERT PATH (no damage to the rest of the app). The feature is on main and its wiring
carries no "ASK-OVERWATCH" markers, so revert it by hand:
    delete   app/logic/ask/
    delete   app/ui/pages/ask.py
    delete   tests/test_ask_registry.py and tests/test_ask_pricing.py
    revert   app/main.py            (the `ask` import + the "Ask": ask.render entry)
    revert   app/config.py          (drop "Ask" from PAGES_BY_PROFILE["DBA"] and the
             NAV_GROUPS "Ask OVERWATCH" entry)
    revert   tests/history_locks/test_codex_r2_wave.py  (the DBA nav pin back to
             ["Watch","Analyze","Govern"]; delete the dict(dba)["Ask OVERWATCH"]==["Ask"] assert)
    revert   tests/history_locks/test_brief_landing.py  (the asserts that "Ask" is in the
             DBA profile and comes last)
    revert   tests/test_usage_sim.py  ("app.ui.pages.ask" in the expected patched modules, and
             the "Ask" _FIRST_PAINT_BUDGET row: its coverage test requires a budget row for
             exactly the DBA pages)
    revert   tests/usage_sim.py and tests/test_pages_shaped.py  (the `ask` page import and
             its place in the patched-module list)
    then     run `grep -rnE "app[./]logic[./]ask|app[./]ui[./]pages[./]ask" tests/` and remove
             or adjust every remaining hit: many bug-hunt and UI-wave locks import this
             package or read the page, and the list grows, so it is not copied here.
This package imports app.data builders and app.logic.anomaly READ-ONLY and mutates
nothing, so once the wiring above is reverted the rest of the app is unchanged.
The Admin 'Ask demand' panel and mart_sql.ask_demand_summary read only APP_USAGE and
survive a revert (tests/test_app_telemetry.py locks that they never import this package).
"""

from __future__ import annotations

from app.logic.ask.registry import REGISTRY
from app.logic.ask.router import RouteResult, question_stem, route
from app.logic.ask.types import Answerer, AnswerResult, AskParams, QuerySpec

__all__ = [
    "REGISTRY",
    "AnswerResult",
    "Answerer",
    "AskParams",
    "QuerySpec",
    "RouteResult",
    "question_stem",
    "route",
]
