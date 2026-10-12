"""v4.607 cleanup follow-ups: user-facing wording that had drifted from the code it describes.

Each lock reads the shown text from the page source (adjacent string literals merge in the AST, so a
caption split across lines is checked whole) and, where it is cheap, ties the claim to the code or the
migration that makes it true, so the wording cannot drift back.
"""

from __future__ import annotations

import ast
import re

import pandas as pd

from app.data import ops_sql
from app.logic import scoring
from tests._source import ROOT, read


def _literal_text(node: ast.AST) -> str:
    """The literal text of a caption argument: a string, an f-string's literal parts, or a `+` chain
    of those (a computed piece reads as '{}')."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(_literal_text(v) if isinstance(v, ast.Constant) else "{}" for v in node.values)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _literal_text(node.left) + _literal_text(node.right)
    return "{}"


def _captions(rel: str) -> list[str]:
    """Every st.caption(...) text in a module (implicit concatenation already merged)."""
    return [_literal_text(node.args[0]) for node in ast.walk(ast.parse(read(rel)))
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr == "caption" and node.args]


def _one(captions: list[str], needle: str) -> str:
    hits = [c for c in captions if needle in c]
    assert len(hits) == 1, (needle, hits)
    return hits[0]


def test_overview_retro_caption_states_the_budget_basis_gap() -> None:
    # R1-108: the retro budget penalty is cumulative MTD, the live one is projected month-end, so retro
    # can sit up to the Budget-pace cap higher — not just "a few points".
    cap = int(re.search(r'ScoreDriver\("Budget pace", _cap\(_raw, (\d+)\)', read("app/logic/scoring.py")).group(1))
    text = _one(_captions("app/ui/pages/overview.py"), "Live-score weights replayed")
    assert "cumulative month-to-date spend" in text and "projected month-end" in text
    assert f"up to {cap} points higher" in text
    assert "judge the trend, not the level" in text
    # #50: with a budget set the leading partial month is left out, so the old "read the first few days
    # as unreliable" warning described an artifact score_history no longer produces.
    assert "left out" in text and "first few days as unreliable" not in text


def test_score_history_leaves_out_the_leading_partial_month_the_caption_names() -> None:
    days = pd.date_range("2026-08-13", "2026-09-11", freq="D")
    frame = pd.DataFrame({"DAY": days, "CREDITS_BILLED": 10.0, "QUERY_COUNT": 100, "TASK_RUNS": 10})
    with_budget = scoring.score_history(frame, monthly_budget_usd=1000.0)
    assert str(with_budget["DAY"].iloc[0]) == "2026-09-01"
    assert len(scoring.score_history(frame)) == len(days)   # no budget: nothing to correct, all kept


def test_control_room_caption_does_not_promise_a_reopen() -> None:
    # R1-343: nothing in the app writes INCIDENTS.REOPENED_FROM, so the caption no longer says a reopen
    # creates a linked incident.
    text = _one(_captions("app/ui/pages/control_room.py"), "forward-only")
    assert "no reopen: a recurrence is a new incident" in text
    assert "REOPENED_FROM" not in text


def test_freshness_source_label_names_the_loader_stamps() -> None:
    # R1-338: V041 retired V040's 10-minute snapshot task; each loader stamps its own row since.
    label = '"SOURCE_FRESHNESS_STATE (stamped by each loader)"'
    for page in ("app/ui/pages/control_room.py", "app/ui/pages/admin.py"):
        src = read(page)
        assert label in src, page
        assert "(10-min snapshot)" not in src, page
    migs = sorted((ROOT / "snowflake" / "migrations").glob("V[0-9]*__*.sql"))
    v041 = next(p for p in migs if p.name.startswith("V041__")).read_text(encoding="utf-8")
    assert "DROP TASK IF EXISTS DBA_MAINT_DB.OVERWATCH.TASK_SNAPSHOT_FRESHNESS;" in v041
    recreate = re.compile(r"CREATE\s+(?:OR\s+REPLACE\s+)?TASK\s+(?:IF\s+NOT\s+EXISTS\s+)?"
                          r"DBA_MAINT_DB\.OVERWATCH\.TASK_SNAPSHOT_FRESHNESS\b", re.I)
    assert not [p.name for p in migs
                if int(p.name[1:4]) > 41 and recreate.search(p.read_text(encoding="utf-8"))]


def test_non_operator_captions_name_the_real_gate() -> None:
    # R1-325: every viewer who can open the app already holds SNOW_ACCOUNTADMINS / SNOW_SYSADMINS, so
    # "requires SNOW_ACCOUNTADMINS / SNOW_SYSADMINS" explained nothing; the in-app gate is a direct
    # SNOW_PRI_GFR_PRD_ALFA_DSA grant (the only admin route since v4.611.0).
    admin = _captions("app/ui/pages/admin.py")
    alerts = _captions("app/ui/pages/alerts.py")
    # v4.610.0 review r1: the Admin settings caption names who can act now too (config.ADMIN_ACCESS_HINT, read here
    # as '{}'); a SNOW_* role alone grants nothing in-app
    assert _one(admin, "Anyone can copy the SQL for review.") == (
        "Saving a setting is an in-app change. {} Anyone can copy the SQL for review.")
    assert not [c for c in admin if "OPERATOR_USERS" in c]
    # v4.610.0: the Alerts captions read config.ADMIN_ACCESS_HINT (direct SNOW_PRI_GFR_PRD_ALFA_DSA members),
    # which _literal_text reads as '{}'; tests/test_alert_rule_edit.py locks that they reference it
    assert _one(alerts, "The SQL is copyable for review").startswith("{}")
    assert _one(alerts, "Waking snoozed events early is an in-app change.").endswith("{}")
    assert not [c for c in alerts if "OPERATOR_USERS" in c]
    for text in admin + alerts:
        assert not text.startswith(("Executing requires SNOW_", "Un-snoozing requires SNOW_")), text


def test_suspended_notifier_note_matches_v071() -> None:
    # R1-351: since V071 the migrations resume TASK_ALERT_NOTIFY with the hourly tree, so a suspended
    # notifier was suspended after deploy, not "until a delivery integration exists".
    note = ops_sql.SUSPENDED_OK_OVERWATCH_TASKS["TASK_ALERT_NOTIFY"]
    assert "since V071" in note and "delivery integration" in note
    assert "suspended until" not in note
    v071 = next((ROOT / "snowflake" / "migrations").glob("V071__*.sql")).read_text(encoding="utf-8")
    assert "ALTER TASK IF EXISTS DBA_MAINT_DB.OVERWATCH.TASK_ALERT_NOTIFY RESUME;" in v071
