"""Rewrite the SQL the opt-in ``snowflake-smoke`` job applies, so the replay can only touch its clone.

This is layer 1 of the ISOLATION CONTRACT in .github/workflows/ci.yml. Copying the migrations and
swapping the identifier DBA_MAINT_DB for the clone's name is not enough (round-2 finding R2-005):
account-level objects are never cloned, so these statements act on PRODUCTION whatever database the
session uses:

* V002 creates WH_ALFA_ADMIN, sets its STATEMENT_TIMEOUT_IN_SECONDS to 300 (live: 1800) and attaches
  the 30-credit SUSPEND resource monitor OVERWATCH_RM that V045 only detaches 40 files later;
* V006-V008 grant to OVERWATCH_MONITOR / OVERWATCH_OPERATOR, the roles roles.sql retired;
* V175 grants USAGE on SP_ADMIN_ROLE_MEMBERS() to SNOW_SYSADMINS, a production account role (the
  clone's copy needs no grant: nothing in the smoke CALLs it as SNOW_SYSADMINS);
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
   * outside a ``$$`` body, holds anything the client (``snow sql``) that splits the file into
     statements reads differently from Snowflake (and this guard): a ``//`` comment, a backslash in a
     "quoted" name, a line that starts with ``!`` (a client command such as ``!source``), or a ``/* */``
     comment with no space on either side (the client deletes comments, joining the two tokens);
   * anywhere, holds client template syntax -- ``<%``, ``{#``, ``&{``, ``&name`` or ``&&`` -- which
     ``snow sql`` renders (``ctx.env`` reads the job's environment) before Snowflake sees the SQL;
   * in any '...' string, at any depth and at every level of a string inside a string, holds a
     backslash escape other than ``\\'``, ``\\"``, ``\\\\``, ``\\t`` and ``\\n`` (those, and ``''``, it
     decodes exactly as Snowflake does; Snowflake drops the backslash of most others, so ``\\_`` is
     ``_``), since a string can run as SQL;
   * still names DBA_MAINT_DB in any case, anywhere;
   * carries, in code at any depth (``$$`` bodies included; ``--``, ``//`` and ``/* */`` comments read
     as whitespace, as Snowflake reads them; '...' strings ignored, except that a literal that runs as
     SQL -- handed straight to EXECUTE IMMEDIATE, the first piece when it is concatenated, or given as
     a body after ``AS`` -- is read, recursively, as the SQL it runs), a ``_FORBIDDEN`` form -- GRANT /
     REVOKE, warehouse, resource monitor, account or user CREATE / ALTER / DROP, role, integration,
     share, network policy and other account-object DDL, database DDL, a role or warehouse switch, a
     task or alert start or any other EXECUTE but IMMEDIATE, an external stage, unload, file copy or
     stage file removal, a body in a language other than SQL (its comments and strings follow other
     rules) -- or a SYSTEM$ function off ``_SYSTEM_FUNCTIONS``;
   * in that code or those literals: creates, alters, drops, comments on, writes, CALLs, renames into
     or USEs an object whose database is named and is not the clone (``_TARGET``: a name in any form --
     quoted or not, with or without a space after the keyword, ``db..object`` for db.PUBLIC.object, or
     a name ending in a dot that a concatenation completes at run time); runs a CREATE / ALTER / DROP /
     UNDROP / COMMENT ON of a kind ``_TARGET`` cannot read the name of (anything off ``_OBJECT_KIND``
     but an ALTER TABLE sub-clause, ALTER SESSION and MODEL MONITOR included), or one whose name is
     followed straight by a qualified name (a kind of two words whose first is a listed kind, where
     ``_TARGET`` would read the second word as the name); uses ``IDENTIFIER(...)`` whatever its
     argument (in any '...' string too, around a literal, a bind or a variable), or ``TABLE(...)``
     around a literal, a bind or a variable (in code or in any string); or hands EXECUTE IMMEDIATE
     anything but a literal, a ``$$`` block or a ``:variable``;
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
    ("grant to the production role SNOW_SYSADMINS (V175: USAGE on SP_ADMIN_ROLE_MEMBERS only)",
     re.compile(r"^GRANT USAGE ON PROCEDURE DBA_MAINT_DB\.OVERWATCH\.SP_ADMIN_ROLE_MEMBERS\(\)"
                r" TO ROLE SNOW_SYSADMINS ?;?$")),
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
    ("grant to a retired role", re.compile(r'\b(TO|FROM)\s+ROLE(\s+|(?="))"?OVERWATCH_\w+', re.I)),
    ("session role or warehouse switch", re.compile(r"\bUSE\s+(ROLE|WAREHOUSE|SECONDARY\s+ROLES)\b", re.I)),
    ("task or alert start", re.compile(
        r"\bEXECUTE\s+(TASK|ALERT)\b|\bALTER\s+(TASK|ALERT)\b[^;]*?\bRESUME\b|SYSTEM\$TASK_DEPENDENTS_ENABLE",
        re.I)),
    # a notebook, a service job, a dbt project ...: only EXECUTE IMMEDIATE (checked below) and EXECUTE AS remain
    ("EXECUTE of a runnable other than a block", re.compile(
        r"\bEXECUTE\s+(?!(IMMEDIATE|AS|TASK|ALERT)\b)[^\W\d]", re.I)),
    # its comments and strings follow other rules than the SQL this guard reads (Python's # it's ...)
    ("body in a language other than SQL", re.compile(r"\bLANGUAGE\s+(?!SQL\b)[^\W\d]", re.I)),
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
        r"\bURL\s*=|\bCREDENTIALS\s*=|\bCOPY\s+(FILES\s+)?INTO\b|\b(PUT|GET)\s+(file:|@)", re.I)),
)
# REMOVE / RM of staged files ('@...' may be quoted, so this one reads the strings-kept view; see _code_findings)
_STAGE_REMOVE = re.compile(r"\b(?:REMOVE|RM)\s+['\"]?@", re.I)

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
# One part of a name, "quoted" or not (an unquoted one may hold letters outside ASCII), and a name of up to
# three parts. A part may be empty: `db..t` is Snowflake's db.PUBLIC.t, and a name that ends in a dot (the
# first piece of a concatenation) is completed at run time.
_PART = r'(?:"(?:[^"]|"")+"|[^\W\d][\w$]*)'
_NAME = rf"{_PART}(?:\s*\.\s*(?:{_PART})?){{0,2}}"
_NAME_TOKEN = re.compile(rf"{_PART}|\.")
# Between a keyword and the name after it: whitespace, or none at all before a "quoted" name.
_SEP = r'(?:\s+|(?="))'
_SEND = re.compile(
    rf'\bCALL{_SEP}(?:(?:{_PART})?\s*\.\s*){{0,2}}"?{_NOTIFIERS}(?![A-Za-z0-9_$])|\bSYSTEM\$SEND_\w*(?=\s*\()',
    re.I)
# IDENTIFIER(...) turns a value -- a '...' or $$...$$ literal, a bind, a variable -- into a name _TARGET cannot
# read, so in code it is refused whatever its argument (the chain never uses it); in a '...' string, which
# can be dynamic SQL, when it wraps a literal, a bind or a variable (prose such as "identifier (e.g." is not).
_IDENTIFIER = re.compile(r"\bIDENTIFIER\s*\(", re.I)
_IDENTIFIER_OF_VALUE = re.compile(r"\bIDENTIFIER\s*\(\s*['$?:]", re.I)
# TABLE('db.schema.t') is IDENTIFIER's twin for a table name; TABLE() may wrap only a table function call.
_TABLE_OF_VALUE = re.compile(r"\bTABLE\s*\(\s*['$?:]", re.I)
# A '...' literal that runs as SQL -- handed straight to EXECUTE IMMEDIATE, or a body given after AS -- is
# read as SQL by the code checks too.
_SQL_LITERAL = re.compile(r"\b(?:EXECUTE\s+IMMEDIATE\s*(?:\(\s*)*|AS\s*)(?=')", re.I)
# What EXECUTE IMMEDIATE may run: a literal or a $$ block (both read), or a :variable (run time, layer 2).
_EXECUTE_IMMEDIATE = re.compile(r"\bEXECUTE\s+IMMEDIATE\b", re.I)
_READABLE_SQL = re.compile(r"\s*(?:\(\s*)*(?:'|\$\$|:[^\W\d])")
_TASK_DDL = re.compile(r"^(CREATE (OR REPLACE )?TASK|ALTER TASK) ")
# (an exemption, so it keeps the narrower ASCII name part: a wider one would only widen what it exempts)
_NOTIFIER_DEFINITION = re.compile(
    rf'^CREATE (OR REPLACE )?PROCEDURE (?:(?:"(?:[^"]|"")+"|[A-Za-z_][A-Za-z0-9_$]*) ?\. ?){{0,2}}"?{_NOTIFIERS}"? ?\(')

# A named target of DDL, a COMMENT ON, a write, a CALL, a rename/swap or a USE. Its database (by the
# target's kind: the whole name of a DATABASE / USE, the first of two parts of a SCHEMA, the first of three
# otherwise) must be the clone; an unqualified target resolves inside the session's database, which a USE
# can only point at the clone.
_DDL_VERB = r"CREATE(?:\s+OR\s+(?:REPLACE|ALTER))?|ALTER|DROP|UNDROP|COMMENT(?:\s+IF\s+EXISTS)?\s+ON"
_OBJECT_KIND = (
    r"(?:(?:TRANSIENT|TEMPORARY|TEMP|VOLATILE|LOCAL|GLOBAL|SECURE|RECURSIVE|MATERIALIZED|DYNAMIC|EXTERNAL"
    r"|HYBRID|ICEBERG|EVENT)\s+)*"
    r"(?P<kind>DATABASE|SCHEMA|TABLE|VIEW|FUNCTION|PROCEDURE|TASK|STAGE|SEQUENCE|STREAM|PIPE|ALERT"
    r"|FILE\s+FORMAT|TAG|(?:MASKING|ROW\s+ACCESS|AGGREGATION|PROJECTION)\s+POLICY|STREAMLIT|NOTEBOOK"
    rf"|SECRET|MODEL(?!\s+MONITOR\b)){_SEP}")
_TARGET = re.compile(
    rf"\b(?:(?:{_DDL_VERB})\s+{_OBJECT_KIND}"
    rf"|TRUNCATE(?:\s+(?:TABLE|MATERIALIZED\s+VIEW))?{_SEP}"
    rf"|(?P<use>USE){_SEP}(?!(?:ROLE|WAREHOUSE|SECONDARY)\b)(?:(?P<use_kind>DATABASE|SCHEMA){_SEP})?"
    rf"|(?:INTO|UPDATE|DELETE\s+FROM|CALL|RENAME\s+TO|SWAP\s+WITH){_SEP})"
    rf"(?:IF\s+(?:NOT\s+)?EXISTS{_SEP})?(?P<name>{_NAME})", re.I)
# The inverse: every CREATE / ALTER / DROP / UNDROP / COMMENT ON in code is of an _OBJECT_KIND kind (so _TARGET
# reads its name), a form _FORBIDDEN refuses, or an ALTER TABLE sub-clause (never a statement of its own).
# Anything else -- DATA METRIC FUNCTION, SEMANTIC VIEW, SERVICE, a class instance such as SNOWFLAKE.ML.FORECAST,
# ALTER SESSION, MODEL MONITOR -- is refused until it is reviewed here.
_DDL = re.compile(rf"\b(?:{_DDL_VERB})\b", re.I)
_DDL_KIND = re.compile(rf"\s+{_OBJECT_KIND}", re.I)
# A kind of two words whose first is a listed kind word (MODEL MONITOR before it was named above, or any added
# later) has _TARGET read its second word as the name: a DDL name followed straight by a qualified one is refused.
_NAME_AFTER_NAME = re.compile(rf"\s*(?:IF\s+(?:NOT\s+)?EXISTS{_SEP})?{_PART}\s*\.", re.I)
_ALTER_SUBCLAUSE = re.compile(
    r"(?:ALTER|DROP)\s+COLUMN\b|DROP\s+(?:CONSTRAINT|DEFAULT|NOT\s+NULL|PRIMARY\s+KEY|UNIQUE|FOREIGN\s+KEY"
    r"|CLUSTERING\s+KEY|SEARCH\s+OPTIMIZATION)\b", re.I)

_RESUME_TASK = re.compile(r"\b(ALTER\s+TASK\s+(?:IF\s+EXISTS\s+)?[\w$.\"]+\s+)RESUME\b", re.I)
_DEPENDENTS_ENABLE = re.compile(r"SYSTEM\$TASK_DEPENDENTS_ENABLE\s*\(", re.I)
_TASK_WAREHOUSE = re.compile(rf"\bWAREHOUSE(\s*)=(\s*){PROD_WAREHOUSE}\b", re.I)
_PROD_DB = re.compile(rf"\b{PROD_DB}\b", re.I)
_SPAN_OPEN = re.compile(r"--|//|/\*|\$\$|['\"]")
# A client command line (!source, !load ... of a file or a URL): the client acts on it and Snowflake never
# sees it.
_CLIENT_COMMAND = re.compile(r"^!", re.M)
# Escapes in a '...' string. literal_sql decodes these exactly as Snowflake does. Snowflake decodes \xhh,
# \uhhhh, octal, \b, \f and \r to characters too, and drops the backslash of any other (\_ is _, \A is A), so
# a string that can run as SQL could spell a quote, a ; or DBA_MAINT\_DB in a way no check reads; every escape
# but these is refused in every string, at every level of a string inside a string (``_unread_escapes``).
# (\r is refused, not decoded: whether a carriage return ends a -- comment is the reader's own rule.)
_DECODED_ESCAPE = {"''": "'", "\\'": "'", '\\"': '"', "\\\\": "\\", "\\t": "\t", "\\n": "\n"}
_ESCAPE = re.compile(r"\\.", re.S)
# Client templating: snow sql renders <% %> / &{ } / SnowSQL &name (&& an escape) and drops Jinja {# #}
# comments in every statement, strings and $$ bodies included, before Snowflake sees it; ctx.env.* reads the
# job's environment (SNOWFLAKE_DATABASE is DBA_MAINT_DB there).
_CLIENT_TEMPLATE = re.compile(r"<%|\{#|&[&{A-Za-z_]")


def _spans(text: str) -> list[tuple[str, int, int]]:
    """Split SQL into (kind, start, end) spans: code, comment (``--`` or ``//`` to the line's end, or
    ``/* */``), string ('...'), dollar ($$...$$), quoted ("..." identifier, treated as code)."""
    out: list[tuple[str, int, int]] = []
    start = 0
    n = len(text)
    while (m := _SPAN_OPEN.search(text, start)) is not None:   # the leftmost opener, tried in this order
        i, two = m.start(), m.group(0)
        if two in ("--", "//"):
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
    """A '...' literal's interior as the SQL it holds, decoded as Snowflake decodes it -- ``''`` and \\'
    to a quote, \\" to a double quote, \\\\ to a backslash, \\t to a tab, \\n to a line break, and any
    other escape to its character (Snowflake's rule for all but \\x, \\u, octal, \\b, \\f and \\r; every
    one of these others is refused by ``_unread_escapes``) -- then padded with spaces at its end to the
    interior's length. Decoded characters stay side by side, so a quote run such as ``''''`` reads as the
    ``''`` Snowflake reads one level down, never as two strings; offsets inside it are approximate (they
    serve the line of a finding only)."""
    decoded = re.sub(r"''|\\.", lambda m: _DECODED_ESCAPE.get(m.group(0), m.group(0)[1]), interior,
                     flags=re.S)
    return decoded + " " * (len(interior) - len(decoded))


def _unread_escapes(sql: str) -> list[int]:
    """Offsets in ``sql`` of the backslash escapes ``literal_sql`` does not decode as Snowflake does (all
    but ``_DECODED_ESCAPE``), in every '...' string at any depth (``$$`` bodies included) and -- each string
    read as the SQL it would run -- in the strings inside it, at every level."""
    found: list[int] = []
    for kind, start, end in _spans(sql):
        seg = sql[start:end]
        if kind == "string":
            body = _interior(seg)
            found += [start + 1 + m.start() for m in _ESCAPE.finditer(body) if m.group(0) not in _DECODED_ESCAPE]
            found += [start + 1 + at for at in _unread_escapes(literal_sql(body))]
        elif kind == "dollar":
            body = seg[2:-2] if len(seg) >= 4 and seg.endswith("$$") else seg[2:]
            found += [start + 2 + at for at in _unread_escapes(body)]
    return found


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
    """A ``_NAME``'s parts, an empty one kept: ``db..t`` is ['db', '', 't'], ``db.`` is ['db', '']."""
    parts = [""]
    for token in _NAME_TOKEN.findall(name):
        if token == ".":
            parts.append("")
        else:
            parts[-1] = token
    return parts


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
    # db.schema.object or db..object; or db. / db.schema. completed at run time, where the first part may
    # be the database
    return parts[0] if len(parts) == 3 or parts[-1] == "" else None


def _snip(s: str) -> str:
    return " ".join(s.split())[:80]


def _code_findings(sql: str, clone_db: str) -> list[tuple[int, str, str]]:
    """(offset, what, snippet) for the forms module step 5 refuses in ``sql``'s code at any depth (``$$``
    bodies included, comments and '...' strings ignored), and -- read as the SQL it runs, recursively -- in
    each '...' literal that runs as SQL (handed straight to EXECUTE IMMEDIATE, the first piece when it is
    concatenated, or a body given after AS)."""
    view, kept = code_view(sql), code_view(sql, strings=True)
    dynamic = code_view(sql, strings=True, dynamic=True)

    def in_code(at: int) -> bool:                       # the words at ``at`` are code, not inside a string
        return view[at] == kept[at]

    found = [(m.start(), what, _snip(m.group(0))) for what, rx in _FORBIDDEN for m in rx.finditer(view)]
    found += [(m.start(), "stage file removal", _snip(m.group(0))) for m in _STAGE_REMOVE.finditer(kept)
              if in_code(m.start())]
    found += [(m.start(), "system function", m.group(0)) for m in _SYSTEM_FUNCTION.finditer(view)
              if m.group(0).upper() not in _SYSTEM_FUNCTIONS]
    targets = list(_TARGET.finditer(view))
    found += [(m.start(), "name outside the clone", _snip(m.group(0))) for m in targets
              if (db := _target_db(m)) is not None and not _same_db(db, clone_db)]
    found += [(m.start(), "DDL of an unreviewed kind", _snip(view[m.start():m.end() + 40]))
              for m in _DDL.finditer(view)
              if not (_DDL_KIND.match(view, m.end()) or _ALTER_SUBCLAUSE.match(view, m.start())
                      or any(rx.match(view, m.start()) for _what, rx in _FORBIDDEN))]
    found += [(m.start(), "DDL of an unreviewed kind", _snip(view[m.start():m.end() + 40]))
              for m in targets if m.group("kind") and _NAME_AFTER_NAME.match(view, m.end())]
    # in code; and in any string, as written and as the SQL it would run (a string can be dynamic SQL)
    idents = {m.start(): _snip(m.group(0)) for m in _IDENTIFIER.finditer(view)}
    idents |= {m.start(): _snip(m.group(0)) for v in (kept, dynamic) for m in _IDENTIFIER_OF_VALUE.finditer(v)
               if m.start() not in idents}
    found += [(at, "IDENTIFIER() name", snip) for at, snip in idents.items()]
    tables = {m.start(): _snip(m.group(0)) for v in (kept, dynamic) for m in _TABLE_OF_VALUE.finditer(v)}
    found += [(at, "TABLE() name", snip) for at, snip in tables.items()]
    found += [(m.start(), "EXECUTE IMMEDIATE of an unread expression", _snip(kept[m.start():m.end() + 30]))
              for m in _EXECUTE_IMMEDIATE.finditer(view) if not _READABLE_SQL.match(kept, m.end())]
    for m in _SQL_LITERAL.finditer(kept):
        if not in_code(m.start()):
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
    # The client (snow sql -> the connector's split_statements) cuts the file into statements by its own
    # reading, which differs from Snowflake's -- and so from this guard's -- outside a $$ body: it reads no
    # // comment, it takes a backslash in a "quoted" name as an escape, it acts on a `!` command line, and
    # it deletes a /* */ comment outright, so one with no space on either side joins two tokens
    # (DBA_MAINT/**/_DB). Each could hand Snowflake SQL none of these checks read, so each is refused;
    # without them the client's statements are exactly ``statements()``, as Snowflake reads them.
    for kind, start, end in _spans(text):
        if kind == "comment" and text.startswith("//", start):
            found.append((line(start), "// comment outside a $$ body", _snip(text[start:end])))
        elif (kind == "comment" and text.startswith("/*", start) and start > 0 and end < len(text)
              and not text[start - 1].isspace() and not text[end].isspace()):
            found.append((line(start), "/* */ comment joining two tokens outside a $$ body",
                          _snip(text[max(0, start - 20):end + 20])))
        elif kind == "quoted" and "\\" in text[start:end]:
            found.append((line(start), "backslash in a quoted name outside a $$ body", _snip(text[start:end])))
    found += [(line(m.start()), "client command line", _snip(text[m.start():].split("\n", 1)[0]))
              for m in _CLIENT_COMMAND.finditer(top_view)]
    found += [(line(m.start()), "client template syntax", _snip(text[m.start():m.start() + 40]))
              for m in _CLIENT_TEMPLATE.finditer(text)]
    found += [(line(at), "string escape the guard cannot decode", _snip(text[at:at + 12]))
              for at in _unread_escapes(text)]
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
