"""rec5 (v4.585.0) — the Warehouses attention-ranked opener's pure merge helper
(anomaly.warehouse_attention_ranking). Unit-tests the ranking/merge logic so the
"lead with what's wrong" opener stays honest and empty-safe."""

from __future__ import annotations

import pandas as pd
import pytest

from app.logic.anomaly import warehouse_attention_ranking


def _anoms(rows: list[tuple[str, float, float]]) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=["WAREHOUSE_NAME", "USD", "Z_SCORE"])


def _peaks(rows: list[tuple[str, float]], intervals: int = 168) -> pd.DataFrame:
    # the builder's real shape (ops_sql.warehouse_concurrency_peaks): R1-074 gates "sustained" on
    # QUEUED_INTERVALS (a count over the whole 14-day read), so a default fixture models a warehouse
    # that queued ~1h/day: 168 five-minute intervals over 14 days, over the 30 min/day bar
    df = pd.DataFrame(rows, columns=["WAREHOUSE_NAME", "PEAK_QUEUED"])
    df["QUEUED_INTERVALS"] = intervals
    df["INTERVALS"] = 4000
    return df


def test_merges_both_signals_and_queueing_sorts_above_pure_spend() -> None:
    anomalies = _anoms([("WH_SPEND", 900.0, 6.2), ("WH_SPEND", 800.0, 4.0)])
    peaks = _peaks([("WH_QUEUE", 3.4)])
    out = warehouse_attention_ranking(anomalies, peaks)
    assert list(out["WAREHOUSE_NAME"]) == ["WH_QUEUE", "WH_SPEND"]   # queueing first
    spend_row = out[out["WAREHOUSE_NAME"] == "WH_SPEND"].iloc[0]
    assert spend_row["WORST_Z"] == 6.2                              # max |z|
    assert int(spend_row["ANOM_DAYS"]) == 2
    assert spend_row["ANOM_USD"] == 1700.0                          # summed anomalous-day spend
    assert pd.isna(spend_row["PEAK_QUEUED"])                        # no queueing signal for it
    queue_row = out[out["WAREHOUSE_NAME"] == "WH_QUEUE"].iloc[0]
    assert queue_row["PEAK_QUEUED"] == 3.4 and pd.isna(queue_row["WORST_Z"])


def test_a_warehouse_with_both_signals_carries_both_in_reason() -> None:
    out = warehouse_attention_ranking(
        _anoms([("WH_HOT", 500.0, -5.0)]), _peaks([("WH_HOT", 2.0)]))
    assert len(out) == 1
    reason = out.iloc[0]["REASON"]
    assert "spend anomaly z=5.0" in reason and "queued ~2.0 sustained" in reason


def test_a_sub_bar_queue_is_demoted_not_deleted_so_the_opener_is_never_falsely_clean() -> None:
    """Review r2 on R1-074: deleting rows under the sustained bar let a warehouse that queued for
    ~6.9 hours (83 five-minute intervals, one under 30 min/day over 14 days) vanish, and with no
    spend anomaly the opener then showed its verified-clean row. It is demoted instead: present,
    "peak queued", never "sustained", ranked after every spend anomaly and after sustained queueing."""
    from app.logic.anomaly import sustained_queue_min_intervals

    bar = sustained_queue_min_intervals()
    alone = warehouse_attention_ranking(_anoms([]), _peaks([("WH_BRIEF", 2.6)], intervals=bar - 1))
    assert list(alone["WAREHOUSE_NAME"]) == ["WH_BRIEF"]                  # not the empty "clean" frame
    assert alone.iloc[0]["REASON"] == "peak queued ~2.6"
    assert alone.iloc[0]["PEAK_QUEUED"] == 2.6 and int(alone.iloc[0]["QUEUED_INTERVALS"]) == bar - 1
    # a deeper peak than the sustained one, still ranked after it AND after the spend anomaly
    peaks = pd.concat([_peaks([("WH_BRIEF", 9.0)], intervals=bar - 1),
                       _peaks([("WH_BUSY", 1.0)], intervals=bar)])
    out = warehouse_attention_ranking(_anoms([("WH_PROD", 900.0, 4.0)]), peaks)
    assert list(out["WAREHOUSE_NAME"]) == ["WH_BUSY", "WH_PROD", "WH_BRIEF"]
    assert list(out["REASON"]) == ["queued ~1.0 sustained (~30m/day over 14d)",
                                   "spend anomaly z=4.0 on 1 day", "peak queued ~9.0"]
    # a NULL count (the read's COUNT_IF never NULLs, but a frame can) is a plain peak, not sustained
    nulled = _peaks([("WH_NULL", 3.0)])
    nulled["QUEUED_INTERVALS"] = None
    out = warehouse_attention_ranking(_anoms([("WH_PROD", 900.0, 4.0)]), nulled)
    assert list(out["WAREHOUSE_NAME"]) == ["WH_PROD", "WH_NULL"]
    assert out.iloc[1]["REASON"] == "peak queued ~3.0"


