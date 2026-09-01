"""App shell: sidebar navigation, global filters, page dispatch."""

from __future__ import annotations

import time
from pathlib import Path

import streamlit as st

# rec15: the browser-tab icon is the rendered brand radar-mark PNG, replacing the
# last emoji in the chrome (emoji render inconsistently across platforms). Falls
# back to the emoji if the asset is somehow missing, so it can never break load.
_FAVICON = Path(__file__).parent / "assets" / "favicon.png"
st.set_page_config(
    page_title="OVERWATCH — Snowflake Command Center",
    page_icon=str(_FAVICON) if _FAVICON.exists() else "🛰️",
    layout="wide",
    # rec14: "auto" collapses the sidebar on narrow/phone viewports (where it
    # otherwise covers the content Brief is built for) and stays open on desktop.
    initial_sidebar_state="auto",
)

from app.companies import COMPANIES, classify_databases, databases_for  # noqa: E402
from app.config import (  # noqa: E402
    DEFAULT_DAY_WINDOW,
    MAX_LIVE_WINDOW_DAYS,
    PAGES_BY_PROFILE,
    TRIAGE_WINDOW_OPTIONS,
    nav_groups_for,
)
from app.core.identity import identity_sql  # noqa: E402
from app.core.query import (  # noqa: E402
    _buffer_write,
    bump_refresh_salt,
    cache_scope,
    flush_write_buffer,
    run,
)
from app.core.session import active_profile, connection_available, current_role  # noqa: E402
from app.core.sqlsafe import sql_literal  # noqa: E402
from app.core.state import (  # noqa: E402
    consume_pending_navigation,
    init_filters,
    remember_page,
    request_navigation,
    requested_page,
)
from app.data import mart_sql, security_sql  # noqa: E402
from app.logic.date_windows import (  # noqa: E402
    resolve_window_days,
    window_option_label,
    window_scope_label,
)
from app.theme import inject_theme  # noqa: E402
from app.ui.components import mark_refreshed  # noqa: E402
from app.ui.icons import icon  # noqa: E402
from app.ui.pages import (  # noqa: E402
    admin,
    alerts,
    ask,
    brief,
    control_room,
    cost,
    decision_studio,
    operations,
    overview,
    security,
)

# Nav labels are plain text (st.radio can't render markup); the sidebar CSS
# active-rail shows position, and each page's header carries its SVG icon.
# This removes the inconsistent emoji CoCo flagged, cleanly.

_RENDERERS = {
    "Overview": overview.render,
    "Control Room": control_room.render,
    "Cost & Contract": cost.render,
    "Operations": operations.render,
    "Decision Studio": decision_studio.render,
    "Alerts": alerts.render,
    "Security": security.render,
    "Admin": admin.render,
    "Brief": brief.render,
    "Ask": ask.render,   # grounded Q&A (app/logic/ask + app/ui/pages/ask.py)
}


