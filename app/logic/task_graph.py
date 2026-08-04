"""Pure task-graph parsing and integrity checks.

Snowflake exposes predecessor arrays as VARIANT values, but Snowpark/pandas can
surface those values as lists, JSON strings, or bracketed text.  Keep that
normalization out of the UI so both renderers validate the same graph before
drawing it.
"""

from __future__ import annotations

import json
import math
from collections import Counter, deque
from collections.abc import Iterable
from dataclasses import dataclass

import pandas as pd


@dataclass(frozen=True)
class TaskGraphShape:
    """Validated, canonical topology for one task-graph snapshot."""

    edges: tuple[tuple[str, str], ...]
    levels: tuple[tuple[str, int], ...]
    missing_predecessors: tuple[str, ...]
    duplicate_nodes: tuple[str, ...]
    cyclic_nodes: tuple[str, ...]


def canonical_task_name(value: object) -> str:
    """Return a comparison key while preserving quoted-name compatibility."""
    text = str(value or "").strip()
    # PREDECESSORS can quote every identifier in a fully-qualified name while
    # TASK_FQN is assembled from the three name columns. Normalize both forms.
    quote_sentinel = "\0"
    text = text.replace('""', quote_sentinel).replace('"', "")
    text = text.replace(quote_sentinel, '"')
    return text.upper()


def parse_task_predecessors(value: object) -> tuple[str, ...]:
    """Normalize TASK_VERSIONS.PREDECESSORS across Snowpark representations."""
    if value is None:
        return ()
    if isinstance(value, float) and math.isnan(value):
        return ()
    if isinstance(value, dict):
        candidate = value.get("name") or value.get("task_name") or value.get("value")
        return (str(candidate),) if candidate else ()
    if isinstance(value, (list, tuple, set)):
        parsed: list[str] = []
        for item in value:
            parsed.extend(parse_task_predecessors(item))
        return tuple(v for v in parsed if str(v).strip())

    text = str(value).strip()
    if not text or text.lower() in {"none", "null", "nan", "[]"}:
        return ()
    try:
        decoded = json.loads(text)
    except (TypeError, ValueError, json.JSONDecodeError):
        decoded = None
    if decoded is not None and decoded != text:
        return parse_task_predecessors(decoded)
    if '"."' in text:
        return (text,)
    return tuple(
        part.strip().strip('"').strip("'")
        for part in text.strip("[]").split(",")
        if part.strip().strip('"').strip("'")
    )


def inspect_task_graph(
    task_names: Iterable[object], predecessor_values: Iterable[object]
) -> TaskGraphShape:
    """Build edges/levels and report any condition that makes a DAG unsafe to draw."""
    raw_names = list(task_names)
    raw_predecessors = list(predecessor_values)
    names = [canonical_task_name(value) for value in raw_names]
    names = [name for name in names if name]
    counts = Counter(names)
    duplicates = tuple(sorted(name for name, count in counts.items() if count > 1))
    nodes = set(names)

    edge_set: set[tuple[str, str]] = set()
    missing: set[str] = set()
    for child_value, predecessors in zip(raw_names, raw_predecessors, strict=False):
        child = canonical_task_name(child_value)
        if not child:
            continue
        for predecessor_value in parse_task_predecessors(predecessors):
            predecessor = canonical_task_name(predecessor_value)
            if not predecessor:
                continue
            if predecessor not in nodes:
                missing.add(predecessor)
                continue
            edge_set.add((predecessor, child))

    edges = tuple(sorted(edge_set))
    children: dict[str, list[str]] = {name: [] for name in nodes}
    indegree = dict.fromkeys(nodes, 0)
    for predecessor, child in edges:
        children[predecessor].append(child)
        indegree[child] += 1

    queue = deque(sorted(name for name, degree in indegree.items() if degree == 0))
    levels = dict.fromkeys(queue, 0)
    visited: list[str] = []
    while queue:
        node = queue.popleft()
        visited.append(node)
        for child in sorted(children[node]):
            levels[child] = max(levels.get(child, 0), levels[node] + 1)
            indegree[child] -= 1
            if indegree[child] == 0:
                queue.append(child)

    cyclic = tuple(sorted(nodes.difference(visited)))
    # Snowflake prevents cycles, but assigning a deterministic final column keeps
    # diagnostics stable if malformed fixture data reaches this pure helper.
    fallback_level = max(levels.values(), default=-1) + 1
    for node in cyclic:
        levels[node] = fallback_level

    return TaskGraphShape(
        edges=edges,
        levels=tuple(sorted(levels.items())),
        missing_predecessors=tuple(sorted(missing)),
        duplicate_nodes=duplicates,
        cyclic_nodes=cyclic,
    )


