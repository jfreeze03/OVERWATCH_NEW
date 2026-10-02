"""The OVERWATCH_EMAIL default-recipient path stays durable (owner decision 2026-10-02: "do not overwrite again").

V164's CRITICAL escalation email names no address: it goes only to the OVERWATCH_EMAIL integration's
DEFAULT_RECIPIENTS, which was empty, so every escalation email failed (escalation_email_failed, page
NotifyWebhook) and the triage SQL sent the owner to the Teams webhook fix. These locks keep every repo path
from undoing the owner's default again:

* nothing in the repo ever UNSETs DEFAULT_RECIPIENTS;
* no ALTER ... SET ALLOWED_RECIPIENTS names only a new address (SET replaces the whole list): each one keeps
  every address DESC already listed;
* alert_pipeline_check.sql STEP 4b routes escalation_* rows to the email fix (FIX D), never the Teams fix;
* webhook_delivery.sql's rotation runbook says it is not the fix for escalation rows;
* the rebuild runbook re-creates OVERWATCH_EMAIL with DEFAULT_RECIPIENTS and the kept allow-list.
The teardown's opt-in DELIVERY GATE is locked in tests/test_teardown_coverage.py. Each check below also runs
against the old (pre-2026-10-02) text and must fail there.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_SELF = Path(__file__).resolve()
_SUFFIXES = {".sql", ".md", ".py", ".yml", ".yaml", ".toml", ".txt"}


def _tracked_text() -> dict[str, str]:
    """Every tracked text file except this one and the CHANGELOG (release history quotes old text)."""
    try:
        out = subprocess.run(["git", "ls-files", "-z"], cwd=_ROOT, capture_output=True, text=True,
                             check=True).stdout
        rels = [r for r in out.split("\0") if r]
    except (OSError, subprocess.SubprocessError):
        rels = [p.relative_to(_ROOT).as_posix() for p in _ROOT.rglob("*") if p.is_file()]
    texts: dict[str, str] = {}
    for rel in rels:
        path = _ROOT / rel
        if path.suffix.lower() not in _SUFFIXES or path.resolve() == _SELF or rel == "CHANGELOG.md":
            continue
        try:
            texts[rel] = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
    return texts


# -- 1. nothing un-sets the default list ------------------------------------------------------------------------

_UNSET = re.compile(r"\bUNSET\s+DEFAULT_RECIPIENTS\b", re.I)


def _unset_hits(texts: dict[str, str]) -> list[str]:
    return [rel for rel, text in texts.items() if _UNSET.search(text)]


def test_nothing_unsets_default_recipients():
    texts = _tracked_text()
    assert "docs/EMAIL_RECIPIENT_RUNBOOK.md" in texts and "snowflake/teardown.sql" in texts   # not vacuous
    assert _unset_hits(texts) == []
    assert _unset_hits({"x.sql": "ALTER NOTIFICATION INTEGRATION OVERWATCH_EMAIL UNSET DEFAULT_RECIPIENTS;"})


# -- 2. an allow-list SET keeps every existing address ----------------------------------------------------------

_SET_ALLOWED = re.compile(r"\bSET\s+ALLOWED_RECIPIENTS\s*=\s*\(([^)]*)\)", re.I)
_KEEP_SLOT = "<every address DESC listed"


def _shrinking_sets(texts: dict[str, str]) -> list[str]:
    return [f"{rel}: {m.group(0)}" for rel, text in texts.items()
            for m in _SET_ALLOWED.finditer(text) if _KEEP_SLOT not in m.group(1)]


def test_no_allowed_recipients_set_drops_an_existing_address():
    texts = _tracked_text()
    assert _shrinking_sets(texts) == []
    runbook = texts["docs/EMAIL_RECIPIENT_RUNBOOK.md"]
    assert _SET_ALLOWED.search(runbook)                                   # the runbook still shows how
    assert "**replaces the whole list**" in runbook
    # the old Step 2 replaced the whole list with one address
    old = "ALTER NOTIFICATION INTEGRATION OVERWATCH_EMAIL\n      SET ALLOWED_RECIPIENTS = ('<recipient>');"
    assert _shrinking_sets({"old.md": old})


# -- 3. triage sends escalation rows to the email fix, never the Teams fix --------------------------------------

def _step4b(text: str) -> str:
    return text[text.index("-- STEP 4b"):text.index("-- STEP 5")]


def _route(error_type: str, step4b: str) -> str | None:
    """Evaluate STEP 4b's ROUTE CASE for one ERROR_TYPE (WHEN arms in order, then ELSE)."""
    case = re.search(r"CASE\b(.*?)\bEND AS ROUTE", step4b, re.S)
    if not case:
        return None
    for op, value, label in re.findall(r"WHEN ERROR_TYPE (=|ILIKE) '([^']+)'\s+THEN '([^']+)'", case.group(1)):
        if op == "=" and error_type == value:
            return label
        pattern = "^" + re.escape(value).replace("%", ".*") + "$"
        if op.upper() == "ILIKE" and re.match(pattern, error_type, re.I):
            return label
    other = re.search(r"ELSE '([^']+)'", case.group(1))
    return other.group(1) if other else None


