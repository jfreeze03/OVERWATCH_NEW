"""Numeric grounding checks for AI text — pure, no Streamlit/Snowflake.

``numbers_preserved``: the strict Ask rule (a REWORDING may introduce no number), moved verbatim from
ui/pages/ask.py. ``check_grounding``: the advisory rule for free-form evaluations (Next-Fifty #24) —
every $ amount and % in the answer is matched against the numbers in the evidence text within
rounding tolerance; the ones with no match are listed so the reader can verify them (they may be
legitimately derived: sums, annualized, re-rounded). The evidence rows reach Cortex serialized as
``COL=value; ...`` (ai_prompts._serialize_rows) with no '$', so the match is numeric, never textual.

What may LICENSE a figure is kept narrow on purpose, or the check goes vacuous (adversarial review):
only the evidence section of a prompt (never the instruction text), never the pieces of a date /
time / timestamp or an ID-like token, a ``COL=value`` pair only for the unit its column names (a
*_USD column licenses a $ figure, a *_PCT column a %, an hours/count/credits column neither), and a
×100 percentage only from a ratio-like value (≤ 1.5).
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

_NUM_RE = re.compile(r"\d[\d,]*\.?\d*")
_PCT_RE = re.compile(r"(\d[\d,]*\.?\d*)\s*%")
# a figure: thousands-grouped (no trailing list comma) or plain, optional decimals
_FIG = r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?"
_USD_RE = re.compile(rf"\$\s?({_FIG})(?:\s?(thousand|million|billion|bn|mm|[kmb])(?![A-Za-z]))?", re.I)
_PCT_FIG_RE = re.compile(rf"({_FIG})\s*%")
_EVIDENCE_NUM_RE = re.compile(rf"(?:{_FIG})(?:[eE][+-]?\d+)?")
_WS_RE = re.compile(r"\s+")
_SCALE = {"k": 1e3, "t": 1e3, "m": 1e6, "b": 1e9}
_REL_TOL = 0.005        # 0.5% relative; the absolute half-step of the shown precision also applies
_MIN_USD = 1.0          # '$0' / sub-dollar unit rates are not claims worth flagging
_TRIVIAL_PCTS = (0.0, 100.0)
_RATIO_MAX = 1.5        # only a ratio-like value (0.4231) licenses its ×100 percentage

# where the evidence starts in the house prompts (ai_prompts._assemble / incident_narrative_prompt)
_EVIDENCE_MARKERS = ("EVIDENCE ROWS:", "RANKED CANDIDATE CAUSES")
_TIMESTAMP_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}(?:[ T]\d{1,2}:\d{2}(?::\d{2}(?:\.\d+)?)?)?(?:\s?(?:[+-]\d{2}:?\d{2}|Z)\b)?"
    r"|\b\d{1,2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:\s?[+-]\d{2}:?\d{2})?")
_ID_TOKEN_RE = re.compile(r"\b\w*[A-Za-z_]\w*\d\w*\b|\b\d+(?:-\d+){2,}\b")   # WH_X1, 01b2c3d4, p95, 0000-1111-2222
_PAIR_RE = re.compile(r"\b([A-Z][A-Z0-9_]{1,63})=([^;\n]*)")
_USD_COL = ("USD", "COST", "DOLLAR", "SPEND", "SAVING", "PRICE", "BUDGET", "AMOUNT")
_PCT_COL = ("PCT", "PERCENT", "SHARE", "RATIO", "CONFIDENCE")
_OTHER_UNIT_COL = ("HOUR", "_SEC", "_MS", "_MIN", "SECONDS", "MINUTES", "COUNT", "CREDIT", "GB", "BYTES",
                   "ROWS", "RUNS", "QUERIES", "DAYS")


def numbers_preserved(grounded: str, phrased: str) -> bool:
    """Enforce the 'grounded numbers unchanged' promise: every numeric token in the AI
    phrasing must already appear in the grounded finding. The prompt TELLS the model not to
    change numbers, but nothing made it true — a drifted figure would render under a caption
    claiming the numbers are unchanged. Thousands-commas are normalized so '1,234' == '1234'.

    ASK-G1: also bind PERCENTAGES to their role. The flat set alone let a bare number in
    one role license the same digits as a percentage in another — e.g. the window '30d'
    licensed a wrong '30%' when the grounded share was 60%. So every phrased ``N%`` must
    also appear as a percentage in the grounded text, not merely as some bare digit."""
    def toks(s: str) -> set[str]:
        return {m.group().replace(",", "").rstrip(".") for m in _NUM_RE.finditer(s)}

    def pct_toks(s: str) -> set[str]:
        return {m.group(1).replace(",", "").rstrip(".") for m in _PCT_RE.finditer(s)}

    return toks(phrased) <= toks(grounded) and pct_toks(phrased) <= pct_toks(grounded)


@dataclass(frozen=True)
class GroundingCheck:
    checked: int                 # distinct $/% figures considered
    ungrounded: tuple[str, ...]  # as written in the answer, first-seen order

    @property
    def ok(self) -> bool:
        return not self.ungrounded


def evidence_section(prompt: str) -> str:
    """The evidence part of a house prompt — the text after its evidence marker — so the
    instruction text ('(1)', 'max 5', 'last 7 days') can never license a figure. A prompt with no
    marker is returned whole."""
    text = str(prompt or "")
    for marker in _EVIDENCE_MARKERS:
        i = text.find(marker)
        if i >= 0:
            return text[i + len(marker):]
    return text


def _num(text: str) -> float:
    return float(text.replace(",", ""))


def _decimals(text: str) -> int:
    return len(text.split(".", 1)[1]) if "." in text else 0


def _col_kind(col: str) -> str:
    c = col.upper()
    if any(t in c for t in _PCT_COL):      # first: SAVINGS_CONFIDENCE / COST_SHARE are ratios, not $
        return "pct"
    if any(t in c for t in _USD_COL):
        return "usd"
    if any(t in c for t in _OTHER_UNIT_COL):
        return "other"
    return "any"


def _free_values(text: str) -> list[float]:
    """Numbers in free text once dates/times/timestamps and ID-like tokens are removed."""
    clean = _ID_TOKEN_RE.sub(" ", _TIMESTAMP_RE.sub(" ", text or ""))
    out: list[float] = []
    for m in _EVIDENCE_NUM_RE.finditer(clean):
        try:
            v = abs(float(m.group().replace(",", "")))
        except ValueError:
            continue
        if math.isfinite(v):
            out.append(v)
    return out


def _pair_value(raw: str) -> float | None:
    s = raw.strip().replace(",", "")
    s = s[1:] if s.startswith("$") else s
    s = s[:-1] if s.endswith("%") else s
    try:
        v = abs(float(s))
    except ValueError:
        return None           # a timestamp, a name, an ID: licenses nothing
    return v if math.isfinite(v) else None


def _evidence_pools(evidence: str) -> tuple[list[float], list[float]]:
    """(usd_pool, pct_pool). ``COL=value`` pairs license only the unit their column names; every other
    number (free text, lowercase ``key=value`` stats) licenses both. A ratio-like value (≤ 1.5) also
    licenses its ×100 percentage."""
    usd: list[float] = []
    pct: list[float] = []

    def _add_pct(v: float) -> None:
        pct.append(v)
        if v <= _RATIO_MAX:
            pct.append(v * 100.0)

    text = str(evidence or "")
    for m in _PAIR_RE.finditer(text):
        v = _pair_value(m.group(2))
        if v is None:
            continue
        kind = _col_kind(m.group(1))
        if kind in ("usd", "any"):
            usd.append(v)
        if kind in ("pct", "any"):
            _add_pct(v)
    for v in _free_values(_PAIR_RE.sub(" ", text)):
        usd.append(v)
        _add_pct(v)
    return usd, pct


def _matches(value: float, tol: float, pool: list[float]) -> bool:
    return any(abs(v - value) <= tol for v in pool)


def check_grounding(answer: str, evidence: str) -> GroundingCheck:
    """Advisory: which $/% figures in ``answer`` match no number in ``evidence``. Tolerance is the
    larger of half a step of the figure's SHOWN precision (so '$1.2K' licenses 1,234.56) and 0.5%
    relative. A ratio in the evidence licenses its percentage (0.4231 -> '42%'). Never raises."""
    usd_pool, pct_pool = _evidence_pools(evidence)
    seen: dict[str, bool] = {}
    for m in _USD_RE.finditer(answer or ""):
        raw, suffix = m.group(1), (m.group(2) or "")
        scale = _SCALE.get(suffix[:1].lower(), 1.0)
        value = _num(raw) * scale
        if value < _MIN_USD:
            continue
        tok = m.group(0).strip()
        if tok in seen:
            continue
        tol = max(0.5 * (10 ** -_decimals(raw)) * scale, _REL_TOL * value)
        seen[tok] = _matches(value, tol, usd_pool)
    for m in _PCT_FIG_RE.finditer(answer or ""):
        raw = m.group(1)
        value = _num(raw)
        if value in _TRIVIAL_PCTS:
            continue
        tok = _WS_RE.sub("", m.group(0))
        if tok in seen:
            continue
        tol = max(0.5 * (10 ** -_decimals(raw)), _REL_TOL * value)
        seen[tok] = _matches(value, tol, pct_pool)
    return GroundingCheck(checked=len(seen), ungrounded=tuple(t for t, ok in seen.items() if not ok))
