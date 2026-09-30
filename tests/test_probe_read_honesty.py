"""v4.603 probe-read honesty: a probe=True read that fails renders by its failure KIND, and every probe=True
Cortex read has a canary that FAILs on a missing column.

A probe read logs neither an absent object nor a missing column (query.run's expected-absence set), so the
page is the only place a failure shows. Before v4.603 the Security AI-guardrails panel said the usage view
"appears only once Guardrails is enabled" for ANY failure (the view exists on this account: owner probe
2026-09-29), its empty branch asserted "Guardrails is enabled", and the CoCo token-economics panel blamed a
timeout on a missing TOKENS_GRANULAR column. None of the four probe=True Cortex reads had a canary, so the
v4.601.1 QUOTA_ACCESS_BLOCK_HISTORY CREATED_ON class (an invalid identifier on every render) had no alarm.
Render tests with fakes (the tests/test_quotas.py _render pattern)."""

from __future__ import annotations

import re
from types import SimpleNamespace

import pandas as pd
import pytest

from tests._source import read

_EXPECTED_ABSENCE = ("absent", "unknown_function", "missing_column")
_OTHER_FAILURES = ("timeout", "other")


def _ok(df: pd.DataFrame):
    return SimpleNamespace(ok=True, empty=df.empty, df=df, error="", error_kind="", usable=lambda: not df.empty)


def _failed(kind: str):
    return SimpleNamespace(ok=False, empty=True, df=pd.DataFrame(), error=f"boom ({kind})", error_kind=kind,
                           usable=lambda: False)


class _FakeSt:
    def __init__(self, *, button_key: str = "", toggles_off: tuple[str, ...] = ()):
        self.calls: list[tuple[str, str]] = []
        self.session_state: dict = {}
        self._button_key = button_key
        self._toggles_off = toggles_off

    def caption(self, text, *_a, **_k):
        self.calls.append(("caption", str(text)))

    def markdown(self, text, *_a, **_k):
        self.calls.append(("markdown", str(text)))

    def error(self, text, *_a, **_k):
        self.calls.append(("error", str(text)))

    def toggle(self, *_a, key: str = "", **_k):
        return key not in self._toggles_off

    def divider(self):
        self.calls.append(("divider", ""))

    def button(self, _label, *_a, key: str = "", **_k):
        return key == self._button_key

    def progress(self, *_a, **_k):
        return SimpleNamespace(progress=lambda *_a, **_k: None, empty=lambda: None)

    def text(self, kind: str) -> str:
        return "\n".join(t for k, t in self.calls if k == kind)


# --------------------------------------------------------------- Security > AI guardrails ----

def _render_guardrails(monkeypatch, result):
    """The AI-guardrails tab with fakes: the behavior half's read is skipped (guard -> False), so the only
    rendered states are the Guardrails section's."""
    from app.ui.pages import security as sec
    fake = _FakeSt()
    seen: dict = {"empty": [], "detail": [], "kpis": [], "tables": 0, "keys": []}

    def fake_run(*_a, key: str = "", **_k):
        seen["keys"].append(key)
        return result if key == "ai_guardrails_daily" else _ok(pd.DataFrame({"X": [1]}))

    monkeypatch.setattr(sec, "st", fake)
    monkeypatch.setattr(sec, "run", fake_run)
    monkeypatch.setattr(sec, "guard", lambda *_a, **_k: False)
    monkeypatch.setattr(sec, "section_header", lambda *_a, **_k: None)
    monkeypatch.setattr(sec, "result_caption", lambda *_a, **_k: None)
    monkeypatch.setattr(sec, "kpi_row", lambda items, *_a, **_k: seen["kpis"].append(items))
    monkeypatch.setattr(sec, "styled_table", lambda *_a, **_k: seen.__setitem__("tables", seen["tables"] + 1))
    monkeypatch.setattr(sec, "empty_state", lambda kind, msg, *_a, **k: (
        seen["empty"].append((kind, msg)), seen["detail"].append(k.get("detail"))))
    sec._ai_guardrails_tab("ALL")
    assert "ai_guardrails_daily" in seen["keys"]
    return fake, seen


