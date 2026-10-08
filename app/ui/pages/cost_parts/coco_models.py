"""Cost Intelligence — Chargeback & AI ▸ Cortex Code models (v4.612.0).

The owner, 2026-10-08: "my boss addressed coco usage. We need to track and drill down by user which models
they select when using coco." ONE live read, cortex_sql.coco_model_usage_daily (Snowflake's unified Cortex
Code view: Snowsight, the CLI and Desktop; 365 days at Central day x user x interface x role x model; one
cache entry per company, historical tier = 1 h), and every lens below is a pandas fold of it
(app.logic.cortex.coco_*), so a click never runs another query.

Read-only: no writes, the same visibility as AI users (every Cost Intelligence viewer). Dollars only through
formulas.credits_to_usd at the AI rate. "Which model they select" is the MAIN model of each request (the one
that carried the most credits); one request can bill several models, so "Requests billing it" is shown apart
and never added up. The source label below avoids the live-telemetry schema literal on purpose (this file's
live-scan budget is 0; its reach is pinned in tests/history_locks/test_v451_trust.py).
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from app.core.query import run
from app.core.result import QueryResult, is_privilege_error, is_schema_drift
from app.data import cortex_sql
from app.logic.cortex import (
    COCO_UNATTRIBUTED,
    coco_breakdown,
    coco_data_from,
    coco_kpis,
    coco_model_mix,
    coco_model_users,
    coco_model_window,
    coco_user_rollup,
    coco_window_start,
)
from app.logic.date_windows import window_label, window_phrase
from app.logic.formulas import credits_to_usd, format_usd, md_dollars
from app.ui import charts
from app.ui.components import (
    empty_state,
    kpi_row,
    master_detail,
    methodology_note,
    nested_sections,
    reconciliation_footer,
    result_caption,
    selectable_table,
    styled_table,
    with_user_names,
)

_PAGE = "Cost Intelligence"
_SOURCE = ("SNOWFLAKE_COCO_USAGE_HISTORY (CREDITS_GRANULAR + TOKENS_GRANULAR by model, Snowsight + CLI + "
           "Desktop, 365d live, window cut in-app)")
_MAX_ROWS = 200_000
_IFACE_KEY = "coco_iface"

_MAIN_MODEL_NOTE = (
    "Main model = the model that carried the most credits in a request. One request can bill several models "
    "(for example a main model plus a smaller helper), so “Requests billing it” can add up to more than the "
    "request count, while “Main-model requests” adds up.")
_SCOPE_NOTE = (
    "Includes Cortex Code Desktop. AI users above (and the company showback and the CoCo spend tile) still read "
    "Snowsight and the CLI only, so their Cortex Code spend is this panel's Snowsight + CLI part.")


def _usd(df: pd.DataFrame, cols: dict[str, str], ai_rate: float) -> pd.DataFrame:
    """Credit columns -> dollar columns ({credit col: usd col}) at the AI rate, unrounded (formulas law). A NaN
    credit (a ratio with no denominator) stays NaN ('—'), never a fabricated $0.00."""
    out = df.copy()
    for src, dst in cols.items():
        out[dst] = out[src].map(
            lambda c: credits_to_usd(c, ai_rate, round_cents=False) if (c is not None and c == c) else float("nan"))
    return out


def _render_failure(res: QueryResult) -> None:
    """A failed probe read renders by its kind (v4.603): an expected absence is a setup state, anything else a
    failed read with the error one click away. A missing column is not logged on a probe read, so its sentence
    names no error log."""
    kind = str(res.error_kind or "")
    if kind == "unknown_function":
        empty_state("needs_setup",
                    "Cortex Code usage telemetry is not available in this account or region (the usage view checks a "
                    "Cortex Code subscription that is not present, 002139). This panel lights up on its own if it "
                    "lands.")
    elif kind == "absent":
        empty_state("needs_setup",
                    "SNOWFLAKE_COCO_USAGE_HISTORY (the unified Cortex Code view) is not readable by this app’s role; "
                    "it needs IMPORTED PRIVILEGES on the SNOWFLAKE database (roles.sql).")
    elif is_privilege_error(kind):
        empty_state("needs_setup",
                    "The app’s role cannot read SNOWFLAKE_COCO_USAGE_HISTORY (Insufficient privileges), so Cortex "
                    "Code models cannot be shown. Re-apply the app’s grants (roles.sql).", detail=res.error)
    elif is_schema_drift(kind):
        empty_state("unavailable",
                    "A column this read uses is missing from Snowflake’s view (schema drift), so models cannot be "
                    "shown.", detail=res.error)
    else:
        empty_state("unavailable",
                    "The Cortex Code model read failed this run. Retry, or an admin can check the Admin error log.",
                    detail=res.error)


def _interfaces(full: pd.DataFrame) -> list[str]:
    """The interface filter (zero queries): every interface in the window, all selected at first. A remembered
    pick keeps only the interfaces still present (a company or window change can drop one); set before the
    widget mounts, so no default= is passed."""
    options = sorted({str(s) for s in full["SOURCE"].dropna()})
    prev = st.session_state.get(_IFACE_KEY)
    if not isinstance(prev, list):
        st.session_state[_IFACE_KEY] = list(options)
    else:
        kept = [s for s in prev if s in options]
        if kept != prev:
            st.session_state[_IFACE_KEY] = kept or list(options)
    picked = st.multiselect("Interface", options, key=_IFACE_KEY,
                            help="Filters every number and table below; the Desktop share always reads all "
                                 "interfaces.")
    return [str(p) for p in picked]


def coco_models_section(company: str, days: int, ai_rate: float, *, bounds: tuple | None = None) -> None:
    """Cost Intelligence > Chargeback & AI > Cortex Code models: summary KPIs, model mix, daily spend by model,
    then By user (that user's models, trend, interfaces, roles and token / credit mix) and By model (who runs
    it). One cached read; every lens is a fold of it."""
    res = run(cortex_sql.coco_model_usage_daily(company), page=_PAGE, key=f"coco_models_{company}",
              tier="historical", source=_SOURCE, probe=True, max_rows=_MAX_ROWS)
    if not res.ok:
        _render_failure(res)
        return
    if res.empty:
        empty_state("no_data_yet",
                    "No Cortex Code usage (Snowsight, CLI or Desktop) in the last 365 days for this scope.")
        result_caption(res)
        return
    wphrase = window_phrase(bounds, days)
    wlab = window_label(bounds, days)
    full = coco_model_window(res.df, days, bounds=bounds)
    if res.truncated:
        # The read is ordered oldest day first, so a cut keeps the OLDEST rows: say which days are missing.
        _last = pd.to_datetime(res.df["USAGE_DATE"], errors="coerce").max()
        _last_s = f"{_last:%b} {_last.day}, {_last:%Y}" if pd.notna(_last) else "the last day read"
        st.caption(f"The read hit its {_MAX_ROWS:,}-row cap, which keeps the oldest days: nothing after {_last_s} "
                   "was read (that day may be partial), so totals for later days are missing or low.")
    if full.empty:
        if res.truncated:
            empty_state("unavailable", f"The read hit its {_MAX_ROWS:,}-row cap before reaching {wphrase}, so "
                                       "this window cannot be shown.")
        else:
            empty_state("no_data_yet", f"No Cortex Code usage in {wphrase} for this scope.")
        result_caption(res)
        return
    _from = coco_data_from(res.df)
    if _from is not None and coco_window_start(days, bounds=bounds) < _from:
        st.caption(f"No Cortex Code usage is recorded before {_from:%b} {_from.day}, {_from:%Y} in this read "
                   "(the view keeps 365 days).")
    picked = _interfaces(full)
    win = full[full["SOURCE"].astype(str).isin(picked)].reset_index(drop=True)
    if win.empty:
        empty_state("no_data_yet", "No interface is selected: pick at least one above.")
        result_caption(res)
        return

    k = coco_kpis(win, full)
    spend_usd = credits_to_usd(k["spend"], ai_rate, round_cents=False)
    _dsh = k["desktop_share_pct"]
    _tsh = k["top_model_share_pct"]
    kpi_row([
        {"label": f"Cortex Code spend, {wlab}", "value": format_usd(spend_usd),
         "help": (f"TOKEN_CREDITS of every request in the selected interfaces, each counted once, x the AI rate "
                  f"(AI_CREDIT_PRICE_USD, ${ai_rate:.2f} per credit). All interfaces in this window: Snowsight + "
                  f"CLI {format_usd(credits_to_usd(k['snowsight_cli'], ai_rate))} (what AI users above reads), "
                  f"Desktop {format_usd(credits_to_usd(k['desktop'], ai_rate))}.")},
        {"label": "Users", "value": f"{k['users']:,}"},
        {"label": "Models used", "value": f"{k['models_used']:,}",
         "help": "Named models that billed credits in this window. The '(not attributed to a model)' row is "
                 "request credits no model breakdown covers; it is not a model."},
        {"label": "Top model", "value": k["top_model"] or "—",
         "delta": f"{_tsh:.0f}% of spend" if _tsh is not None else None, "delta_color": "off",
         "help": "The named model with the most credits in this window."},
        {"label": "Desktop share", "value": f"{_dsh:.1f}%" if _dsh is not None else "—",
         "help": "Cortex Code Desktop's share of this window's Cortex Code spend across all interfaces (the "
                 "interface filter does not change it). AI users above does not read Desktop yet."},
    ])

    mix = _usd(coco_model_mix(win), {"COCO_CREDITS": "USD"}, ai_rate)
    daily = _usd(coco_model_mix(win, by=("USAGE_DATE",)), {"COCO_CREDITS": "USD"}, ai_rate)
    daily = daily.rename(columns={"USAGE_DATE": "DAY"})
    left, right = st.columns([1.0, 1.1])
    with left:
        st.markdown("**Spend by model**")
        charts.bar_usd(mix, "MODEL_NAME", "USD", title="Spend (USD)", top_n=12, takeaway=True)
    with right:
        st.markdown("**Daily spend by model**")
        charts.daily_stacked_usd(daily, "DAY", "MODEL_NAME", "USD")
    # Real coverage, not a sum compared with itself: the named models' own credits against the request totals
    # (the residual row in the tables explains the gap).
    reconciliation_footer(credits_to_usd(k["named_model_credits"], ai_rate, round_cents=False), spend_usd,
                          label="named models", expected_label="request totals (TOKEN_CREDITS)")
    st.caption(_MAIN_MODEL_NOTE)
    st.caption(_SCOPE_NOTE)

    view = nested_sections(["By user", "By model"], key="coco_model_view")
    if view == "By model":
        _by_model(win, ai_rate)
    else:
        _by_user(win, ai_rate)

    result_caption(res, note="Live, up to ~1 h behind; read once for 365 days and cached 1 h; the window is cut "
                             "in-app on the Central day. Snowsight, CLI and Desktop.")
    methodology_note(
        "Method: spend = each request's TOKEN_CREDITS, counted once on its main model (the most credits in that "
        "request; ties by name). A model's own credits are its CREDITS_GRANULAR input / cache-read / cache-write / "
        "output leaves (each read as a number, a missing leaf as 0); the gap to the request totals is the "
        "'(not attributed to a model)' row (or '(model credits above request totals)' when the leaves are "
        "higher), and requests with no breakdown sit on it too. Tokens come from TOKENS_GRANULAR, joined on day, "
        "user, interface, role and model. Users are USERS.NAME by USER_ID, the AI users key; every row is summed "
        "exactly as AI users sums its requests.")


def _by_user(win: pd.DataFrame, ai_rate: float) -> None:
    users = _usd(coco_user_rollup(win), {"TOKEN_CREDITS": "SPEND_USD"}, ai_rate)
    disp = with_user_names(users, _PAGE)
    disp = disp[[c for c in ("USER", "USER_NAME", "SPEND_USD", "REQUESTS", "MOST_USED_MODEL",
                             "MOST_USED_SHARE_PCT", "TOP_MODEL", "TOP_MODEL_SHARE_PCT", "MODELS_USED",
                             "INTERFACES", "CACHE_HIT_PCT", "LAST_USED_AT") if c in disp.columns]]

    def _list(display_df: pd.DataFrame, list_key: str):
        return selectable_table(
            display_df, key=list_key, height=380, slug="coco-models-by-user", sort_label="by $ desc",
            column_config={
                "USER_NAME": st.column_config.TextColumn("Login"),
                "REQUESTS": st.column_config.NumberColumn("Main-model requests", format="%d"),
                "MOST_USED_MODEL": st.column_config.TextColumn("Most-used model"),
                "MOST_USED_SHARE_PCT": st.column_config.NumberColumn("% of requests", format="%.0f%%"),
                "TOP_MODEL": st.column_config.TextColumn("Top model (by $)"),
                "TOP_MODEL_SHARE_PCT": st.column_config.NumberColumn("% of spend", format="%.0f%%"),
                "MODELS_USED": st.column_config.NumberColumn("Models", format="%d"),
            })

    master_detail(disp, key="coco_by_user", id_col="USER_NAME", list_render_fn=_list,
                  detail_render_fn=lambda row: _user_detail(row, win, ai_rate), ratio=(1.15, 1.0),
                  empty_detail_msg="Select a user on the left to see their models.")


def _user_detail(row: pd.Series, win: pd.DataFrame, ai_rate: float) -> None:
    login = str(row.get("USER_NAME") or "")
    name = str(row.get("USER") or login)
    uwin = win[win["USER_NAME"].astype(str) == login]
    st.markdown(md_dollars(f"**{name} ({login})**" if name != login else f"**{login}**"))
    k = coco_kpis(uwin)
    _most = row.get("MOST_USED_MODEL")
    kpi_row([
        {"label": "Spend", "value": format_usd(credits_to_usd(k["spend"], ai_rate))},
        {"label": "Main-model requests", "value": f"{k['requests']:,.0f}"},
        {"label": "Models", "value": f"{k['models_used']:,}"},
        {"label": "Most-used model", "value": str(_most) if pd.notna(_most) and str(_most) else "—",
         "help": "The model most of this user's requests ran on (their main model)."},
    ])
    models = _usd(coco_model_mix(uwin), {"COCO_CREDITS": "SPEND_USD", "COCO_CREDITS_INPUT": "INPUT_USD",
                                         "COCO_CREDITS_CACHE_READ": "CACHE_READ_USD",
                                         "COCO_CREDITS_CACHE_WRITE": "CACHE_WRITE_USD",
                                         "COCO_CREDITS_OUTPUT": "OUTPUT_USD"}, ai_rate)
    charts.bar_usd(models, "MODEL_NAME", "SPEND_USD", title="Spend (USD)", top_n=12)
    daily = _usd(coco_model_mix(uwin, by=("USAGE_DATE",)), {"COCO_CREDITS": "USD"}, ai_rate)
    charts.daily_stacked_usd(daily.rename(columns={"USAGE_DATE": "DAY"}), "DAY", "MODEL_NAME", "USD")
    st.markdown("**Models**")
    styled_table(
        models[["MODEL_NAME", "SPEND_USD", "SHARE_PCT", "MAIN_REQUESTS", "REQUESTS_USING", "CREDITS_PER_REQUEST",
                "INPUT_USD", "CACHE_READ_USD", "CACHE_WRITE_USD", "OUTPUT_USD", "TOKENS_INPUT",
                "TOKENS_CACHE_READ", "TOKENS_CACHE_WRITE", "TOKENS_OUTPUT", "CACHE_HIT_PCT"]],
        slug=f"coco-user-{login}-models", size_note=False,
        column_config={
            "SHARE_PCT": st.column_config.NumberColumn("% of spend", format="%.1f%%"),
            "MAIN_REQUESTS": st.column_config.NumberColumn("Main-model requests", format="%d"),
            "REQUESTS_USING": st.column_config.NumberColumn("Requests billing it", format="%d"),
            "CREDITS_PER_REQUEST": st.column_config.NumberColumn("Credits per request billing it",
                                                                 format="%.4f"),
            "TOKENS_INPUT": st.column_config.NumberColumn("Input tokens", format="%d"),
            "TOKENS_CACHE_READ": st.column_config.NumberColumn("Cache-read tokens", format="%d"),
            "TOKENS_CACHE_WRITE": st.column_config.NumberColumn("Cache-write tokens", format="%d"),
            "TOKENS_OUTPUT": st.column_config.NumberColumn("Output tokens", format="%d"),
        })
    for dim, title, slug in (("SOURCE", "Interfaces", "interfaces"), ("ROLE_NAME", "Roles used", "roles")):
        st.markdown(f"**{title}**")
        part = _usd(coco_breakdown(uwin, dim), {"TOKEN_CREDITS": "SPEND_USD"}, ai_rate)
        styled_table(part[[dim, "SPEND_USD", "REQUESTS", "MODELS", "SHARE_PCT"]],
                     slug=f"coco-user-{login}-{slug}", size_note=False,
                     column_config={
                         "REQUESTS": st.column_config.NumberColumn("Main-model requests", format="%d"),
                         "SHARE_PCT": st.column_config.NumberColumn("% of spend", format="%.1f%%"),
                     })
    if uwin["ROLE_NAME"].astype(str).eq("(not recorded)").any():
        st.caption("'(not recorded)' roles are usage from before Snowflake recorded the role.")


def _by_model(win: pd.DataFrame, ai_rate: float) -> None:
    mix = _usd(coco_model_mix(win), {"COCO_CREDITS": "SPEND_USD", "CREDITS_PER_1M_TOKENS": "USD_PER_1M_TOKENS"},
               ai_rate)
    mix = mix.rename(columns={"SHARE_PCT": "MODEL_SHARE_PCT"})
    disp = mix[["MODEL_NAME", "SPEND_USD", "MODEL_SHARE_PCT", "USERS", "MAIN_REQUESTS", "REQUESTS_USING",
                "CREDITS_PER_REQUEST", "USD_PER_1M_TOKENS", "CACHE_HIT_PCT"]]

    def _list(display_df: pd.DataFrame, list_key: str):
        return selectable_table(
            display_df, key=list_key, height=380, slug="coco-models-by-model", sort_label="by $ desc",
            column_config={
                "MODEL_SHARE_PCT": st.column_config.NumberColumn("% of spend", format="%.1f%%"),
                "MAIN_REQUESTS": st.column_config.NumberColumn("Main-model requests", format="%d"),
                "REQUESTS_USING": st.column_config.NumberColumn("Requests billing it", format="%d"),
                "CREDITS_PER_REQUEST": st.column_config.NumberColumn("Credits per request billing it",
                                                                     format="%.4f"),
                "USD_PER_1M_TOKENS": st.column_config.NumberColumn("$/1M tokens", format="$%.2f"),
            })

    master_detail(disp, key="coco_by_model", id_col="MODEL_NAME", list_render_fn=_list,
                  detail_render_fn=lambda row: _model_detail(row, win, ai_rate),
                  empty_detail_msg="Select a model on the left to see who uses it.")


def _model_detail(row: pd.Series, win: pd.DataFrame, ai_rate: float) -> None:
    model = str(row.get("MODEL_NAME") or "")
    st.markdown(md_dollars(f"**{model}**"))
    if model == COCO_UNATTRIBUTED:
        st.caption("Each user's request credits that no model breakdown covers, requests with no breakdown "
                   "included.")
    users = _usd(coco_model_users(win, model), {"COCO_CREDITS": "SPEND_USD"}, ai_rate)
    disp = with_user_names(users, _PAGE)
    styled_table(
        disp[[c for c in ("USER", "USER_NAME", "SPEND_USD", "SHARE_OF_USER_PCT", "MAIN_REQUESTS", "REQUESTS_USING")
              if c in disp.columns]],
        slug=f"coco-model-{model}-users", sort_label="by $ desc",
        column_config={
            "USER_NAME": st.column_config.TextColumn("Login"),
            "SHARE_OF_USER_PCT": st.column_config.NumberColumn("% of the user’s CoCo spend", format="%.1f%%"),
            "MAIN_REQUESTS": st.column_config.NumberColumn("Main-model requests", format="%d"),
            "REQUESTS_USING": st.column_config.NumberColumn("Requests billing it", format="%d"),
        })
    by_iface = coco_model_mix(win, by=("USAGE_DATE", "SOURCE"))
    by_iface = _usd(by_iface[by_iface["MODEL_NAME"].astype(str) == model], {"COCO_CREDITS": "USD"}, ai_rate)
    st.markdown("**Daily spend by interface**")
    charts.daily_stacked_usd(by_iface.rename(columns={"USAGE_DATE": "DAY"}), "DAY", "SOURCE", "USD")
