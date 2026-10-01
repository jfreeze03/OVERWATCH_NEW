"""R1-141: the Cost Intelligence + Brief page verdicts read an OVERRUN contract as "Healthy".

contract_exhaustion computes DAYS_LEFT = CEIL((TOTAL - CONSUMED) / DAILY_BURN) with no clamp, so an
overrun (TOTAL 1000, CONSUMED 1200, burn 10) is -20 days and formulas.contract_runway keeps it with
severity 'bad'; a NULL burn is the -1 sentinel with severity 'warn'. Both pages re-derived the band
with `0 <= days <= 30 / 90`, which drops every negative -- so the opener said "Healthy — contract on
track" while the COST_CONTRACT_BREACH arm paged CRITICAL "Contract EXHAUSTED". With no contract
configured (TOTAL 0) the opener still claimed "contract on track". Both pages now share
verdict.contract_runway_signal / contract_runway_clause; these tests drive the pure helper AND the two
real pages (the shaped harness from tests/test_pages_shaped.py, with only the runway read replaced).
"""

from __future__ import annotations

import pandas as pd
import pytest

from app.core.result import QueryResult
from app.logic import contract_planner
from app.logic.formulas import contract_runway
from app.logic.verdict import (
    NO_CONTRACT_RUNWAY,
    RUNWAY_ON_TRACK,
    contract_runway_clause,
    contract_runway_signal,
    page_verdict,
)

_OVERRUN = {"TOTAL": 1000.0, "CONSUMED": 1200.0, "DAILY_BURN": 10.0, "DAYS_LEFT": -20.0,
            "EXHAUST_DATE": "2026-09-10"}
_NULL_BURN = {"TOTAL": 1000.0, "CONSUMED": 400.0, "DAILY_BURN": None, "DAYS_LEFT": None,
              "EXHAUST_DATE": None}
_NO_CONTRACT = {"TOTAL": 0.0, "CONSUMED": 0.0, "DAILY_BURN": 10.0, "DAYS_LEFT": None,
                "EXHAUST_DATE": None}
_LONG = {"TOTAL": 100000.0, "CONSUMED": 1000.0, "DAILY_BURN": 10.0, "DAYS_LEFT": 9900.0,
         "EXHAUST_DATE": "2053-08-01"}


def _best(row: dict):
    """The pages' own chain: no readable billing balance -> the configured-credits runway."""
    return contract_planner.best_runway(None, contract_runway(pd.Series(row)))


# ------------------------------------------------------------------------------- pure helper ----
def test_overrun_contract_is_attention_not_healthy():
    best = _best(_OVERRUN)
    assert best is not None and best["days_left"] == -20.0 and best["severity"] == "bad"
    sig = contract_runway_signal(best, basis="configured credits")
    assert sig is not None and sig.level == "bad"
    assert "exhausted" in sig.phrase
    v = page_verdict([sig], healthy=RUNWAY_ON_TRACK)
    assert v["label"] == "Attention needed" and "on track" not in v["body"]


def test_uncomputable_burn_is_watch():
    best = _best(_NULL_BURN)
    assert best is not None and best["days_left"] == -1.0 and best["severity"] == "warn"
    sig = contract_runway_signal(best, basis="configured credits")
    assert sig is not None and sig.level == "warn" and "not computable" in sig.phrase
    assert page_verdict([sig], healthy=RUNWAY_ON_TRACK)["label"] == "Watch"


def test_in_band_runways_keep_their_bands_and_basis():
    for days, level in ((0.0, "bad"), (30.0, "bad"), (31.0, "warn"), (90.0, "warn")):
        sig = contract_runway_signal({"days_left": days, "severity": "x"}, basis="billing balance")
        assert sig is not None and sig.level == level, days
        assert sig.phrase == f"contract runway {days:,.0f} days at current burn (billing balance)"
    assert contract_runway_signal({"days_left": 91.0, "severity": "ok"}) is None
    assert contract_runway_signal(_best(_LONG)) is None


def test_no_runway_is_quiet_when_read_and_watch_when_the_read_failed():
    assert _best(_NO_CONTRACT) is None                       # TOTAL <= 0 -> no contract configured
    assert contract_runway_signal(None, read_ok=True) is None
    failed = contract_runway_signal(None, read_ok=False)
    assert failed is not None and failed.level == "warn" and "unavailable" in failed.phrase
    # the healthy clause claims "on track" only for a real runway
    assert contract_runway_clause(None) == NO_CONTRACT_RUNWAY and "on track" not in NO_CONTRACT_RUNWAY
    assert contract_runway_clause(_best(_LONG)) == RUNWAY_ON_TRACK


