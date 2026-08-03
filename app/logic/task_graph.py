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
