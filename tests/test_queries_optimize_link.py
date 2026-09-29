"""Next-Fifty #28 close-out (KEEP + LINK): Operations ▸ Queries' "Optimization opportunities" board
links a selected family to Operations ▸ Optimize, and Optimize lands honestly.

Source locks + the pure resolver (all run on the floor-compat leg); the end-to-end click-through AppTests
live in test_pages_shaped.py. The link is a momentary BUTTON in the detail pane (a row click stays the
in-place QOP breakdown; a button never re-fires from st.dataframe's sticky selection), it carries a
one-shot live_profile so the landing diagnosis matches the breakdown just read, and a deep link is
consumed exactly once whether or not the queue lists the family."""

from __future__ import annotations

import re

from tests._source import ROOT, read

_OPS = "app/ui/pages/operations.py"
_OPT = "app/ui/pages/ops_parts/optimize_queue.py"


def _board() -> str:
    src = read(_OPS)
    return src.split('section_header("Optimization opportunities"', 1)[1].split(
        'section_header("Operator profile"', 1)[0]


def test_queries_board_links_the_selected_row_to_optimize() -> None:
    block = _board()
    detail = block.split("if _sel is not None and 0 <= int(_sel) < len(_disp):", 1)[1]
    button = 'st.button("Open in Optimize →", key=f"ops_qopp_open_opt:{_fp[:16]}"'
    assert button in detail
    assert detail.index(button) < detail.index('request_navigation("Operations", "Optimize",')
    assert block.count("request_navigation(") == 1            # only behind the button, never the selection
    assert "selectable_table(" in block                        # the row click stays the in-place breakdown
    assert "entity_nav_table(" not in block and "selectable_nav_table(" not in block
    assert "Operations ▸ Optimize" in block and "OOS ranks; it is not dollars." in block
    assert block.count("ACCOUNT_USAGE") == 1                   # only the existing q_opp source label
    assert len(re.findall(r"\brun\(", block)) == 1             # only the existing q_opp read


def test_link_context_keys_match_what_optimize_reads() -> None:
    ops, opt = read(_OPS), read(_OPT)
    assert 'context={"fingerprint": _fp, "live_profile": True}' in ops
    assert 'navigation_context().get("fingerprint")' in opt
    assert '_arrival.get("live_profile")' in opt
    seed = opt.index('st.session_state["ops_opt_live"] = True')
    assert seed < opt.index('_live_on = bool(st.session_state.get("ops_opt_live", False))')
    # the carry pops live_profile (one-shot) and runs AFTER the queue guard, like the fingerprint pop
    assert opt.index('if not guard(result, "No measured recurring-query cost exists in this scope."):') < seed
    assert '{k: v for k, v in _arrival.items() if k != "live_profile"}' in opt
    from app.logic.navigate import PAGE_SECTION_KEYS, PAGE_SECTION_LABELS
    assert "Optimize" in PAGE_SECTION_LABELS["Operations"]
    assert PAGE_SECTION_KEYS["Operations"] == "ops_section"


def test_both_sides_key_on_query_parameterized_hash() -> None:
    from app.data import ops_sql, workbench_sql
    assert "QUERY_PARAMETERIZED_HASH AS FINGERPRINT" in ops_sql.query_opportunity_fingerprints(7)
    assert "c.QUERY_HASH AS FINGERPRINT" in workbench_sql.optimize_queue(30)
    # the Optimize mart's QUERY_HASH comes from the NEWEST SP_LOAD_PATTERN_COST definition (never pinned)
    defs = [p for p in (ROOT / "snowflake" / "migrations").glob("V[0-9]*__*.sql")
            if "PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_LOAD_PATTERN_COST(" in p.read_text(encoding="utf-8")]
    assert defs, "no migration defines SP_LOAD_PATTERN_COST"
    newest = max(defs, key=lambda p: int(re.match(r"V(\d+)__", p.name).group(1)))
    assert "QUERY_PARAMETERIZED_HASH AS QUERY_HASH" in newest.read_text(encoding="utf-8"), newest.name


