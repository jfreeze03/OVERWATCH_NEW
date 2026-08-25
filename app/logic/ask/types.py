"""Ask-OVERWATCH types — the contract every answerer speaks.

Kept Streamlit-free and side-effect-free so the whole registry is unit-testable:
`needs()` only builds SQL strings, `analyze()` is a pure function of the fetched
frames. See app/logic/ask/__init__.py for the design and the revert path.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

import pandas as pd

# Modified-z threshold above which a contributor "stands out from peers". Matches
# the app's robust-outlier convention (median/MAD modified z, ~3.5 = outlier).
OUTLIER_Z = 3.5


@dataclass(frozen=True)
class AskParams:
    """Everything an answerer needs to build its queries, resolved by the router.

    `company` comes from the page filter (never parsed from free text — scope is a
    security boundary, not a guess). `days`/`warehouse` may be lifted from the
    question, else defaulted by the caller.
    """

    days: int
    company: str
    warehouse: str = ""
    raw: str = ""  # the original question, for provenance/telemetry


@dataclass(frozen=True)
class QuerySpec:
    """One builder call an answerer needs. `sql` is the already-built SQL string;
    the UI layer executes it via app.core.query.run at `tier` and hands the frame
    back keyed by `key`."""

    key: str
    sql: str
    tier: str = "recent"
    # Row cap for the fetch. None -> the run() default. Use 0 (uncapped) when the
    # answerer AGGREGATES the frame and a truncated read would distort a total
    # (e.g. summing per-task runs — a capped read understates the denominator).
    max_rows: int | None = None


@dataclass
class AnswerResult:
    """A grounded answer. Every field derives from real query output.

    `confidence`: "grounded" (a real finding), "no_data" (query ran, nothing to
    report — an honest empty state), or "partial" (some inputs were empty).
    """

    intent: str
    headline: str
    bullets: list[str] = field(default_factory=list)
    evidence: pd.DataFrame | None = None
    source: str = ""
    confidence: str = "grounded"
    params: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class Answerer:
    """One question type wired to existing builders.

    `keywords`      — OR signals; any hit makes this answerer a weak candidate.
    `require_all`   — groups of synonyms; the answerer is a STRONG candidate only
                      when every group has at least one hit in the question. This
                      is the honesty gate: no strong candidate -> the router
                      refuses rather than guessing.
    `needs`         — build the SQL specs from resolved params.
    `analyze`       — PURE: (params, {key: frame}) -> AnswerResult.
    """

    intent: str
    title: str
    examples: tuple[str, ...]
    keywords: tuple[str, ...]
    needs: Callable[[AskParams], list[QuerySpec]]
    analyze: Callable[[AskParams, dict[str, pd.DataFrame]], AnswerResult]
    require_all: tuple[tuple[str, ...], ...] = ()
    # Tie-break weight among strong candidates: a more SPECIFIC domain answerer
    # (e.g. cloud-services, gated on an explicit phrase) outranks a generic one so
    # a question naming that domain can't be out-scored by duplicated generic
    # keywords. Higher wins; score breaks equal priority.
    priority: int = 0
