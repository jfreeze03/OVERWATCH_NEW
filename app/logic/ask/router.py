"""Ask-OVERWATCH router — deterministic question -> answerer.

No AI, no SQL. A question strongly matches an answerer only when EVERY one of its
`require_all` synonym groups is present (the honesty gate); among strong matches
the most keyword hits wins. Zero strong matches -> `answerer is None`, and the UI
renders an honest refusal. Window (`days`) is lifted from the text when stated,
else the caller's default; `company` is always the page filter, never parsed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.logic.ask.registry import REGISTRY
from app.logic.ask.types import Answerer, AskParams

# Word-ish phrases -> trailing-window days. Longest phrase wins (checked first).
_WINDOW_WORDS: tuple[tuple[str, int], ...] = (
    ("today", 1),
    ("yesterday", 2),
    ("this week", 7),
    ("past week", 7),
    ("last week", 7),
    ("this month", 30),
    ("past month", 30),
    ("last month", 30),
    ("this quarter", 90),
    ("past quarter", 90),
    ("last quarter", 90),
    ("this year", 365),
    ("past year", 365),
    ("last year", 365),
)

_DAYS_RE = re.compile(r"\b(\d+)\s*[- ]?(?:days?|d)\b", re.IGNORECASE)  # any width; clamped below
_MAX_DAYS = 365

# Plain single-word signals match on WORD boundaries (so "who" never fires inside
# "whole" and "cs" never inside "docs"); deliberate prefix stems match a word's
# start; multi-word / hyphenated phrases fall back to substring.
_WORD_RE = re.compile(r"[a-z0-9]+")
# spik -> spike/spikes/spiking; spend -> spends/spending/spender/spenders/spent.
_STEMS = frozenset({"spik", "spend"})


@dataclass(frozen=True)
class RouteResult:
    """Outcome of routing. `answerer is None` => honest refusal."""

    answerer: Answerer | None
    params: AskParams
    score: int
    considered: int  # how many answerers were strong candidates


def extract_days(text: str, default: int) -> int:
    """Lift an explicit window from the question, else the default (clamped)."""
    low = text.lower()
    m = _DAYS_RE.search(low)
    if m:
        return max(1, min(_MAX_DAYS, int(m.group(1))))
    for phrase, days in _WINDOW_WORDS:
        # word-boundary match so "this week" does not fire inside "this weekend"
        # / "this weekly", nor "today" inside "todaywalk".
        if re.search(r"\b" + re.escape(phrase) + r"\b", low):
            return days
    return max(1, min(_MAX_DAYS, default))


def _term_matches(term: str, low: str, tokens: frozenset[str]) -> bool:
    if term in _STEMS:
        return any(w.startswith(term) for w in tokens)
    if term.isalnum():          # a plain word -> exact token (word-boundary) match
        return term in tokens
    return term in low          # a phrase / hyphenated form -> substring match


def _hits(low: str, tokens: frozenset[str], terms: tuple[str, ...]) -> int:
    return sum(1 for t in terms if _term_matches(t, low, tokens))


def _strong(low: str, tokens: frozenset[str], ans: Answerer) -> bool:
    """Strong match = every require_all group has at least one hit. An answerer
    with no require_all falls back to needing any keyword."""
    if ans.require_all:
        return all(_hits(low, tokens, group) >= 1 for group in ans.require_all)
    return _hits(low, tokens, ans.keywords) >= 1


def route(
    question: str,
    *,
    default_days: int,
    company: str,
    registry: tuple[Answerer, ...] | list[Answerer] = REGISTRY,
) -> RouteResult:
    """Pick the best strong-matching answerer, or refuse honestly."""
    low = (question or "").lower().strip()
    tokens = frozenset(_WORD_RE.findall(low))
    days = extract_days(low, default_days)
    params = AskParams(days=days, company=company, warehouse="", raw=question or "")

    if not low:
        return RouteResult(answerer=None, params=params, score=0, considered=0)

    strong = [a for a in registry if _strong(low, tokens, a)]
    if not strong:
        return RouteResult(answerer=None, params=params, score=0, considered=0)

    # Rank strong candidates by total keyword hits (require_all terms count too);
    # ties break on registry order (stable via index).
    def _score(a: Answerer) -> int:
        base = _hits(low, tokens, a.keywords)
        for group in a.require_all:
            base += _hits(low, tokens, group)
        return base

    # Higher priority wins (a domain-specific answerer beats a generic one that
    # merely double-counts overlapping keywords); score breaks equal priority.
    best = max(strong, key=lambda a: (a.priority, _score(a)))
    return RouteResult(
        answerer=best, params=params, score=_score(best), considered=len(strong)
    )
