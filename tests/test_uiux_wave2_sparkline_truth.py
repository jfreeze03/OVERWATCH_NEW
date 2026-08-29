"""UI/UX master list — Wave 2 sparkline-truth cluster (F42 + F43).

Two "the little chart lies" bugs in the KPI-card sparkline:

* F42 — magnitude. `spark_svg` scaled to the series' own min..max, so EVERY
  series filled the full height: a 99.1->99.4 wiggle drew the same dramatic
  climb as a doubling. The domain now includes zero, so amplitude is
  proportional to the real change.

* F43 — polarity color. The spark was tinted by card SEVERITY, so a cost card
  trending up (a red delta) could draw a calm blue line. The card spark is now
  colored by the delta's trend polarity, matching the delta chip exactly.
"""

from __future__ import annotations

import re

from app.ui import components, palette


def _poly_y_span(svg: str) -> float:
    """Vertical pixel span of the polyline in a spark SVG (max y - min y)."""
    m = re.search(r'<polyline points="([^"]+)"', svg)
    assert m, "no polyline in spark svg"
    ys = [float(pt.split(",")[1]) for pt in m.group(1).split(" ")]
    return max(ys) - min(ys)


def test_f42_domain_anchor_kills_false_drama_on_a_tiny_wiggle():
    # a near-flat high-baseline series must render nearly flat...
    flat = _poly_y_span(components.spark_svg([99.1, 99.2, 99.4], height=24))
    # ...while a genuine doubling rises across a real chunk of the height.
    climb = _poly_y_span(components.spark_svg([40.0, 80.0], height=24))
    assert flat < 1.0, f"a 0.3% wiggle should be near-flat, got y-span {flat}"
    assert climb > 6.0, f"a doubling should rise visibly, got y-span {climb}"
    # and the wiggle is dramatically flatter than the doubling (the whole point).
    assert flat < climb / 5


def test_f42_zero_anchor_does_not_break_normal_series():
    # still returns empty for < 2 finite points, and produces a valid polyline.
    assert components.spark_svg([5.0]) == ""
    assert components.spark_svg([]) == ""
    svg = components.spark_svg([1.0, 2.0, 3.0])
    assert "<polyline" in svg and "aria-hidden" in svg


def test_f43_delta_polarity_is_one_shared_source():
    # normal: up is good, down is bad. inverse (cost/errors): flipped.
    assert components._delta_is_good("+5%", "normal") is True
    assert components._delta_is_good("-5%", "normal") is False
    assert components._delta_is_good("+5%", "inverse") is False   # cost UP = bad
    assert components._delta_is_good("-5%", "inverse") is True    # cost DOWN = good
    # neutral cases collapse to None (no green/red implied)
    assert components._delta_is_good("x", "off") is None
    assert components._delta_is_good(None, "normal") is None
    assert components._delta_is_good("", "normal") is None


def test_f43_spark_color_matches_the_delta_chip_never_contradicts_it():
    # a cost card trending UP: delta_color 'inverse', a positive delta -> the delta
    # chip is RED; the spark must be red too, not a calm severity blue.
    up_cost = {"delta": "+12%", "delta_color": "inverse", "severity": "ok", "spark": [1, 2]}
    assert components._spark_color_for(up_cost, "ok") == palette.BAD
    # an improvement (down cost) -> green spark, matching a green delta chip.
    down_cost = {"delta": "-12%", "delta_color": "inverse", "severity": "bad"}
    assert components._spark_color_for(down_cost, "bad") == palette.OK
    # normal metric rising -> good/green.
    assert components._spark_color_for({"delta": "+3", "delta_color": "normal"}, "") == palette.OK
    # an explicit 'off' delta is neutral ink-mute (not severity, not green/red).
    assert components._spark_color_for({"delta": "0", "delta_color": "off"}, "warn") == palette.INK_MUTE
    # NO labeled delta -> keep the severity tint (nothing to contradict).
    assert components._spark_color_for({"spark": [1, 2]}, "warn") == palette.WARN
    assert components._spark_color_for({}, "") == palette.INFO


def test_f43_spark_color_is_a_literal_hex_not_a_css_var():
    # a spark stroke is an SVG presentation attribute; var(--...) would not resolve.
    for sev in ("ok", "warn", "bad", "info", ""):
        for item in ({"delta": "+1", "delta_color": "normal"},
                     {"delta": "-1", "delta_color": "inverse"},
                     {"delta": "0", "delta_color": "off"},
                     {}):
            col = components._spark_color_for(item, sev)
            assert col.startswith("#"), f"spark color must be hex, got {col!r}"
