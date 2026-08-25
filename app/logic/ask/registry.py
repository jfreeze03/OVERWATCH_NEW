"""Ask-OVERWATCH registry — the hand-built answerers.

Each answerer maps one question type to EXISTING, tested builders and a pure
`analyze()` that turns their real output into a grounded AnswerResult. Adding a
new question = add one Answerer here; nothing else in the app changes.

Grounding rule: every number in a headline/bullet/evidence cell comes from a
frame produced by these builders. `analyze()` never fabricates or estimates a
value it did not read.
"""

from __future__ import annotations

import pandas as pd

from app.data import mart27_sql, mart_sql
from app.logic.anomaly import robust_zscores
from app.logic.ask.types import (
    OUTLIER_Z,
    Answerer,
    AnswerResult,
    AskParams,
    QuerySpec,
)


# --------------------------------------------------------------------------- #
# small pure formatters (kept local so the registry never imports the UI layer)
# --------------------------------------------------------------------------- #
def _fmt(x: float) -> str:
    if x >= 100:
        return f"{x:,.0f}"
    if x >= 1:
        return f"{x:,.1f}"
    return f"{x:.3f}"


def _clip(text: str, n: int) -> str:
    t = " ".join(str(text).split())
    return t if len(t) <= n else t[: n - 1].rstrip() + "…"


def _num(series_val: object, default: float = 0.0) -> float:
    v = pd.to_numeric(series_val, errors="coerce")
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    return default if pd.isna(f) else f


# --------------------------------------------------------------------------- #
# Answerer 1 — "which user is causing spend spikes?"
# --------------------------------------------------------------------------- #
_SPEND_INTENT = "spend_spike_by_user"


def _needs_spend_by_user(params: AskParams) -> list[QuerySpec]:
    # Credits-grounded per-user attribution (ALLOC_CREDITS is dollarizable).
    return [
        QuerySpec(
            key="alloc",
            sql=mart27_sql.alloc_attribution(params.days, "USER", params.company),
            tier="recent",
        )
    ]


def _analyze_spend_by_user(
    params: AskParams, frames: dict[str, pd.DataFrame]
) -> AnswerResult:
    src = f"mart27_sql.alloc_attribution(USER, {params.days}d) + robust_zscores"
    meta: dict[str, object] = {"days": params.days, "company": params.company}
    df = frames.get("alloc")
    if df is None or df.empty or "ALLOC_CREDITS" not in df.columns:
        return AnswerResult(
            intent=_SPEND_INTENT,
            headline=f"No attributable user spend in the last {params.days}d.",
            source=src,
            confidence="no_data",
            params=meta,
        )

    d = df.copy()
    d["ALLOC_CREDITS"] = pd.to_numeric(d["ALLOC_CREDITS"], errors="coerce").fillna(0.0)
    # Never name unattributed load as "the top spender" — the builder coalesces
    # missing owners to NONE/UNKNOWN; those are not a user and must not head the
    # answer (grounding honesty). They stay out of the ranking and the % base.
    d = d[~d["DIMENSION"].astype(str).str.upper().isin(("NONE", "UNKNOWN"))]
    d = d.sort_values("ALLOC_CREDITS", ascending=False).reset_index(drop=True)
    if d.empty:
        return AnswerResult(
            intent=_SPEND_INTENT,
            headline=f"No attributable user spend in the last {params.days}d.",
            source=src,
            confidence="no_data",
            params=meta,
        )
    total = float(d["ALLOC_CREDITS"].sum())
    if total <= 0:
        return AnswerResult(
            intent=_SPEND_INTENT,
            headline=f"No attributable user spend in the last {params.days}d.",
            source=src,
            confidence="no_data",
            params=meta,
        )

    z = robust_zscores(d["ALLOC_CREDITS"])
    top = d.iloc[0]
    top_user = str(top["DIMENSION"])
    top_credits = float(top["ALLOC_CREDITS"])
    top_share = top_credits / total
    top_z = _num(z.iloc[0]) if len(z) else 0.0
    outlier = top_z >= OUTLIER_Z
    meta["top_z"] = round(top_z, 2)

    tail = f" — a clear outlier vs peers (z={top_z:.1f})" if outlier else ""
    headline = (
        f"Over the last {params.days}d, {top_user} is the top spender: "
        f"{_fmt(top_credits)} credits ({top_share * 100:.0f}% of named-user "
        f"spend){tail}."
    )

    bullets: list[str] = []
    for i in range(min(5, len(d))):
        r = d.iloc[i]
        cr = float(r["ALLOC_CREDITS"])
        bullets.append(
            f"{r['DIMENSION']}: {_fmt(cr)} credits ({cr / total * 100:.0f}%)"
        )
    if not outlier:
        bullets.append(
            "No single user is a statistical outlier this window — spend is "
            "spread across the cohort, not one runaway account."
        )

    evidence_cols = [c for c in ("DIMENSION", "ALLOC_CREDITS", "ELAPSED_SHARE") if c in d.columns]
    return AnswerResult(
        intent=_SPEND_INTENT,
        headline=headline,
        bullets=bullets,
        evidence=d.head(15)[evidence_cols].copy(),
        source=src,
        confidence="grounded",
        params=meta,
    )