def _sidebar(pages: tuple[str, ...], role: str, profile: str, connected: bool) -> str:
    """Navigation-only sidebar; scope filters live in the top bar (original-app layout)."""
    with st.sidebar:
        # Branding, pronounced. Version lives on Admin (App version); the connected role
        # is operator detail that belongs off the primary chrome, not the sidebar.
        # C4: the pulse is BOUND to the live Snowflake connection — a static grey
        # dot when disconnected, so the animation asserts a state that is real.
        _dot = "ow-brand-dot" if connected else "ow-brand-dot ow-brand-dot--off"
        st.markdown(
            f'<div class="ow-brand"><span class="{_dot}"></span>'
            '<span class="ow-brand-word">OVERWATCH</span></div>'
            '<div class="ow-brand-sub">Snowflake Command Center</div>',
            unsafe_allow_html=True,
        )
        # Wave 1 #16: a viewer with no operator entitlement (the READER tier, or any
        # non-operator) gets a trimmed surface with write controls hidden. Say so
        # explicitly with a persistent badge, rather than leaving apparent feature gaps.
        from app.core.session import is_operator as _is_op
        if connected and not _is_op():
            st.markdown(
                '<div style="font-size:0.7rem;font-weight:600;letter-spacing:.04em;'
                'margin-top:6px;display:inline-block;padding:1px 9px;border-radius:999px;'
                'color:var(--ow-ink-mute);border:1px solid var(--ow-ink-mute)">'
                '🔒 Read-only</div>', unsafe_allow_html=True)
        if connected:
            from app.ui.components import last_refreshed_note
            st.markdown(
                f'<div style="font-size:0.72rem;color:var(--ow-ink-mute);margin-top:8px">'
                f'{icon("refresh", 11)} {last_refreshed_note()}</div>', unsafe_allow_html=True)
        else:
            st.caption("Not connected to Snowflake")
        st.divider()

        # rec14: workflow-grouped nav. st.radio has no native section headers, so each
        # group is its own single-select radio under a caption; the chosen page lives
        # in _ow_page (the ONE source of truth).
        #
        # multi-select bug fix: a persistent per-group key made st.radio remember its
        # OWN selection and IGNORE `index`, so a sibling group kept its stale highlight
        # (two groups selected) and the earlier callback-pop of sibling keys was
        # unreliable — a failed pop left the nav stuck. Now each group's key is scoped
        # to the CURRENT page (`_ow_nav_{group}_{current}`): when the page changes the
        # keys change too, so every radio re-seeds cleanly from `index` (derived from
        # _ow_page) and exactly one page is ever highlighted. No popping, no stale keys.
        #
        # r4 desync note: _ow_page is authoritative; a ?page= deep link overrides it
        # only when it CHANGES (the query param lags a click by one rerun).
        _req = requested_page(pages)
        if _req and _req != st.session_state.get("_ow_req_seen"):
            st.session_state["_ow_page"] = _req
            st.session_state["_ow_req_seen"] = _req
        current = st.session_state.get("_ow_page") or pages[0]
        if current not in pages:
            current = pages[0]
        groups = nav_groups_for(pages)

        def _nav_pick(changed_key: str) -> None:
            chosen = st.session_state.get(changed_key)
            if chosen:
                st.session_state["_ow_page"] = chosen   # Streamlit auto-reruns; keys re-scope

        for group, members in groups:
            gkey = f"_ow_nav_{group}_{current}"          # key varies with the page -> no stale state
            st.caption(group)
            idx = members.index(current) if current in members else None
            st.radio(group, members, index=idx, key=gkey,
                     label_visibility="collapsed", on_change=_nav_pick, args=(gkey,))
        page = st.session_state.get("_ow_page") or current
        if page not in pages:
            page = current
        st.session_state["_ow_page"] = page
        remember_page(page)
        # C44 review fix: leaving Alerts expires the momentum queue — returning
        # later must not surprise-open a drawer from a spent triage chain. Gated
        # on the queue key so real deep-link arming semantics are untouched.
        if page != "Alerts" and st.session_state.get("_ow_alert_next_up"):
            st.session_state.pop("_ow_alert_next_up", None)
            st.session_state.pop("_ow_alert_deeplink_armed", None)
        # the ?page= we just wrote is NOT a new deep-link request — record it as seen so
        # its stale echo next rerun can't override a fresh nav click (the _req reconcile
        # above runs after the click callback, so an un-seeded seen clobbered the click).
        st.session_state["_ow_req_seen"] = page

        st.divider()
        _global_jump(pages)
        if st.button("Refresh data", width="stretch"):
            bump_refresh_salt()
            # Re-resolve the role too: a grant/role change mid-session should
            # be picked up here, not only on a full browser reload.
            st.session_state.pop("_ow_current_role", None)
            st.session_state.pop("_ow_current_user", None)
            mark_refreshed()
            # rec48: acknowledge the click — it clears caches + refetches. The
            # button used to bump the salt and rerun with no feedback at all.
            if hasattr(st, "toast"):
                st.toast("Caches cleared — fetching fresh data.")
            st.rerun()
        # Wave 1 #9: a persistent Case File presence in the shell. Additions used to
        # vanish into a bottom-of-Brief expander; the running count now rides every
        # page (session-only, so no query), with a one-click jump to open it on Brief.
        from app.logic.case_file import CASE_STATE_KEY as _CASE_KEY
        _case_items = st.session_state.get(_CASE_KEY) or []
        if _case_items:
            _latest = str((_case_items[-1] or {}).get("title") or "").strip()
            if st.button(f"🗂️ Case File · {len(_case_items)}",
                         width="stretch", key="_ow_case_shell",
                         help=(f"Open the operator Case File on Brief. Latest: {_latest}"
                               if _latest else "Open the operator Case File on Brief.")):
                from app.core.state import request_navigation as _reqnav
                _reqnav("Brief")
        # C19: operator vs audit presentation mode. Operator (default) keeps the
        # daily surface lean; audit shows the full evidence chain (per-panel
        # fetched-at, methodology notes, how-computed expanders). Persists to
        # USER_PREFS via the same idempotent MERGE density used, scoped to the
        # viewer — a personal display pref, so it's not operator-gated.
        #
        # Persist ONLY on a real user flip, via on_change. Review fix: reading
        # the toggle in the render body and diffing against present_mode() was
        # destructive — the widget key latches on a pre-hydrate render, and once
        # PRESENT_MODE hydrates to 'audit' from USER_PREFS the stale toggle read
        # 'operator' back OVER the saved pref. on_change never fires on a
        # hydration re-render, and the hydrate seeds the widget key (below) so
        # its displayed state matches the pref. No value= (the seeded key owns it).
        def _on_present_toggle() -> None:
            _m = "audit" if st.session_state.get("_ow_present_mode_toggle") else "operator"
            st.session_state["_ow_present_mode"] = _m
            if connected:
                from app.core.query import execute_statement
                from app.data import prefs_sql
                execute_statement(prefs_sql.upsert_pref_sql("PRESENT_MODE", _m), page="Views")

        st.toggle(
            "Audit detail", key="_ow_present_mode_toggle", on_change=_on_present_toggle,
            help="Show the full evidence chain — per-panel source timestamps, "
                 "methodology notes, and how-computed detail — for reproducing or "
                 "defending a number. Off keeps the daily surface lean.")
        # rec11: the ACCOUNT_USAGE lag note lives once per page in page_header now
        # (was duplicated here in the sidebar — same sentence twice is chrome noise).
    return page


