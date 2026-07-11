"""Locks for the Codex r10 fix-now batch (v4.25.0) — and, per r10 #13,
these run the logic instead of grepping for it wherever the code allows."""

from __future__ import annotations

from pathlib import Path

from app.core.query import _classify_error, _with_row_cap

_ROOT = Path(__file__).resolve().parents[1]


def test_row_cap_is_tail_aware_executable():
    # nested LIMIT no longer disables the outer cap (r10 #6)
    nested = "SELECT * FROM (SELECT X FROM T LIMIT 50) g WHERE X > 0"
    assert _with_row_cap(nested, 100).rstrip().endswith("LIMIT 101")
    # a genuine trailing limit is respected, with or without semicolon
    assert _with_row_cap("SELECT X FROM T LIMIT 20", 100).rstrip().endswith("LIMIT 20")
    assert _with_row_cap("SELECT X FROM T LIMIT 20;", 100).rstrip().endswith("LIMIT 20;")
    # cap+1 lets truncation be detected; zero cap is a no-op
    assert _with_row_cap("SELECT X FROM T", 10).rstrip().endswith("LIMIT 11")
    assert _with_row_cap("SELECT X FROM T", 0) == "SELECT X FROM T"
    # the RATE_LIMIT column-name false positive stays fixed
    assert "LIMIT 101" in _with_row_cap("SELECT RATE_LIMIT FROM T", 100)


def test_error_classifier_executable():
    assert _classify_error(Exception("Object 'X' does not exist or not authorized.")) == "absent"
    assert _classify_error(Exception("SQL compilation error: Unknown function SYSTEM$FOO")) == "unknown_function"
    assert _classify_error(Exception("Statement reached its statement or warehouse timeout")) == "timeout"
    assert _classify_error(Exception("syntax error line 1")) == "other"
    assert _classify_error(None) == "other"


def test_result_carries_the_kind_and_consumers_use_it():
    res = (_ROOT / "app" / "core" / "result.py").read_text(encoding="utf-8")
    assert 'error_kind: str = ""' in res
    adm = (_ROOT / "app" / "ui" / "pages" / "admin.py").read_text(encoding="utf-8")
    assert 'res.error_kind in ("absent", "unknown_function")' in adm   # canary GAP fixed
    ai = (_ROOT / "app" / "ui" / "pages" / "cost_parts" / "ai_chargeback.py").read_text(encoding="utf-8")
    assert 'error_kind == "unknown_function"' in ai


def test_prefs_bootstrap_commits_on_success():
    main = (_ROOT / "app" / "main.py").read_text(encoding="utf-8")
    body = main.split("def _apply_default_landing", 1)[1].split("\ndef ", 1)[0]
    assert body.index("if not prefs.ok:") < body.index('st.session_state["_ow_default_applied"] = True\n    if prefs.empty:')
    assert "_ow_default_attempts" in body                              # bounded retries
    assert "deep link wins" in body                                    # explicit nav still commits


def test_batch_quarantine_and_submission_harvest():
    q = (_ROOT / "app" / "core" / "query.py").read_text(encoding="utf-8")
    rb = q.split("def run_batch", 1)[1]
    assert "_ow_batch_quarantine" in rb
    assert '_q.get("salt") != _salt' in rb                             # refresh clears it
    assert '_q["keys"] |= {str(bspecs[i]["key"]) for i in bp.errors}' in rb
    assert "if not bspecs:" in rb                                      # all-quarantined shortcut
    eb = q.split("def _execute_batch", 1)[1].split("\ndef ", 1)[0]
    assert "jobs.append(" in eb and "for idx in range(len(jobs), len(sqls)):" in eb
    assert "raise _BatchPartial(frames0, errors0) from sub_exc" in eb  # in-flight harvested


def test_drill_survives_an_empty_candidate_list():
    ops = (_ROOT / "app" / "ui" / "pages" / "operations.py").read_text(encoding="utf-8")
    assert "works even with no candidates above" in ops
    seg = ops.split("works even with no candidates above", 1)[1][:400]
    assert 'st.session_state["_ops_drill_target"] = target' in seg     # flow intact after dedent
