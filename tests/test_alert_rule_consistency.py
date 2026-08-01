"""The app must not lie about which alert rules are live.

Two real incidents motivated this (2026-08-01):
  1. The app said the PIPE_VOLUME_DROP alert "fires" — true, it turned out, but only after a
     confusing trace, because it fires from SP_ANOMALY_SWEEP, NOT the SP_ALERT_SCAN everyone
     looks at first.
  2. A "fix" then flipped the app text to "the alert was retired / pages nothing" — which was
     FALSE and dangerous (a live HIGH PROD alert described as dead).

Neither the migration byte-compare tests nor sqlglot could catch this: they verify a proc
matches its generator and that SQL parses — not that the thing the app advertises still exists
in the implementation. This test closes that gap by cross-referencing three statically-derived
sets:
  RAISED   — rule ids the latest alert-raiser procs actually INSERT into ALERT_EVENTS
  ENABLED  — rule ids seeded (and not later disabled/deleted) in ALERT_CONFIG
  CLAIMS   — rule ids the app presents as LIVE ("fires") or RETIRED ("no alert fires")

Guard A: an app LIVE claim must be backed by a raiser (or be on RETIRED_ALLOWLIST).
Guard B: an app RETIRED claim must NOT be a rule that is still raised AND enabled.
Guard C: every ENABLED-seeded rule is raised (or explicitly retired) — no dormant config.

Horizon = the highest migration present: the app ships against the full built migration set, so
that is the right reference for "what the app may claim".
"""

from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_MIG = _ROOT / "snowflake" / "migrations"

# Rule ids the app may still NAME while the rule is intentionally retired (config deleted/
# disabled and every always-visible surface says so). Adding a rule here is the deliberate,
# greppable act that records "the app references this for history only". Verified 2026-08-01.
RETIRED_ALLOWLIST = frozenset({
    "SEC_BREAK_GLASS_USE",   # config DELETEd V034:181; Security>Changes says "no alert fires"
    "PIPE_TASK_FAILURES",    # retired V043:2005 (kept as a disabled row for history)
})

_RULE = r"(?:PIPE|COST|SEC|OPS|DATA)_[A-Z0-9_]+"        # the alert-rule id namespace
_RULE_RE = re.compile(rf"'({_RULE})'")                 # SQL: rule ids are quoted literals
_RULE_APP_RE = re.compile(rf"\b({_RULE})\b")           # app: rule ids appear bare in prose/comments
_APP_FILES = [
    "app/logic/playbooks.py", "app/logic/navigate.py", "app/logic/actions.py",
    "app/logic/anomaly.py", "app/ui/pages/operations.py", "app/ui/pages/alerts.py",
    "app/ui/pages/security.py", "app/ui/pages/control_room.py", "app/data/ops_sql.py",
]

_LIVE_RE = re.compile(r"alert fires|fires past|\bis live\b|authoritative|raises a (?:HIGH|CRITICAL)", re.I)
# Only UNAMBIGUOUS "this alert does not fire" phrasing. Deliberately NOT "dropped from" or
# "dormant" — those matched unrelated prose near a rule id ("events dropped from this feed",
# "Dormant users"). The real stale-text case ("the alert was retired ... pages nothing") is
# still caught by 'retired' + 'pages nothing'.
_RETIRED_RE = re.compile(r"\bretired\b|no alert fires|pages nothing|does not fire|"
                         r"scan arm was removed", re.I)


def _version(path: Path) -> int:
    return int(re.match(r"V(\d+)", path.name).group(1))


def _mig_files() -> list[Path]:
    return sorted(_MIG.glob("V*.sql"), key=_version)


def _latest_proc_bodies() -> dict[str, str]:
    """{proc_name: body} for the LAST CREATE OR REPLACE of each proc (last-wins across
    version-ordered files) — mirrors how the live account resolves a re-derived proc."""
    pat = re.compile(r"CREATE OR REPLACE PROCEDURE DBA_MAINT_DB\.OVERWATCH\.(\w+)\(.*?\n\$\$;", re.S)
    bodies: dict[str, str] = {}
    for f in _mig_files():
        for m in pat.finditer(f.read_text(encoding="utf-8")):
            bodies[m.group(1)] = m.group(0)      # later file / later match overwrites
    return bodies


def _raiser_bodies() -> dict[str, str]:
    """Procs whose latest body INSERTs into ALERT_EVENTS — the things that can raise a rule."""
    return {n: b for n, b in _latest_proc_bodies().items()
            if "INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS" in b}


def _raised_rule_ids() -> set[str]:
    ids: set[str] = set()
    for body in _raiser_bodies().values():
        ids |= set(_RULE_RE.findall(body))
    return ids


