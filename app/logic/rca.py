"""Incidents that investigate themselves — ranked root-cause synthesis (read-only).

When a DBA opens a declared incident, this turns the signals that already exist (object
& warehouse changes, task failures, spend anomalies, query-family drift, grant changes)
into RANKED competing hypotheses for what caused it — each with its evidence, timing, and
a confidence band — so the investigation is already done. It never remediates and never
writes: it explains.

The ranking is deliberately transparent (a DBA has to trust it). Each candidate scores on
three factors visible in its ``why``:
  * proximity — how close the change/event was to onset, and that it PRECEDED it (a cause
    comes before its effect); a same-shift change ranks far above a day-old one.
  * magnitude — the size of the signal (a REGRESSED verdict, a big robust-z, a root-cause
    task failure, a p95 blow-up), normalized within its kind.
  * match — does the candidate touch the incident's own entity / alert families.
score = 0.45·proximity + 0.35·magnitude + 0.20·match, banded HIGH/MED/LOW.

Pure pandas + datetime math over timestamps PASSED IN (no wall-clock, no SQL), so it
unit-tests like the rest of app/logic. Adapters normalize each existing builder's frame
into a common Candidate; ``rank_root_causes`` scores + ranks them. Tested in
tests/test_rca.py.
"""

from __future__ import annotations

import math
from datetime import datetime

import pandas as pd

from app.logic.formulas import safe_float

# Scoring weights + bands (uncalibrated starting points; the why-breakdown makes them auditable).
_W_PROX, _W_MAG, _W_MATCH = 0.45, 0.35, 0.20
_TAU_HOURS = 8.0            # proximity decay: a change ~8h before onset scores e^-1 of one at onset
_LEAD_WINDOW_H = 48.0       # nothing earlier than this is considered a plausible trigger
_AFTER_ONSET_PROX = 0.2     # a change AFTER onset is weak evidence of cause (could be a response)
_BAND_HIGH, _BAND_MED = 0.6, 0.35


def _to_dt(value) -> datetime | None:
    ts = pd.to_datetime(value, errors="coerce")
    return None if pd.isna(ts) else ts.to_pydatetime()


def _s(value) -> str:
    """Trimmed string, '' for None/NaN (a DataFrame fills a column absent from one row with
    NaN, and bool(NaN) is True — so raw truthiness misreads a missing column as present)."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    out = str(value).strip()
    return "" if out.lower() == "nan" else out


def _first(row, *cols: str) -> str:
    """First non-empty column value as a string."""
    for c in cols:
        v = _s(row.get(c))
        if v:
            return v
    return ""


def _proximity(when: datetime | None, onset: datetime | None) -> float:
    """1.0 at onset, decaying the earlier the trigger; low for post-onset; 0 beyond the lead window."""
    if when is None or onset is None or when != when or onset != onset:
        return 0.3            # unknown timing (None/NaT) — can't credit or discredit
    lead_h = (onset - when).total_seconds() / 3600.0
    if lead_h < 0:            # happened AFTER onset — a cause should precede the effect
        return _AFTER_ONSET_PROX
    if lead_h > _LEAD_WINDOW_H:
        return 0.0
    return math.exp(-lead_h / _TAU_HOURS)


def _entity_match(cand_entity: object, entity_name: str, families: set) -> float:
    """1.0 exact-entity, 0.6 mentioned in the incident's families/members, 0.3 same-scope only."""
    ce = _s(cand_entity).upper()
    if not ce:
        return 0.3
    if entity_name and ce == str(entity_name).strip().upper():
        return 1.0
    fam = {str(f).strip().upper() for f in (families or set()) if str(f).strip()}
    if ce in fam or any(ce in f or f in ce for f in fam):
        return 0.6
    return 0.3


# ------------------------------------------------------------------ adapters ----
# Each turns an existing builder's frame into a list of normalized Candidate dicts:
#   {kind, title, when, entity, magnitude (0-1), magnitude_text, changed_by, evidence}