def _parse_view(raw: str) -> dict | None:
    import json

    try:
        data = json.loads(raw or "")
        return data if isinstance(data, dict) else None
    except (TypeError, ValueError):
        return None


def _apply_default_landing() -> None:
    """Once per session: land on the user's saved default view. An explicit
    ?page= deep link always wins over the default."""
    if st.session_state.get("_ow_default_applied"):
        return
    try:
        if st.query_params.get("page"):
            st.session_state["_ow_default_applied"] = True   # deep link wins, done
            return
    except Exception:  # noqa: BLE001
        pass
    from app.core.state import consume_pending_navigation
    from app.data import prefs_sql

    if not str(st.session_state.get("_ow_current_user") or ""):
        # r11 #3: identity not hydrated (or no connection yet) — retry next
        # rerun WITHOUT spending an attempt. Pre-identity reads would also
        # cache under the anonymous scope (the r9 #1 leak class).
        return
    prefs = run(prefs_sql.user_prefs(), page="Views", key="user_prefs", tier="live",
                source="USER_PREFS")
    if not prefs.ok:
        # r10 #1: commit-on-success — a transient failure retries next rerun
        # instead of silently skipping the saved landing for the session.
        tries = int(st.session_state.get("_ow_default_attempts", 0)) + 1
        st.session_state["_ow_default_attempts"] = tries
        if tries >= 3:
            st.session_state["_ow_default_applied"] = True
        return
    st.session_state["_ow_default_applied"] = True
    if prefs.empty:
        return
    tz_pref = next((str(r["PREF_VALUE"] or "") for _, r in prefs.df.iterrows()
                    if str(r["PREF_KEY"]) == "DISPLAY_TZ"), "")
    if tz_pref:
        st.session_state["_ow_display_tz"] = tz_pref
    # rec12: density persists across sessions now (was session-only) — hydrate it
    # here alongside the timezone.
    density_pref = next((str(r["PREF_VALUE"] or "") for _, r in prefs.df.iterrows()
                         if str(r["PREF_KEY"]) == "DENSITY"), "")
    if density_pref in ("compact", "comfortable"):
        st.session_state["_ow_density"] = density_pref
    # C19: hydrate the operator/audit presentation mode the same way density is.
    mode_pref = next((str(r["PREF_VALUE"] or "") for _, r in prefs.df.iterrows()
                      if str(r["PREF_KEY"]) == "PRESENT_MODE"), "")
    if mode_pref in ("operator", "audit"):
        st.session_state["_ow_present_mode"] = mode_pref
        # Seed the sidebar toggle's widget key so a late hydration flips it to
        # match the saved pref (review fix). This runs before _sidebar renders
        # the toggle; programmatic assignment never fires its on_change, so the
        # user's saved 'audit' can't be clobbered by a stale pre-hydrate False.
        st.session_state["_ow_present_mode_toggle"] = (mode_pref == "audit")
    raw = next((str(r["PREF_VALUE"] or "") for _, r in prefs.df.iterrows()
                if str(r["PREF_KEY"]) == "DEFAULT_VIEW"), "")
    data = _parse_view(raw)
    if data:
        st.session_state["_ow_nav_pending"] = {
            "page": str(data.get("page") or ""),
            "section": str(data.get("section") or ""),
            "filters": dict(data.get("filters") or {}),
        }
        consume_pending_navigation()  # pre-widget: applies immediately, no rerun