# ----------------------------------------------------------------------------- the real pages ----
st = pytest.importorskip("streamlit")
from tests.test_pages_shaped import (  # noqa: E402,F401  (autouse fixture: shaped reads everywhere)
    _APPTEST_BUTTONGROUP_OK,
    _entry,
    _nav_to,
    _stub_shaped,
)

_VERDICTS: list[dict] = []


def _drive(monkeypatch, page: str, row: dict | None) -> dict:
    """row=None: the runway read FAILED (a timeout)."""
    from streamlit.testing.v1 import AppTest

    from app.ui.pages import brief, cost

    exh = (QueryResult(df=pd.DataFrame([row]), ok=True, source="runway stub") if row is not None
           else QueryResult(ok=False, error="statement timeout", error_kind="timeout"))
    _VERDICTS.clear()
    for mod in (cost, brief):
        monkeypatch.setattr(mod, "org_balance_result", lambda _page: None)     # credits basis
        monkeypatch.setattr(mod, "page_verdict_line", lambda v, *a, **k: _VERDICTS.append(dict(v)))
    _cost_run = cost.run

    def _cost_run_stub(sql, *a, **k):
        return exh if k.get("key") == "cost_verdict_exhaustion" else _cost_run(sql, *a, **k)

    monkeypatch.setattr(cost, "run", _cost_run_stub)
    _brief_batch = brief.run_batch

    def _brief_batch_stub(specs, **k):
        out = _brief_batch(specs, **k)
        return {**out, "exh": exh} if "exh" in out else out

    monkeypatch.setattr(brief, "run_batch", _brief_batch_stub)
    # the Brief's shared attention signals are the subject of tests/test_attention_parity.py; quiet them
    # so the verdict here is the runway's alone
    monkeypatch.setattr(brief, "attention_signals", lambda _b: [])

    at = AppTest.from_function(_entry, default_timeout=30)
    at.run()
    _nav_to(at, page)
    at.run()
    assert not at.exception, at.exception
    assert _VERDICTS, f"{page}: no page verdict rendered"
    return _VERDICTS[-1]


@pytest.mark.skipif(not _APPTEST_BUTTONGROUP_OK, reason="streamlit<1.55 AppTest ButtonGroup bug")
@pytest.mark.parametrize("page", ["Cost Intelligence", "Brief"])
def test_pages_flag_an_overrun_contract(monkeypatch, page):
    v = _drive(monkeypatch, page, _OVERRUN)
    assert v["label"] == "Attention needed", v
    assert "exhausted" in v["body"] and "on track" not in v["body"] and "healthy" not in v["body"]


@pytest.mark.skipif(not _APPTEST_BUTTONGROUP_OK, reason="streamlit<1.55 AppTest ButtonGroup bug")
@pytest.mark.parametrize("page", ["Cost Intelligence", "Brief"])
def test_pages_watch_an_uncomputable_burn(monkeypatch, page):
    v = _drive(monkeypatch, page, _NULL_BURN)
    assert v["label"] == "Watch" and "not computable" in v["body"], v


@pytest.mark.skipif(not _APPTEST_BUTTONGROUP_OK, reason="streamlit<1.55 AppTest ButtonGroup bug")
@pytest.mark.parametrize("page", ["Cost Intelligence", "Brief"])
def test_pages_claim_nothing_about_an_unconfigured_contract(monkeypatch, page):
    v = _drive(monkeypatch, page, _NO_CONTRACT)
    assert v["label"] == "Healthy", v
    assert NO_CONTRACT_RUNWAY in v["body"]
    assert "on track" not in v["body"] and "contract runway healthy" not in v["body"]


@pytest.mark.skipif(not _APPTEST_BUTTONGROUP_OK, reason="streamlit<1.55 AppTest ButtonGroup bug")
@pytest.mark.parametrize("page", ["Cost Intelligence", "Brief"])
def test_pages_watch_a_failed_runway_read(monkeypatch, page):
    """Round 5 guarded this on Cost only; the shared helper gives the Brief the same Watch (it used to
    append "contract runway healthy" to its all-clear on a failed read)."""
    v = _drive(monkeypatch, page, None)
    assert v["label"] == "Watch" and "contract runway unavailable" in v["body"], v