def test_below_floor_queueing_is_dropped() -> None:
    # PEAK_QUEUED below the floor (default 1.0) is not "what's wrong"
    out = warehouse_attention_ranking(_anoms([]), _peaks([("WH_QUIET", 0.4)]))
    assert out.empty


def test_empty_inputs_and_none_peaks_return_empty() -> None:
    assert warehouse_attention_ranking(_anoms([]), None).empty
    assert warehouse_attention_ranking(pd.DataFrame(), None).empty
    # missing columns must not raise
    assert warehouse_attention_ranking(pd.DataFrame({"X": [1]}), pd.DataFrame({"Y": [2]})).empty


def test_columns_are_stable_and_carry_no_duration_suffix() -> None:
    out = warehouse_attention_ranking(_anoms([("WH_A", 100.0, 4.0)]), _peaks([("WH_B", 2.0)]))
    # R1-074 added QUEUED_INTERVALS (the count behind "sustained") between PEAK_QUEUED and REASON
    assert list(out.columns) == [
        "WAREHOUSE_NAME", "WORST_Z", "ANOM_DAYS", "ANOM_USD", "PEAK_QUEUED", "QUEUED_INTERVALS", "REASON"]
    # counts + dollars, never durations -> no _SEC/_MS/_S suffix obligation
    assert not any(c.endswith(("_SEC", "_MS", "_S", "_MIN")) for c in out.columns)


def test_a_one_interval_burst_is_not_sustained_and_never_outranks_spend() -> None:
    """R1-074: PEAK_QUEUED is a single-interval MAX. A 5-minute burst (1 queued interval of 4000)
    used to read "queued ~1.0 sustained" and sort above a z=12 $9,000 spend anomaly -- five such
    bursts pushed every real anomaly out of the opener's head(5). Review r2: the bursts are DEMOTED
    (shown as plain peaks after the spend anomaly), never deleted."""
    from app.logic.anomaly import sustained_queue_min_intervals

    anomalies = _anoms([("WH_PROD", 9000.0, 12.0)])
    sandboxes = {f"WH_SANDBOX_{i}" for i in range(5)}
    bursts = pd.concat([_peaks([(w, 1.0)], intervals=1) for w in sorted(sandboxes)])
    out = warehouse_attention_ranking(anomalies, bursts)
    assert out.iloc[0]["WAREHOUSE_NAME"] == "WH_PROD"                # the spend anomaly leads head(5)
    tail = out.iloc[1:]
    assert set(tail["WAREHOUSE_NAME"]) == sandboxes and len(out) == 6  # present, after it
    assert list(tail["REASON"]) == ["peak queued ~1.0"] * 5             # ...never called sustained
    assert "sustained" not in " ".join(out["REASON"])
    # at the bar the queue signal is real again, sorts first and says how much per day
    held = _peaks([("WH_BUSY", 1.0)], intervals=sustained_queue_min_intervals())
    out = warehouse_attention_ranking(anomalies, held)
    assert list(out["WAREHOUSE_NAME"]) == ["WH_BUSY", "WH_PROD"]
    assert out.iloc[0]["REASON"] == "queued ~1.0 sustained (~30m/day over 14d)"
    assert int(out.iloc[0]["QUEUED_INTERVALS"]) == sustained_queue_min_intervals()