def _log_usage(page: str, render_ms: int | None = None) -> None:
    """Usage analytics (APP_USAGE). First paint per page logs RENDER_MS (the
    p95 the OPS_SLOW_RENDER sentinel checks); same-page reruns log a sampled
    (10%) EVENT_KIND='rerun' row with RENDER_MS NULL so interaction volume is
    measurable WITHOUT polluting the first-paint p95 (V027 rider; the scan
    gains an IS_RERUN filter with V028). Best-effort; degrades to the
    pre-V027 column shape, then off entirely."""
    if st.session_state.get("_ow_usage_off"):
        return
    is_rerun = st.session_state.get("_ow_last_logged") == page
    if is_rerun:
        import random as _random
        if _random.random() >= 0.10:
            return
        kind, ms = "rerun", "NULL"
    else:
        st.session_state["_ow_last_logged"] = page
        kind = "page_visit"
        ms = "NULL" if render_ms is None else str(max(0, min(int(render_ms), 600000)))
    # N12: enqueue into the shared write buffer (flushed once per rerun) instead
    # of a single-row INSERT round trip here. A V027-shape flush failure downgrades
    # to the old shape next call; an old-shape failure turns usage off.
    if not st.session_state.get("_ow_usage_oldshape"):
        _buffer_write(
            "INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_USAGE (PAGE, RENDER_MS, EVENT_KIND, IS_RERUN, USER_NAME) ",
            f"SELECT {sql_literal(str(page)[:80])}, {ms}, {sql_literal(kind)}, "
            f"{'TRUE' if is_rerun else 'FALSE'}, {identity_sql()}",
            off_flag="_ow_usage_off", downgrade_flag="_ow_usage_oldshape")
        return
    if is_rerun:
        return
    _buffer_write(
        "INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_USAGE (PAGE, RENDER_MS) ",
        f"SELECT {sql_literal(str(page)[:80])}, {ms}", off_flag="_ow_usage_off")


def _global_jump(pages: tuple) -> None:
    """Jump-to: pages, databases, warehouses, alert rules — one box."""
    from app.companies import ALFA_DATABASES, TREXIS_DATABASES, TREXIS_WAREHOUSES
    from app.logic.navigate import PAGE_SECTION_LABELS
    from app.logic.workbench import investigation_target

    options = [f"Page · {p}" for p in pages]
    # rec4: sections are jumpable too (request_navigation already accepts one) — the
    # box becomes the command palette it wants to be. Only sections on pages the
    # profile can actually see (pages is already profile-filtered).
    for _pg in pages:
        options.extend(f"Section · {_pg} → {_lbl}" for _lbl in PAGE_SECTION_LABELS.get(_pg, ()))
    options += [f"DB · {d}" for d in sorted(set(ALFA_DATABASES) | set(TREXIS_DATABASES))]
    # Live targets (SHOW WAREHOUSES + alert rules) load on demand: a normal
    # page paint pays ZERO queries for the jump box (Codex #3). Static pages,
    # databases, and the known Trexis warehouses are always offered; picking
    # the loader row fetches the full account list once per session.
    if bool(st.session_state.get("_ow_jump_loaded")):
        wh_names = list(TREXIS_WAREHOUSES)
        whs = run(security_sql.show_warehouses_sql(), page="Sidebar", key="jump_wh",
                  tier="metadata", source="SHOW WAREHOUSES", max_rows=0)
        if whs.ok and not whs.empty:
            wdf = whs.df.copy()
            wdf.columns = [str(c).lower() for c in wdf.columns]
            if "name" in wdf.columns:
                wh_names = sorted(set(wdf["name"].astype(str)))
        options += [f"WH · {w}" for w in wh_names]
        rules = run(mart_sql.alert_rules(), page="Sidebar", key="jump_rules", tier="recent",
                    source="ALERT_CONFIG")
        if rules.usable() and "RULE_ID" in rules.df.columns:
            options += [f"Rule · {r}" for r in sorted(rules.df["RULE_ID"].astype(str))]
    else:
        options += [f"WH · {w}" for w in TREXIS_WAREHOUSES]
    # C3: recents strip — the destinations this session jumped to, as one-click
    # buttons (the selectbox retains its last value, so re-picking the same
    # place is inert; a recent button always re-dispatches).
    # C3: after every jump the box remounts EMPTY under a bumped nonce key, so a
    # retained selection can never re-fire, AND a jump to the page you are
    # already on (request_navigation no-ops without a rerun) doesn't strand a
    # stale, un-re-jumpable selection — the explicit st.rerun() clears the box
    # even on that no-op path (review fix).
    _jump_nonce = int(st.session_state.get("_ow_jump_nonce", 0))

    def _go(dest: str) -> None:
        st.session_state["_ow_jump_nonce"] = _jump_nonce + 1
        _record_recent(dest)
        _dispatch_jump(dest, pages)   # reruns for a real navigation
        st.rerun()                    # covers the current-page no-op

    _recents = [r for r in (st.session_state.get("_ow_jump_recents") or []) if r in options]
    if _recents:
        st.caption("Recent")
        for _r in _recents[:4]:
            if st.button(_r, key=f"_ow_recent_{abs(hash(_r)) % 10**8}",
                         type="tertiary", width="stretch"):
                _go(_r)
    # C3: enter-to-go — picking a destination navigates immediately (no Open button).
    pick = st.selectbox("Jump to", options, index=None, placeholder="Jump to…",
                        key=f"_ow_jump_{_jump_nonce}", label_visibility="collapsed")
    if pick:
        _go(pick)
    # rec16: an explicit button — not a fake "More …" OPTION that mutated state
    # when "selected" (surprising, and invisible unless you opened the list). The
    # `and` short-circuits so the button only RENDERS while not yet loaded.
    if not st.session_state.get("_ow_jump_loaded") and st.button(
            "Load all warehouses & alert rules", key="_ow_jump_loadall",
            type="tertiary", width="stretch"):
        st.session_state["_ow_jump_loaded"] = True
        st.rerun()
    investigation_kinds = []
    if "Operations" in pages:
        investigation_kinds.append("Query ID")
    if "Alerts" in pages:
        investigation_kinds.append("Alert ID")
    if "Control Room" in pages:
        investigation_kinds.extend([
            "Action ID", "Incident ID", "Warehouse", "Database", "Object",
            "Task", "Query fingerprint", "User", "Role", "Data product",
        ])
    if investigation_kinds:
        # C3: the ID-lookup is folded into the same palette as a MODE (a type
        # selector + one field), enter-to-go — no separate Open button. Typing
        # an id and pressing Enter reruns; we dispatch when the value changes.
        with st.expander("Look up an ID / entity"):
            target_kind = st.selectbox(
                "Type", investigation_kinds, key="_ow_investigate_kind",
            )
            target_value = st.text_input(
                "ID or entity — press Enter to open", key="_ow_investigate_value",
                max_chars=500,
            )
            # review fix: dispatch on a change to the VALUE only, under the
            # CURRENT kind — folding the kind into the trigger meant merely
            # switching Type re-fired navigation against leftover text (the
            # selectbox blur re-commits the same value, so a kind change alone
            # no longer navigates). Re-looking-up the same value as a different
            # kind = clear and retype (the safe direction — no surprise jump).
            _iv = str(target_value or "").strip()
            if _iv and _iv != st.session_state.get("_ow_investigate_last"):
                st.session_state["_ow_investigate_last"] = _iv
                try:
                    target = investigation_target(target_kind, _iv)
                    if target.page not in pages:
                        st.warning("The active profile cannot open that investigation surface.")
                    else:
                        request_navigation(
                            target.page, target.section, context=target.context,
                        )
                except ValueError as exc:
                    st.warning(str(exc))
            elif not _iv:
                st.session_state.pop("_ow_investigate_last", None)


