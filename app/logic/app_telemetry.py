"""App self-telemetry readers for Admin (#50): error-context parsing and section-visit zero-fill.

Two pure helpers behind Admin's Errors & telemetry and Performance sections:

* ``parse_error_context`` reads back the APP_ERROR_LOG.CONTEXT shape that
  ``app.core.errors.record_error`` writes from v4.599 on —
  ``ref=… · viewer=… · v<build> · <context> · tb=…`` — and tolerates every older
  shape: legacy ``ref=… · page render`` rows (no viewer/build/tb) and server-proc
  rows (``route <id> …``) that never carried a ref at all. A missing piece is ``""``.
* ``with_unvisited_sections`` turns the section-visit summary (APP_USAGE
  section_visit / subsection_visit rows) into a retirement-candidate table: every
  current page-level section label that was never visited appears as a 0-visit row,
  and a logged label no page offers any more is flagged as retired.

Pure pandas — no Streamlit, no Snowflake.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence

import pandas as pd

_REF_RE = re.compile(r"ref=(OW-\d{8}-\d{6}-[0-9A-F]{6})")
# The FIRST whitespace-led viewer= wins: query._entitlement_refusal already embeds
# "(viewer=X)" inside some caller contexts, and the "(" prefix keeps that one out.
_VIEWER_RE = re.compile(r"(?:^|\s)viewer=([^\s·)]+)")
_BUILD_RE = re.compile(r"· v(\d+\.\d+\.\d+)(?=\s|$)")
_TB_MARK = " · tb="
# The structured head record_error writes; stripped to leave the caller's own context.
_HEAD_RE = re.compile(
    r"^\s*ref=OW-\d{8}-\d{6}-[0-9A-F]{6}"
    r"(?: · viewer=[^\s·)]+)?"
    r"(?: · v\d+\.\d+\.\d+(?=\s|$))?"
    r"(?: · |\s*$)"
)
# record_error writes '—' when the viewer lookup fails; the entitlement audit writes '?'.
_UNKNOWN_VIEWERS = frozenset({"—", "?", "-"})

SECTION_LEVEL = "Section"
SUBVIEW_LEVEL = "Sub-view"
RETIRED_LEVEL = "Section (retired label)"
_LEVEL_ORDER = {SECTION_LEVEL: 0, SUBVIEW_LEVEL: 1, RETIRED_LEVEL: 2}


def _text(value: object) -> str:
    """A CONTEXT cell as text: None / NaN / NaT / garbage become ''."""
    if value is None:
        return ""
    if isinstance(value, float) and math.isnan(value):
        return ""
    try:
        if pd.isna(value):
            return ""
    except (TypeError, ValueError):
        pass  # array-likes: not a scalar NA; fall through to str()
    try:
        return str(value)
    except (TypeError, ValueError, AttributeError, RecursionError):  # a broken __str__ reads as empty
        return ""


def parse_error_context(ctx: object) -> dict[str, str]:
    """Split an APP_ERROR_LOG.CONTEXT into ref / viewer / build / context / tb ('' when absent).

    ``context`` is the caller's own part (e.g. 'page render', 'query key=…'); on a row with
    no recognizable head it is the whole text, so a server-proc 'route R1 …' row still reads.
    An unknown viewer ('—' / '?') is ''. Never raises."""
    out = {"ref": "", "viewer": "", "build": "", "context": "", "tb": ""}
    text = _text(ctx).strip()
    if not text:
        return out
    body = text
    cut = body.rfind(_TB_MARK)
    if cut >= 0:
        out["tb"] = body[cut + len(_TB_MARK):].strip()
        body = body[:cut]
    m = _REF_RE.search(body)
    if m:
        out["ref"] = m.group(1)
    m = _VIEWER_RE.search(body)
    if m and m.group(1) not in _UNKNOWN_VIEWERS:
        out["viewer"] = m.group(1)
    m = _BUILD_RE.search(body)
    if m:
        out["build"] = m.group(1)
    out["context"] = _HEAD_RE.sub("", body, count=1).strip()
    return out


def with_unvisited_sections(df: pd.DataFrame,
                            labels_by_page: Mapping[str, Sequence[str]]) -> pd.DataFrame:
    """Section-visit rows plus a 0-visit row for every current page-level label never logged.

    ``df`` is mart_sql.section_visit_summary's frame (PAGE, LEVEL, SECTION, VISITS_<n>D,
    VISITS, USERS, LAST_VISIT_AT, FIRST_LOGGED_AT). A 'Section' row whose (PAGE, SECTION)
    is no longer a current label becomes 'Section (retired label)'. Appended rows carry
    0 in every VISITS* / USERS* column, NaT for LAST_VISIT_AT and the frame's
    FIRST_LOGGED_AT. Sorted by the mapping's page order, then level, then visits (most
    first). Never mutates ``df``; an empty frame, or one without PAGE / SECTION, comes
    back as an unchanged copy."""
    if df is None or df.empty or "PAGE" not in df.columns or "SECTION" not in df.columns:
        return df.copy() if df is not None else pd.DataFrame()
    out = df.copy()
    out["PAGE"] = out["PAGE"].astype(str)
    out["SECTION"] = out["SECTION"].astype(str)
    out["LEVEL"] = out["LEVEL"].astype(str) if "LEVEL" in out.columns else SECTION_LEVEL
    current = {(str(page), str(label)) for page, labels in labels_by_page.items() for label in labels}
    is_section = out["LEVEL"] == SECTION_LEVEL
    retired = is_section & ~pd.Series(
        [(p, s) in current for p, s in zip(out["PAGE"], out["SECTION"], strict=True)], index=out.index)
    out.loc[retired, "LEVEL"] = RETIRED_LEVEL
    seen = {(p, s) for p, s, lvl in zip(out["PAGE"], out["SECTION"], out["LEVEL"], strict=True)
            if lvl == SECTION_LEVEL}
    first_logged = None
    if "FIRST_LOGGED_AT" in out.columns:
        _fl = out["FIRST_LOGGED_AT"].dropna()
        first_logged = _fl.iloc[0] if not _fl.empty else None
    zero_cols = [c for c in out.columns if str(c).startswith(("VISITS", "USERS"))]
    fill = []
    for page, labels in labels_by_page.items():
        for label in labels:
            if (str(page), str(label)) in seen:
                continue
            row: dict[str, object] = dict.fromkeys(out.columns)
            row.update({"PAGE": str(page), "LEVEL": SECTION_LEVEL, "SECTION": str(label)})
            row.update(dict.fromkeys(zero_cols, 0))
            if "LAST_VISIT_AT" in out.columns:
                row["LAST_VISIT_AT"] = pd.NaT
            if "FIRST_LOGGED_AT" in out.columns:
                row["FIRST_LOGGED_AT"] = first_logged
            fill.append(row)
    if fill:
        out = pd.concat([out, pd.DataFrame(fill, columns=out.columns)], ignore_index=True)
    page_rank = {str(p): i for i, p in enumerate(labels_by_page)}
    visits = (pd.to_numeric(out["VISITS"], errors="coerce").fillna(0)
              if "VISITS" in out.columns else pd.Series(0, index=out.index))
    keys = pd.DataFrame({
        "_p": out["PAGE"].map(lambda p: page_rank.get(p, len(page_rank))),
        "_l": out["LEVEL"].map(lambda lvl: _LEVEL_ORDER.get(lvl, len(_LEVEL_ORDER))),
        "_v": -visits,
    }, index=out.index)
    order = keys.sort_values(["_p", "_l", "_v"], kind="mergesort").index
    return out.loc[order].reset_index(drop=True)
