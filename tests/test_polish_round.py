"""Locks for the polish batch (v4.20.0, Codex r7 adopts + owner-approved #1).

Partial-success run_batch keeps the cache rule (failures never cached; the
cached unit stays all-or-nothing) while falling back PER KEY; heavy toggles
price themselves; the leaderboard prefills the trend; the legend explains
the colors; tables share one styler; the docs match the app."""

from __future__ import annotations

from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]


def test_run_batch_falls_back_per_key_not_wholesale():
    q = (_ROOT / "app" / "core" / "query.py").read_text(encoding="utf-8")
    body = q.split("def run_batch", 1)[1].split("\ndef ", 1)[0]
    assert "ALWAYS returns {key: QueryResult}" in body       # new contract, documented
    assert 'key=f"bfb:{spec[\'key\']}"' in body              # per-key retry through run()
    assert "return None" not in body                         # wholesale fallback is gone
    assert "batch_fallback" in body                          # evidence stream survives
    assert "failures are never cached" in body               # the house rule, restated


def test_heavy_toggles_price_themselves():
    comp = (_ROOT / "app" / "ui" / "components.py").read_text(encoding="utf-8")
    assert "def toggle_cost_hint(" in comp
    assert "First run this session" in comp                  # honest unknown state
    for page, key in (("security.py", '"dormant"'), ("cost_parts/optimize.py", '"sizing"'),
                      ("cost_parts/optimize.py", '"repeatq"')):
        src = (_ROOT / "app" / "ui" / "pages" / page).read_text(encoding="utf-8")
        assert f"toggle_cost_hint({key})" in src, (page, key)


def test_leaderboard_prefills_the_trend():
    uc = (_ROOT / "app" / "ui" / "pages" / "cost_parts" / "unit_costs.py").read_text(encoding="utf-8")
    assert 'selectable_table(pdf, key="uc_proc_sel"' in uc
    assert 'st.session_state["uc_proc_trend_name"] = str(pdf.iloc[int(_psel)]["PROC_NAME"])' in uc


def test_legend_exists_and_is_wired():
    comp = (_ROOT / "app" / "ui" / "components.py").read_text(encoding="utf-8")
    assert "def legend_popover(" in comp
    assert "ESTIMATED vs VERIFIED" in comp                   # the money semantics
    main = (_ROOT / "app" / "main.py").read_text(encoding="utf-8")
    assert "legend_popover()" in main


def test_table_consistency_progress():
    import glob
    n = sum(Path(p).read_text(encoding="utf-8").count("st.dataframe(")
            for p in glob.glob(str(_ROOT / "app" / "ui" / "pages" / "**" / "*.py"), recursive=True))
    assert n <= 8, n                                         # only kwargs-variant holdouts remain


def test_docs_match_the_app():
    c = (_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "(2026-07-11)" not in c                           # date drift fixed
    f = (_ROOT / "FEATURES.md").read_text(encoding="utf-8")
    assert "Since v4.9" in f and "Incident object" in f
