"""Operator Case File — session-only cross-section handoff builder (pure logic).

A DBA clicks "Add to Case" on loaded evidence across sections; each item snapshots
its scope / source / freshness / a short summary / a next action / a few preview
rows. ``assemble_markdown`` turns the collected items into ONE Markdown document to
paste into a ticket or hand to the next shift — the cross-section artifact a
per-section CSV export cannot produce.

Everything here is Streamlit-free and deterministic (timestamps are passed in, not
read from the clock) so it is fully unit-testable. The UI layer (app/ui) captures
the live scope/result metadata and renders/exports; validation, dedup, escaping,
and assembly live here. Mirrors the pure-builder pattern of
``formulas.ExecutiveSummaryView`` + ``executive_slide_bullets``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

# Session-state key for the item list (a UI concern, but the constant is single-
# sourced here so the pure logic and the UI agree).
CASE_STATE_KEY = "_ow_case_items"

# Bound the captured preview so session_state and the exported doc stay small.
MAX_PREVIEW_ROWS = 8
MAX_PREVIEW_COLS = 8

# Fields that define an item's IDENTITY for dedup + removal — deliberately NOT the
# timestamps or preview, so re-adding the same evidence is a no-op.
_FINGERPRINT_FIELDS = ("section", "company", "window", "source", "title", "summary")


def escape_md_cell(value: object) -> str:
    """Make an untrusted Snowflake cell safe inside a Markdown table cell.

    Collapses newlines/tabs (they break table rows), drops other control chars, and
    escapes the ``|`` column delimiter. Object names etc. are data, never markup —
    the same reason Alerts renders DETAIL as plain text, not markdown.
    """
    s = "" if value is None else str(value)
    s = s.replace("\r", " ").replace("\n", " ").replace("\t", " ")
    s = "".join(ch for ch in s if ch >= " ")
    # Escape the backslash FIRST, then the pipe — otherwise a cell like `x\|y`
    # would become `x\\|y`, which a renderer reads as an escaped backslash plus a
    # LIVE column delimiter, adding a phantom column and breaking table alignment.
    return s.replace("\\", "\\\\").replace("|", "\\|").strip()


def preview_from_records(
    columns: Sequence[object], rows: Sequence[Sequence[object]]
) -> tuple[list[str], list[list[str]]]:
    """Cap a ``df.head()`` projection to MAX_PREVIEW_COLS x MAX_PREVIEW_ROWS and
    stringify every cell (so the item holds only serializable primitives)."""
    cols = [str(c) for c in list(columns)[:MAX_PREVIEW_COLS]]
    ncol = len(cols)
    out_rows = [
        [("" if v is None else str(v)) for v in list(row)[:ncol]]
        for row in list(rows)[:MAX_PREVIEW_ROWS]
    ]
    return cols, out_rows


def item_fingerprint(item: Mapping[str, object]) -> str:
    """Stable identity string for dedup + removal (excludes timestamps/preview)."""
    return "␟".join(str(item.get(f, "") or "").strip().lower() for f in _FINGERPRINT_FIELDS)


def new_case_item(
    *,
    section: str,
    company: str = "ALL",
    window: str = "",
    days: object = "",
    source: str = "",
    fetched_at: str = "",
    tier: str = "",
    summary: str = "",
    next_action: str = "",
    as_of: str = "",
    title: str = "",
    added_at: str = "",
    preview_columns: Sequence[object] = (),
    preview_rows: Sequence[Sequence[object]] = (),
    truncated: bool = False,
) -> dict:
    """Build one normalized, capped case item. ``id`` is the fingerprint, so an
    identical re-add is a no-op and removal is id-keyed. Timestamps are passed in
    (never read from the clock here) to keep this pure and testable."""
    cols, rows = preview_from_records(preview_columns, preview_rows)
    capped = truncated or len(list(preview_rows)) > MAX_PREVIEW_ROWS or len(list(preview_columns)) > MAX_PREVIEW_COLS
    item = {
        "section": str(section or "").strip(),
        "company": str(company or "ALL").strip(),
        "window": str(window or "").strip(),
        "days": days,
        "source": str(source or "").strip(),
        "fetched_at": str(fetched_at or "").strip(),
        "tier": str(tier or "").strip(),
        "summary": str(summary or "").strip(),
        "next_action": str(next_action or "").strip(),
        "as_of": str(as_of or "").strip(),
        "title": str(title or "").strip(),
        "added_at": str(added_at or "").strip(),
        "preview": {"columns": cols, "rows": rows},
        "truncated": bool(capped),
    }
    item["id"] = item_fingerprint(item)
    return item


def add_item(items: Sequence[Mapping[str, object]], item: Mapping[str, object]) -> list[dict]:
    """Append ``item`` unless an item with the same id (fingerprint) is present."""
    out = [dict(i) for i in items]
    ident = item.get("id") or item_fingerprint(item)
    if any((i.get("id") or item_fingerprint(i)) == ident for i in out):
        return out
    out.append(dict(item))
    return out


def remove_item(items: Sequence[Mapping[str, object]], item_id: str) -> list[dict]:
    """Drop the item whose id matches ``item_id``."""
    return [dict(i) for i in items if (i.get("id") or item_fingerprint(i)) != item_id]


def clear_items(_items: Sequence[Mapping[str, object]] | None = None) -> list[dict]:
    """Empty the case."""
    return []


def _scope_line(item: Mapping[str, object]) -> str:
    company = str(item.get("company") or "ALL")
    window = str(item.get("window") or "").strip()
    days = item.get("days")
    parts = [company]
    if window:
        parts.append(window)
    if days not in (None, "", 0):
        parts.append(f"{days}d")
    return " · ".join(parts)


def _source_line(item: Mapping[str, object]) -> str:
    bits = []
    if item.get("source"):
        bits.append(str(item["source"]))
    if str(item.get("as_of") or "").strip():
        bits.append(f"as of {item['as_of']}")
    if item.get("fetched_at"):
        bits.append(f"fetched {item['fetched_at']}")
    if item.get("tier"):
        bits.append(f"tier {item['tier']}")
    return " · ".join(bits)


def _preview_table(preview: Mapping[str, object]) -> list[str]:
    cols_raw = preview.get("columns")
    rows_raw = preview.get("rows")
    cols = list(cols_raw) if isinstance(cols_raw, (list, tuple)) else []
    rows = list(rows_raw) if isinstance(rows_raw, (list, tuple)) else []
    if not cols or not rows:
        return []
    lines = ["| " + " | ".join(escape_md_cell(c) for c in cols) + " |",
             "| " + " | ".join("---" for _ in cols) + " |"]
    for row in rows:
        cells = [escape_md_cell(v) for v in row]
        cells += [""] * (len(cols) - len(cells))          # pad ragged rows
        lines.append("| " + " | ".join(cells[:len(cols)]) + " |")
    return lines


def assemble_markdown(
    items: Sequence[Mapping[str, object]], *, generated: str, title: str = "Operator Case File"
) -> str:
    """Assemble the collected items into ONE Markdown handoff document. Returns ''
    for an empty case (the caller shows a placeholder). Every preview cell is
    escaped; no ``$`` escaping is applied (the .md is downloaded raw — a live
    Streamlit preview must apply that at its own sink, per md_dollars)."""
    if not items:
        return ""
    companies = sorted({str(i.get("company") or "ALL") for i in items})
    sources = sorted({str(i.get("source") or "") for i in items if i.get("source")})
    out = [
        f"# {title} — {generated}",
        "",
        f"_{len(items)} item(s) · companies: {', '.join(companies)}"
        + (f" · {len(sources)} source(s)_" if sources else "_"),
    ]
    for idx, item in enumerate(items, start=1):
        heading = str(item.get("title") or item.get("section") or "Item")
        out += ["", f"## {idx}. {escape_md_cell(heading)}"]
        if item.get("section") and item.get("title"):
            out.append(f"_Section: {escape_md_cell(item['section'])}_")
        out.append(f"- **Scope:** {_scope_line(item)}")
        src = _source_line(item)
        if src:
            out.append(f"- **Source:** {src}")
        if item.get("summary"):
            out.append(f"- **Summary:** {escape_md_cell(item['summary'])}")
        if item.get("next_action"):
            out.append(f"- **Next action:** {escape_md_cell(item['next_action'])}")
        preview = item.get("preview")
        table = _preview_table(preview if isinstance(preview, Mapping) else {})
        if table:
            note = " (truncated)" if item.get("truncated") else ""
            out += ["", f"**Preview{note}:**", "", *table]
    return "\n".join(out) + "\n"