def _record_recent(label: str) -> None:
    """C3: keep the last few jump destinations for the recents strip (session
    only — no query, no persistence)."""
    recents = [r for r in (st.session_state.get("_ow_jump_recents") or []) if r != label]
    recents.insert(0, label)
    st.session_state["_ow_jump_recents"] = recents[:6]


def _dispatch_jump(pick: str, pages: tuple) -> None:
    """C3: resolve a 'Kind · name' jump selection to a navigation (shared by the
    selectbox and the recents buttons)."""
    from app.companies import ALFA_DATABASES, TREXIS_DATABASES
    kind, _, name = pick.partition(" · ")
    if kind == "Page":
        request_navigation(name)
    elif kind == "Section":
        _pg, _, _lbl = name.partition(" → ")
        request_navigation(_pg.strip(), _lbl.strip())
    elif kind == "DB":
        # r6-bug9: carry the DB's owning company. The jump list offers databases from BOTH
        # tenants, so picking a Trexis DB while the company filter sits at ALFA used to be
        # silently dropped by init_filters' company guard (DB classified Trexis != ALFA ->
        # reset to ALL databases). Derive the company from the SAME sets that built the
        # option so it always matches; fall back to no company if somehow unknown.
        _co = "Trexis" if name in TREXIS_DATABASES else ("ALFA" if name in ALFA_DATABASES else "")
        _dbf = {"company": _co, "database": name} if _co else {"database": name}
        request_navigation("Operations", "Queries", _dbf)
    elif kind == "WH":
        request_navigation("Operations", "Warehouses", {"warehouse_contains": name})
    elif kind == "Rule":
        request_navigation("Alerts", "Rules")


# r6-bug4: a sentinel that lets the health-status caller tell "not passed" (fetch it
# yourself) apart from None. None now means the health read ERRORED — distinct from {} (a
# healthy/undeployed account with no rows) — so a failed safety read never renders as a
# blank/green "all clear" bar while criticals are actually open.
_UNSET: object = object()


