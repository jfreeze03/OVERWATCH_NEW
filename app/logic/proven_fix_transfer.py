"""Proven-fix transfer engine (pure, tested, zero AI cost).

If a fix TYPE was VERIFIED to save money on one warehouse, suggest the SAME fix on
OTHER warehouses that independently match that fix's actionable profile but have not
had it applied — evidence-backed quick wins. Read-only: each suggestion carries the
proven warehouse's realized $ as EVIDENCE and the candidate's OWN measured figure as
an ESTIMATE (never the proven warehouse's dollars).

Only ``STATE='VERIFIED'`` rows with a realized ``VERIFIED_USD`` seed a proven win —
never ESTIMATED. Candidate profiles come from the existing detectors: idle_advisor
(AUTO_SUSPEND) and sizing.size_recommendations (RESIZE); the app already computes
whether each fix is still available and its honest saving, so the transfer engine
only cross-references — it never re-derives a saving. Mirrors the DataFrame-in /
ranked-DataFrame-out shape of sizing.size_recommendations.
"""

from __future__ import annotations

import pandas as pd

from .formulas import safe_float
from .sizing import RECOMMEND_DOWN
from .workbench import experiment_state_by_key

# Only fix types whose actionable profile the app can independently detect.
TRANSFERABLE_FIX_TYPES = ("AUTO_SUSPEND", "RESIZE")
MIN_VERIFIED_USD = 5.0     # mirror the autobook settle floor — don't transfer noise
# Registry SETTING 'SIZE' and ledger FINDING_TYPE 'RESIZE' are the same fix.
_FIX_ALIASES = {"SIZE": "RESIZE"}
_OPEN_EXPERIMENT = frozenset({"PLANNED", "RUNNING", "OBSERVING"})
_OUT_COLS = ["FIX_TYPE", "CANDIDATE_WAREHOUSE", "COMPANY", "EVIDENCE_WAREHOUSE",
             "EVIDENCE_VERIFIED_USD", "EVIDENCE_COUNT", "CANDIDATE_EST_MONTHLY_USD",
             "EST_CONFIDENCE", "RATIONALE"]


def _norm(value: object) -> str:
    return str(value or "").strip().upper()


def _norm_fix(value: object) -> str:
    fix = _norm(value)
    return _FIX_ALIASES.get(fix, fix)


# Snowflake warehouse size ladder (normalized: spaces/dashes stripped, uppercased).
_SIZE_RANK = {"XSMALL": 0, "SMALL": 1, "MEDIUM": 2, "LARGE": 3, "XLARGE": 4,
              "2XLARGE": 5, "3XLARGE": 6, "4XLARGE": 7, "5XLARGE": 8, "6XLARGE": 9}


def _is_downsize(old: object, new: object) -> bool:
    """True if a SIZE change went DOWN (or the direction is unknown). The app and
    autobook paths only ever book a size-down, but a manually-inserted VERIFIED
    size-UP row must not transfer as a 'size down' suggestion."""
    ro = _SIZE_RANK.get(_norm(old).replace(" ", "").replace("-", ""))
    rn = _SIZE_RANK.get(_norm(new).replace(" ", "").replace("-", ""))
    if ro is None or rn is None:
        return True   # unknown (app-path rows carry no registry OLD/NEW) — allow
    return rn < ro


def _wins_by_type(verified_wins: pd.DataFrame | None) -> dict:
    """{fix_type: {evidence_wh, verified_usd, count, proven_whs}} — the exemplar
    (largest realized $) per transferable fix type, over VERIFIED rows only."""
    wins: dict = {}
    if (verified_wins is None or verified_wins.empty
            or not {"FIX_TYPE", "TARGET_WAREHOUSE", "VERIFIED_USD"}.issubset(verified_wins.columns)):
        return wins
    w = verified_wins.copy()
    w["_FT"] = w["FIX_TYPE"].map(_norm_fix)
    w["_WH"] = w["TARGET_WAREHOUSE"].map(_norm)
    w["_USD"] = w["VERIFIED_USD"].map(lambda v: safe_float(v, 0.0))
    w = w[w["_FT"].isin(TRANSFERABLE_FIX_TYPES) & (w["_WH"] != "") & (w["_USD"] >= MIN_VERIFIED_USD)]
    if not w.empty:
        # a proven RESIZE win must be a size-DOWN — never transfer a size-up as one.
        w = w[w.apply(lambda r: r["_FT"] != "RESIZE"
                      or _is_downsize(r.get("OLD_VALUE"), r.get("NEW_VALUE")), axis=1)]
    for fix_type, group in w.groupby("_FT"):
        top = group.sort_values("_USD", ascending=False).iloc[0]
        wins[str(fix_type)] = {
            "evidence_wh": str(top["TARGET_WAREHOUSE"]).strip(),
            "verified_usd": round(float(top["_USD"]), 2),
            "count": int(group["_WH"].nunique()),
            "proven_whs": set(group["_WH"]),
        }
    return wins


