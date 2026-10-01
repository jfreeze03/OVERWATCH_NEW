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
2. rewrites the identifier DBA_MAINT_DB to the clone database, everywhere (comments included);
3. rewrites ``WAREHOUSE = WH_ALFA_ADMIN`` (task / dynamic-table compute) to the CI warehouse;
4. keeps every task in the clone suspended: ``ALTER TASK ... RESUME`` becomes ``... SUSPEND`` and
   ``SYSTEM$TASK_DEPENDENTS_ENABLE(x)`` becomes ``TO_VARCHAR(x)`` (both stay valid in any context,
   inside scripting blocks included);
5. refuses -- exit 1, nothing written -- when anything account-level survives in the code (comments
   and single-quoted strings are ignored, ``$$`` bodies are code). A new migration that touches
   account-level state must be reviewed here before the smoke can run it;
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
    ("account or user change", re.compile(r"\bALTER\s+(ACCOUNT|USER)\b", re.I)),
    ("integration DDL", re.compile(r"\b(CREATE|ALTER|DROP)\s+(OR\s+REPLACE\s+)?(\w+\s+){0,2}INTEGRATION\b", re.I)),
    ("role DDL", re.compile(r"\b(CREATE|ALTER|DROP)\s+(OR\s+REPLACE\s+)?ROLE\b", re.I)),
    ("grant to a retired role", re.compile(r"\b(TO|FROM)\s+ROLE\s+OVERWATCH_\w+", re.I)),
    ("session role or warehouse switch", re.compile(r"\bUSE\s+(ROLE|WAREHOUSE|SECONDARY\s+ROLES)\b", re.I)),
    ("task or alert start", re.compile(
        r"\bEXECUTE\s+(TASK|ALERT)\b|\bALTER\s+(TASK|ALERT)\b[^;]*?\bRESUME\b|SYSTEM\$TASK_DEPENDENTS_ENABLE",
        re.I)),
)
# Top-level CALLs that send to real notification integrations (they are account-level objects).
_FORBIDDEN_CALL = re.compile(r"^CALL \S*\b(SP_NOTIFY_WEBHOOK|SP_DAILY_DIGEST)\b")

_RESUME_TASK = re.compile(r"\b(ALTER\s+TASK\s+(?:IF\s+EXISTS\s+)?[\w$.\"]+\s+)RESUME\b", re.I)
_DEPENDENTS_ENABLE = re.compile(r"SYSTEM\$TASK_DEPENDENTS_ENABLE\s*\(", re.I)
_TASK_WAREHOUSE = re.compile(rf"\bWAREHOUSE(\s*)=(\s*){PROD_WAREHOUSE}\b", re.I)
_PROD_DB = re.compile(rf"\b{PROD_DB}\b")


def _spans(text: str) -> list[tuple[str, int, int]]:
    """Split SQL into (kind, start, end) spans: code, comment, string ('...'), dollar ($$...$$),
    quoted ("..." identifier, treated as code)."""
    out: list[tuple[str, int, int]] = []
    i = start = 0
    n = len(text)
    while i < n:
        two = text[i:i + 2]
        if two == "--":
            nl = text.find("\n", i)
            kind, end = "comment", (n if nl < 0 else nl)
        elif two == "/*":
            close = text.find("*/", i + 2)
            kind, end = "comment", (n if close < 0 else close + 2)
        elif two == "$$":
            close = text.find("$$", i + 2)
            kind, end = "dollar", (n if close < 0 else close + 2)
        elif text[i] in "'\"":
            quote, j = text[i], i + 1
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
            kind, end = ("string" if quote == "'" else "quoted"), min(j + 1, n)
        else:
            i += 1
            continue
        if start < i:
            out.append(("code", start, i))
        out.append((kind, i, end))
        i = start = end
    if start < n:
        out.append(("code", start, n))
    return out


def _blank(segment: str) -> str:
    return re.sub(r"[^\n]", " ", segment)


def code_view(text: str, *, dollar_bodies: bool = True) -> str:
    """``text`` with comments and '...' strings blanked (same length, newlines kept). ``$$`` bodies
    are code-viewed recursively, or blanked when ``dollar_bodies`` is False (the top-level view)."""
    parts = []
    for kind, start, end in _spans(text):
        seg = text[start:end]
        if kind in ("comment", "string"):
            parts.append(_blank(seg))
        elif kind == "dollar":
            body = seg[2:-2] if len(seg) >= 4 and seg.endswith("$$") else seg[2:]
            inner = code_view(body) if dollar_bodies else _blank(body)
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


def violations(text: str) -> list[tuple[int, str, str]]:
    """(line, what, snippet) for everything account-level left in a rewritten copy."""
    found = [(text.count("\n", 0, m.start()) + 1, "production database", m.group(0))
             for m in _PROD_DB.finditer(text)]
    view = code_view(text)
    found += [(view.count("\n", 0, m.start()) + 1, what, " ".join(m.group(0).split())[:80])
              for what, rx in _FORBIDDEN for m in rx.finditer(view)]
    found += [(text.count("\n", 0, start) + 1, "notification send", code[:80])
              for start, _end, code in statements(text) if _FORBIDDEN_CALL.search(code)]
    return sorted(found)


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
        problems += [f"{rel}:{line}: {what}: {snippet}" for line, what, snippet in violations(text)]
        copies.append((rel, text))
    if problems:
        print("FATAL: the smoke would run account-level SQL against production; nothing was written:",
              file=sys.stderr)
        for problem in problems:
            print(f"  {problem}", file=sys.stderr)
        print("Review the statement, then neutralize it in .github/scripts/snowflake_smoke_rewrite.py "
              "(_NEUTRALIZE) or keep it out of the replay.", file=sys.stderr)
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