def test_scattered_bursts_across_the_14d_window_are_not_sustained() -> None:
    """Review r1 on R1-074: QUEUED_INTERVALS counts over the WHOLE 14-day read, so a flat floor of 6
    was ~30 min per 14 days (~2 min/day). Six separate 5-minute bursts on six different days (6 of
    4000 intervals) still read "queued ~1.0 sustained" and sorted above a z=12 $9,000 spend anomaly.
    Sustained is sizing's 30 min/day rate across the window: 30 x 14 / 5 = 84 intervals."""
    anomalies = _anoms([("WH_PROD", 9000.0, 12.0)])
    for n in (6, 83):           # six scattered bursts; one interval under 30 min/day over 14 days
        out = warehouse_attention_ranking(anomalies, _peaks([("WH_BURSTY", 1.0)], intervals=n))
        # review r2: present (a sub-bar queue is demoted, not deleted), after the spend anomaly
        assert list(out["WAREHOUSE_NAME"]) == ["WH_PROD", "WH_BURSTY"], n
        assert out.iloc[1]["REASON"] == "peak queued ~1.0", n
        assert int(out.iloc[1]["QUEUED_INTERVALS"]) == n
        assert "sustained" not in " ".join(out["REASON"]), n
    out = warehouse_attention_ranking(anomalies, _peaks([("WH_BURSTY", 1.0)], intervals=84))
    assert list(out["WAREHOUSE_NAME"]) == ["WH_BURSTY", "WH_PROD"]
    assert out.iloc[0]["REASON"] == "queued ~1.0 sustained (~30m/day over 14d)"
    # the rate in the reason is per day of the window, humanized (never raw minutes)
    out = warehouse_attention_ranking(_anoms([]), _peaks([("WH_HOT", 4.0)], intervals=1000))
    assert out.iloc[0]["REASON"] == "queued ~4.0 sustained (~5h 57m/day over 14d)"


def test_the_sustained_bar_is_sizings_per_day_rate_scaled_to_the_window() -> None:
    from app.logic.anomaly import LOAD_INTERVAL_MIN, sustained_queue_min_intervals
    from app.logic.sizing import QUEUE_UP_MIN_PER_DAY

    assert sustained_queue_min_intervals(14) == 84 and sustained_queue_min_intervals(7) == 42
    for days in (1, 7, 14, 30, 90):
        n = sustained_queue_min_intervals(days)
        # the smallest whole count whose per-day queueing reaches sizing's bar
        assert n * LOAD_INTERVAL_MIN / days >= QUEUE_UP_MIN_PER_DAY
        assert (n - 1) * LOAD_INTERVAL_MIN / days < QUEUE_UP_MIN_PER_DAY
    # a caller reading a different window passes it, and the bar and the wording follow
    anomalies = _anoms([("WH_PROD", 9000.0, 12.0)])
    out = warehouse_attention_ranking(anomalies, _peaks([("WH_W", 1.0)], intervals=41), window_days=7)
    assert list(out["WAREHOUSE_NAME"]) == ["WH_PROD", "WH_W"]          # under the 7d bar: a plain peak
    assert out.iloc[1]["REASON"] == "peak queued ~1.0"
    out = warehouse_attention_ranking(anomalies, _peaks([("WH_W", 1.0)], intervals=42), window_days=7)
    assert list(out["WAREHOUSE_NAME"]) == ["WH_W", "WH_PROD"]
    assert out.iloc[0]["REASON"] == "queued ~1.0 sustained (~30m/day over 7d)"


