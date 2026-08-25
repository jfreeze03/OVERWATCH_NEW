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

REVERT PATH (no damage to the original app) — delete THREE new paths and revert
THREE marked "ASK-OVERWATCH" wiring blocks:
    delete   app/logic/ask/
    delete   app/ui/pages/ask.py
    delete   tests/test_ask_registry.py
    revert   app/main.py            (the `ask` import + the "Ask": ask.render entry)
    revert   app/config.py          (drop "Ask" from PAGES_BY_PROFILE["DBA"])
    revert   tests/history_locks/test_codex_r2_wave.py  (restore the DBA nav pin to
             ["Watch","Analyze","Govern"] and delete the dict(dba)["More"]==["Ask"] line)
This package imports app.data builders and app.logic.anomaly READ-ONLY and mutates
nothing, so those deletions leave the original app byte-for-byte unchanged. (The
whole feature lives on branch feature/ask-overwatch, so `git checkout main` also
reverts it cleanly.)
"""

from __future__ import annotations

from app.logic.ask.registry import REGISTRY
from app.logic.ask.router import RouteResult, route
from app.logic.ask.types import Answerer, AnswerResult, AskParams, QuerySpec

__all__ = [
    "REGISTRY",
    "AnswerResult",
    "Answerer",
    "AskParams",
    "QuerySpec",
    "RouteResult",
    "route",
]