def test_resolve_deep_link_is_case_insensitive_and_blank_safe() -> None:
    from app.logic.fix_queue import resolve_deep_link
    assert resolve_deep_link("ABC", ["x", "abc"]) == "abc"            # the queue's own spelling
    assert resolve_deep_link(" abc ", ["ABC"]) == "ABC"
    assert resolve_deep_link("", ["abc"]) == ""
    assert resolve_deep_link(None, ["abc"]) == ""
    assert resolve_deep_link(float("nan"), ["nan", "abc"]) == ""
    assert resolve_deep_link("zz", ["abc", float("nan"), None]) == ""
    assert resolve_deep_link("1.0", [1.0, 2.0]) == "1.0"             # a float id matches its str form
    assert resolve_deep_link("abc", []) == ""


def test_unqueued_link_is_consumed_and_explained() -> None:
    opt = read(_OPT)
    link = opt.split("_ctx_fp = ", 1)[1].split("def _list(", 1)[0]
    assert "_preselect = resolve_deep_link(_ctx_fp, portfolio[\"FINGERPRINT\"])" in link
    assert link.index("if _ctx_fp:") < link.index('if k != "fingerprint"}')    # consumed found-or-not
    assert "if _preselect:" not in link                                         # the old found-only gate
    assert "is not in this fix queue" in link and "Operations ▸ Queries" in link
    assert "ACCOUNT_USAGE" not in opt
    # an unqueued link clears a previously selected family so the empty pane can say so
    assert '"_ow_md_sel_ops_optimize"' in link
    assert "empty_detail_msg=_missing or" in opt
    # the anchor the write-polish lock splits on still follows the Track-all block
    assert opt.index("# Track all ACT NOW:") < opt.index("_ctx_fp =")
    # master_detail's call prefix is unchanged (tests/test_optimize_queue.py)
    assert 'master_detail(\n        portfolio, key="ops_optimize", id_col="FINGERPRINT",' in opt


def test_an_empty_queue_consumes_and_explains_the_link() -> None:
    """Review F23: an EMPTY queue (the read succeeded, no family has attributed credits in this Company and
    Window) returned at the guard before the link was used, so the fingerprint AND the one-shot live_profile
    lingered — and switched on an unasked-for live scan after a later Company/Window change. Split empty
    from failed before the guard: empty consumes both keys and says why; a FAILED read still keeps them."""
    opt = read(_OPT)
    guard_at = opt.index('if not guard(result, "No measured recurring-query cost exists in this scope."):')
    split = opt.index("if result.ok and result.empty:")
    assert split < guard_at
    block = opt[split:guard_at]
    assert "if result.ok and result.empty:" in block and "not result.ok" not in block    # ok-empty only
    assert '{k: v for k, v in _arr.items()\n' in block
    assert 'if k not in ("fingerprint", "live_profile")}' in block                     # both keys, one pop
    assert '"ops_opt_live"' not in block                                               # never switched on
    guarded = opt[guard_at:].split("\n    # W12:", 1)[0]
    assert "if _empty_link_fp:" in guarded and guarded.rstrip().endswith("return")
    assert 'empty_state("no_data_yet", f"Query family {_short_fp(_empty_link_fp)} is not in this fix queue: no "' \
        in guarded
    assert '"query family has attributed warehouse credits in the daily marts for this Company "' in guarded
    assert "Operations ▸ Queries" in guarded
    # the in-queue notice and the empty-queue notice shorten the fingerprint the same way
    from app.ui.pages.ops_parts.optimize_queue import _short_fp
    assert _short_fp("ABCDEF0123456789") == "ABCDEF012345…" and _short_fp("SHORT") == "SHORT"
    assert opt.count("_short_fp(") == 3                                                # def + the two notices


def test_the_link_does_not_prefetch_or_read_on_first_paint() -> None:
    block = _board()
    toggle = block.index('key="ops_qopp_toggle"')
    assert toggle < block.index("if _qopp_on:") < block.index('st.button("Open in Optimize →"')