def analyze_task_run(frame: pd.DataFrame, shape: TaskGraphShape) -> pd.DataFrame:
    """Attach critical-path and downstream-impact evidence to one graph run."""
    if frame is None or frame.empty:
        return pd.DataFrame() if frame is None else frame.copy()
    out = frame.copy()
    out["_TASK_KEY"] = out.get("TASK_FQN", pd.Series("", index=out.index)).map(
        canonical_task_name
    )
    queue_sec = pd.to_numeric(
        out.get("QUEUE_SEC", pd.Series(0.0, index=out.index)), errors="coerce"
    ).fillna(0.0)
    exec_sec = pd.to_numeric(
        out.get("EXEC_SEC", pd.Series(0.0, index=out.index)), errors="coerce"
    ).fillna(0.0)
    duration = pd.to_numeric(
        out.get("RUN_SEC", queue_sec + exec_sec), errors="coerce"
    ).fillna(queue_sec + exec_sec).clip(lower=0.0)
    durations = dict(zip(out["_TASK_KEY"], duration, strict=False))

    predecessors: dict[str, list[str]] = {name: [] for name in durations}
    children: dict[str, list[str]] = {name: [] for name in durations}
    for predecessor, child in shape.edges:
        if predecessor in durations and child in durations:
            predecessors[child].append(predecessor)
            children[predecessor].append(child)

    levels = dict(shape.levels)
    cumulative: dict[str, float] = {}
    path_parent: dict[str, str] = {}
    for name in sorted(durations, key=lambda item: (levels.get(item, 0), item)):
        prior = predecessors.get(name, [])
        if prior:
            parent = max(prior, key=lambda item: cumulative.get(item, 0.0))
            path_parent[name] = parent
            cumulative[name] = durations[name] + cumulative.get(parent, 0.0)
        else:
            cumulative[name] = durations[name]

    critical: set[str] = set()
    if cumulative:
        cursor = max(cumulative, key=lambda item: cumulative[item])
        while cursor:
            critical.add(cursor)
            cursor = path_parent.get(cursor, "")

    downstream_cache: dict[str, int] = {}

    def downstream_count(name: str) -> int:
        if name in downstream_cache:
            return downstream_cache[name]
        seen: set[str] = set()
        pending = list(children.get(name, []))
        while pending:
            child = pending.pop()
            if child in seen:
                continue
            seen.add(child)
            pending.extend(children.get(child, []))
        downstream_cache[name] = len(seen)
        return len(seen)

    out["CRITICAL_PATH"] = out["_TASK_KEY"].isin(critical)
    out["PATH_SEC"] = out["_TASK_KEY"].map(cumulative).fillna(0.0).round(2)
    out["DOWNSTREAM_TASKS"] = out["_TASK_KEY"].map(downstream_count).fillna(0).astype(int)
    out["BOTTLENECK_SCORE"] = (
        duration * (1.0 + out["DOWNSTREAM_TASKS"].map(lambda value: math.log2(value + 1)))
    ).round(2)
    return out.drop(columns=["_TASK_KEY"])


def compare_task_versions(before: pd.DataFrame, after: pd.DataFrame,
                          *, include_unchanged: bool = False) -> pd.DataFrame:
    """Return node and dependency changes between two coherent graph versions."""
    columns = (
        "TASK_FQN",
        "CHANGE",
        "BEFORE_PREDECESSORS",
        "AFTER_PREDECESSORS",
    )

    def records(frame: pd.DataFrame) -> dict[str, tuple[str, tuple[str, ...]]]:
        found: dict[str, tuple[str, tuple[str, ...]]] = {}
        if frame is None or frame.empty:
            return found
        for _, row in frame.iterrows():
            fqn = str(row.get("TASK_FQN") or "").strip()
            key = canonical_task_name(fqn)
            if not key:
                continue
            predecessors = tuple(
                sorted(canonical_task_name(value) for value in parse_task_predecessors(
                    row.get("PREDECESSORS")
                ) if canonical_task_name(value))
            )
            found[key] = (fqn, predecessors)
        return found

    old = records(before)
    new = records(after)
    changes: list[dict[str, str]] = []
    for key in sorted(set(old) | set(new)):
        if key not in old:
            change = "ADDED"
        elif key not in new:
            change = "REMOVED"
        elif old[key][1] != new[key][1]:
            change = "DEPENDENCIES_CHANGED"
        else:
            change = "UNCHANGED"
        if change == "UNCHANGED" and not include_unchanged:
            continue
        before_preds = old.get(key, ("", ()))[1]
        after_preds = new.get(key, ("", ()))[1]
        changes.append({
            "TASK_FQN": new.get(key, old.get(key, (key, ())))[0],
            "CHANGE": change,
            "BEFORE_PREDECESSORS": ", ".join(before_preds),
            "AFTER_PREDECESSORS": ", ".join(after_preds),
        })
    return pd.DataFrame(changes, columns=columns)