# --------------------------------------------------------------------------- #
# Answerer 2 — "which query is causing cloud services to spike?"
# --------------------------------------------------------------------------- #
_CS_INTENT = "cloud_services_spike_by_query"


def _needs_cs_by_query(params: AskParams) -> list[QuerySpec]:
    return [
        QuerySpec(
            key="shapes",
            sql=mart_sql.cloud_svc_top_shapes(params.days, params.company, params.warehouse),
            tier="recent",
        ),
        QuerySpec(
            key="byuser",
            sql=mart_sql.cloud_svc_by_user(params.days, params.company, params.warehouse),
            tier="recent",
        ),
        QuerySpec(
            key="ratio",
            sql=mart_sql.fact_cloud_services_ratio(params.days, params.company),
            tier="recent",
        ),
    ]


def _analyze_cs_by_query(
    params: AskParams, frames: dict[str, pd.DataFrame]
) -> AnswerResult:
    src = (
        f"mart_sql.cloud_svc_top_shapes / cloud_svc_by_user / "
        f"fact_cloud_services_ratio ({params.days}d)"
    )
    meta: dict[str, object] = {"days": params.days, "company": params.company}
    shapes = frames.get("shapes")
    if shapes is None or shapes.empty or "CS_CREDITS" not in shapes.columns:
        return AnswerResult(
            intent=_CS_INTENT,
            headline=f"No cloud-services query activity in the last {params.days}d.",
            source=src,
            confidence="no_data",
            params=meta,
        )

    s = shapes.copy()
    s["CS_CREDITS"] = pd.to_numeric(s["CS_CREDITS"], errors="coerce").fillna(0.0)
    s = s.sort_values("CS_CREDITS", ascending=False).reset_index(drop=True)
    total_cs = float(s["CS_CREDITS"].sum())
    if total_cs <= 0:
        return AnswerResult(
            intent=_CS_INTENT,
            headline=f"No cloud-services query activity in the last {params.days}d.",
            source=src,
            confidence="no_data",
            params=meta,
        )

    top = s.iloc[0]
    qtype = str(top.get("QUERY_TYPE", "query")) or "query"
    cs = float(top["CS_CREDITS"])
    runs = int(_num(top.get("RUNS", 0)))
    share = cs / total_cs

    headline = (
        f"The biggest cloud-services driver over {params.days}d is a {qtype} "
        f"pattern: {_fmt(cs)} CS credits across {runs:,} runs "
        f"({share * 100:.0f}% of cloud-services credits)."
    )

    bullets: list[str] = []
    for i in range(min(3, len(s))):
        r = s.iloc[i]
        bullets.append(
            f"{_clip(r.get('SAMPLE_TEXT', ''), 90)} — "
            f"{_fmt(float(r['CS_CREDITS']))} CS credits ({int(_num(r.get('RUNS', 0))):,} runs)"
        )

    byuser = frames.get("byuser")
    if byuser is not None and not byuser.empty and "CS_CREDITS" in byuser.columns:
        bu = byuser.copy()
        bu["CS_CREDITS"] = pd.to_numeric(bu["CS_CREDITS"], errors="coerce").fillna(0.0)
        bu = bu.sort_values("CS_CREDITS", ascending=False)
        if not bu.empty and float(bu.iloc[0]["CS_CREDITS"]) > 0:
            r0 = bu.iloc[0]
            bullets.append(
                f"Heaviest user: {r0.get('USER_NAME', '(unknown)')} "
                f"({_fmt(float(r0['CS_CREDITS']))} CS credits)"
            )

    ratio = frames.get("ratio")
    if ratio is not None and not ratio.empty and "STATUS" in ratio.columns:
        elev = ratio[ratio["STATUS"].isin(["ELEVATED", "WATCH"])].copy()
        if not elev.empty and "CLOUD_SVC_PCT" in elev.columns:
            elev = elev.sort_values("CLOUD_SVC_PCT", ascending=False).head(3)
            names = ", ".join(
                f"{r['WAREHOUSE_NAME']} ({_num(r['CLOUD_SVC_PCT']):.0f}%)"
                for _, r in elev.iterrows()
            )
            bullets.append(f"Elevated cloud-services share on: {names}")

    ev_cols = [c for c in ("QUERY_TYPE", "SAMPLE_TEXT", "RUNS", "CS_CREDITS", "CS_PER_1K_RUNS") if c in s.columns]
    ev = s.head(10)[ev_cols].copy()
    if "SAMPLE_TEXT" in ev.columns:
        ev["SAMPLE_TEXT"] = ev["SAMPLE_TEXT"].map(lambda t: _clip(t, 100))
    return AnswerResult(
        intent=_CS_INTENT,
        headline=headline,
        bullets=bullets,
        evidence=ev,
        source=src,
        confidence="grounded",
        params=meta,
    )