def _config_enabled() -> set[str]:
    """Rule ids seeded into ALERT_CONFIG and not later disabled/deleted (replayed in order)."""
    enabled: set[str] = set()
    for f in _mig_files():
        t = f.read_text(encoding="utf-8")
        for seg in re.split(r"(?=MERGE INTO|INSERT INTO|UPDATE |DELETE FROM)", t):
            if "ALERT_CONFIG" not in seg:
                continue
            rules = set(_RULE_RE.findall(seg))
            head = seg[:40].upper()
            if head.startswith(("MERGE", "INSERT")):
                # a seed row carries ENABLED=FALSE only if the segment says so per-rule; the
                # house seeds are all TRUE, so treat a seeded rule as enabled unless a later
                # UPDATE/DELETE turns it off.
                enabled |= {r for r in rules if not re.search(rf"'{r}'[^)]*\bFALSE\b", seg)}
            elif (head.startswith("UPDATE") and re.search(r"ENABLED\s*=\s*FALSE", seg, re.I)) or head.startswith("DELETE"):
                enabled -= rules
    return enabled


def _app_claims() -> dict[str, set[str]]:
    """{rule_id: {'LIVE'|'RETIRED'}} — classify each rule-id mention by nearby claim words."""
    claims: dict[str, set[str]] = {}
    for rel in _APP_FILES:
        text = (_ROOT / rel).read_text(encoding="utf-8")
        for m in _RULE_APP_RE.finditer(text):
            rid = m.group(1)
            window = text[max(0, m.start() - 260): m.end() + 260]
            if _RETIRED_RE.search(window):
                claims.setdefault(rid, set()).add("RETIRED")
            elif _LIVE_RE.search(window):
                claims.setdefault(rid, set()).add("LIVE")
    return claims


# ---------------------------------------------------------------------------
def test_extraction_sanity():
    raised, enabled = _raised_rule_ids(), _config_enabled()
    assert "SP_ANOMALY_SWEEP" in _raiser_bodies(), "SP_ANOMALY_SWEEP must be recognized as a raiser"
    assert "SP_ALERT_SCAN" in _raiser_bodies()
    # the rule that started all this: raised by SP_ANOMALY_SWEEP and enabled in config
    assert "PIPE_VOLUME_DROP" in raised, "PIPE_VOLUME_DROP is raised by SP_ANOMALY_SWEEP"
    assert "PIPE_VOLUME_DROP" in enabled, "and its ALERT_CONFIG row is seeded/enabled"


def test_classifier_is_not_vacuous():
    """A guard against the guard: if the rule-id/claim regexes ever stop matching (as they
    did on the first cut — SQL-quote regex vs bare-word prose), the three guards below pass
    trivially. Pin that the classifier actually sees the app's claims."""
    claims = _app_claims()
    assert claims, "no alert-rule claims detected in the app — the classifier is broken (vacuous)"
    assert claims.get("PIPE_VOLUME_DROP") == {"LIVE"}, (
        "the app should present PIPE_VOLUME_DROP as a live/firing alert (it fires via "
        f"SP_ANOMALY_SWEEP); classifier saw {claims.get('PIPE_VOLUME_DROP')}")
    assert any("LIVE" in c for c in claims.values())


def test_A_app_live_claims_are_backed_by_a_raiser():
    raised = _raised_rule_ids()
    offenders = [rid for rid, c in _app_claims().items()
                 if "LIVE" in c and rid not in raised and rid not in RETIRED_ALLOWLIST]
    assert not offenders, (
        "App presents these rules as LIVE/firing but no latest alert-raiser proc emits them "
        f"(and they are not on RETIRED_ALLOWLIST): {sorted(offenders)}. Either the scan arm was "
        "lost in a re-derivation, or the app copy is wrong.")


def test_B_app_retired_claims_are_truly_not_live():
    raised, enabled = _raised_rule_ids(), _config_enabled()
    offenders = [rid for rid, c in _app_claims().items()
                 if "RETIRED" in c and rid in raised and rid in enabled]
    assert not offenders, (
        "App copy calls these rules retired/'pages nothing'/'no alert fires', but the latest "
        f"raiser still emits them AND their ALERT_CONFIG row is enabled — a live alert described "
        f"as dead: {sorted(offenders)}. Correct the app text, or actually disable the rule.")


def test_C_enabled_config_rules_have_a_raiser():
    raised, enabled = _raised_rule_ids(), _config_enabled()
    dormant = [rid for rid in enabled if rid not in raised and rid not in RETIRED_ALLOWLIST]
    assert not dormant, (
        "These rules are ENABLED in ALERT_CONFIG but no latest raiser proc emits them — a "
        f"dormant config row that no scan honors: {sorted(dormant)}. Re-add the scan arm, "
        "disable the config row, or add to RETIRED_ALLOWLIST with a reason.")