_WINDOW = "ATTENTION_PEAKS_WINDOW_DAYS"


def _opener_window_violations(src: str) -> list[str]:
    """Why the Warehouses opener's peaks reads and its ranking could disagree on the window. EVERY
    warehouse_concurrency_peaks( call in _wh_activity_anomalies must pass the shared constant as its
    window, and the ranking must judge on it (pass it, or keep the default it equals): a 30-day read
    judged against a 14-day bar calls half the real rate "sustained". (Review r2: the old regex lock
    only saw LITERAL windows, so one read switched to a variable or a days= keyword fell out of it.)"""
    import ast
    import inspect

    from app.logic.anomaly import ATTENTION_PEAKS_WINDOW_DAYS, warehouse_attention_ranking

    tree = ast.parse(src)
    fns = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_wh_activity_anomalies"]
    if len(fns) != 1:
        return ["_wh_activity_anomalies moved; re-point this lock"]
    if not any(isinstance(n, ast.ImportFrom) and n.module == "app.logic.anomaly"
               and any(a.name == _WINDOW and a.asname is None for a in n.names) for n in tree.body):
        return [f"operations.py no longer imports {_WINDOW} from app.logic.anomaly"]

    def _callee(call: ast.Call) -> str:
        f = call.func
        return f.attr if isinstance(f, ast.Attribute) else f.id if isinstance(f, ast.Name) else ""

    calls = [n for n in ast.walk(fns[0]) if isinstance(n, ast.Call)]
    peaks = [c for c in calls if _callee(c) == "warehouse_concurrency_peaks"]
    ranks = [c for c in calls if _callee(c) == "warehouse_attention_ranking"]
    bad = [] if peaks else ["the opener's peaks read moved; re-point this lock"]
    if len(ranks) != 1:
        bad.append(f"expected one warehouse_attention_ranking call, found {len(ranks)}")
    for c in peaks:
        arg = c.args[0] if c.args else next((k.value for k in c.keywords if k.arg == "days"), None)
        if not (isinstance(arg, ast.Name) and arg.id == _WINDOW):
            bad.append(f"line {c.lineno}: warehouse_concurrency_peaks window is "
                       f"{ast.unparse(arg) if arg is not None else '<missing>'}, not {_WINDOW}")
    for c in ranks:
        kw = next((k.value for k in c.keywords if k.arg == "window_days"), None)
        if kw is None:
            default = inspect.signature(warehouse_attention_ranking).parameters["window_days"].default
            if default != ATTENTION_PEAKS_WINDOW_DAYS:
                bad.append(f"line {c.lineno}: the ranking's default window_days is not {_WINDOW}")
        elif not (isinstance(kw, ast.Name) and kw.id == _WINDOW):
            bad.append(f"line {c.lineno}: warehouse_attention_ranking window_days={ast.unparse(kw)}")
    return bad


def test_the_opener_reads_peaks_over_the_window_the_bar_is_scaled_to() -> None:
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "app" / "ui" / "pages" / "operations.py").read_text(
        encoding="utf-8")
    assert _opener_window_violations(src) == []


