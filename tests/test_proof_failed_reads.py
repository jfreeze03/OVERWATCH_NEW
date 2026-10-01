"""R1-085 / R1-208 and R1-087 / R1-209: Proof renders a failed read by its KIND, never as setup or as zeros.

* The savings-ledger read gates the whole Proof record. Any failure used to read as "Apply the action + savings
  layer (V051+)" (needs_setup) on a fully migrated install, the page-open verdict vanished, and Pipeline's
  Settling tile said "ledger not set up". needs_setup is now only for a true absence
  (app.core.result.is_setup_absence -- 'privilege' names the grants, not V051); a timeout / drift / other
  failure is unavailable with its error, and the verdict says the record could not be read.
* The acceptance (ACTION_QUEUE) and precision (ALERT_EVENTS) reads fed None into helpers that return zero
  counts, so a timed-out read printed "0 done · 0 dismissed · 0 open" / "0 actioned · 0 noise" -- the same as a
  clean, empty queue -- and the projection said "nothing decided yet". A failed side read now says so.
"""

from __future__ import annotations

import pandas as pd
import pytest
from test_pages_shaped import _shaped_from_sql

from app.core.result import QueryResult
from app.ui import decision_studio as ds

_SETUP = ("absent", "privilege", "unknown_function")
_FAILED = ("missing_column", "timeout", "other")


@pytest.fixture(autouse=True)
def _clear_memo():
    ds.reset_proof_memo()
    yield
    ds.reset_proof_memo()


def _failed(kind: str) -> QueryResult:
    return QueryResult(df=pd.DataFrame(), ok=False, source="t", error=f"boom ({kind})", error_kind=kind)


def _patch(monkeypatch, failing: dict[str, QueryResult]) -> dict:
    seen: dict = {"empty": [], "detail": [], "kpis": []}

    def fake_run(sql, *_a, key: str = "", **_k):
        return failing.get(key) or _shaped_from_sql(sql)

    def fake_batch(specs, **_k):
        return {s["key"]: failing.get(s["key"]) or _shaped_from_sql(s.get("sql", "")) for s in specs or []}

    def fake_empty(kind, msg, *_a, **k):
        seen["empty"].append((kind, msg))
        seen["detail"].append(k.get("detail"))

    monkeypatch.setattr(ds, "run", fake_run)
    monkeypatch.setattr(ds, "run_batch", fake_batch)
    monkeypatch.setattr(ds, "empty_state", fake_empty)
    monkeypatch.setattr(ds, "kpi_row", lambda items, *_a, **_k: seen["kpis"].append(items))
    for name in ("hero_metric", "section_header", "result_caption", "styled_table", "selectable_nav_table"):
        monkeypatch.setattr(ds, name, lambda *_a, **_k: None)
    monkeypatch.setattr(ds, "can_open", lambda *_a, **_k: False)
    return seen


# ------------------------------------------------------------------------------- the ledger read ----

@pytest.mark.parametrize("kind", _SETUP + _FAILED)
def test_failed_ledger_read_renders_by_kind(monkeypatch, kind):
    seen = _patch(monkeypatch, {"decision_roi_ledger_full": _failed(kind)})
    verdict = ds.decision_verdict(3.68)
    ds._proof_tab(3.68)
    ((state, msg),) = seen["empty"]
    if kind in _SETUP:
        assert verdict == {}
        assert state == "needs_setup"
        assert ("roles.sql" in msg) == (kind == "privilege") and ("V051" in msg) == (kind != "privilege")
    else:
        assert state == "unavailable" and "V051" not in msg and "could not be read" in msg
        assert seen["detail"] == [f"boom ({kind})"]
        # the page-open line says the record could not be read -- it never just vanishes
        assert verdict["severity"] == "warn" and "could not be read" in verdict["body"]


@pytest.mark.parametrize(("kind", "gap"), [("timeout", "ledger unavailable"), ("absent", "ledger not set up")])
def test_pipeline_settling_tile_names_the_failure(monkeypatch, kind, gap):
    """The Settling tile's delta is a small expression in a long function: lock the expression and the
    helper it reads (the same memoized failure the Proof tab splits on)."""
    _patch(monkeypatch, {"decision_roi_ledger_full": _failed(kind)})
    assert ds._proof_signals(3.68) is None
    failure = ds._proof_ledger_failure()
    assert failure is not None and failure.error_kind == kind
    got = ("ledger unavailable" if failure is not None and not ds.is_setup_absence(failure.error_kind)
           else "ledger not set up")
    assert got == gap
    from tests._source import read
    pipe = read("app/ui/decision_studio.py").split("def _pipeline_tab(", 1)[1].split("\ndef ", 1)[0]
    assert "_ledger_fail = _proof_ledger_failure()" in pipe
    assert ('_ledger_gap = ("ledger unavailable" if _ledger_fail is not None and not '
            'is_setup_absence(_ledger_fail.error_kind)') in pipe
    assert "if sig is not None else _ledger_gap)," in pipe


def test_a_successful_ledger_read_clears_the_failure_marker(monkeypatch):
    _patch(monkeypatch, {})
    assert ds._proof_signals(3.68) is not None
    assert ds._proof_ledger_failure() is None


# ------------------------------------------------------------------------ acceptance / precision ----

def _cards(seen: dict) -> dict:
    return {k["label"]: k for row in seen["kpis"] for k in row}


@pytest.mark.parametrize("kind", _FAILED)
def test_failed_acceptance_and_precision_reads_never_print_zero_counts(monkeypatch, kind):
    seen = _patch(monkeypatch, {"sc_accept": _failed(kind), "sc_precision": _failed(kind)})
    ds._proof_tab(3.68)
    cards = _cards(seen)
    acted, prec = cards["Acted on"], cards["Alert precision"]
    assert acted["value"] == "—" and prec["value"] == "—"
    assert "0 done" not in acted["delta"] and "0 open" not in acted["delta"]
    assert "0 actioned" not in prec["delta"] and "0 noise" not in prec["delta"]
    assert acted["delta"] == "ACTION_QUEUE unavailable (read failed)"
    assert prec["delta"] == "ALERT_EVENTS unavailable (read failed)"
    failed = [m for s, m in seen["empty"] if s == "unavailable"]
    assert any("Team follow-through" in m for m in failed) and any("Alert precision" in m for m in failed)
    assert seen["detail"].count(f"boom ({kind})") == 2
    # the projection's adoption default cannot claim "nothing decided yet" off a failed read
    sig = ds._proof_signals(3.68)
    help_text = ds._projection_defaults(sig, None)["adoption_help"]
    assert "nothing decided yet" not in help_text and "could not be completed" in help_text


def test_absent_side_reads_are_setup_not_zeros(monkeypatch):
    seen = _patch(monkeypatch, {"sc_precision": _failed("absent")})
    ds._proof_tab(3.68)
    assert _cards(seen)["Alert precision"]["delta"] == "ALERT_EVENTS needs setup (read failed)"
    assert [s for s, m in seen["empty"] if "Alert precision" in m] == ["needs_setup"]


def test_successful_side_reads_keep_their_counts(monkeypatch):
    seen = _patch(monkeypatch, {})
    ds._proof_tab(3.68)
    cards = _cards(seen)
    assert "done ·" in cards["Acted on"]["delta"] and "actioned ·" in cards["Alert precision"]["delta"]
    assert not [m for _s, m in seen["empty"] if "Team follow-through" in m or "Alert precision" in m]