def _match(win: dict, fix_type: str, profiles: pd.DataFrame, *, cand_mask: pd.Series,
           est_col: str, confidence: str, open_experiments: pd.DataFrame | None,
           verb: str) -> list[dict]:
    cands = profiles[cand_mask].copy()
    if cands.empty:
        return []
    exp_status = experiment_state_by_key(cands, open_experiments, "WAREHOUSE", "WAREHOUSE_NAME")
    rows: list[dict] = []
    for idx, r in cands.iterrows():
        warehouse = str(r.get("WAREHOUSE_NAME") or "").strip()
        key = _norm(warehouse)
        if not key or key in win["proven_whs"]:            # not self, not an already-proven WH
            continue
        if _norm(exp_status.get(idx, "")) in _OPEN_EXPERIMENT:  # not under an open experiment
            continue
        est = round(safe_float(r.get(est_col), 0.0), 2)
        if est <= 0:
            continue
        more = f" (+{win['count'] - 1} more)" if win["count"] > 1 else ""
        rows.append({
            "_CWKEY": key,
            "FIX_TYPE": fix_type,
            "CANDIDATE_WAREHOUSE": warehouse,
            "COMPANY": str(r.get("COMPANY") or ""),
            "EVIDENCE_WAREHOUSE": win["evidence_wh"],
            "EVIDENCE_VERIFIED_USD": win["verified_usd"],
            "EVIDENCE_COUNT": win["count"],
            "CANDIDATE_EST_MONTHLY_USD": est,
            "EST_CONFIDENCE": confidence,
            "RATIONALE": (f"{fix_type} verified ${win['verified_usd']:,.0f}/mo on "
                          f"{win['evidence_wh']}{more}; {warehouse} matches the same profile — "
                          f"{verb} here (est ~${est:,.0f}/mo, ESTIMATE)."),
        })
    return rows


def transfer_suggestions(
    verified_wins: pd.DataFrame | None,
    idle_profiles: pd.DataFrame | None = None,
    sizing_profiles: pd.DataFrame | None = None,
    open_experiments: pd.DataFrame | None = None,
    *,
    limit: int = 25,
) -> pd.DataFrame:
    """Ranked 'replicate this proven fix' suggestions. ``verified_wins`` is the typed
    VERIFIED-ledger frame (mart_sql.verified_wins); ``idle_profiles`` and
    ``sizing_profiles`` are the idle_advisor / size_recommendations outputs the page
    already holds. Empty/absent inputs -> empty frame; never raises."""
    wins = _wins_by_type(verified_wins)
    if not wins:
        return pd.DataFrame(columns=_OUT_COLS)
    rows: list[dict] = []
    if ("AUTO_SUSPEND" in wins and idle_profiles is not None and not idle_profiles.empty
            and {"WAREHOUSE_NAME", "ACTION_STATUS"}.issubset(idle_profiles.columns)):
        rows += _match(
            wins["AUTO_SUSPEND"], "AUTO_SUSPEND", idle_profiles,
            cand_mask=idle_profiles["ACTION_STATUS"].map(_norm) == "ACTIONABLE",
            est_col="ACTIONABLE_MONTHLY_USD", confidence="MEDIUM",
            open_experiments=open_experiments, verb="tighten AUTO_SUSPEND")
    if ("RESIZE" in wins and sizing_profiles is not None and not sizing_profiles.empty
            and {"WAREHOUSE_NAME", "RECOMMENDATION"}.issubset(sizing_profiles.columns)):
        rows += _match(
            wins["RESIZE"], "RESIZE", sizing_profiles,
            cand_mask=sizing_profiles["RECOMMENDATION"].map(_norm) == _norm(RECOMMEND_DOWN),
            est_col="SAVING_LOW_USD", confidence="LOW",
            open_experiments=open_experiments, verb="size down one step")
    if not rows:
        return pd.DataFrame(columns=_OUT_COLS)
    # A warehouse flagged for BOTH idle-tune and size-down would double-count the
    # same idle credits — keep only the higher-value suggestion per warehouse
    # (mirrors rollup_savings: the two fixes are alternatives, not additive).
    out = (pd.DataFrame(rows)
           .sort_values(["CANDIDATE_EST_MONTHLY_USD", "EVIDENCE_VERIFIED_USD"], ascending=[False, False])
           .drop_duplicates(subset="_CWKEY", keep="first")
           .head(max(1, int(limit))).reset_index(drop=True))
    return out[_OUT_COLS]
