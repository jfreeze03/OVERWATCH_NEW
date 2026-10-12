"""v4.612.0: the Cortex Code models section's honesty rules, rendered with fakes (the
tests/test_probe_read_honesty.py pattern), plus its wiring on Cost > Chargeback & AI.

A probe read logs neither an absent object nor a missing column, so the panel is the only place a failure
shows: each kind renders its own state, a capped read never claims "no usage", and the AI users hint finally
matches the key its live scan runs under (toggle_cost_hint('cortex_users') never matched cortex_user_daily_*)."""

from __future__ import annotations

import re
from datetime import date
from types import SimpleNamespace

import pandas as pd
import pytest

import app.logic.cortex as cortex_logic
from tests._source import read


def _res(df: pd.DataFrame | None = None, *, kind: str = "", truncated: bool = False):
    df = pd.DataFrame() if df is None else df
    return SimpleNamespace(ok=not kind, empty=df.empty, df=df, error=f"boom ({kind})" if kind else "",
                           error_kind=kind, truncated=truncated, source="stub", usable=lambda: not df.empty)


def _render(monkeypatch, result, *, days: int = 30, bounds=None):
    from app.ui.pages.cost_parts import coco_models as cm
    seen: dict = {"empty": [], "detail": [], "captions": [], "kpis": 0, "runs": []}

    def fake_run(sql, **kw):
        seen["runs"].append(kw)
        return result

    monkeypatch.setattr(cm, "run", fake_run)
    monkeypatch.setattr(cm, "empty_state", lambda kind, msg, *_a, **k: (
        seen["empty"].append((kind, msg)), seen["detail"].append(k.get("detail"))))
    monkeypatch.setattr(cm, "result_caption", lambda *_a, **_k: None)
    monkeypatch.setattr(cm, "kpi_row", lambda *_a, **_k: seen.__setitem__("kpis", seen["kpis"] + 1))
    monkeypatch.setattr(cm.st, "caption", lambda text, *_a, **_k: seen["captions"].append(str(text)))
    cm.coco_models_section("ALFA", days, 2.20, bounds=bounds)
    return seen


def test_the_read_is_one_cached_probe_per_company(monkeypatch):
    seen = _render(monkeypatch, _res(kind="other"))
    (kw,) = seen["runs"]
    assert kw["key"] == "coco_models_ALFA" and kw["tier"] == "historical" and kw["probe"] is True
    assert kw["max_rows"] == 200_000 and "SNOWFLAKE_COCO_USAGE_HISTORY" in kw["source"]
    assert "ACCOUNT_USAGE" not in kw["source"]


@pytest.mark.parametrize("kind", ["unknown_function", "absent", "privilege"])
def test_absence_is_a_setup_state(monkeypatch, kind):
    seen = _render(monkeypatch, _res(kind=kind))
    ((state, msg),) = seen["empty"]
    assert state == "needs_setup", kind
    assert ("002139" in msg) if kind == "unknown_function" else ("roles.sql" in msg)
    assert "Retry" not in msg and not seen["kpis"]


def test_a_missing_column_is_drift_and_names_no_error_log(monkeypatch):
    seen = _render(monkeypatch, _res(kind="missing_column"))
    ((state, msg),) = seen["empty"]
    assert state == "unavailable" and "schema drift" in msg
    assert "error log" not in msg.lower()                 # a probe read does not log a missing column
    assert seen["detail"] == ["boom (missing_column)"] and not seen["kpis"]


@pytest.mark.parametrize("kind", ["timeout", "other"])
def test_other_failures_are_unavailable_with_the_error(monkeypatch, kind):
    seen = _render(monkeypatch, _res(kind=kind))
    ((state, msg),) = seen["empty"]
    assert state == "unavailable" and "failed this run" in msg
    assert seen["detail"] == [f"boom ({kind})"] and not seen["kpis"]


def test_no_rows_is_a_quiet_no_data_state_never_clean(monkeypatch):
    seen = _render(monkeypatch, _res(pd.DataFrame()))
    ((state, msg),) = seen["empty"]
    assert state == "no_data_yet" and "365 days" in msg and "Desktop" in msg


def _rows(days):
    from tests.test_coco_models_logic import _row
    return pd.DataFrame([_row(d, "A", "m", req_cr=1.0, cr=1.0) for d in days])


