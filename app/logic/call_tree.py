"""Build a display hierarchy for statements executed under one CALL."""

from __future__ import annotations

from collections import defaultdict

import pandas as pd


def build_call_tree(frame: pd.DataFrame, root_query_id: str) -> pd.DataFrame:
    """Return CALL statements in parent-before-child order with a depth label.

    Raw numeric columns remain numeric. Missing parents and malformed cycles
    degrade to top-level rows instead of dropping cost evidence.
    """
    if frame is None or frame.empty:
        return pd.DataFrame()

    out = frame.copy()
    out["QUERY_ID"] = out["QUERY_ID"].fillna("").astype(str)
    if "PARENT_QUERY_ID" not in out.columns:
        out["PARENT_QUERY_ID"] = ""
    out["PARENT_QUERY_ID"] = out["PARENT_QUERY_ID"].fillna("").astype(str)
    root = str(root_query_id or "")

    row_by_id = {str(row["QUERY_ID"]): idx for idx, row in out.iterrows()}
    children: dict[str, list[str]] = defaultdict(list)
    top_level: list[str] = []
    for _, row in out.iterrows():
        query_id = str(row["QUERY_ID"])
        parent_id = str(row["PARENT_QUERY_ID"])
        if query_id == root:
            top_level.append(query_id)
        elif parent_id in row_by_id and parent_id != query_id:
            children[parent_id].append(query_id)
        elif root in row_by_id:
            children[root].append(query_id)
        else:
            top_level.append(query_id)

    def _sort_key(query_id: str) -> tuple[str, str]:
        row = out.loc[row_by_id[query_id]]
        start = str(row.get("STEP_START", "") or "")
        return start, query_id

    for query_ids in children.values():
        query_ids.sort(key=_sort_key)
    top_level = sorted(dict.fromkeys(top_level), key=_sort_key)

    ordered: list[tuple[str, int]] = []
    visited: set[str] = set()

    def _walk(query_id: str, depth: int, lineage: set[str]) -> None:
        if query_id in visited:
            return
        visited.add(query_id)
        ordered.append((query_id, depth))
        if query_id in lineage:
            return
        next_lineage = {*lineage, query_id}
        for child in children.get(query_id, []):
            _walk(child, depth + 1, next_lineage)

    if root in row_by_id:
        _walk(root, 0, set())
    for query_id in top_level:
        _walk(query_id, 0, set())
    for query_id in sorted(row_by_id, key=_sort_key):
        _walk(query_id, 0, set())

    rows = []
    for query_id, depth in ordered:
        row = out.loc[row_by_id[query_id]].copy()
        step_type = str(row.get("STEP_TYPE", "?") or "?")
        row["DEPTH"] = depth
        branch = f"{'|  ' * max(depth - 1, 0)}+- " if depth else ""
        row["STEP"] = f"{branch}{step_type}"
        rows.append(row)
    result = pd.DataFrame(rows).reset_index(drop=True)
    lead = ["STEP", "DEPTH", "QUERY_ID", "PARENT_QUERY_ID", "STEP_START"]
    return result[[column for column in lead if column in result.columns]
                  + [column for column in result.columns if column not in lead]]