def _routing_problems(text: str) -> list[str]:
    step = _step4b(text)
    problems = []
    if "WHERE PAGE = 'NotifyWebhook'" not in step:
        problems.append("STEP 4b no longer reads the NotifyWebhook rows")
    email = _route("escalation_email_failed", step) or ""
    if not (email.startswith("FIX D") and "DEFAULT_RECIPIENTS" in email):
        problems.append(f"escalation_email_failed -> {email or 'no route'}")
    for etype in ("escalation_failed", "escalation_something_new"):
        route = _route(etype, step) or ""
        if not route.startswith("FIX D"):
            problems.append(f"{etype} -> {route or 'no route'}")
    for etype in ("route_send_failed", "undelivered_expired", "webhook_run_failed"):
        route = _route(etype, step) or ""
        if not route.startswith("FIX C"):
            problems.append(f"{etype} -> {route or 'no route'}")
    if "FIX D" not in text[text.index("-- FIXES"):]:
        problems.append("no FIX D")
    return problems


def test_step_4b_routes_escalation_rows_to_the_email_fix():
    apc = (_ROOT / "snowflake" / "alert_pipeline_check.sql").read_text(encoding="utf-8")
    assert _routing_problems(apc) == []
    fixes = apc[apc.index("-- FIXES"):]
    fix_d = fixes[fixes.index("-- FIX D"):]
    for needle in ("DEFAULT_RECIPIENTS", "docs/EMAIL_RECIPIENT_RUNBOOK.md requirement 4",
                   "DESC NOTIFICATION INTEGRATION OVERWATCH_EMAIL;", "NOTIFICATION_HISTORY",
                   "SET replaces a whole list"):
        assert needle in fix_d, needle
    assert "NOT for STEP 4b's escalation_* rows" in fixes[fixes.index("-- FIX C"):fixes.index("-- FIX D")]
    # the old STEP 4b: every NotifyWebhook row, escalation included, went to the Teams fix
    old = ("-- STEP 4b: sender-side failures\n"
           "-- webhook/notification errors -> the Teams integration end (STEP 6, FIX C).\n"
           "SELECT DATE(LOGGED_AT) AS DAY, ERROR_TYPE, LEFT(ERROR_MESSAGE, 140) AS MSG, COUNT(*) AS N\n"
           "FROM APP_ERROR_LOG\nWHERE PAGE = 'NotifyWebhook'\nGROUP BY 1, 2, 3 ORDER BY 1 DESC, 2;\n"
           "-- STEP 5: routes\n-- FIXES\n-- FIX C (deliveries failing): the Teams URL rotated\n")
    assert _routing_problems(old)


def test_webhook_rotation_is_not_the_escalation_fix():
    wd = (_ROOT / "snowflake" / "webhook_delivery.sql").read_text(encoding="utf-8")
    rotation = wd[wd.index("-- ROTATION RUNBOOK"):wd.index("-- Severity-based multi-channel routing")]
    assert "NOT for APP_ERROR_LOG escalation_email_failed / escalation_failed rows" in rotation
    assert "alert_pipeline_check.sql FIX D" in rotation
    recipe = wd[wd.index("-- V164 CRITICAL escalation email"):]
    assert "(optional; Teams re-post works without it)" not in recipe       # it ships ON
    assert "ON by default" in recipe and "escalation_email_failed" in recipe


# -- 4. a rebuild re-creates the integration with its default list ---------------------------------------------

def test_full_rebuild_recreates_the_email_integration_with_its_default_list():
    fr = (_ROOT / "docs" / "FULL_REBUILD.md").read_text(encoding="utf-8")
    email = " ".join(fr[fr.index("(b) **Email**"):fr.index("(c) **Drill and ML forecast**")].split())
    for needle in ("ALLOWED_RECIPIENTS naming EVERY address step 0's DESC listed",
                   "SET replaces the whole list", "DEFAULT_RECIPIENTS set as docs/EMAIL_RECIPIENT_RUNBOOK.md "
                   "requirement 4", "escalation_email_failed", "Integration and alerts kept (the default)"):
        assert needle in email, needle
    step0 = fr[fr.index("## 0. Decide what survives"):fr.index("## 1. Backups")]
    assert "record ALLOWED_ / DEFAULT_RECIPIENTS" in step0
    assert "are KEPT" in step0