@pytest.mark.parametrize("kind", ["absent", "unknown_function"])
def test_guardrails_absent_view_is_setup_and_never_blames_guardrails_being_disabled(monkeypatch, kind):
    _, seen = _render_guardrails(monkeypatch, _failed(kind))
    ((state, msg),) = seen["empty"]
    assert state == "needs_setup"
    assert "not readable by this app" in msg
    # the view exists on this account whether or not Guardrails is enabled: never name "enabled" as the cause
    assert "enable" not in msg.lower()
    assert not seen["kpis"] and not seen["tables"]


@pytest.mark.parametrize("kind", ["missing_column", "timeout", "other"])
def test_guardrails_other_failures_are_unavailable_with_the_error(monkeypatch, kind):
    _, seen = _render_guardrails(monkeypatch, _failed(kind))
    ((state, msg),) = seen["empty"]
    assert state == "unavailable", kind
    assert seen["detail"] == [f"boom ({kind})"]                      # the error reaches the owner
    assert "could not be read" in msg and "enable" not in msg.lower()
    assert not seen["kpis"] and not seen["tables"]


def test_guardrails_zero_rows_never_asserts_enabled_or_clean(monkeypatch):
    _, seen = _render_guardrails(monkeypatch, _ok(pd.DataFrame()))
    ((state, msg),) = seen["empty"]
    assert state == "no_data_yet" and state != "clean"
    assert "enabled" not in msg.lower()
    assert "No guardrails-checked requests" in msg


def test_guardrails_rows_still_render_the_kpis(monkeypatch):
    df = pd.DataFrame({"DAY": ["2026-09-28", "2026-09-29"], "REQUESTS": [10, 30], "FLAGGED": [1, 0]})
    fake, seen = _render_guardrails(monkeypatch, _ok(df))
    assert seen["empty"] == [] and seen["tables"] == 1
    ((kpis),) = seen["kpis"]
    assert [k["value"] for k in kpis] == ["40", "1"]
    assert "no company grain" in fake.text("caption")


# ------------------------------------------------------------- Cost > CoCo token economics ----

def _render_token_panel(monkeypatch, result):
    from app.ui.pages.cost_parts import ai_chargeback as cb
    fake = _FakeSt()
    seen: dict = {"empty": [], "detail": [], "runs": 0}

    def fake_run(*_a, **_k):
        seen["runs"] += 1
        return result

    monkeypatch.setattr(cb, "st", fake)
    monkeypatch.setattr(cb, "run", fake_run)
    monkeypatch.setattr(cb, "empty_state", lambda kind, msg, *_a, **k: (
        seen["empty"].append((kind, msg)), seen["detail"].append(k.get("detail"))))
    cb._token_economics_panel("ALL", 30, 15.0)
    return fake, seen


_TOKENS_NOTE = "TOKENS_GRANULAR isn't available on this account's Cortex Code views yet"


@pytest.mark.parametrize("kind", _EXPECTED_ABSENCE)
def test_token_panel_expected_absence_keeps_the_locked_caption(monkeypatch, kind):
    fake, seen = _render_token_panel(monkeypatch, _failed(kind))
    assert _TOKENS_NOTE in fake.text("caption"), kind
    assert seen["empty"] == [] and seen["runs"] == 1                   # returns before the credit read


@pytest.mark.parametrize("kind", _OTHER_FAILURES)
def test_token_panel_other_failures_are_unavailable_not_a_missing_column(monkeypatch, kind):
    fake, seen = _render_token_panel(monkeypatch, _failed(kind))
    assert _TOKENS_NOTE not in fake.text("caption"), kind
    ((state, msg),) = seen["empty"]
    assert state == "unavailable" and "token-type read failed" in msg
    assert seen["detail"] == [f"boom ({kind})"]
    assert seen["runs"] == 1


