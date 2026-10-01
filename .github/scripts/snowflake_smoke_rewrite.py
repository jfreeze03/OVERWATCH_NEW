"""Rewrite the SQL the opt-in ``snowflake-smoke`` job applies, so the replay can only touch its clone.

This is layer 1 of the ISOLATION CONTRACT in .github/workflows/ci.yml. Copying the migrations and
swapping the identifier DBA_MAINT_DB for the clone's name is not enough (round-2 finding R2-005):
account-level objects are never cloned, so these statements act on PRODUCTION whatever database the
session uses:

* V002 creates WH_ALFA_ADMIN, sets its STATEMENT_TIMEOUT_IN_SECONDS to 300 (live: 1800) and attaches
  the 30-credit SUSPEND resource monitor OVERWATCH_RM that V045 only detaches 40 files later;
* V006-V008 grant to OVERWATCH_MONITOR / OVERWATCH_OPERATOR, the roles roles.sql retired;
* tasks are defined with ``WAREHOUSE = WH_ALFA_ADMIN`` (production compute), the chain RESUMEs them
  and V158 starts one with EXECUTE TASK. The roots are ``CREATE TASK IF NOT EXISTS`` in V002-V004, so
  in a clone they keep the CLONED definition, whose body still CALLs DBA_MAINT_DB procedures: a resumed
  clone task would write to production.

What the rewrite does, in order:

1. comments out the reviewed account-level top-level statements (``_NEUTRALIZE``), line by line;
2. rewrites the identifier DBA_MAINT_DB, in any letter case, to the clone database, everywhere
   (comments included);
3. rewrites ``WAREHOUSE = WH_ALFA_ADMIN`` (task / dynamic-table compute) to the CI warehouse;
4. keeps every task in the clone suspended: ``ALTER TASK ... RESUME`` becomes ``... SUSPEND`` and
   ``SYSTEM$TASK_DEPENDENTS_ENABLE(x)`` becomes ``TO_VARCHAR(x)`` (both stay valid in any context,
   inside scripting blocks included);
5. refuses -- exit 1, nothing written -- when a copy (``violations``):

   * has a top-level statement whose kind is not on ``_TOP_LEVEL_KINDS``, the ALLOWLIST of the kinds
     the real chain uses (a test replays every migration through it and requires each kind to be used);
   * still names DBA_MAINT_DB in any case, anywhere;
   * carries, in code at any depth (``$$`` bodies included; comments read as whitespace; '...' strings
     ignored, except that a literal handed straight to EXECUTE IMMEDIATE -- the first piece when it is
     concatenated -- is read, recursively, as the SQL it runs), a ``_FORBIDDEN`` form -- GRANT /
     REVOKE, warehouse, resource monitor, account or user CREATE / ALTER / DROP, role, integration,
     share, network policy and other account-object DDL, database DDL, a role or warehouse switch, a
     task start, an external stage or unload -- or a SYSTEM$ function off ``_SYSTEM_FUNCTIONS``;
   * in that code or those literals, creates, alters, drops, writes, CALLs, renames into or USEs an
     object whose database is named and is not the clone (``_TARGET``), or hides a name in
     ``IDENTIFIER('...')``;
   * CALLs SP_NOTIFY_WEBHOOK / SP_DAILY_DIGEST, or uses a SYSTEM$SEND_* primitive, anywhere -- top
     level, in a ``$$`` body, or in any '...' string that could be dynamic SQL (read as written and as
     the SQL it would run), a comment between two tokens included -- except a suspended task's
     top-level ``AS CALL`` body and SYSTEM$SEND_SNOWFLAKE_NOTIFICATION as code in the two procedures'
     own definitions.

   A new migration that uses a new top-level statement kind or any form above is refused until it is
   reviewed here. Nothing else is promised: SQL assembled at run time -- held in a variable, or the
   pieces of a concatenation after its first literal (the send ban above still reads every literal) --
   and what a CALLed procedure reaches through it are beyond a text check and rest on layer 2, the CI
   role's privileges;
6. starts every copy with ``USE SECONDARY ROLES NONE``, so each session holds the CI role's own
   privileges and none a user's default secondary roles would add.

Usage (ci.yml): ``python .github/scripts/snowflake_smoke_rewrite.py --out DIR --clone-db NAME
--ci-warehouse NAME``. Stdlib only.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PROD_DB = "DBA_MAINT_DB"
PROD_WAREHOUSE = "WH_ALFA_ADMIN"
SKIP_MARK = "-- [ci-smoke skipped: account-level] "
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_$]{0,254}")

# Top-level statements the smoke never runs: (why, pattern over the statement's code, upper-cased,
# whitespace collapsed). Each is commented out line by line in the copy.
_NEUTRALIZE: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("warehouse DDL (WH_ALFA_ADMIN is production compute)",
     re.compile(r"^(CREATE|ALTER|DROP)( OR REPLACE)? WAREHOUSE\b")),
    ("resource monitor DDL (owner decision: no resource monitor)",
     re.compile(r"^(CREATE|ALTER|DROP)( OR REPLACE)? RESOURCE MONITOR\b")),
    ("grant to a retired role (roles.sql drops OVERWATCH_MONITOR / OVERWATCH_OPERATOR)",
     re.compile(r"^(GRANT|REVOKE)\b.*\b(TO|FROM) ROLE OVERWATCH_(MONITOR|OPERATOR)\b")),
    ("EXECUTE TASK starts a task run, suspended or not",
     re.compile(r"^EXECUTE TASK\b")),
)

# Anything here that survives the rewrite in CODE (comments and '...' strings ignored, $$ bodies
# included) stops the smoke before it connects to the clone.
_FORBIDDEN: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("production warehouse", re.compile(r"\bWH_ALFA_ADMIN\b", re.I)),
    ("warehouse DDL", re.compile(r"\b(CREATE|ALTER|DROP)\s+(OR\s+REPLACE\s+)?WAREHOUSE\b", re.I)),
    ("resource monitor", re.compile(r"\bRESOURCE\s+MONITOR\b|\bRESOURCE_MONITOR\s*=", re.I)),
    ("account or user change", re.compile(
        r"\b(CREATE|ALTER|DROP|UNDROP)\s+(OR\s+(REPLACE|ALTER)\s+)?(ACCOUNT|USER)\b", re.I)),
    ("integration DDL", re.compile(r"\b(CREATE|ALTER|DROP)\s+(OR\s+REPLACE\s+)?(\w+\s+){0,2}INTEGRATION\b", re.I)),
    ("role DDL", re.compile(r"\b(CREATE|ALTER|DROP)\s+(OR\s+REPLACE\s+)?ROLE\b", re.I)),
    ("grant to a retired role", re.compile(r"\b(TO|FROM)\s+ROLE\s+OVERWATCH_\w+", re.I)),
    ("session role or warehouse switch", re.compile(r"\bUSE\s+(ROLE|WAREHOUSE|SECONDARY\s+ROLES)\b", re.I)),
    ("task or alert start", re.compile(
        r"\bEXECUTE\s+(TASK|ALERT)\b|\bALTER\s+(TASK|ALERT)\b[^;]*?\bRESUME\b|SYSTEM\$TASK_DEPENDENTS_ENABLE",
        re.I)),
    ("grant or revoke", re.compile(r"\b(GRANT|REVOKE)\b", re.I)),
    ("account-level object DDL", re.compile(
        r"\b(CREATE|ALTER|DROP|UNDROP)\s+(OR\s+(REPLACE|ALTER)\s+)?(SHARE|NETWORK\s+(POLICY|RULE)"
        r"|(PASSWORD|SESSION|AUTHENTICATION|PACKAGES)\s+POLICY|(REPLICATION|FAILOVER)\s+GROUP|CONNECTION"
        r"|EXTERNAL\s+VOLUME|COMPUTE\s+POOL|APPLICATION|MANAGED\s+ACCOUNT|DATABASE\s+ROLE|ORGANIZATION)\b", re.I)),
    # CREATE DATABASE is policed by _TARGET (only the clone's own V001 no-op); DROP SCHEMA by _TARGET too.
    ("database DDL or schema move", re.compile(r"\b(ALTER|DROP|UNDROP)\s+DATABASE\b|\bALTER\s+SCHEMA\b", re.I)),
    ("integration reference", re.compile(
        r"\b(STORAGE|API|NOTIFICATION|ERROR|EXTERNAL_ACCESS)_INTEGRATIONS?\s*=", re.I)),
    ("external stage or data unload", re.compile(
        r"\bURL\s*=|\bCREDENTIALS\s*=|\bCOPY\s+INTO\b|\b(PUT|GET)\s+(file:|@)", re.I)),
)

# Top-level statement kinds the replay may run: exactly the kinds the real chain uses (after the rewrite
# and _NEUTRALIZE). Matched against the normalized top-level code (upper-cased, strings and $$ bodies
# blanked, whitespace collapsed, trailing ';' dropped). Anything else is refused.
_TOP_LEVEL_KINDS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("CREATE DATABASE IF NOT EXISTS (V001; the clone already exists)",
     re.compile(r"^CREATE DATABASE IF NOT EXISTS [^ ]+$")),
    ("CREATE SCHEMA IF NOT EXISTS", re.compile(r"^CREATE (TRANSIENT )?SCHEMA IF NOT EXISTS\b")),
    ("CREATE of a schema object", re.compile(
        r"^CREATE (OR REPLACE )?(TRANSIENT |SECURE )?(TABLE|DYNAMIC TABLE|VIEW|FUNCTION|PROCEDURE|TASK|STAGE)\b")),
    ("ALTER TABLE / ALTER TASK", re.compile(r"^ALTER (TABLE|TASK)\b")),
    ("DROP of a schema object", re.compile(r"^DROP (TABLE|DYNAMIC TABLE|PROCEDURE|TASK)\b")),
    ("DML", re.compile(r"^(INSERT (OVERWRITE )?INTO|MERGE INTO|UPDATE|DELETE FROM)\b")),
    ("CALL", re.compile(r"^CALL\b")),
    ("anonymous scripting block", re.compile(r"^EXECUTE IMMEDIATE \$\$ ?\$\$$")),
    ("read", re.compile(r"^(SELECT|WITH|SHOW)\b")),
    ("USE DATABASE / USE SCHEMA", re.compile(r"^USE (DATABASE|SCHEMA)\b")),
)

# SYSTEM$ functions the code may call (case-insensitive). SEND_SNOWFLAKE_NOTIFICATION is further confined to
# the notifier's and the digest's own definitions by the send check.
_SYSTEM_FUNCTIONS = frozenset({"SYSTEM$WAIT", "SYSTEM$SEND_SNOWFLAKE_NOTIFICATION"})
_SYSTEM_FUNCTION = re.compile(r"\bSYSTEM\$\w*", re.I)

# The procedures that send to real notification integrations (account-level objects), and the sends.
_NOTIFIERS = r"(?:SP_NOTIFY_WEBHOOK|SP_DAILY_DIGEST)"
_PART = r'(?:"(?:[^"]|"")+"|[A-Za-z_][A-Za-z0-9_$]*)'
_NAME = rf"{_PART}(?:\s*\.\s*{_PART}){{0,2}}"
_SEND = re.compile(
    rf'\bCALL\s+(?:{_PART}\s*\.\s*){{0,2}}"?{_NOTIFIERS}(?![A-Za-z0-9_$])|\bSYSTEM\$SEND_\w*(?=\s*\()', re.I)
# IDENTIFIER('db.schema.t') hides a name from _TARGET inside a string; the chain never uses it.
_IDENTIFIER_LITERAL = re.compile(r"\bIDENTIFIER\s*\(\s*'", re.I)
# A '...' literal handed straight to EXECUTE IMMEDIATE runs as SQL, so the code checks read it as SQL too.
_EXECUTE_LITERAL = re.compile(r"\bEXECUTE\s+IMMEDIATE\s+(?:\(\s*)?(?=')", re.I)
_TASK_DDL = re.compile(r"^(CREATE (OR REPLACE )?TASK|ALTER TASK) ")
_NOTIFIER_DEFINITION = re.compile(
    rf'^CREATE (OR REPLACE )?PROCEDURE (?:{_PART} ?\. ?){{0,2}}"?{_NOTIFIERS}"? ?\(')

# A named target of DDL, a write, a CALL, a rename/swap or a USE. Its database (by the target's kind:
# the whole name of a DATABASE / USE, the first of two parts of a SCHEMA, the first of three otherwise)
# must be the clone; an unqualified target resolves inside the session's database, which a USE can only
# point at the clone.
_OBJECT_KIND = (
    r"(?:(?:TRANSIENT|TEMPORARY|TEMP|VOLATILE|LOCAL|GLOBAL|SECURE|RECURSIVE|MATERIALIZED|DYNAMIC|EXTERNAL"
    r"|HYBRID|ICEBERG|EVENT)\s+)*"
    r"(?P<kind>DATABASE|SCHEMA|TABLE|VIEW|FUNCTION|PROCEDURE|TASK|STAGE|SEQUENCE|STREAM|PIPE|ALERT"
    r"|FILE\s+FORMAT|TAG|(?:MASKING|ROW\s+ACCESS|AGGREGATION|PROJECTION)\s+POLICY|STREAMLIT|NOTEBOOK"
    r"|SECRET|MODEL)\s+")
_TARGET = re.compile(
    rf"\b(?:(?:CREATE(?:\s+OR\s+(?:REPLACE|ALTER))?|ALTER|DROP|UNDROP)\s+{_OBJECT_KIND}"
    r"|TRUNCATE\s+(?:TABLE\s+)?"
    r"|(?P<use>USE)\s+(?!(?:ROLE|WAREHOUSE|SECONDARY)\b)(?:(?P<use_kind>DATABASE|SCHEMA)\s+)?"
    r"|(?:INTO|UPDATE|DELETE\s+FROM|CALL|RENAME\s+TO|SWAP\s+WITH)\s+)"
    rf"(?:IF\s+(?:NOT\s+)?EXISTS\s+)?(?P<name>{_NAME})", re.I)

_RESUME_TASK = re.compile(r"\b(ALTER\s+TASK\s+(?:IF\s+EXISTS\s+)?[\w$.\"]+\s+)RESUME\b", re.I)
_DEPENDENTS_ENABLE = re.compile(r"SYSTEM\$TASK_DEPENDENTS_ENABLE\s*\(", re.I)
_TASK_WAREHOUSE = re.compile(rf"\bWAREHOUSE(\s*)=(\s*){PROD_WAREHOUSE}\b", re.I)
_PROD_DB = re.compile(rf"\b{PROD_DB}\b", re.I)
_SPAN_OPEN = re.compile(r"--|/\*|\$\$|['\"]")


def _spans(text: str) -> list[tuple[str, int, int]]:
    """Split SQL into (kind, start, end) spans: code, comment, string ('...'), dollar ($$...$$),
    quoted ("..." identifier, treated as code)."""
    out: list[tuple[str, int, int]] = []
    start = 0
    n = len(text)
    while (m := _SPAN_OPEN.search(text, start)) is not None:   # the leftmost opener, tried in this order
        i, two = m.start(), m.group(0)
        if two == "--":
            nl = text.find("\n", i)
            kind, end = "comment", (n if nl < 0 else nl)
        elif two == "/*":
            close = text.find("*/", i + 2)
            kind, end = "comment", (n if close < 0 else close + 2)
        elif two == "$$":
            close = text.find("$$", i + 2)
            kind, end = "dollar", (n if close < 0 else close + 2)
        else:
            kind, end = ("string" if two == "'" else "quoted"), _quoted_end(text, i)
        if start < i:
            out.append(("code", start, i))
        out.append((kind, i, end))
        start = end
    if start < n:
        out.append(("code", start, n))
    return out


def _quoted_end(text: str, i: int) -> int:
    """Just past the '...' string or "..." identifier that opens at ``i`` (the text's end when unclosed)."""
    quote, j, n = text[i], i + 1, len(text)
    while j < n:
        if quote == "'" and text[j] == "\\":
            j += 2
            continue
        if text[j] == quote:
            if text[j + 1:j + 2] == quote:
                j += 2
                continue
            break
        j += 1
    return min(j + 1, n)


def _blank(segment: str) -> str:
    return "\n".join(" " * len(part) for part in segment.split("\n"))


def _interior(literal: str) -> str:
    """The text between a '...' literal's quotes (to its end when unclosed)."""
    return literal[1:-1] if len(literal) >= 2 and literal.endswith("'") else literal[1:]


def literal_sql(interior: str) -> str:
    """A '...' literal's interior as the SQL it holds, same length: each escaped character ('' or a
    backslash pair) becomes a space plus the character, so ``''x''`` reads as `` 'x '``."""
    return re.sub(r"''|\\.", lambda m: " " + m.group(0)[1], interior, flags=re.S)


def code_view(text: str, *, dollar_bodies: bool = True, strings: bool = False, dynamic: bool = False) -> str:
    """``text`` with comments and '...' strings blanked (same length, newlines kept). ``$$`` bodies
    are code-viewed recursively, or blanked when ``dollar_bodies`` is False (the top-level view).
    ``strings=True`` keeps the '...' strings (they can be dynamic SQL); comments are always blanked.
    ``dynamic=True`` (with ``strings``) reads each kept string as the SQL it would run: unescaped by
    ``literal_sql`` and code-viewed in turn, so a comment between two of its tokens is whitespace."""
    parts = []
    for kind, start, end in _spans(text):
        seg = text[start:end]
        if kind == "comment" or (kind == "string" and not strings):
            parts.append(_blank(seg))
        elif kind == "string" and dynamic:
            body = _interior(seg)
            inner = code_view(literal_sql(body), strings=True, dynamic=True)
            parts.append("'" + inner + seg[1 + len(body):])
        elif kind == "dollar":
            body = seg[2:-2] if len(seg) >= 4 and seg.endswith("$$") else seg[2:]
            inner = code_view(body, strings=strings, dynamic=dynamic) if dollar_bodies else _blank(body)
            parts.append("$$" + inner + ("$$" if len(seg) >= 4 and seg.endswith("$$") else ""))
        else:
            parts.append(seg)
    return "".join(parts)


def statements(text: str) -> list[tuple[int, int, str]]:
    """Top-level statements as (start, end, normalized code): ``end`` is just past the ``;``; the code
    is the top-level view (comments, strings and $$ bodies blanked), upper-cased, whitespace collapsed."""
    top = code_view(text, dollar_bodies=False)
    out: list[tuple[int, int, str]] = []
    pos = 0
    for m in re.finditer(";", top):
        seg = top[pos:m.end()]
        lead = len(seg) - len(seg.lstrip())
        if seg.strip(" \t\r\n;"):
            out.append((pos + lead, m.end(), " ".join(seg.split()).upper()))
        pos = m.end()
    if top[pos:].strip():
        seg = top[pos:]
        out.append((pos + len(seg) - len(seg.lstrip()), len(text), " ".join(seg.split()).upper()))
    return out


def _comment_out(text: str, start: int, end: int, why: str) -> str:
    line_start = text.rfind("\n", 0, start) + 1
    if text[line_start:start].strip():
        raise ValueError(f"cannot skip a statement that does not start its line ({why}): "
                         f"{text[start:start + 60]!r}")
    line_end = text.find("\n", end)
    line_end = len(text) if line_end < 0 else line_end
    if code_view(text[end:line_end]).strip():
        raise ValueError(f"cannot skip a statement followed by code on its last line ({why}): "
                         f"{text[start:start + 60]!r}")
    block = text[line_start:line_end]
    commented = "\n".join(SKIP_MARK + line for line in block.split("\n"))
    return text[:line_start] + commented + text[line_end:]


def neutralize(text: str) -> tuple[str, list[str]]:
    """Comment out the account-level top-level statements in ``_NEUTRALIZE``; returns the new text and
    one reason per statement skipped."""
    hits = []
    for start, end, code in statements(text):
        for why, rx in _NEUTRALIZE:
            if rx.search(code):
                hits.append((start, end, why))
                break
    for start, end, why in reversed(hits):          # back to front keeps earlier offsets valid
        text = _comment_out(text, start, end, why)
    return text, [why for _, _, why in hits]


def rewrite(text: str, clone_db: str, ci_warehouse: str) -> tuple[str, list[str]]:
    """The clone-only copy of one SQL file (steps 1-4 of the module docstring)."""
    text, skipped = neutralize(text)
    text = _PROD_DB.sub(clone_db, text)
    text = _TASK_WAREHOUSE.sub(lambda m: f"WAREHOUSE{m.group(1)}={m.group(2)}{ci_warehouse}", text)
    text = _RESUME_TASK.sub(r"\1SUSPEND", text)
    text = _DEPENDENTS_ENABLE.sub("TO_VARCHAR(", text)
    return text, skipped


def top_level_kind(code: str) -> str | None:
    """The ``_TOP_LEVEL_KINDS`` entry a normalized top-level statement (``statements``) is, or None."""
    code = code.rstrip(" ;")
    return next((why for why, rx in _TOP_LEVEL_KINDS if rx.search(code)), None)


def _parts(name: str) -> list[str]:
    return [p.strip() for p in re.findall(_PART, name)]


def _same_db(part: str, clone_db: str) -> bool:
    if part.startswith('"'):
        return part[1:-1].replace('""', '"') == clone_db
    return part.upper() == clone_db.upper()


def _target_db(m: re.Match[str]) -> str | None:
    """The database a ``_TARGET`` match names, or None when it names none (it resolves in the session's)."""
    parts = _parts(m.group("name"))
    kind = " ".join((m.group("kind") or m.group("use_kind") or "").upper().split())
    if kind == "DATABASE" or (m.group("use") and not kind):
        return parts[0]
    if kind == "SCHEMA":
        return parts[0] if len(parts) >= 2 else None
    return parts[0] if len(parts) == 3 else None


def _snip(s: str) -> str:
    return " ".join(s.split())[:80]


def _code_findings(sql: str, clone_db: str) -> list[tuple[int, str, str]]:
    """(offset, what, snippet) for the forms module step 5 refuses in ``sql``'s code at any depth (``$$``
    bodies included, comments and '...' strings ignored), and -- read as the SQL it runs, recursively -- in
    each '...' literal handed straight to EXECUTE IMMEDIATE (the first piece when it is concatenated)."""
    view, kept = code_view(sql), code_view(sql, strings=True)
    found = [(m.start(), what, _snip(m.group(0))) for what, rx in _FORBIDDEN for m in rx.finditer(view)]
    found += [(m.start(), "system function", m.group(0)) for m in _SYSTEM_FUNCTION.finditer(view)
              if m.group(0).upper() not in _SYSTEM_FUNCTIONS]
    found += [(m.start(), "name outside the clone", _snip(m.group(0))) for m in _TARGET.finditer(view)
              if (db := _target_db(m)) is not None and not _same_db(db, clone_db)]
    found += [(m.start(), "IDENTIFIER() of a literal name", _snip(m.group(0)))
              for m in _IDENTIFIER_LITERAL.finditer(kept)]
    for m in _EXECUTE_LITERAL.finditer(kept):
        if view[m.start():m.end()] != kept[m.start():m.end()]:
            continue                                    # the words sit inside a string: prose, not code
        body = _interior(sql[m.end():_quoted_end(sql, m.end())])
        found += [(m.end() + 1 + at, what, snip)
                  for at, what, snip in _code_findings(literal_sql(body), clone_db)]
    return found


def violations(text: str, clone_db: str) -> list[tuple[int, str, str]]:
    """(line, what, snippet) for everything in a rewritten copy the smoke must not run (module step 5)."""

    def line(at: int) -> int:
        return text.count("\n", 0, at) + 1

    found = [(line(m.start()), "production database", m.group(0)) for m in _PROD_DB.finditer(text)]
    found += [(line(at), what, snip) for at, what, snip in _code_findings(text, clone_db)]
    # A statement starts at top-level code and ends past a code ';', so no span crosses its bounds and a
    # slice of a whole-text view is that view of the statement.
    top_view, code_only_view = code_view(text, dollar_bodies=False), code_view(text)
    # every '...' string too, as written and as the SQL it would run: a string can be dynamic SQL
    send_views = (code_view(text, strings=True), code_view(text, strings=True, dynamic=True))
    for start, end, code in statements(text):
        if top_level_kind(code) is None:
            found.append((line(start), "statement kind not on the allowlist", code[:80]))
        top, code_only = top_view[start:end], code_only_view[start:end]
        hits = {m.start(): m for view in send_views for m in _SEND.finditer(view[start:end])}
        for at, m in sorted(hits.items()):
            hit, span = m.group(0), slice(m.start(), m.end())
            if hit.upper().startswith("CALL"):
                # only a task's own top-level AS CALL body (every clone task stays suspended)
                allowed = top[span] == hit and bool(_TASK_DDL.search(code))
            else:
                # only the send primitive as code in the notifier's / the digest's own $$ body
                allowed = (hit.upper() == "SYSTEM$SEND_SNOWFLAKE_NOTIFICATION" and top[span] != hit
                           and code_only[span] == hit and bool(_NOTIFIER_DEFINITION.search(code)))
            if not allowed:
                found.append((line(start + at), "notification send", _snip(hit)))
    return sorted(set(found))


def sources(root: Path = ROOT) -> list[tuple[Path, str]]:
    """(source file, relative output path) for every SQL file the smoke applies."""
    sf = root / "snowflake"
    out = [(p, f"migrations/{p.name}") for p in sorted((sf / "migrations").glob("V[0-9]*.sql"))]
    out += [(sf / name, name) for name in ("validate.sql", "task_audit.sql") if (sf / name).exists()]
    return out


def header(clone_db: str) -> str:
    return (f"-- [ci-smoke] clone-only copy for {clone_db}, written by "
            ".github/scripts/snowflake_smoke_rewrite.py; never apply it anywhere else.\n"
            "USE SECONDARY ROLES NONE;\n")


def _check_identifier(name: str, what: str, prod: str) -> None:
    if not _IDENT.fullmatch(name):
        raise SystemExit(f"FATAL: {what} {name!r} is not a plain Snowflake identifier.")
    if name.upper() == prod:
        raise SystemExit(f"FATAL: {what} must not be the production {prod}.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--clone-db", required=True)
    parser.add_argument("--ci-warehouse", required=True)
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args(argv)
    _check_identifier(args.clone_db, "--clone-db", PROD_DB)
    _check_identifier(args.ci_warehouse, "--ci-warehouse", PROD_WAREHOUSE)

    copies: list[tuple[str, str]] = []
    problems: list[str] = []
    skipped = 0
    for src, rel in sources(args.root):
        try:
            text, why = rewrite(src.read_text(encoding="utf-8"), args.clone_db, args.ci_warehouse)
        except ValueError as exc:
            problems.append(f"{rel}: {exc}")
            continue
        skipped += len(why)
        problems += [f"{rel}:{line}: {what}: {snippet}"
                     for line, what, snippet in violations(text, args.clone_db)]
        copies.append((rel, text))
    if problems:
        print("FATAL: the smoke would run account-level SQL against production; nothing was written:",
              file=sys.stderr)
        for problem in problems:
            print(f"  {problem}", file=sys.stderr)
        print("Review the statement, then neutralize it in .github/scripts/snowflake_smoke_rewrite.py "
              "(_NEUTRALIZE), allow a new statement kind (_TOP_LEVEL_KINDS) only when it stays inside the "
              "clone, or keep it out of the replay.", file=sys.stderr)
        return 1
    for rel, text in copies:
        dest = args.out / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(header(args.clone_db) + text, encoding="utf-8", newline="\n")
    print(f"Rewrote {len(copies)} files onto {args.clone_db} (compute {args.ci_warehouse}); "
          f"skipped {skipped} account-level statements; every task stays suspended.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