def test_an_empty_window_says_so(monkeypatch):
    monkeypatch.setattr(cortex_logic, "account_today", lambda: date(2026, 8, 20))
    seen = _render(monkeypatch, _res(_rows([date(2026, 1, 5)])), days=7)
    ((state, msg),) = seen["empty"]
    assert state == "no_data_yet" and "the last 7 days" in msg


def test_a_capped_read_never_claims_no_usage(monkeypatch):
    """The read is ordered oldest day first, so the row cap drops the NEWEST days: an empty window under a cap
    is unknown (unavailable), and the caption names the last day read."""
    monkeypatch.setattr(cortex_logic, "account_today", lambda: date(2026, 8, 20))
    seen = _render(monkeypatch, _res(_rows([date(2026, 1, 5)]), truncated=True), days=7)
    ((state, msg),) = seen["empty"]
    assert state == "unavailable" and "cap" in msg
    assert any("nothing after Jan 5, 2026 was read" in c for c in seen["captions"])


# ---- wiring on Cost > Chargeback & AI -----------------------------------------------------------------------

def test_section_sits_after_ai_users_behind_its_own_toggle():
    cost = read("app/ui/pages/cost.py")
    block = cost.split('elif section == "Chargeback & AI":', 1)[1].split("elif section ==", 1)[0]
    i_ai = block.index('section_header("AI users", "", "operations", anchor="cost-ai-users")')
    i_models = block.index('section_header("Cortex Code models", "", "cost", anchor="cost-coco-models")')
    assert i_ai < i_models
    tail = block[i_models:]
    assert 'key="coco_models_scan"' in tail
    assert 'coco_models_section(f["company"], f["days"], ai_rate, bounds=f["bounds"])' in tail
    # a sibling of AI users: never inside _ai_users_tab, whose early returns would hide it
    assert "coco_models" not in read("app/ui/pages/cost_parts/ai_chargeback.py")


def test_toggle_hints_match_the_keys_their_reads_run_under():
    cost = read("app/ui/pages/cost.py")
    hints = re.findall(r'toggle_cost_hint\("([^"]+)"\)', cost)
    assert "cortex_users" not in hints                         # matched only the fact fallback's key
    live_key = re.search(r'key=f"(cortex_user_daily)_\{company\}"', read("app/ui/pages/cost_parts/ai_chargeback.py"))
    assert live_key and live_key.group(1) in hints
    coco_key = re.search(r'key=f"(coco_models_)\{company\}"', read("app/ui/pages/cost_parts/coco_models.py"))
    assert coco_key and coco_key.group(1) in hints


def test_dollars_only_through_the_formula_at_the_ai_rate():
    src = read("app/ui/pages/cost_parts/coco_models.py")
    assert "credits_to_usd(" in src and "* ai_rate" not in src and "ai_rate *" not in src
    assert "ACCOUNT_USAGE" not in src                          # live-scan budget 0; reach pinned in test_v451_trust
    for banned in ("st.info(", "st.success(", "st.warning(", "execute_statement", "st.button("):
        assert banned not in src, banned


def test_the_interface_filter_widens_back_to_all(monkeypatch):
    """Review r1: 'all' narrowed by a window with no Desktop must widen again when Desktop is back, while a
    strict subset the viewer chose survives the change (the reproduced AppTest case: ['CLI'] stuck)."""
    from app.ui.pages.cost_parts import coco_models as cm

    state: dict = {}
    fake_st = SimpleNamespace(session_state=state,
                              multiselect=lambda _label, _options, key, **_k: state[key])
    monkeypatch.setattr(cm, "st", fake_st)

    def pick(*sources):
        return cm._interfaces(pd.DataFrame({"SOURCE": list(sources)}))

    assert pick("CLI", "Desktop", "Snowsight") == ["CLI", "Desktop", "Snowsight"]     # first paint: all
    assert pick("CLI", "Snowsight") == ["CLI", "Snowsight"]                           # 7d: no Desktop
    assert pick("CLI", "Desktop", "Snowsight") == ["CLI", "Desktop", "Snowsight"]     # back: Desktop rejoins
    state[cm._IFACE_KEY] = ["CLI"]                                                    # the viewer narrows
    assert pick("CLI", "Desktop", "Snowsight") == ["CLI"]                             # same options: kept
    assert pick("CLI", "Snowsight") == ["CLI"]                                        # a strict pick survives
    assert pick("CLI", "Desktop", "Snowsight") == ["CLI"]
    assert pick("Desktop") == ["Desktop"]                                             # nothing left: all