# --------------------------------------------------------------------------- #
# the registry
# --------------------------------------------------------------------------- #
REGISTRY: tuple[Answerer, ...] = (
    Answerer(
        intent=_SPEND_INTENT,
        title="Which user is driving spend",
        examples=(
            "which user is causing spend spikes",
            "who is the most expensive user this month",
            "which account is driving credits in the last 7 days",
        ),
        keywords=(
            "user", "who", "whom", "account", "spend", "spending", "cost",
            "credits", "spik", "expensive", "driving", "driver", "top",
        ),
        require_all=(
            ("user", "users", "who", "whom", "account", "login"),
            # "spik" stems spike/spikes/spiking/spiked (substring match).
            ("spend", "spending", "cost", "costs", "credit", "credits",
             "spik", "expensive", "burn", "driving", "driver"),
        ),
        needs=_needs_spend_by_user,
        analyze=_analyze_spend_by_user,
    ),
    Answerer(
        intent=_CS_INTENT,
        title="Which query is spiking cloud services",
        examples=(
            "which query is causing cloud services to spike",
            "what statement is driving cloud services credits",
            "top cloud services queries in the last 30 days",
        ),
        keywords=(
            "query", "queries", "statement", "sql", "cloud service",
            "cloud services", "cloud-services", "cs", "spik", "driving",
        ),
        require_all=(
            # Any cloud-services question routes here (the answer IS the driving
            # queries); the second group is the hard "cloud services" gate, so
            # this never steals a plain per-user spend question.
            ("query", "queries", "statement", "sql", "pattern", "what",
             "which", "who", "user", "driving", "driver", "causing", "top"),
            ("cloud service", "cloud services", "cloud-services",
             "cloud_services", "cs credit", "cs credits", "cloud compute"),
        ),
        needs=_needs_cs_by_query,
        analyze=_analyze_cs_by_query,
    ),
)