def test_the_window_lock_sees_every_peaks_read() -> None:
    # teeth (review r2): ONE of the two reads drifting -- to another literal, a variable or a days=
    # keyword -- fails the lock, as does the ranking judging on a different window
    good = f"{_WINDOW}, company"

    def _opener(first: str, second: str, rank_kw: str = "") -> str:
        return (f"from app.logic.anomaly import {_WINDOW}\n"
                "def _wh_activity_anomalies(company, rate):\n"
                f"    pf = run_batch([{{'sql': ops_sql.warehouse_concurrency_peaks({first})}}])\n"
                f"    peaks = pf or run(ops_sql.warehouse_concurrency_peaks({second}))\n"
                f"    return warehouse_attention_ranking(a, peaks{rank_kw})\n")

    assert _opener_window_violations(_opener(good, good, f", window_days={_WINDOW}")) == []
    assert _opener_window_violations(_opener(good, good)) == []            # the default equals it
    assert _opener_window_violations(_opener(good, f"company=company, days={_WINDOW}")) == []
    for first, second, rank_kw in ((good, "30, company", ""), ("14, company", good, ""),
                                   (good, "company=company, days=win", ""), (good, "win, company", ""),
                                   (good, "company=company", ""), (good, good, ", window_days=30")):
        assert _opener_window_violations(_opener(first, second, rank_kw)), (first, second, rank_kw)


def test_a_peak_without_an_interval_count_is_only_a_peak() -> None:
    # an older frame shape (no QUEUED_INTERVALS): the peak is shown, never called sustained,
    # never sorted above a spend anomaly
    peaks = pd.DataFrame({"WAREHOUSE_NAME": ["WH_OLDSHAPE"], "PEAK_QUEUED": [3.0]})
    out = warehouse_attention_ranking(_anoms([("WH_PROD", 900.0, 5.0)]), peaks)
    assert list(out["WAREHOUSE_NAME"]) == ["WH_PROD", "WH_OLDSHAPE"]
    assert out.iloc[1]["REASON"] == "peak queued ~3.0"


def test_the_opener_lists_a_sub_bar_queue_instead_of_reading_clean(monkeypatch) -> None:
    """Review r2 on R1-074, on the rendered opener: no spend anomaly and one warehouse that queued
    just under the sustained bar. It used to get the green header and the verified-clean row; now it
    is flagged and listed as a plain peak, the Queueing KPI says none of it is sustained, and the
    caption no longer claims every queue outranks a spend anomaly."""
    from datetime import date, timedelta
    from types import SimpleNamespace

    from test_ops_c01_p606 import _ok, _page, _Stop

    from app.logic.anomaly import sustained_queue_min_intervals

    days = [date(2026, 9, 1) + timedelta(days=i) for i in range(30)]
    res = _ok(pd.DataFrame({"DAY": days, "WAREHOUSE_NAME": ["WH_A"] * 30, "CREDITS_TOTAL": [10.0] * 30}))
    peaks = _ok(_peaks([("WH_BRIEF", 2.0)], intervals=sustained_queue_min_intervals() - 1))

    def header(title, health="", *_a, **_k):
        if title.startswith("Warehouse spend"):
            raise _Stop
        seen["headers"].append((title, health))

    ops, fake, seen = _page(monkeypatch, {}, run_batch_mixed=lambda *_a, **_k: {"res": res, "peaks": peaks},
                            load_settings=lambda *_a, **_k: {}, section_header=header)
    fake.column_config = SimpleNamespace(NumberColumn=lambda *_a, **_k: None)
    with pytest.raises(_Stop):
        ops._wh_activity_anomalies("ALL", 3.0)
    assert seen["empty"] == []                                   # neither "clean" nor "no data yet"
    assert seen["headers"][0][0] == "Warehouses that need attention now"
    assert seen["headers"][0][1] not in ("", "ok")               # flagged, never the green all-clear
    (table,) = seen["tables"]
    assert list(table["WAREHOUSE_NAME"]) == ["WH_BRIEF"] and list(table["REASON"]) == ["peak queued ~2.0"]
    ((kpis,),) = [(k,) for k in seen["kpis"]]
    queueing = {k["label"]: k for k in kpis}["Queueing"]
    assert queueing["value"] == "1"
    assert queueing["help"].startswith("0 of 1 sustained (queued at least 30m/day across the 14-day read)")
    caption = fake.text("caption")
    assert "sustained queueing outranks a spend anomaly; a brief queue peak ranks after it" in caption
