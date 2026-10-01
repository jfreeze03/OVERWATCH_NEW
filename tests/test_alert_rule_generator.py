"""Review R1-233: the Alerts > Rules threshold generator never writes a threshold nobody chose.

The generator's "New threshold" box defaulted to 0.0 (no value=) and its UPDATE always wrote
`SET THRESHOLD_NUM = <box>, ENABLED = ...`, so an operator using it only to toggle Enabled generated
THRESHOLD_NUM = 0.0. Most scan arms trust THRESHOLD_NUM unfloored (e.g. V163 [29] SEC_TRUST_REGRESSION tests
CUR_N - PRIOR_N >= COALESCE(THRESHOLD_NUM, 1)), so 0 raised every unchanged at-risk count as a 'regression'
daily. Now both inputs seed from the picked rule and the UPDATE writes only the columns that change.
"""

from __future__ import annotations

import pandas as pd
import pytest

from tests._source import read

_RULES = pd.DataFrame({"RULE_ID": ["COST_X", "SEC_TRUST_REGRESSION"], "FAMILY": ["COST", "SECURITY"],
                       "NAME": ["x", "trust"], "ENABLED": [True, True], "SEVERITY": ["HIGH", "HIGH"],
                       "THRESHOLD_NUM": [30.0, 1.0], "WINDOW_HOURS": [24, 24], "OWNER": ["", ""],
                       "CHANNEL": ["", ""], "UPDATED_AT": [pd.Timestamp("2026-09-01")] * 2})


def test_toggling_enabled_never_rewrites_the_threshold() -> None:
    from app.ui.pages.alerts import _rule_change_sql
    sql = _rule_change_sql("SEC_TRUST_REGRESSION", 1.0, True, 1.0, False)
    assert "THRESHOLD_NUM" not in sql
    assert "SET ENABLED = FALSE, UPDATED_AT = CURRENT_TIMESTAMP()" in sql
    assert "WHERE RULE_ID = 'SEC_TRUST_REGRESSION';" in sql and "ALERT_CONFIG" in sql


def test_only_changed_columns_are_written() -> None:
    from app.ui.pages.alerts import _rule_change_sql
    assert _rule_change_sql("COST_X", 30.0, True, 30.0, True) == ""                 # nothing changed
    assert "SET THRESHOLD_NUM = 45.0, UPDATED_AT" in _rule_change_sql("COST_X", 30.0, True, 45.0, True)
    both = _rule_change_sql("COST_X", 30.0, False, 45.0, True)
    assert "SET THRESHOLD_NUM = 45.0, ENABLED = TRUE, UPDATED_AT" in both
    # an empty box (None) leaves the threshold alone; an unread current threshold is never assumed 0
    assert "THRESHOLD_NUM" not in _rule_change_sql("COST_X", None, True, None, False)
    assert "SET THRESHOLD_NUM = 5.0" in _rule_change_sql("COST_X", None, True, 5.0, True)
    assert "'x''y'" in _rule_change_sql("x'y", 1.0, True, 2.0, True)               # literal-quoted


def test_current_values_read_from_the_config_frame() -> None:
    from app.ui.pages.alerts import _rule_current
    assert _rule_current(_RULES, "SEC_TRUST_REGRESSION") == (1.0, True)
    off = _RULES.assign(ENABLED=["false", "TRUE"], THRESHOLD_NUM=[None, 2.0])
    assert _rule_current(off, "COST_X") == (None, False)
    assert _rule_current(off, "SEC_TRUST_REGRESSION") == (2.0, True)
    assert _rule_current(_RULES, "MISSING") == (None, True)


def test_generator_seeds_from_the_picked_rule() -> None:
    src = read("app/ui/pages/alerts.py")
    block = src.split('with st.expander("Generate a threshold change"):', 1)[1].split('elif section == "History":', 1)[0]
    assert "_cur_thr, _cur_en = _rule_current(rules.df, rule_id)" in block
    assert "value=_cur_thr if _cur_thr is not None and _cur_thr >= 0 else None" in block
    assert 'key=f"rule_thresh:{rule_id}"' in block and 'key=f"rule_enabled:{rule_id}"' in block
    assert 'st.checkbox("Enabled", value=_cur_en' in block
    assert "SET THRESHOLD_NUM = {new_threshold}, ENABLED" not in block              # the always-both UPDATE
    assert 'key="rule_thresh")' not in block


def test_rendered_enable_toggle_keeps_the_threshold(monkeypatch) -> None:
    pytest.importorskip("streamlit")
    from streamlit.testing.v1 import AppTest

    from app.ui.pages import alerts
    from tests.test_alerts_failed_reads import _ok, _render_section, _stub
    _stub(monkeypatch, {"alert_rules": _ok(_RULES)})
    # Pin the section by stubbing the picker, not by seeding its session key: this test re-runs the
    # script, and on the Streamlit 1.52.2 floor (ci.yml floor-compat) a re-run re-serializes the
    # section ButtonGroup, whose `indices` walks a seeded 'Rules' string per character and raises.
    monkeypatch.setattr(alerts, "lazy_sections", lambda *_a, **_k: "Rules")
    at = AppTest.from_function(_render_section, default_timeout=30)
    at.session_state["rule_pick"] = "SEC_TRUST_REGRESSION"
    at.run()
    assert not at.exception, at.exception
    assert at.number_input(key="rule_thresh:SEC_TRUST_REGRESSION").value == 1.0   # seeded, not 0.0
    at.checkbox(key="rule_enabled:SEC_TRUST_REGRESSION").uncheck().run()
    assert not at.exception, at.exception
    codes = [c.value for c in at.code if "ALERT_CONFIG" in c.value]
    assert codes and all("THRESHOLD_NUM" not in c for c in codes)
    assert any("SET ENABLED = FALSE" in c for c in codes)
