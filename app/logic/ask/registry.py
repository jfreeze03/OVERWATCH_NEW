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

from app.data import cortex_sql, insights_sql, mart27_sql, mart_sql
from app.data.common import bounded_days, resolve_effective_window
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


def _clip(text: object, n: int) -> str:
    if text is None or (not isinstance(text, str) and pd.isna(text)):
        return ""
    t = " ".join(str(text).split())
    return t if len(t) <= n else t[: n - 1].rstrip() + "…"


def _sample(text: object, n: int) -> str:
    """Display SAMPLE_TEXT honestly: a SQL-NULL cell becomes a neutral
    placeholder, never the literal 'None'/'nan'."""
    return _clip(text, n) or "(no sample text)"


def _code(text: object) -> str:
    """Wrap untrusted SQL / error text as an inline markdown code span, so its
    markdown-special characters render literally in a bullet: no '*'/'_' emphasis and,
    crucially, no '$$'/'$1' pair collapsing into a serif-italic LaTeX math span. Ask
    bullets go straight to st.markdown, so raw SAMPLE_TEXT / LAST_ERROR would otherwise
    mangle. A stray backtick in the text would break the span, so neutralize it."""
    return "`" + str(text).replace("`", "'") + "`"


def _num(series_val: object, default: float = 0.0) -> float:
    v = pd.to_numeric(series_val, errors="coerce")
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    return default if pd.isna(f) else f


def _notnull(v: object) -> bool:
    return v is not None and not pd.isna(v) and str(v).strip() != ""


def _task_label(row: pd.Series) -> str:
    """DB.SCHEMA.TASK from the available parts (matching how the Ops surfaces
    qualify tasks), or a neutral placeholder for a null/blank task name — so
    same-named tasks in different schemas stay distinguishable and a SQL NULL
    never renders as 'nan'/'None'."""
    if not _notnull(row.get("TASK_NAME")):
        return "(unnamed task)"
    parts = [str(row.get(c)).strip()
             for c in ("DATABASE_NAME", "SCHEMA_NAME", "TASK_NAME") if _notnull(row.get(c))]
    return ".".join(parts)


# --------------------------------------------------------------------------- #
# Answerer 1 — "which user is causing spend spikes?"
# --------------------------------------------------------------------------- #
_SPEND_INTENT = "spend_spike_by_user"


def _needs_spend_by_user(params: AskParams) -> list[QuerySpec]:
    # Use the SAME builder the Cost & Contract page serves for per-user attribution
    # (alloc_xdim_attribution over FACT_COST_ALLOC_XDIM_DAILY): warehouse-grain
    # company scope, per-warehouse-hour credit weighting, and the resolved
    # today-excluded window — so Ask's "top spender" reconciles with the Cost page
    # instead of the owner-scoped mart the Cost page abandoned for divergent totals.
    return [
        QuerySpec(
            key="alloc",
            sql=mart27_sql.alloc_xdim_attribution(params.days, "USER", params.company, ""),
            tier="recent",
        )
    ]


