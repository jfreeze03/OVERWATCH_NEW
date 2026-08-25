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

REVERT PATH (no damage to the original app)
    Delete  app/logic/ask/  and  app/ui/pages/ask.py , then remove the two clearly
    marked "ASK-OVERWATCH" wiring blocks in app/main.py and app/config.py. This
    package imports app.data builders and app.logic.anomaly READ-ONLY and mutates
    nothing, so removal leaves the original app byte-for-byte unchanged.
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