class _HealthUnavailable(Exception):
    """The health read FAILED. Raised out of the cache_data-wrapped parse so a
    failure is never pinned for the wrapper's TTL — the same all-or-nothing
    invariant core.query relies on (Streamlit does not cache exceptions)."""


@st.cache_data(ttl=120, show_spinner=False, max_entries=8)
def _health_values_cached(scope: str) -> dict[str, tuple[str, str]]:
    """P1: the strip renders on the SHELL of every page, so at the 30s live TTL
    every viewer re-paid it several times a minute for badges whose inputs move
    on a 10-minute (freshness snapshot) / daily (metering) cadence. 120s is the
    shell's own budget; the underlying run() stays on the live tier so the
    Brief/Control Room reads of the SAME statement keep sharing one cache entry,
    and the in-page Alerts panels are untouched — they are the live surface.

    ``scope`` is core.query's cache identity (role + refresh salt + the alerts
    domain salt), so Refresh and any ack/resolve write still invalidate this
    layer immediately rather than leaving a stale badge for 2 minutes."""
    # Perf: 'recent' (300s), not 'live' (30s) — health_strip reads FACT/freshness marts that
    # load hourly, so a 30s TTL re-paid the scan up to 120x/hour on every page shell. Ack/resolve
    # writes still invalidate this immediately via the domain salt, and backend alert scans ride
    # the hourly chain, so interactive freshness is unchanged and Refresh forces an instant re-read.
    res = run(mart_sql.health_strip(), page="Sidebar", key="health_strip", tier="recent",
              source="ALERT_EVENTS + SOURCE_FRESHNESS_STATE + FACT_METERING_DAILY")
    if not res.ok:
        raise _HealthUnavailable(res.error or "health strip read failed")
    if res.empty:
        return {}
    return {str(r["METRIC"]): (str(r["VALUE"]), str(r["STATE"])) for _, r in res.df.iterrows()}


def _health_values() -> dict[str, tuple[str, str]] | None:
    """Fetch and parse the health-strip mart once for the persistent pulse.

    Returns None on a read error and {} on a successful-but-empty read.
    """
    try:
        return _health_values_cached(cache_scope(mart_sql.health_strip()))
    except _HealthUnavailable:
        return None


def _persistent_status_bar(pages: tuple[str, ...], vals: object = _UNSET) -> None:
    """The one global pulse: four routed signals, rendered once per page."""
    from app.logic.formulas import (
        blended_billed_usd,
        credits_to_usd,
        format_usd,
        humanize_duration,
        safe_float,
    )
    from app.ui.components import load_settings, status_bar

    def _target(page: str, section: str) -> tuple[str, str] | None:
        # Never offer a control that the viewer's profile cannot open.
        return (page, section) if page in pages else None

    vals = _health_values() if vals is _UNSET else vals
    if vals is None:
        status_bar([{
            "k": "Health",
            "v": "unavailable",
            "icon": "alerts",
            "sev": "warn",
            "target": _target("Admin", "Errors & telemetry"),
        }])
        return
    if not vals:
        return

    crit, crit_state = vals.get("OPEN_CRITICAL", ("0", "OK"))
    undelivered, undelivered_state = vals.get("UNDELIVERED_CRITICAL", ("0", "OK"))
    stale, stale_state = vals.get("STALEST_SOURCE_H", ("-1", "MUTED"))
    stale_name = vals.get("STALEST_SOURCE_NAME", ("", ""))[0]
    # The -1 never-loaded sentinel arrives as the scale-1 string "-1.0", so compare
    # NUMERICALLY (a literal "-1" match never fired, showing "-1h"); a real age is
    # >= 0, and a negative/unparseable value falls through to the "never loaded"
    # (stale_state == "BAD") branch below.
    stale_age = humanize_duration(stale, "h") if safe_float(stale, -1.0) >= 0 else ""
    mtd, _ = vals.get("MTD_CREDITS", ("", ""))
    _sev = {"BAD": "bad", "WARN": "warn", "OK": "ok", "INFO": "info", "MUTED": ""}
    stats = [
        {"k": "Open criticals", "v": crit, "icon": "alerts",
         "sev": "bad" if crit_state == "BAD" or crit not in ("0", "") else "ok",
         "target": _target("Alerts", "Open events")},
        {"k": "Undelivered criticals", "v": undelivered, "icon": "bolt",
         "sev": "bad" if undelivered_state == "BAD" or undelivered not in ("0", "") else "ok",
         "target": _target("Alerts", "Native delivery")},
        {"k": "Telemetry age",
         "v": (f"{stale_name} · {stale_age}" if stale_age and stale_name
               else stale_age if stale_age
               else "never loaded" if stale_state == "BAD" else "n/a"),
         "icon": "clock", "sev": _sev.get(stale_state, ""),
         "target": _target("Control Room", "Freshness & replay")},
    ]
    if mtd:
        settings = load_settings("Status bar")
        rate = safe_float(settings.get("CREDIT_PRICE_USD"), 3.68)
        ai_rate = safe_float(settings.get("AI_CREDIT_PRICE_USD"), 2.20)
        credits = safe_float(mtd)
        other = vals.get("MTD_CREDITS_OTHER", ("", ""))[0]
        ai = vals.get("MTD_CREDITS_AI", ("", ""))[0]
        usd = (
            blended_billed_usd(safe_float(other), safe_float(ai), rate, ai_rate)
            if other or ai
            else credits_to_usd(credits, rate)
        )
        stats.append({
            "k": "MTD credit spend",
            "v": f"{format_usd(usd)} · {credits:,.0f} cr",
            "icon": "cost",
            "sev": "info",
            "target": _target("Cost & Contract", "Spend & Attribution"),
        })
    status_bar(stats)