def _kinds_tuple(src: str, anchor: str) -> set[str]:
    m = re.search(re.escape(anchor) + r"\s+in\s+\(([^)]*)\)", src)
    assert m, anchor
    return {s.strip().strip("\"'") for s in m.group(1).split(",") if s.strip()}


def test_token_panel_absence_set_is_query_runs_unlogged_set():
    """The note is shown for exactly the kinds run() leaves unlogged on a probe read, so 'not logged' and
    'shown as absent' move together; anything run() logs renders as a failed read."""
    run_set = _kinds_tuple(read("app/core/query.py"), "_expected_absence = probe and kind")
    panel = read("app/ui/pages/cost_parts/ai_chargeback.py").split("def _token_economics_panel", 1)[1]
    assert _kinds_tuple(panel, "if te_res.error_kind") == run_set == set(_EXPECTED_ABSENCE)


# ------------------------------------------------------------------------------- canaries ----

_PROBE_CANARIES = {
    "cortex.guardrails_daily": ("guardrails_daily", (1,)),
    "cortex.code_token_types": ("cortex_code_token_types", ()),
    "cortex.quota_access_block_history": ("quota_access_block_history", (1,)),
    "cortex.app_self_cost": ("app_cortex_self_cost", (1,)),
}


def test_every_probe_cortex_read_has_a_declared_canary():
    from app.data import cortex_sql, mart_sql
    from app.data.canary import CANARIES, EXPECTED_GAPS
    reg = dict(CANARIES)
    for name, (builder, args) in _PROBE_CANARIES.items():
        assert name in reg, name
        assert name in EXPECTED_GAPS, name                             # absence = account-feature state
        mod = mart_sql if builder == "app_cortex_self_cost" else cortex_sql
        assert reg[name]() == getattr(mod, builder)(*args), name
    # the token-types canary is the app's exact text (the builder has no window knob by design)
    assert reg["cortex.code_token_types"]() == cortex_sql.cortex_code_token_types()


def _run_canary_tab(monkeypatch, kinds: dict[str, str]) -> dict[str, str]:
    """Admin > Canary with fakes: ``kinds`` maps a canary name to the error kind its read fails with (every
    other entry passes). Returns name -> STATUS from the runner's own classification."""
    import app.ui.components as components
    from app.ui.pages import admin
    fake = _FakeSt(button_key="adm_canary_run", toggles_off=("adm_recon_on",))   # stop before the recon

    def fake_run(_sql, *_a, source: str = "", **_k):
        kind = kinds.get(source)
        return _failed(kind) if kind else _ok(pd.DataFrame({"X": [1]}))

    monkeypatch.setattr(admin, "st", fake)
    monkeypatch.setattr(admin, "run", fake_run)
    monkeypatch.setattr(admin, "audit_mode", lambda: False)
    monkeypatch.setattr(admin, "panel_help", lambda *_a, **_k: None)
    monkeypatch.setattr(admin, "empty_state", lambda *_a, **_k: None)
    monkeypatch.setattr(admin, "section_header", lambda *_a, **_k: None)
    monkeypatch.setattr(components, "styled_table", lambda *_a, **_k: None)
    admin._canary_tab()
    frame = fake.session_state["_adm_canary_results"]
    return dict(zip(frame["CHECK"], frame["STATUS"], strict=True))


def test_canary_absence_is_a_gap_and_a_missing_column_fails(monkeypatch):
    for kind, want in (("absent", "GAP"), ("unknown_function", "GAP"), ("missing_column", "FAIL"),
                       ("timeout", "FAIL")):
        status = _run_canary_tab(monkeypatch, dict.fromkeys(_PROBE_CANARIES, kind))
        for name in _PROBE_CANARIES:
            assert status[name] == want, (name, kind)
    # the CREATED_ON class itself: an invalid identifier on the quota read is a FAIL, never a calm GAP
    status = _run_canary_tab(monkeypatch, {"cortex.quota_access_block_history": "missing_column"})
    assert status["cortex.quota_access_block_history"] == "FAIL"
    assert {s for n, s in status.items() if n != "cortex.quota_access_block_history"} == {"PASS"}