def candidates_from_changes(df: pd.DataFrame | None) -> list[dict]:
    """From entity_recent_changes / change_registry / warehouse_change_registry (normalized:
    CHANGED_AT, CHANGE, CHANGED_BY, VERDICT, DETAIL, and an entity column)."""
    out: list[dict] = []
    if df is None or df.empty:
        return out
    for _, r in df.iterrows():
        verdict = _first(r, "VERDICT").upper()
        detail = _first(r, "DETAIL", "VERDICT_DETAIL")
        mag = 1.0 if verdict == "REGRESSED" else (0.6 if detail else 0.35)
        entity = _first(r, "ENTITY", "WAREHOUSE_NAME", "OBJECT_NAME", "DATABASE_NAME", "TARGET")
        change = _first(r, "CHANGE", "CHANGE_DDL", "CHANGE_SOURCE")
        if not change:
            # warehouse_change_registry describes the change as SETTING: OLD -> NEW.
            setting = _first(r, "SETTING")
            if setting:
                change = f"{setting}: {_first(r, 'OLD_VALUE') or '?'} → {_first(r, 'NEW_VALUE') or '?'}"
        change = change or "change"
        is_wh = bool(_first(r, "WAREHOUSE_NAME")) or "WAREHOUSE" in change.upper()
        by = _first(r, "CHANGED_BY")
        out.append({
            "kind": "warehouse_change" if is_wh else "object_change",
            "title": (f"{'Warehouse' if is_wh else 'Object'} change on {entity}: {change[:80]}"
                      + (f" ({verdict.title()})" if verdict else "")),
            "when": _to_dt(_first(r, "CHANGED_AT", "CHANGE_SEEN_AT") or None),
            "entity": entity, "magnitude": mag,
            "magnitude_text": (verdict.title() if verdict else "changed")
                              + (f" — {detail[:60]}" if detail else ""),
            "changed_by": by,
            "evidence": {"change": change[:120], "verdict": verdict, "by": by},
        })
    return out


def candidates_from_tasks(df: pd.DataFrame | None) -> list[dict]:
    """From build_failure_timeline output (ROLE_IN_GRAPH root/cascade, ERROR_FAMILY,
    DATABASE_NAME, TASK_NAME/NAME, QUERY_START_TIME)."""
    out: list[dict] = []
    if df is None or df.empty:
        return out
    for _, r in df.iterrows():
        role = _first(r, "ROLE_IN_GRAPH")
        is_root = role.lower().startswith("root")
        task = _first(r, "TASK_NAME", "NAME") or "task"
        db = _first(r, "DATABASE_NAME")
        fam = _first(r, "ERROR_FAMILY", "LAST_ERROR") or "failure"
        out.append({
            "kind": "task_failure",
            "title": f"{'Root-cause' if is_root else 'Cascaded'} task failure: {db + '.' if db else ''}{task}",
            "when": _to_dt(r.get("QUERY_START_TIME") or r.get("COMPLETED_TIME")),
            "entity": task, "magnitude": 1.0 if is_root else 0.4,
            "magnitude_text": f"{role or 'failure'} · {fam[:50]}",
            "changed_by": "",
            "evidence": {"task": f"{db}.{task}", "role": role, "error_family": fam[:80]},
        })
    return out


def candidates_from_anomalies(hits: list | pd.DataFrame | None) -> list[dict]:
    """From anomaly_summary output (list of {label, value, z, day}) — a spend/usage spike."""
    if isinstance(hits, pd.DataFrame):
        hits = hits.to_dict("records") if not hits.empty else []
    out: list[dict] = []
    for h in (hits or []):
        z = safe_float(h.get("z"))
        label = str(h.get("label") or "")
        # |z| -> 0..1 (z of 3.5 is the flag threshold; 8+ is extreme)
        mag = max(0.0, min(abs(z) / 8.0, 1.0))
        out.append({
            "kind": "spend_anomaly",
            "title": f"Spend {'spike' if z > 0 else 'drop'} on {label} (robust-z {z:+.1f})",
            "when": _to_dt(h.get("day")), "entity": label, "magnitude": mag,
            "magnitude_text": f"z {z:+.1f}, ${safe_float(h.get('value')):,.0f} that day",
            "changed_by": "",
            "evidence": {"warehouse": label, "z": round(z, 1), "value_usd": round(safe_float(h.get("value")), 0)},
        })
    return out