def _reset_scope() -> None:
    """One-click return to the account-wide default scope (v4.39)."""
    from app.core.state import FILTER_DEFAULTS
    for key, default in FILTER_DEFAULTS.items():
        st.session_state[key] = default


def _active_filter_count() -> int:
    """How many non-default filters are live — drives the strip's border glow, the
    Reset button, and the 'N active' readout (rec25). (v4.65: replaced the scope-chip
    summary; the controls themselves show the scope, and a live warehouse/user/schema
    filter auto-opens the 'More filters' row below so it can never hide.)"""
    from app.core.state import FILTER_DEFAULTS
    count = 0
    for key in ("flt_company", "flt_days"):
        if st.session_state.get(key) != FILTER_DEFAULTS[key]:
            count += 1
    for key in ("flt_database", "flt_warehouse_contains", "flt_user_contains", "flt_schema_contains"):
        if str(st.session_state.get(key) or "").strip():
            count += 1
    return count


def _topbar_scope() -> None:
    """One-row operator scope strip: primary filters + a compact More popover.

    v4.157.0: dropped the Legend and "Views & display" popovers (owner: nobody
    uses them once the app is in daily use). Saved default views / density /
    display-timezone still hydrate at startup from USER_PREFS (see
    _apply_default_landing) — only the in-strip editors are gone. More (the
    warehouse/user/schema contains-filters) stays.
    """
    active_n = _active_filter_count()
    active = active_n > 0
    box = st.container(border=True, key="ow_triage_toolbar")
    with box:
        c_scope, c_company, c_days, c_db, c_more, c_reset = st.columns(
            [0.82, 0.95, 1.08, 1.55, 0.78, 0.58],
            gap="small",
        )
        with c_scope:
            marker = '<span class="ow-scope-active"></span>' if active else ""
            active_text = f"{active_n} active" if active_n else "Account view"
            # Single-line label ("Scope · Account view") — a two-line stack clipped its
            # top line inside the fixed-height toolbar row on SiS; one line matches the
            # selectbox height and can't clip.
            st.markdown(
                f'{marker}<div class="ow-triage-title">'
                f'<span class="ow-triage-label">Scope</span>'
                f'<span class="ow-triage-sub">{active_text}</span></div>',
                unsafe_allow_html=True,
            )
        with c_company:
            st.selectbox(
                "Company", COMPANIES, key="flt_company",
                label_visibility="collapsed", help="Company scope",
            )
        with c_days:
            st.selectbox(
                "Date range", options=list(TRIAGE_WINDOW_OPTIONS), key="flt_days",
                format_func=window_option_label, label_visibility="collapsed",
                help="Analysis window",
            )
        with c_db:
            _company = st.session_state.get("flt_company", COMPANIES[0])
            _opts = ()
            _inv = run(
                security_sql.show_databases_sql(), page="Sidebar", key="db_inventory",
                tier="metadata", source="SHOW DATABASES", max_rows=0,
            )
            if _inv.ok and not _inv.empty:
                _idf = _inv.df.copy()
                _idf.columns = [str(c).lower() for c in _idf.columns]
                if "name" in _idf.columns:
                    _opts = classify_databases(_idf["name"].astype(str).tolist(), _company)
            if not _opts:
                _opts = databases_for(_company)
            db_options = ["", *_opts]
            if st.session_state.get("flt_database") not in db_options:
                st.session_state["flt_database"] = ""
            st.selectbox(
                "Database", db_options, key="flt_database",
                format_func=lambda value: value or "All databases",
                label_visibility="collapsed",
                help="Database scope where the selected panel supports it.",
            )
        with c_more:
            _adv_n = sum(
                1 for key in (
                    "flt_warehouse_contains", "flt_user_contains", "flt_schema_contains"
                )
                if str(st.session_state.get(key) or "").strip()
            )
            _more_label = f"More · {_adv_n}" if _adv_n else "More"
            with st.popover(_more_label, width="stretch"):
                st.caption("Contains filters")
                st.text_input("Warehouse contains", key="flt_warehouse_contains")
                st.text_input("User contains", key="flt_user_contains")
                st.text_input(
                    "Schema contains", key="flt_schema_contains",
                    help="Case-insensitive match where the source has schema grain.",
                )
        with c_reset:
            if active:
                st.button(
                    "Reset", key="flt_reset", on_click=_reset_scope,
                    help="Back to the account-wide default scope.",
                    width="stretch",
                )
        _window = st.session_state.get("flt_days", DEFAULT_DAY_WINDOW)
        _days = resolve_window_days(_window)
        if _days > MAX_LIVE_WINDOW_DAYS:
            st.caption(
                f"{window_scope_label(_window)} applies to mart history; live Operations "
                f"and Security scans cap at {MAX_LIVE_WINDOW_DAYS}d."
            )


