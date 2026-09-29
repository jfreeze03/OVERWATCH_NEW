"""The morning digest's measured grounding (Next-Fifty #24, V165) -- pure, no Streamlit, no Snowflake.

SP_DAILY_DIGEST (V165) checks every figure in the Cortex draft against the FACTS it gave the model and sends a
templated digest, labelled not AI-written, when any figure does not match or Cortex fails. ``check_digest`` is
the Python mirror of that rule; ``digest_provenance`` turns a DAILY_DIGEST row into the label the Brief and
Overview digest expanders show (the MEASURED result, never a fixed "grounded").

Every pattern / keyword / scale literal here is copied byte-for-byte into the proc by outputs/gen_v165.py (which
never imports app/); tests/test_digest_grounding_parity.py compares them with the LATEST SP_DAILY_DIGEST body.
The patterns are backslash-free ([0-9], [.], [$]) so one literal is valid in Python re and in a Snowflake
string inside a $$ body.

The rule, per distinct figure the draft states (after dates, clock times, identifier-like tokens such as WH_X1,
p95 or V112, and list markers are stripped): a figure matches a FACT when
  * unit -- a $ figure binds only a *_USD fact, a % (or 'percent') figure only a *_PCT fact, a bare figure any;
  * noun -- the word right after it (credits, critical, high, minutes, GB, queries, failed, tasks, alerts,
    hours, days) must be named by the fact key;
  * value -- within half a step of the figure's shown precision or 0.5% (ai_grounding's tolerance), after a
    k / m / b (thousand / million / billion) scale word; the half step is inclusive (DIGEST_TOL_SLACK).
A draft with no figures passes. 'n/a' facts license nothing.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any

from app.logic.ai_grounding import GroundingCheck

# (pattern, flags) applied in order, each match replaced by ' ' (REGEXP_REPLACE(..., ' ' [, 1, 0, flags]))
DIGEST_STRIP_PATTERNS: tuple[tuple[str, str], ...] = (
    ("[0-9]{4}-[0-9]{2}-[0-9]{2}([ T][0-9]{1,2}:[0-9]{2}(:[0-9]{2})?)?", ""),   # ISO dates / timestamps
    ("[0-9]{1,2}:[0-9]{2}(:[0-9]{2})?", ""),                                     # clock times
    ("[A-Za-z_]+[0-9][A-Za-z0-9_]*", ""),                                        # WH_X1, p95, V112, Q3
    ("[(][0-9]{1,2}[)]|#[0-9]{1,2}", ""),                                        # (1) / #1 list markers
    ("^[ *#]*[0-9]{1,2}[.)] ", "m"),                                             # '1. ' / '2) ' line starts
)
DIGEST_FIGURE_PATTERN = "[$]?([0-9]{1,3}(,[0-9]{3})+|[0-9]+)([.][0-9]+)?( ?[a-z]+)?( ?%)?"   # flags 'i'
DIGEST_NUM_PATTERN = "[0-9][0-9,]*([.][0-9]+)?"
DIGEST_WORD_PATTERN = "[A-Za-z]+"
DIGEST_FACT_PATTERN = "[A-Z][A-Z0-9_]*=[0-9]+([.][0-9]+)?"
# the word right after a figure -> the fact-key substring it binds to (first match wins; 'x%' = prefix)
DIGEST_KEYWORDS: tuple[tuple[str, str], ...] = (
    ("credit%", "CREDIT"), ("critical", "CRITICAL"), ("high", "HIGH"), ("minute%", "MINUTE"),
    ("gb", "_GB"), ("quer%", "QUER"), ("fail%", "FAIL"), ("task%", "TASK"), ("alert%", "ALERT"),
    ("hour%", "HOUR"), ("day%", "DAY"),
)
DIGEST_SCALE_WORDS: tuple[tuple[tuple[str, ...], int], ...] = (
    (("k", "thousand"), 1000), (("m", "mm", "mn", "million"), 1000000), (("b", "bn", "billion"), 1000000000),
)
DIGEST_PCT_WORDS: tuple[str, ...] = ("percent", "pct")
DIGEST_REL_TOL = 0.005          # == ai_grounding._REL_TOL (tests/test_digest_grounding_parity.py)
# The half step is INCLUSIVE, but the comparison runs in doubles (Snowflake FLOAT too): fact 1.25 shown as
# '1.3%' differs by 0.050000000000000044 against a tolerance of 0.05000000000000000277. The tolerance is at
# least 0.5% of the figure, so double noise is ~1e-13 of it at any scale; a 1e-9 RELATIVE slack absorbs it
# (an absolute epsilon does not at billion scale) and admits nothing a rounded figure could exploit.
DIGEST_TOL_SLACK = 1.000000001
# The migration that started measuring (a row written before it carries no grounding columns).
DIGEST_GROUNDING_MIGRATION = 165
# The run() source label of the digest read (Brief + Overview). It names the check only once V165 is applied:
# before that the live proc checks nothing (review r1 W4/W15; app/ui/schema_gate.py: a claim of a new behaviour
# asks has_migration first). Neither label says "grounded" (tests/test_digest_grounding.py).
DIGEST_SOURCE_CHECKED = "DAILY_DIGEST (Cortex draft; figures checked against the exec board, templated on mismatch)"
DIGEST_SOURCE_UNCHECKED = "DAILY_DIGEST (Cortex draft)"


def digest_source(grounded: bool) -> str:
    """The digest read's source label: ``grounded`` is the page's ``has_migration(165, page)``."""
    return DIGEST_SOURCE_CHECKED if grounded else DIGEST_SOURCE_UNCHECKED


def _flags(f: str) -> int:
    return re.M if "m" in f else 0


def strip_non_figures(body: str) -> str:
    """The proc's five REGEXP_REPLACE strips, in order."""
    text = body or ""
    for pat, f in DIGEST_STRIP_PATTERNS:
        text = re.sub(pat, " ", text, flags=_flags(f))
    return text


