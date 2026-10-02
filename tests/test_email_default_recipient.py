"""The OVERWATCH_EMAIL default-recipient path stays durable (owner decision 2026-10-02: "do not overwrite again").

V164's CRITICAL escalation email names no address: it goes only to the OVERWATCH_EMAIL integration's
DEFAULT_RECIPIENTS, which was empty, so every escalation email failed (escalation_email_failed, page
NotifyWebhook) and the triage SQL sent the owner to the Teams webhook fix. These locks keep every repo path
from undoing the owner's default again:

* nothing in the repo ever UNSETs DEFAULT_RECIPIENTS or ALLOWED_RECIPIENTS (alone or in an UNSET list), and
  nothing CREATE OR REPLACEs the OVERWATCH_EMAIL integration;
* no ALTER ... SET of ALLOWED_RECIPIENTS or DEFAULT_RECIPIENTS names only a new address (SET replaces the
  whole list): each one keeps every address DESC already listed;
* runbook Step 3 adds a recipient beside the live ones in the alert bodies, never instead of them;
* a kept (suspended, then resumed) alert's first evaluation covers the whole teardown window, and the
  pre-flight and the rebuild runbook say so;
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


# -- 1. nothing un-sets a recipient list, and nothing re-creates the integration from scratch -------------------

# any UNSET list that names either recipient list ("UNSET DEFAULT_SUBJECT, DEFAULT_RECIPIENTS" too)
_UNSET = re.compile(r"\bUNSET\s+(?:\w+\s*,\s*)*(?:DEFAULT|ALLOWED)_RECIPIENTS\b", re.I)
# CREATE OR REPLACE drops the live integration's lists and grants; only CREATE ... IF NOT EXISTS is allowed
_REPLACE_EMAIL = re.compile(r"\bCREATE\s+OR\s+REPLACE\s+NOTIFICATION\s+INTEGRATION\s+(?:IF\s+NOT\s+EXISTS\s+)?"
                            r"OVERWATCH_EMAIL\b", re.I)


def _unset_hits(texts: dict[str, str]) -> list[str]:
    return [rel for rel, text in texts.items() if _UNSET.search(text) or _REPLACE_EMAIL.search(text)]


def test_nothing_unsets_default_recipients():
    texts = _tracked_text()
    assert "docs/EMAIL_RECIPIENT_RUNBOOK.md" in texts and "snowflake/teardown.sql" in texts   # not vacuous
    assert _unset_hits(texts) == []
    for bad in ("ALTER NOTIFICATION INTEGRATION OVERWATCH_EMAIL UNSET DEFAULT_RECIPIENTS;",
                "ALTER NOTIFICATION INTEGRATION OVERWATCH_EMAIL UNSET DEFAULT_SUBJECT, DEFAULT_RECIPIENTS;",
                "ALTER NOTIFICATION INTEGRATION OVERWATCH_EMAIL UNSET ALLOWED_RECIPIENTS;",
                "CREATE OR REPLACE NOTIFICATION INTEGRATION OVERWATCH_EMAIL TYPE = EMAIL ENABLED = TRUE\n"
                "    ALLOWED_RECIPIENTS = ('<new>');"):
        assert _unset_hits({"x.sql": bad}), bad
    assert not _unset_hits({"x.sql": "CREATE NOTIFICATION INTEGRATION IF NOT EXISTS OVERWATCH_EMAIL TYPE = EMAIL;"})


# -- 2. a recipient-list SET keeps every existing address -------------------------------------------------------

# SET ALLOWED_RECIPIENTS and SET DEFAULT_RECIPIENTS each REPLACE the whole list, so every ALTER ... SET of
# either list carries the keep-every-address slot (also inside a commented recipe, and with DEFAULT_SUBJECT or
# another property set in the same statement)
_SET_ALLOWED = re.compile(r"\bSET\s+ALLOWED_RECIPIENTS\s*=\s*\(([^)]*)\)", re.I)
_ALTER_SET = re.compile(r"\bALTER\s+NOTIFICATION\s+INTEGRATION\s+(?:IF\s+EXISTS\s+)?\w+\s+SET\b([^;]*)", re.I)
_LIST = re.compile(r"\b(?:ALLOWED|DEFAULT)_RECIPIENTS\s*=\s*\(([^)]*)\)", re.I)
_KEEP_SLOT = "<every address DESC listed"


def _uncomment(text: str) -> str:
    return re.sub(r"(?m)^[ \t]*--", " ", text)


def _shrinking_sets(texts: dict[str, str]) -> list[str]:
    hits = []
    for rel, text in texts.items():
        for alter in _ALTER_SET.finditer(_uncomment(text)):
            hits += [f"{rel}: {' '.join(m.group(0).split())}" for m in _LIST.finditer(alter.group(1))
                     if _KEEP_SLOT not in m.group(1)]
    return hits


def test_no_recipient_list_set_drops_an_existing_address():
    texts = _tracked_text()
    assert _shrinking_sets(texts) == []
    inspected = {rel for rel, text in texts.items()
                 for alter in _ALTER_SET.finditer(_uncomment(text)) if _LIST.search(alter.group(1))}
    assert {"docs/EMAIL_RECIPIENT_RUNBOOK.md", "snowflake/webhook_delivery.sql"} <= inspected   # not vacuous
    runbook = texts["docs/EMAIL_RECIPIENT_RUNBOOK.md"]
    assert _SET_ALLOWED.search(runbook)                                   # the runbook still shows how
    assert "**replaces the whole list**" in runbook
    step2 = " ".join(runbook[runbook.index("### Step 2"):runbook.index("### Step 3")].split())
    assert "`SET ALLOWED_RECIPIENTS` and `SET DEFAULT_RECIPIENTS` each **replaces the whole list**" in step2
    assert ("SET DEFAULT_RECIPIENTS = ('<recipient>', <every address DESC listed, each in quotes>)"
            in step2)
    # the old Step 2 replaced the whole allow-list with one address, and 850f15f's replaced the default list
    old = "ALTER NOTIFICATION INTEGRATION OVERWATCH_EMAIL\n      SET ALLOWED_RECIPIENTS = ('<recipient>');"
    assert _shrinking_sets({"old.md": old})
    old_default = ("ALTER NOTIFICATION INTEGRATION OVERWATCH_EMAIL\n      SET DEFAULT_RECIPIENTS = ('<recipient>')\n"
                   "          DEFAULT_SUBJECT = 'OVERWATCH escalation';")
    assert _shrinking_sets({"old.md": old_default})
    old_recipe = ("-- ALTER NOTIFICATION INTEGRATION OVERWATCH_EMAIL SET\n--     DEFAULT_RECIPIENTS = ('<recipient>')\n"
                  "--     DEFAULT_SUBJECT = 'OVERWATCH escalation';")
    assert _shrinking_sets({"old.sql": old_recipe})
    later = ("ALTER NOTIFICATION INTEGRATION OVERWATCH_EMAIL SET DEFAULT_SUBJECT = 'x'\n"
             "    DEFAULT_RECIPIENTS = ('<new>');")
    assert _shrinking_sets({"x.sql": later})


def test_runbook_step_3_adds_a_recipient_beside_the_live_ones():
    """Step 3 re-runs native_alert_templates.sql, whose four CREATE OR REPLACE ALERTs overwrite the live alert
    bodies: it must add the new address beside every address the live alerts mail, never replace them."""
    runbook = (_ROOT / "docs" / "EMAIL_RECIPIENT_RUNBOOK.md").read_text(encoding="utf-8")
    step3 = " ".join(runbook[runbook.index("### Step 3"):runbook.index("### Step 4")].split())
    assert "replacing the recipient" not in step3
    for needle in ("never only the new one", "comma-separated list", "SHOW ALERTS IN SCHEMA DBA_MAINT_DB.OVERWATCH",
                   "-- add, never replace"):
        assert needle in step3, needle


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


# -- 5. a KEPT alert's first evaluation after a resume covers the whole suspended window ------------------------

def test_kept_alerts_resume_with_their_pre_teardown_window():
    """The dead-man alerts look back to SNOWFLAKE.ALERT.LAST_SUCCESSFUL_SCHEDULED_TIME(), falling back to one hour
    only when there is none. A CREATE OR REPLACE starts that over; an alert the teardown only SUSPENDed keeps its
    last success from before the suspend, so its first evaluation after the resume covers the whole rebuild."""
    tpl = (_ROOT / "snowflake" / "native_alert_templates.sql").read_text(encoding="utf-8")
    assert tpl.count("COALESCE(SNOWFLAKE.ALERT.LAST_SUCCESSFUL_SCHEDULED_TIME()") == 4       # the reason holds
    runbook = (_ROOT / "docs" / "EMAIL_RECIPIENT_RUNBOOK.md").read_text(encoding="utf-8")
    pre = " ".join(runbook[runbook.index("## Pre-flight before RESUME"):runbook.index("-- (1)")].split())
    for needle in ("only SUSPENDed (a teardown or full rebuild keeps them)",
                   "everything logged since the suspend", "widen (2)"):
        assert needle in pre, needle
    assert "-- after a teardown or rebuild: replace 24 with the hours since the teardown" in runbook
    fr = (_ROOT / "docs" / "FULL_REBUILD.md").read_text(encoding="utf-8")
    email = " ".join(fr[fr.index("(b) **Email**"):fr.index("(c) **Drill and ML forecast**")].split())
    for needle in ("A kept alert resumes with its last successful evaluation from before the teardown",
                   "one catch-up email per alert"):
        assert needle in email, needle


# -- 6. the retry wording matches V164's stamp-after-any-channel rule -----------------------------------------

def test_runbook_escalation_retry_sentence_matches_v164():
    runbook = " ".join((_ROOT / "docs" / "EMAIL_RECIPIENT_RUNBOOK.md").read_text(encoding="utf-8").split())
    assert "an escalation where every channel failed is retried every hour" in runbook
    assert "also for a route-delivered one whose Teams re-post failed too" in runbook
    assert "that no Teams route delivered (an email-only event" not in runbook      # the 850f15f wording
    apc = (_ROOT / "snowflake" / "alert_pipeline_check.sql").read_text(encoding="utf-8")
    fix_d = " ".join(apc[apc.index("-- FIX D"):].replace("--", " ").split())
    # runbook Step 2 DOES carry an ALLOWED_RECIPIENTS SET (run only when DESC lacks the address)
    assert "without touching ALLOWED_RECIPIENTS" not in fix_d
    assert "run its ALLOWED_RECIPIENTS SET only when DESC lacks the address" in fix_d
