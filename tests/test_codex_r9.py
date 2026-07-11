"""Locks for the Codex r9 correctness batch (v4.24.0) — four verified-real
bugs (pre-identity pref caching, empty-mart fallback reviving the 46-56 GB
scan, full-batch re-execution on one failure, racy cache-hit sentinel) and
two metric/paint fixes."""

from __future__ import annotations

from pathlib import Path

from app.data import mart_sql

_ROOT = Path(__file__).resolve().parents[1]


def test_identity_hydrates_before_the_first_cached_read():
    main = (_ROOT / "app" / "main.py").read_text(encoding="utf-8")
    body = main.split("def main()", 1)[1]
    assert body.index("current_role()") < body.index("_apply_default_landing()")
    assert "Identity first" in body or "Identity resolved" in main or "identity" in body.lower()


def test_settings_failures_are_never_cached():
    comp = (_ROOT / "app" / "ui" / "components.py").read_text(encoding="utf-8")
    body = comp.split("def _settings_frame_cached", 1)[1].split("\ndef ", 1)[0]
    assert "if not res.ok:" in body and "raise RuntimeError" in body
    assert "return res.df" in body


def test_empty_mart_can_be_the_answer_where_declared():
    comp = (_ROOT / "app" / "ui" / "components.py").read_text(encoding="utf-8")
    body = comp.split("def run_mart_first", 1)[1].split("\ndef ", 1)[0]
    assert "empty_is_answer: bool = False" in body                # default unchanged: young
    assert "if empty_is_answer and res.ok:" in body               # marts still fall back
    ops = (_ROOT / "app" / "ui" / "pages" / "operations.py").read_text(encoding="utf-8")
    assert "empty_is_answer=True)" in ops                         # lock panel: no waits = answer


def test_partial_batches_keep_their_survivors():
    q = (_ROOT / "app" / "core" / "query.py").read_text(encoding="utf-8")
    assert "class _BatchPartial(Exception):" in q
    assert "never cached" in q.split("class _BatchPartial", 1)[1].split("def _execute_batch", 1)[0]
    fb = q.split("except _BatchPartial as bp:", 1)[1].split("except Exception as exc:", 1)[0]
    assert "if idx in bp.frames:" in fb                           # survivors returned as-is
    assert 'key=f"bfb:{spec[' in fb                               # only the failed re-run


def test_adoption_counts_visits_not_events():
    sql = mart_sql.app_usage_summary(30)
    assert "COALESCE(EVENT_KIND, 'page_visit') = 'page_visit'" in sql
    assert "AS WAU" in sql                                        # weekly-active alongside


def test_microtext_is_readable():
    theme = (_ROOT / "app" / "theme.py").read_text(encoding="utf-8")
    assert "font-size:9px" not in theme.split(".ow-src-badge", 1)[1].split("}", 1)[0]