def main() -> None:
    _main_started = time.perf_counter()  # full render incl. chrome (Codex #18)
    # C48: full-script run counter — the write latch's run-sequence check rides
    # this (a queued duplicate click always lands on the very next run, however
    # long its cold-cache render takes; fragment reruns don't advance it).
    st.session_state["_ow_run_seq"] = int(st.session_state.get("_ow_run_seq", 0) or 0) + 1
    # N12: drain any telemetry/usage rows a PRIOR rerun buffered but couldn't
    # flush because st.rerun() unwound past its end-of-render flush (session_state
    # is the durability seam). Cheap no-op when the buffer is empty.
    flush_write_buffer()
    consume_pending_navigation()
    inject_theme()
    if "_ow_refreshed_at" not in st.session_state:
        mark_refreshed()
    init_filters()

    connected = connection_available()
    role = current_role() if connected else ""   # hydrates role+user scope keys
    # Codex r9 #1 (real): the USER_PREFS read used to run BEFORE identity
    # hydrated, so it cached under the anonymous scope — and since the SQL
    # text is identical across users, one user's prefs frame could serve
    # another in-process. Identity first, THEN the first cached read.
    _apply_default_landing()
    # Page visibility keys on the VIEWER (st.user), NOT current_role() — under
    # owner's-rights SiS the role is the app owner's for every viewer, so a
    # role-based profile would show every viewer the owner's DBA pages. See
    # session.active_profile(): admins -> DBA, ETL/unmapped -> read-only READER.
    profile = active_profile(role)
    pages = PAGES_BY_PROFILE.get(profile, PAGES_BY_PROFILE["ANALYST"])

    page = _sidebar(pages, role, profile, connected)
    if connected:
        _topbar_scope()

    if not connected:
        st.title("OVERWATCH")
        st.error("No Snowflake connection.")
        st.markdown(
            "- **Streamlit-in-Snowflake:** the session is injected automatically — if you see this "
            "in SiS, the app's owner role lost access.\n"
            "- **Local dev:** add `[connections.snowflake]` to `.streamlit/secrets.toml` "
            "(see DEPLOYMENT.md).\n"
        )
        from app.core.session import connection_error
        reason = connection_error()
        if reason:
            with st.expander("Connection error detail"):
                st.code(reason)
        if st.button("Retry connection"):
            st.cache_resource.clear()
            st.session_state.pop("_ow_current_role", None)
            st.rerun()
        return

    # rec: the persistent status strip is orientation for the two morning
    # surfaces only. Brief IS the compact status view (renders these signals as
    # its body), and Overview keeps the strip; every drill/govern page below
    # them is task-focused and does not repeat it (owner 2026-08-14).
    # Explicit last-line authorization deny (belt-and-suspenders over _sidebar's
    # clamp). Under owner's-rights SiS page visibility is the SOLE boundary — a
    # page outside the viewer's set must NEVER reach _RENDERERS, whatever produced
    # `page` (stale session_state, a saved deep-link, a future nav path).
    if page not in pages:
        page = pages[0]
    if page == "Overview":
        _persistent_status_bar(pages)
    _RENDERERS[page]()
    # RENDER_MS now spans sidebar/topbar/status chrome too, not just the page
    # body — chrome overhead was invisible in APP_USAGE (Codex #18).
    _log_usage(page, int((time.perf_counter() - _main_started) * 1000))
    # N12: flush the render's buffered telemetry/usage rows as one INSERT per
    # table+shape (this render enqueued page reads' telemetry + the usage row).
    flush_write_buffer()


if __name__ == "__main__":
    main()