def candidates_from_grants(df: pd.DataFrame | None) -> list[dict]:
    """From recent_grant_changes (GRANTED_AT/CHANGED_AT, ACTION grant/revoke, GRANTEE/ROLE,
    PRIVILEGE/OBJECT). A privilege change — usually supporting, decisive for a security incident."""
    out: list[dict] = []
    if df is None or df.empty:
        return out
    for _, r in df.iterrows():
        action = (_first(r, "ACTION", "CHANGE") or "grant").lower()
        who = _first(r, "GRANTEE", "ROLE", "GRANTEE_NAME")
        what = _first(r, "PRIVILEGE", "OBJECT_NAME", "GRANTED_ON") or "privilege"
        out.append({
            "kind": "grant_change",
            "title": f"Privilege {action}: {what} to {who}",
            "when": _to_dt(_first(r, "GRANTED_AT", "CHANGED_AT", "CREATED_ON") or None),
            "entity": who, "magnitude": 0.5,
            "magnitude_text": f"{action} {what}",
            "changed_by": _first(r, "GRANTED_BY", "CHANGED_BY"),
            "evidence": {"action": action, "what": what, "who": who},
        })
    return out


# ------------------------------------------------------------------ ranker ----

def rank_root_causes(candidates: list[dict], onset, *, entity_name: str = "",
                     families=None, top: int = 5) -> list[dict]:
    """Score + rank the normalized candidates for one incident. ``onset`` is the incident's
    onset (STARTED_AT or DETECTED_AT). Returns strongest-first hypotheses with a transparent
    ``why`` — empty list when there is nothing plausible to point at (honest 'no clear cause
    in the signals')."""
    onset_dt = _to_dt(onset)
    fams = set(families or [])
    scored: list[dict] = []
    for c in (candidates or []):
        prox = _proximity(c.get("when"), onset_dt)
        mag = max(0.0, min(safe_float(c.get("magnitude")), 1.0))
        match = _entity_match(c.get("entity"), entity_name, fams)
        score = _W_PROX * prox + _W_MAG * mag + _W_MATCH * match
        band = "HIGH" if score >= _BAND_HIGH else ("MEDIUM" if score >= _BAND_MED else "LOW")
        when = c.get("when")
        when_dt = _to_dt(when)               # None for missing / NaN / NaT
        _timing_unknown = when_dt is None
        # Timing gates causation: a change outside the plausible trigger window (proximity 0),
        # one that happened AFTER onset, OR one with NO timing at all cannot be a confident
        # cause however big or on-entity it is — a cause must precede the effect, and an
        # untimed candidate has no evidence it did. Cap those at LOW so neither a post-onset
        # NOR an untimed change can headline as "most likely cause (high confidence)"
        # (bug-hunt: an untimed candidate got proximity 0.3 and, with magnitude + entity
        # match, crossed HIGH while its own reason read "timing unknown").
        _after_onset = (when_dt is not None and onset_dt is not None
                        and (onset_dt - when_dt).total_seconds() < 0)
        if prox <= 0.0 or _after_onset or _timing_unknown:
            band = "LOW"
        if when_dt is not None and onset_dt is not None:
            lead_h = (onset_dt - when_dt).total_seconds() / 3600.0
            lead_text = (f"{lead_h:.1f}h before onset" if lead_h >= 0
                         else f"{-lead_h:.1f}h AFTER onset")
        else:
            lead_text = "timing unknown"
        scored.append({
            "kind": c.get("kind"), "title": c.get("title"), "score": round(score, 3),
            "band": band, "when": when, "lead_text": lead_text,
            "magnitude_text": c.get("magnitude_text", ""), "changed_by": c.get("changed_by", ""),
            "why": (f"timing {prox:.0%} ({lead_text}), magnitude {mag:.0%}, "
                    f"entity match {match:.0%}"),
            "evidence": c.get("evidence", {}),
        })
    scored.sort(key=lambda h: (h["score"],
                               1 if h["band"] == "HIGH" else 0), reverse=True)
    return scored[:max(0, int(top))]


def rca_summary(hypotheses: list[dict]) -> dict:
    """Headline for the panel: the leading hypothesis + how strong the field is."""
    if not hypotheses:
        return {"has_lead": False, "top_band": None, "n": 0,
                "headline": "No clear trigger in the signals around this incident's window — "
                            "the change/anomaly/task feeds show nothing that lines up with onset."}
    top = hypotheses[0]
    strong = top["band"] in ("HIGH", "MEDIUM")
    return {
        "has_lead": strong, "top_band": top["band"], "n": len(hypotheses),
        "headline": ((f"Most likely cause ({top['band'].lower()} confidence): " if strong
                      else "Best available lead (low confidence): ") + str(top["title"])),
    }