def _keyword(word: str) -> str | None:
    for pat, key in DIGEST_KEYWORDS:
        if (pat.endswith("%") and word.startswith(pat[:-1])) or word == pat:
            return key
    return None


def _facts(facts: str) -> list[tuple[str, float]]:
    out: list[tuple[str, float]] = []
    for m in re.finditer(DIGEST_FACT_PATTERN, facts or ""):
        key, _, raw = m.group().partition("=")
        out.append((key, float(raw)))
    return out


def check_digest(body: str, facts: str) -> GroundingCheck:
    """Mirror of the proc: which figures in ``body`` match no FACT (unit + noun bound, within rounding).

    ``checked`` counts DISTINCT figures (as written, first-seen order), like the proc's GROUP BY on the token."""
    rows = _facts(facts)
    seen: dict[str, bool] = {}
    for m in re.finditer(DIGEST_FIGURE_PATTERN, strip_non_figures(body), flags=re.I):
        tok = m.group().strip()
        if tok in seen:
            continue
        num_m = re.search(DIGEST_NUM_PATTERN, tok)
        num = num_m.group() if num_m else ""
        word_m = re.search(DIGEST_WORD_PATTERN, tok)
        word = word_m.group().lower() if word_m else ""
        unit = ("usd" if tok.startswith("$") else
                "pct" if "%" in tok or word in DIGEST_PCT_WORDS else "num")
        scale = next((s for words, s in DIGEST_SCALE_WORDS if word in words), 1)
        try:
            val = float(num.replace(",", "")) * scale
        except ValueError:
            seen[tok] = False
            continue
        dec = len(num.split(".", 1)[1]) if "." in num else 0
        tol = max(0.5 * 10 ** -dec * scale, DIGEST_REL_TOL * val)
        key = _keyword(word)
        seen[tok] = any(
            (unit == "num" or (unit == "usd" and fk.endswith("_USD")) or (unit == "pct" and fk.endswith("_PCT")))
            and (key is None or key in fk) and abs(fv - val) <= tol * DIGEST_TOL_SLACK
            for fk, fv in rows)
    return GroundingCheck(checked=len(seen), ungrounded=tuple(t for t, ok in seen.items() if not ok))


# ---------------------------------------------------------------------------------------------- provenance
@dataclass(frozen=True)
class DigestProvenance:
    ai_written: bool | None      # None = not measured (a row written before V165, or read before V165 applied)
    title: str                   # the expander title, without the date
    chip: str
    tone: str                    # 'ok' | 'warn' | '' (theme.chip states)
    detail: str | None           # one caption line under the chip, or None
    show_draft: bool             # a withheld AI draft exists (AI_BODY) and may be shown on request


AI_TITLE = "AI morning narrative"
TEMPLATE_TITLE = "Morning digest (templated, not AI-written)"


def _tri(v: Any) -> bool | None:
    """Snowflake BOOLEAN -> True/False/None; tolerant of numpy bools, NaN, 0/1 floats and 'true'/'false'."""
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return None
    if isinstance(v, str):
        s = v.strip().lower()
        return True if s in ("true", "1", "y", "yes") else False if s in ("false", "0", "n", "no") else None
    try:
        return bool(v)
    except (TypeError, ValueError):
        return None


def _text(v: Any) -> str:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return ""
    return str(v).strip()


def digest_provenance(row: Any) -> DigestProvenance:
    """What the latest DAILY_DIGEST row IS: an AI draft whose figures all matched the facts, an AI draft with no
    figures, a template sent because the draft's figures did not match (show_draft), a template sent because
    Cortex returned nothing, or a row whose grounding was never measured (written before V165)."""
    get = row.get if hasattr(row, "get") else (lambda _k, _d=None: None)
    source = _text(get("BODY_SOURCE")).upper()
    ok = _tri(get("GROUNDING_OK"))
    try:
        n = int(float(get("FIGURES_CHECKED") or 0))
    except (TypeError, ValueError):
        n = 0
    if source == "TEMPLATE":
        why = ("the AI draft stated figures that are not in the exec-board facts" if ok is False
               else "Cortex returned no digest")
        bad = _text(get("UNGROUNDED"))
        return DigestProvenance(False, TEMPLATE_TITLE, f"Templated, not AI-written: {why}", "warn",
                                (f"Unmatched figures in the withheld draft: {bad}" if bad else None),
                                bool(_text(get("AI_BODY"))))
    if source == "AI":
        if ok is True:
            chip = (f"AI-written; all {n} figures match the exec-board facts" if n > 0
                    else "AI-written; it states no figures to check")
            return DigestProvenance(True, AI_TITLE, chip, "ok" if n > 0 else "", None, False)
        return DigestProvenance(True, AI_TITLE, "AI-written; figures not verified", "warn", None, False)
    return DigestProvenance(None, AI_TITLE, "Figures not checked", "",
                            f"This digest has no grounding record: it was written before "
                            f"V{DIGEST_GROUNDING_MIGRATION}, or V{DIGEST_GROUNDING_MIGRATION} is not applied yet.",
                            False)
