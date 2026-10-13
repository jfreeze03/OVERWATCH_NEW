"""A small Snowflake-aware SQL scanner for the guard rules.

It is deliberately NOT a parser: it splits a script into top-level statements the way Flyway's Snowflake parser
does (``;`` outside comments, strings, quoted identifiers and ``$$`` bodies) and gives every statement three views
of the same text, each the same length as the original so offsets and line numbers line up:

* ``raw``     the statement exactly as written;
* ``code``    comments blanked (``--``, ``//`` and ``/* */``), strings and ``$$`` bodies kept;
* ``masked``  comments, string contents, quoted-identifier contents and ``$$`` bodies all blanked -- what the
              statement *is* (its keywords, names and clauses), without anything it merely carries.

Blanking keeps newlines, so ``line_of`` works on any view. ``bodies`` holds the ``$$ ... $$`` contents (comments
blanked) for rules that must look inside procedure / function / scripting bodies.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_WS = re.compile(r"\s+")


@dataclass(frozen=True)
class Body:
    """One ``$$ ... $$`` block: its text (comments blanked) and where it starts in the file."""

    text: str
    offset: int


@dataclass(frozen=True)
class Statement:
    raw: str
    code: str
    masked: str
    offset: int  # character offset of the statement in the file
    line: int  # 1-based line of the first non-blank character
    bodies: tuple[Body, ...] = field(default_factory=tuple)

    @property
    def head(self) -> str:
        """The masked statement collapsed to single spaces and upper-cased: the form rules match keywords on."""
        return _WS.sub(" ", self.masked).strip().upper()


@dataclass(frozen=True)
class Script:
    text: str
    code: str  # whole file, comments blanked
    masked: str  # whole file, comments + strings + bodies blanked
    statements: tuple[Statement, ...]
    unterminated: str | None  # "string", "identifier", "comment" or "dollar" when the file ends inside one

    def line_of(self, offset: int) -> int:
        return self.text.count("\n", 0, offset) + 1


def _blank(s: str) -> str:
    return "".join("\n" if ch == "\n" else " " for ch in s)


def scan(text: str) -> Script:
    """Scan ``text`` once, producing the code / masked views and the statement list."""
    n = len(text)
    code = list(text)
    masked = list(text)
    bodies: list[Body] = []
    cuts: list[int] = []  # offsets of top-level ';'
    unterminated: str | None = None
    i = 0
    while i < n:
        ch = text[i]
        nxt = text[i + 1] if i + 1 < n else ""
        if (ch == "-" and nxt == "-") or (ch == "/" and nxt == "/"):
            j = text.find("\n", i)
            j = n if j < 0 else j
            for k in range(i, j):
                code[k] = masked[k] = " "
            i = j
            continue
        if ch == "/" and nxt == "*":
            j = text.find("*/", i + 2)
            if j < 0:
                unterminated, j = "comment", n
            else:
                j += 2
            for k in range(i, j):
                if text[k] != "\n":
                    code[k] = masked[k] = " "
            i = j
            continue
        if ch == "$" and nxt == "$":
            j = text.find("$$", i + 2)
            if j < 0:
                unterminated, j = "dollar", n - 2
            body_text = scan_body(text[i + 2 : j])
            bodies.append(Body(body_text, i + 2))
            for k in range(i + 2, j):
                code[k] = body_text[k - i - 2]
                masked[k] = "\n" if text[k] == "\n" else " "
            i = j + 2
            continue
        if ch == "'":
            j = i + 1
            while j < n:
                if text[j] == "\\":
                    j += 2
                    continue
                if text[j] == "'":
                    if j + 1 < n and text[j + 1] == "'":
                        j += 2
                        continue
                    break
                j += 1
            if j >= n:
                unterminated, j = "string", n - 1
            for k in range(i + 1, j):
                masked[k] = "\n" if text[k] == "\n" else " "
            i = j + 1
            continue
        if ch == '"':
            j = text.find('"', i + 1)
            while j >= 0 and j + 1 < n and text[j + 1] == '"':
                j = text.find('"', j + 2)
            if j < 0:
                unterminated, j = "identifier", n - 1
            # quoted identifiers stay visible in `masked` (they are names), with blanks kept as written
            i = j + 1
            continue
        if ch == ";":
            cuts.append(i)
        i += 1

    code_s, masked_s = "".join(code), "".join(masked)
    statements: list[Statement] = []
    start = 0
    for cut in [*cuts, n]:
        seg_masked = masked_s[start:cut]
        if seg_masked.strip():
            lead = len(seg_masked) - len(seg_masked.lstrip())
            off = start + lead
            stmt_bodies = tuple(b for b in bodies if start <= b.offset < cut)
            statements.append(
                Statement(
                    raw=text[off:cut],
                    code=code_s[off:cut],
                    masked=masked_s[off:cut],
                    offset=off,
                    line=text.count("\n", 0, off) + 1,
                    bodies=stmt_bodies,
                )
            )
        start = cut + 1
    return Script(text, code_s, masked_s, tuple(statements), unterminated)


def scan_body(body: str) -> str:
    """Blank SQL comments inside a ``$$`` body (strings kept); used for body-level rules."""
    out = list(body)
    n, i = len(body), 0
    while i < n:
        ch = body[i]
        nxt = body[i + 1] if i + 1 < n else ""
        if (ch == "-" and nxt == "-") or (ch == "/" and nxt == "/"):
            j = body.find("\n", i)
            j = n if j < 0 else j
            for k in range(i, j):
                out[k] = " "
            i = j
            continue
        if ch == "/" and nxt == "*":
            j = body.find("*/", i + 2)
            j = n if j < 0 else j + 2
            for k in range(i, j):
                if body[k] != "\n":
                    out[k] = " "
            i = j
            continue
        if ch == "'":
            j = i + 1
            while j < n:
                if body[j] == "\\":
                    j += 2
                    continue
                if body[j] == "'":
                    if j + 1 < n and body[j + 1] == "'":
                        j += 2
                        continue
                    break
                j += 1
            i = j + 1
            continue
        i += 1
    return "".join(out)


# --------------------------------------------------------------------------- object names

_IDENT = r'(?:"(?:[^"]|"")+"|\$\{[A-Za-z0-9_:.]+\}|[A-Za-z_][A-Za-z0-9_$]*)'
NAME_RE = re.compile(rf"{_IDENT}(?:\s*\.\s*{_IDENT}){{0,2}}")


def normalize_name(name: str) -> str:
    """Canonical form of a (possibly quoted, possibly ${edw_db}-prefixed) object name: SCHEMA.OBJECT, upper-case
    for unquoted parts, quoted parts kept verbatim without their quotes."""
    parts = [p.strip() for p in re.findall(_IDENT, name)]
    parts = [p for p in parts if not (p.startswith("${") and p.endswith("}"))]
    return ".".join(p[1:-1].replace('""', '"') if p.startswith('"') else p.upper() for p in parts)


def name_parts(name: str) -> list[str]:
    return re.findall(_IDENT, name)
