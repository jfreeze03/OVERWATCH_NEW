"""Codex M/L visual wave (v4.585.0) — charts.py: rec33 color de-collision + rec36
horizontal dumbbell for long entity names. Both are presentation-only; these lock the
intended behavior so a regression re-fails here."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pandas as pd

from app.ui import charts


def test_rec33_stable_color_decollides_simultaneously_visible_entities() -> None:
    # WH_ETL and WH_TEST crc32%10-collide to the SAME base palette slot — in a stacked
    # bar their segments were indistinguishable. The pure map (pinned by the C15 test)
    # keeps the collision; _stable_color must resolve it to two distinct hexes.
    base = charts._stable_color_map(["WH_ETL", "WH_TEST"])
    assert base["WH_ETL"] == base["WH_TEST"]          # the collision this rec fixes

    color = charts._stable_color("X", ["WH_ETL", "WH_TEST"])
    spec = color.to_dict()["scale"]
    domain, rng = spec["domain"], spec["range"]
    assert len(rng) == len(set(rng)) == 2             # de-collided: two distinct fills
    # the alphabetically-first claimant keeps its natural crc32 color; the collider moves
    first = sorted(["WH_ETL", "WH_TEST"])[0]
    assert rng[domain.index(first)] == base[first]


def test_rec33_unique_entity_keeps_its_color_even_beside_a_collision() -> None:
    # The de-collision must never STEAL a non-colliding entity's natural slot to rehome a
    # collider (the two-pass fix). WH_ETL & WH_STG collide; WH_TASKS is unique in the pure
    # map — it must keep its natural color whether or not its colliders share the frame.
    from collections import Counter
    names = ["WH_ETL", "WH_STG", "WH_TASKS"]
    base = charts._stable_color_map(names)
    counts = Counter(base.values())
    unique = [n for n in names if counts[base[n]] == 1]
    colliding = [n for n in names if counts[base[n]] > 1]
    assert unique and colliding, "test needs a non-colliding entity beside a real collision"
    spec = charts._stable_color("X", names).to_dict()["scale"]
    got = dict(zip(spec["domain"], spec["range"], strict=True))
    for n in unique:
        assert got[n] == base[n], f"{n} is unique in the pure map — its color must not shift"
    assert len(set(spec["range"])) == len(spec["range"])   # every visible entity still distinct


def _fake_st() -> SimpleNamespace:
    captured: dict = {}
    ns = SimpleNamespace(
        altair_chart=lambda chart, **_k: captured.__setitem__("spec", chart.to_dict()),
        caption=lambda *_a, **_k: None,
    )
    ns._captured = captured
    return ns


def test_rec36_paired_bars_is_horizontal_no_angled_labels(monkeypatch) -> None:
    fake = _fake_st()
    monkeypatch.setattr(charts, "st", fake)
    df = pd.DataFrame({
        "WH": ["WAREHOUSE_WITH_A_VERY_LONG_NAME", "WH_SHORT"],
        "A": [10.0, 5.0],
        "B": [7.0, 6.0],
    })
    charts.paired_bars(df, "WH", "A", "B", a_label="This month", b_label="Prior month")
    spec = fake._captured["spec"]
    blob = json.dumps(spec)
    # rec36: the old vertical grouped bars angled the x labels to -30°; the horizontal
    # dumbbell puts names on Y, so that angled label must be gone (the theme's default
    # axis labelAngle:0 is unrelated and fine).
    assert "-30" not in blob and '"labelAngle": -30' not in blob
    # the entity Label is encoded on the Y channel in every layer (dumbbell = layered)
    layers = spec.get("layer", [])
    assert layers, "paired_bars should render a layered (connector + points) chart"
    assert all(layer.get("encoding", {}).get("y", {}).get("field") == "Label" for layer in layers)
    # A=accent / B=gray coding is preserved on the points layer's color scale
    assert any(
        layer.get("encoding", {}).get("color", {}).get("scale", {}).get("range")
        == [charts._ACCENT, "#64748b"]
        for layer in layers
    )