def _analyze_spend_by_user(
    params: AskParams, frames: dict[str, pd.DataFrame]
) -> AnswerResult:
    # alloc_xdim_attribution clamps the window to the mart's effective horizon
    # (MAX_MART_WINDOW_DAYS//2), so a "last year" (365d) question actually runs
    # ~182d. Label EVERY surface with that effective window, never the raw request,
    # so a 182d result is never presented as 365d. meta["days"] carries eff to the
    # page caption too.
    eff = resolve_effective_window(params.days, "DAY")[0]
    src = f"mart27_sql.alloc_xdim_attribution(USER, {eff}d) + robust_zscores"
    meta: dict[str, object] = {"days": eff, "company": params.company}
    # The builder is coverage-gated, so an empty frame can mean "no spend" OR "the
    # attribution mart hasn't accrued this window yet" — say so rather than assert
    # a bare zero.
    no_data_line = (
        f"No attributable user spend for the last {eff}d "
        "(the cost-allocation mart may still be accruing this window)."
    )
    df = frames.get("alloc")
    if (df is None or df.empty or "ALLOC_CREDITS" not in df.columns
            or "DIMENSION" not in df.columns):
        return AnswerResult(
            intent=_SPEND_INTENT,
            headline=no_data_line,
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
            headline=no_data_line,
            source=src,
            confidence="no_data",
            params=meta,
        )
    total = float(d["ALLOC_CREDITS"].sum())
    if total <= 0:
        return AnswerResult(
            intent=_SPEND_INTENT,
            headline=no_data_line,
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
        f"Over the last {eff}d, {top_user} is the top spender: "
        f"{_fmt(top_credits)} credits ({top_share * 100:.0f}% of named-user "
        f"spend){tail}."
    )

    bullets: list[str] = []
    if eff < params.days:  # window was clamped to the mart horizon — say so
        bullets.append(
            f"Per-user attribution is capped to the mart's {eff}-day horizon "
            f"(you asked for {params.days}d)."
        )
    for i in range(min(5, len(d))):
        r = d.iloc[i]
        cr = float(r["ALLOC_CREDITS"])
        bullets.append(
            f"{r['DIMENSION']}: {_fmt(cr)} credits ({cr / total * 100:.0f}%)"
        )
    # Honest "why isn't this flagged" note. robust_zscores can't test <5 points
    # (returns zeros), so NEVER claim "spread across the cohort" off a test that
    # silently didn't run — key the reassurance off the real n and top-share.
    n_users = len(d)
    if not outlier:
        if n_users < 5:
            bullets.append(
                f"Only {n_users} user{'s' if n_users != 1 else ''} had attributable "
                "spend this window — too few for the peer-outlier test; read the "
                "ranking directly."
            )
        elif top_share >= 0.5:
            bullets.append(
                f"{top_user} carries the majority ({top_share * 100:.0f}%) of "
                "named-user spend, though not a statistical outlier vs peers."
            )
        else:
            bullets.append(
                "No single user is a statistical outlier this window — spend is "
                "spread across the cohort, not one runaway account."
            )

    ev = d.head(15)[["DIMENSION", "ALLOC_CREDITS"]].copy()
    # CREDIT_SHARE is recomputed from the SAME named-user total the headline and
    # bullets use — NOT the builder's ELAPSED_SHARE, whose denominator is the whole
    # scoped pool INCLUDING the NONE/UNKNOWN load we dropped, which would show a
    # different (smaller) share than the headline for the very same row.
    ev["CREDIT_SHARE"] = ev["ALLOC_CREDITS"] / total
    return AnswerResult(
        intent=_SPEND_INTENT,
        headline=headline,
        bullets=bullets,
        evidence=ev,
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
    # QUERY_TYPE can be SQL NULL (ANY_VALUE over a null group) -> None/NaN, which
    # str() would render as the literal "None"/"nan". Coerce those to "query".
    _qt = top.get("QUERY_TYPE")
    qtype = str(_qt) if (_qt is not None and not pd.isna(_qt) and str(_qt).strip()) else "query"
    cs = float(top["CS_CREDITS"])
    runs = int(_num(top.get("RUNS", 0)))
    # cloud_svc_top_shapes is LIMIT 30, so total_cs is the sum of the TOP shapes,
    # not all cloud-services credits — label the share for exactly that base so
    # the headline never overstates the top shape's fraction of true CS spend.
    n_shapes = len(s)
    share = cs / total_cs
    if n_shapes == 1:
        share_clause = "the only cloud-services query shape this window"
    else:
        share_clause = f"{share * 100:.0f}% of the top {n_shapes} query shapes' CS credits"

    headline = (
        f"The biggest cloud-services driver over {params.days}d is a {qtype} "
        f"pattern: {_fmt(cs)} CS credits across {runs:,} runs ({share_clause})."
    )

    bullets: list[str] = []
    for i in range(min(3, len(s))):
        r = s.iloc[i]
        bullets.append(
            f"{_code(_sample(r.get('SAMPLE_TEXT'), 90))} — "
            f"{_fmt(float(r['CS_CREDITS']))} CS credits ({int(_num(r.get('RUNS', 0))):,} runs)"
        )

    byuser = frames.get("byuser")
    if byuser is not None and not byuser.empty and "CS_CREDITS" in byuser.columns:
        bu = byuser.copy()
        bu["CS_CREDITS"] = pd.to_numeric(bu["CS_CREDITS"], errors="coerce").fillna(0.0)
        bu = bu.sort_values("CS_CREDITS", ascending=False)
        if not bu.empty and float(bu.iloc[0]["CS_CREDITS"]) > 0:
            r0 = bu.iloc[0]
            _u = r0.get("USER_NAME")
            uname = str(_u) if (_u is not None and not pd.isna(_u) and str(_u).strip()) else "(unknown)"
            bullets.append(
                f"Heaviest user: {uname} ({_fmt(float(r0['CS_CREDITS']))} CS credits)"
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
        ev["SAMPLE_TEXT"] = ev["SAMPLE_TEXT"].map(lambda t: _sample(t, 100))
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
# Answerer 3 — "which warehouse is wasting credits?"
# --------------------------------------------------------------------------- #
_WASTE_INTENT = "warehouse_idle_waste"


def _needs_warehouse_waste(params: AskParams) -> list[QuerySpec]:
    return [
        QuerySpec(
            key="idle",
            sql=insights_sql.idle_warehouse_analysis(params.days, params.company),
            tier="recent",
        )
    ]


def _analyze_warehouse_waste(
    params: AskParams, frames: dict[str, pd.DataFrame]
) -> AnswerResult:
    # idle_warehouse_analysis is a LIVE builder clamped to 90d (bounded_days
    # default) — label the effective window, never the raw request (see the spend
    # answerer for the same discipline).
    eff = bounded_days(params.days)
    src = f"insights_sql.idle_warehouse_analysis({eff}d)"
    meta: dict[str, object] = {"days": eff, "company": params.company}
    df = frames.get("idle")
    if (df is None or df.empty or "IDLE_CREDITS" not in df.columns
            or "WAREHOUSE_NAME" not in df.columns):
        return AnswerResult(
            intent=_WASTE_INTENT,
            headline=f"No metered warehouse activity in the last {eff}d.",
            source=src, confidence="no_data", params=meta,
        )

    d = df.copy()
    for c in ("IDLE_CREDITS", "TOTAL_CREDITS", "IDLE_HOURS", "METERED_HOURS"):
        if c in d.columns:
            d[c] = pd.to_numeric(d[c], errors="coerce").fillna(0.0)
    d = d.sort_values("IDLE_CREDITS", ascending=False).reset_index(drop=True)
    total_idle = float(d["IDLE_CREDITS"].sum())

    cap_note = (
        [f"Idle analysis is capped to the live {eff}-day window (you asked for {params.days}d)."]
        if eff < params.days else []
    )
    # Never claim a scope wider than the query ran: the builder is company-scoped
    # for a non-ALL filter, so a subtotal is that company's slice, not the account's.
    scope_wh = "warehouse" if params.company == "ALL" else f"{params.company} warehouse"
    if total_idle <= 0:
        # Honest good news, not a false "no data": warehouses are well-utilized.
        return AnswerResult(
            intent=_WASTE_INTENT,
            headline=(
                f"No idle-credit waste in the last {eff}d — every metered {scope_wh} "
                "ran queries in the hours it was billed for."
            ),
            bullets=cap_note,
            evidence=d.head(15),
            source=src, confidence="grounded", params=meta,
        )

    top = d.iloc[0]
    wh = str(top["WAREHOUSE_NAME"])
    idle_cr = float(top["IDLE_CREDITS"])
    tot_cr = float(top.get("TOTAL_CREDITS", 0.0))
    idle_h = int(_num(top.get("IDLE_HOURS", 0)))
    met_h = int(_num(top.get("METERED_HOURS", 0)))
    idle_share = idle_cr / tot_cr if tot_cr > 0 else 0.0

    headline = (
        f"Over the last {eff}d, {wh} wastes the most: {_fmt(idle_cr)} idle credits "
        f"— {idle_share * 100:.0f}% of its {_fmt(tot_cr)} credits, idle {idle_h} of "
        f"{met_h} metered hours."
    )
    bullets: list[str] = []
    for i in range(min(5, len(d))):
        r = d.iloc[i]
        rc = float(r["IDLE_CREDITS"])
        if rc <= 0:
            break
        rt = float(r.get("TOTAL_CREDITS", 0.0))
        pct = rc / rt * 100 if rt > 0 else 0.0
        bullets.append(f"{r['WAREHOUSE_NAME']}: {_fmt(rc)} idle credits ({pct:.0f}% of its spend)")
    idle_scope = ("Account-wide idle waste this window" if params.company == "ALL"
                  else f"Idle waste across {params.company} warehouses this window")
    bullets.append(f"{idle_scope}: {_fmt(total_idle)} credits.")
    bullets.extend(cap_note)

    ev_cols = [c for c in ("WAREHOUSE_NAME", "IDLE_CREDITS", "TOTAL_CREDITS", "IDLE_HOURS", "METERED_HOURS") if c in d.columns]
    return AnswerResult(
        intent=_WASTE_INTENT,
        headline=headline, bullets=bullets, evidence=d.head(15)[ev_cols].copy(),
        source=src, confidence="grounded", params=meta,
    )


# --------------------------------------------------------------------------- #
# Answerer 4 — "which task is failing most?"
# --------------------------------------------------------------------------- #
_TASK_INTENT = "task_failures"


def _needs_task_failures(params: AskParams) -> list[QuerySpec]:
    return [
        QuerySpec(
            key="tasks",
            sql=mart_sql.fact_task_daily(params.days, params.company),
            tier="recent",
            # Uncapped: we SUM RUNS/FAILED per task, so a 5000-row truncation of
            # this per-task-day fact would drop a busy task's clean days and inflate
            # its failure rate. Row count is bounded by tasks x active days.
            max_rows=0,
        )
    ]


def _analyze_task_failures(
    params: AskParams, frames: dict[str, pd.DataFrame]
) -> AnswerResult:
    src = f"mart_sql.fact_task_daily({params.days}d, aggregated by task)"
    meta: dict[str, object] = {"days": params.days, "company": params.company}
    df = frames.get("tasks")
    if (df is None or df.empty or "TASK_NAME" not in df.columns
            or "FAILED" not in df.columns):
        return AnswerResult(
            intent=_TASK_INTENT,
            headline=f"No task activity in the last {params.days}d.",
            source=src, confidence="no_data", params=meta,
        )

    d = df.copy()
    d["FAILED"] = pd.to_numeric(d["FAILED"], errors="coerce").fillna(0)
    d["RUNS"] = pd.to_numeric(d.get("RUNS", 0), errors="coerce").fillna(0)
    keys = [c for c in ("DATABASE_NAME", "SCHEMA_NAME", "TASK_NAME") if c in d.columns]
    agg = d.groupby(keys, dropna=False, as_index=False).agg(
        FAILED=("FAILED", "sum"), RUNS=("RUNS", "sum"))
    if "LAST_ERROR" in d.columns and "DAY" in d.columns:
        last = d.sort_values("DAY").groupby(keys, dropna=False, as_index=False).agg(
            LAST_ERROR=("LAST_ERROR", "last"))
        agg = agg.merge(last, on=keys, how="left")

    fails = agg[agg["FAILED"] > 0].sort_values("FAILED", ascending=False).reset_index(drop=True)
    if fails.empty:
        return AnswerResult(
            intent=_TASK_INTENT,
            headline=(
                f"No task failures in the last {params.days}d — every task run that "
                "was recorded succeeded."
            ),
            source=src, confidence="grounded", params=meta,
        )

    top = fails.iloc[0]
    task = _task_label(top)
    failed = int(top["FAILED"])
    runs = int(top["RUNS"])
    rate = failed / runs * 100 if runs > 0 else 0.0
    headline = (
        f"Over the last {params.days}d, {task} is failing most: {failed:,} failures "
        f"across {runs:,} runs ({rate:.0f}%)."
    )
    bullets: list[str] = []
    for i in range(min(5, len(fails))):
        r = fails.iloc[i]
        rf = int(r["FAILED"])
        rr = int(r["RUNS"])
        rrate = rf / rr * 100 if rr > 0 else 0.0
        bullets.append(f"{_task_label(r)}: {rf:,} failures / {rr:,} runs ({rrate:.0f}%)")
    if "LAST_ERROR" in top.index:
        err = _sample(top.get("LAST_ERROR"), 160)
        if err and err != "(no sample text)":
            bullets.append(f"Latest error on {task}: {_code(err)}")

    ev = fails.head(15).copy()
    ev["FAIL_RATE_PCT"] = (ev["FAILED"] / ev["RUNS"].where(ev["RUNS"] > 0) * 100).round(0)
    if "TASK_NAME" in ev.columns:
        ev["TASK_NAME"] = ev["TASK_NAME"].map(lambda t: str(t) if _notnull(t) else "(unnamed task)")
    ev_cols = [c for c in ("DATABASE_NAME", "SCHEMA_NAME", "TASK_NAME", "FAILED", "RUNS", "FAIL_RATE_PCT", "LAST_ERROR") if c in ev.columns]
    return AnswerResult(
        intent=_TASK_INTENT,
        headline=headline, bullets=bullets, evidence=ev[ev_cols],
        source=src, confidence="grounded", params=meta,
    )


# --------------------------------------------------------------------------- #
# the registry
# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
# Answerer 5 — "which model is driving AI / Cortex spend?"
# --------------------------------------------------------------------------- #
_CORTEX_INTENT = "cortex_spend_by_model"


def _needs_cortex_by_model(params: AskParams) -> list[QuerySpec]:
    # CORTEX_FUNCTIONS_USAGE_HISTORY has no company/db grain, so the page company
    # filter does not apply — the answer is honestly account-wide.
    return [
        QuerySpec(
            key="cortex",
            sql=cortex_sql.cortex_model_costs(params.days),
            tier="recent",
        )
    ]


def _analyze_cortex_by_model(
    params: AskParams, frames: dict[str, pd.DataFrame]
) -> AnswerResult:
    src = (f"cortex_sql.cortex_model_costs({params.days}d) — "
           "ACCOUNT_USAGE.CORTEX_FUNCTIONS_USAGE_HISTORY (account-wide)")
    meta: dict[str, object] = {"days": params.days, "company": params.company}
    no_data = (
        f"No Cortex/AI function spend in the last {params.days}d — "
        "CORTEX_FUNCTIONS_USAGE_HISTORY is empty or unavailable on this account "
        "(Cortex Code via Snowsight/CLI bills through a separate usage view)."
    )
    df = frames.get("cortex")
    if (df is None or df.empty or "CREDITS" not in df.columns
            or "MODEL_NAME" not in df.columns):
        return AnswerResult(intent=_CORTEX_INTENT, headline=no_data,
                            source=src, confidence="no_data", params=meta)

    d = df.copy()
    d["CREDITS"] = pd.to_numeric(d["CREDITS"], errors="coerce").fillna(0.0)
    # A model can appear across several functions; roll up to the model so "which
    # model" ranks the model, not each (function, model) pair.
    agg = (
        d.groupby("MODEL_NAME", as_index=False)
        .agg(AI_CREDITS=("CREDITS", "sum"))
        .sort_values("AI_CREDITS", ascending=False)
        .reset_index(drop=True)
    )
    total = float(agg["AI_CREDITS"].sum())
    if agg.empty or total <= 0:
        return AnswerResult(intent=_CORTEX_INTENT, headline=no_data,
                            source=src, confidence="no_data", params=meta)

    z = robust_zscores(agg["AI_CREDITS"])
    top = agg.iloc[0]
    top_model = str(top["MODEL_NAME"])
    # 'n/a' is real spend the usage view could not tag to a model — keep it in the
    # ranking (it is not fabricated) but name it honestly in prose.
    top_disp = "unattributed AI functions (no model tag)" if top_model == "n/a" else top_model
    top_credits = float(top["AI_CREDITS"])
    top_share = top_credits / total
    top_z = _num(z.iloc[0]) if len(z) else 0.0
    outlier = top_z >= OUTLIER_Z
    meta["top_z"] = round(top_z, 2)

    tail = f" — a clear outlier vs the other models (z={top_z:.1f})" if outlier else ""
    headline = (
        f"Over the last {params.days}d, {top_disp} is the top AI/Cortex spender: "
        f"{_fmt(top_credits)} credits ({top_share * 100:.0f}% of AI-function spend){tail}."
    )

    bullets: list[str] = ["Account-wide — Cortex function usage carries no company grain."]
    for i in range(min(5, len(agg))):
        r = agg.iloc[i]
        cr = float(r["AI_CREDITS"])
        name = "n/a (no model tag)" if str(r["MODEL_NAME"]) == "n/a" else str(r["MODEL_NAME"])
        bullets.append(f"{name}: {_fmt(cr)} credits ({cr / total * 100:.0f}%)")
    n = len(agg)
    if not outlier:
        if n < 5:
            bullets.append(
                f"Only {n} model{'s' if n != 1 else ''} had AI spend this window — "
                "too few for the peer-outlier test; read the ranking directly."
            )
        elif top_share >= 0.5:
            bullets.append(
                f"{top_disp} carries the majority ({top_share * 100:.0f}%) of "
                "AI-function spend, though not a statistical outlier vs the others."
            )
        else:
            bullets.append(
                "No single model is a statistical outlier this window — AI spend is "
                "spread across models, not one runaway."
            )

    ev = agg.head(15)[["MODEL_NAME", "AI_CREDITS"]].copy()
    ev["CREDIT_SHARE"] = ev["AI_CREDITS"] / total
    return AnswerResult(
        intent=_CORTEX_INTENT,
        headline=headline,
        bullets=bullets,
        evidence=ev,
        source=src,
        confidence="grounded",
        params=meta,
    )


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
            # A "spender"/"spenders" IS a user reference, so it satisfies the
            # user gate for phrasings like "top spenders" that name no who/user.
            ("user", "users", "who", "whom", "account", "login",
             "spender", "spenders"),
            # "spik"/"spend" are prefix stems (router._STEMS): spik -> spike/…,
            # spend -> spends/spending/spender/spent, so the verb family all gate.
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
            # SINGLE gate: any question that names cloud services routes here —
            # the answer IS the driving queries, so "why are cloud services
            # spiking" must not refuse. A plain per-user spend question has no
            # cloud-services phrase, so this never steals it.
            ("cloud service", "cloud services", "cloud-services",
             "cloud_services", "cs credit", "cs credits", "cloud compute"),
        ),
        needs=_needs_cs_by_query,
        analyze=_analyze_cs_by_query,
        # A question that names "cloud services" is a specific intent — it must win
        # over the generic spend answerer even when it also carries user/spend words.
        priority=1,
    ),
    Answerer(
        intent=_WASTE_INTENT,
        title="Which warehouse is wasting credits",
        examples=(
            "which warehouse is wasting credits",
            "which warehouse is idle the most",
            "where am I paying for idle warehouses",
        ),
        keywords=(
            "warehouse", "warehouses", "wh", "idle", "wast", "unused",
            "paying", "credits", "underused",
        ),
        require_all=(
            ("warehouse", "warehouses", "wh"),
            # "wast" stems waste/wasting/wasted (router._STEMS).
            ("wast", "idle", "unused", "underused", "paying", "burn"),
        ),
        needs=_needs_warehouse_waste,
        analyze=_analyze_warehouse_waste,
    ),
    Answerer(
        intent=_TASK_INTENT,
        title="Which task is failing most",
        examples=(
            "what task is failing most",
            "which task fails the most",
            "which pipeline task keeps failing",
        ),
        keywords=(
            "task", "tasks", "pipeline", "job", "fail", "fails", "failed",
            "failing", "failure", "failures", "error", "broken",
        ),
        require_all=(
            ("task", "tasks", "pipeline", "pipelines", "job", "jobs"),
            # Enumerated (not a "fail" stem) so failover/failsafe never match.
            ("fail", "fails", "failed", "failing", "failure", "failures",
             "error", "errors", "broken"),
        ),
        needs=_needs_task_failures,
        analyze=_analyze_task_failures,
    ),
    Answerer(
        intent=_CORTEX_INTENT,
        title="Which model is driving AI / Cortex spend",
        examples=(
            "which model is driving AI spend",
            "what is driving cortex credits",
            "top cortex models in the last 30 days",
        ),
        keywords=(
            "cortex", "ai", "model", "models", "llm", "token", "tokens",
            "genai", "embedding", "spend", "credits", "driving", "coco",
        ),
        require_all=(
            # SINGLE gate: any AI/Cortex-domain phrase routes here. A plain spend
            # question carries no AI phrase, so this never steals it; priority breaks
            # a tie with the generic spender for "who is driving AI spend".
            ("cortex", "genai", "gen ai", "llm", "token", "tokens",
             "ai spend", "ai cost", "ai credit", "ai credits", "ai model",
             "ai models", "coco", "embedding", "embeddings"),
        ),
        needs=_needs_cortex_by_model,
        analyze=_analyze_cortex_by_model,
        priority=1,
    ),
)
